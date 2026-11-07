"""Fast probabilistic citation verification using decision models.

Evaluates synthesized claims against retrieved evidence chunks via remote
(OpenRouter Decisions API) or local (Laya uv tool) decision models, returning
calibrated support probabilities for attribution filtering and popover badges.
"""

import asyncio
from collections.abc import Iterator
import concurrent.futures
import contextlib
import json
import logging
import math
import os
import re
import statistics
import time
import urllib.parse
from typing import Any

import requests

from zocrates.config import (
    get_secure_api_key,
    load_zocrates_config,
)

logger = logging.getLogger(__name__)

try:
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import StatusCode as _StatusCode
except Exception:
    _otel_trace = None
    _StatusCode = None

__all__ = [
    "MAX_CLAIM_CHARS",
    "MAX_SOURCE_TEXT_CHARS",
    "extract_claim_occurrences",
    "extract_claims_for_citations",
    "prepare_source_text",
    "verify_citations_support",
]

# Service endpoints & defaults
OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "inception/mercury-decide"
DEFAULT_LOCAL_URL = "http://127.0.0.1:8005/v1/systemone"
DEFAULT_HEALTH_TIMEOUT_SEC: float = 0.150  # 150ms probe
DEFAULT_TIMEOUT_SEC: float = 10.0
DEFAULT_TURN_BUDGET_SEC: float = 20.0
DEFAULT_THRESHOLD: float = 0.60
MAX_SOURCE_TEXT_CHARS: int = 6000
MAX_LOCAL_SOURCE_TEXT_CHARS: int = 1800
MIN_CLAIM_CHARS: int = 30
MAX_CLAIM_CHARS: int = 800

# Regex patterns for masking
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_DEF_RE = re.compile(r"^\s*\[\d+\]:\s*\S+.*$", re.MULTILINE)

# Citation token regex matching both standard brackets ([1], [1, 2]) and fullwidth (【1】)
_IN_TEXT_CITATION_TOKEN_RE = re.compile(
    r"\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】"
)

# Abbreviation guard: reject candidate sentence periods preceded by common academic abbreviations or initials
_ABBREVIATION_RE = re.compile(
    r"\b(?:et al|e\.g|i\.e|etc|vs|cf|fig|eq|no|sec|approx|ca|dr|prof|mr|mrs|jr|sr|[a-z])\.?$",
    re.IGNORECASE,
)

# Author-only fragment regex to detect mid-sentence author citations needing forward extension
_AUTHOR_ONLY_RE = re.compile(
    r"^(?:according to|as (?:shown|noted|described|stated|reported) by|in)?\s*"
    r"(?:[A-Z][a-zA-Z\-\']+(?:,\s*|\s+and\s+|\s*&\s*|\s+et\s+al\.?\s*)*)"
    r"(?:\s*\(\s*\d{4}[a-z]?\s*\))?\s*$",
    re.IGNORECASE,
)

# Structural boundaries for paragraph- and block-scoped claim extraction
_STRUCTURAL_BOUNDARY_RES = [
    re.compile(r"\n\s*\n"),
    re.compile(r"\n\s*[-*+]\s+"),
    re.compile(r"\n\s*\d+\.\s+"),
    re.compile(r"\n\s*#{1,6}\s+"),
]


def _safe_set(span: Any, key: str, value: Any) -> None:
    """Safely set an attribute on an active OpenTelemetry span, no-oping on error or None."""
    if span is None:
        return
    try:
        if hasattr(span, "set_attribute"):
            if isinstance(value, (dict, list)):
                span.set_attribute(key, json.dumps(value))
            else:
                span.set_attribute(key, value)
        elif hasattr(span, "_attributes") and isinstance(span._attributes, dict):
            span._attributes[key] = value
    except Exception as exc:
        logger.debug("Failed setting span attribute %s: %s", key, exc)


@contextlib.contextmanager
def _verification_span(name: str) -> Iterator[Any]:
    """Context manager yielding an active OpenTelemetry span or None on fallback.

    Args:
        name: Name of the OpenTelemetry span.

    Yields:
        Active span if tracing is available, otherwise None.
    """
    if _otel_trace is None:
        yield None
        return

    span_cm = None
    try:
        tracer = _otel_trace.get_tracer("zocrates.citation_verifier")
        span_cm = tracer.start_as_current_span(name)
    except Exception as exc:
        logger.debug("Failed starting verification span: %s", exc)
        yield None
        return

    span = None
    if hasattr(span_cm, "__enter__") and hasattr(span_cm, "__exit__"):
        try:
            with span_cm as s:
                span = s
                yield span
        except Exception as exc:
            if span is not None and _StatusCode is not None:
                try:
                    span.set_status(_StatusCode.ERROR, str(exc))
                except Exception:
                    pass
            raise
    else:
        yield span_cm


def mask_text_for_extraction(text: str) -> tuple[str, dict[str, str]]:
    """Mask code blocks, images, and markdown link reference definitions."""
    placeholders: dict[str, str] = {}
    counter = 0

    def _mask(m: re.Match) -> str:
        """Replace a matched span with an extraction mask placeholder.

        Args:
            m: Regular expression match object containing the matched text.

        Returns:
            Placeholder string mapped to the matched content.
        """
        nonlocal counter
        ph = f"@@ZOCRATES_VERIFY_MASK_{counter}@@"
        counter += 1
        placeholders[ph] = m.group(0)
        return ph

    masked = _CODE_BLOCK_RE.sub(_mask, text)
    masked = _MD_IMAGE_RE.sub(_mask, masked)
    masked = _MD_LINK_DEF_RE.sub(_mask, masked)
    return masked, placeholders


def unmask_text(text: str, placeholders: dict[str, str]) -> str:
    """Restore masked spans in extracted claims."""
    for ph, orig in placeholders.items():
        text = text.replace(ph, orig)
    return text


def _find_backward_sentence_boundary(prefix: str) -> int:
    """Find start index of the sentence preceding a citation token.

    Rejects candidate periods preceded by abbreviations or followed by digits.
    Treats ** or __ following sentence-ending punctuation (. ! ?) as a boundary
    so lead-in headings like - **Lead-in.** Claim yield Claim without trailing syntax.
    Returns the character index in prefix where the sentence starts.
    """
    n = len(prefix)
    if n == 0:
        return 0

    idx = n - 1
    while idx >= 0:
        ch = prefix[idx]
        if ch in ("\n", "\r"):
            return idx + 1
        elif ch in ("!", "?"):
            # Check for trailing bold/italic markers: e.g. !** or ?__
            if idx + 2 < n and prefix[idx + 1:idx + 3] in ("**", "__"):
                if idx + 3 == n or prefix[idx + 3].isspace():
                    return idx + 3
            if idx + 1 == n or prefix[idx + 1].isspace():
                return idx + 1
        elif ch == ".":
            # Check if followed by ** or __ (e.g. - **Lead-in.** Claim)
            if idx + 2 < n and prefix[idx + 1:idx + 3] in ("**", "__"):
                if idx + 3 == n or prefix[idx + 3].isspace():
                    before_period = prefix[:idx].rstrip()
                    if not _ABBREVIATION_RE.search(before_period):
                        return idx + 3
            is_followed_by_space = (idx + 1 == n) or prefix[idx + 1].isspace()
            is_followed_by_digit = (idx + 1 < n) and prefix[idx + 1].isdigit()
            if not is_followed_by_digit and is_followed_by_space:
                before_period = prefix[:idx].rstrip()
                if not _ABBREVIATION_RE.search(before_period):
                    return idx + 1
        idx -= 1

    return 0


def _find_forward_sentence_boundary(suffix: str) -> int:
    """Find end index of the clause/sentence following a mid-sentence citation token.

    Rejects candidate periods preceded by abbreviations or followed by digits.
    Stops at sentence boundaries, newlines, semicolons, or the next citation token.
    """
    n = len(suffix)
    if n == 0:
        return 0

    cit_match = _IN_TEXT_CITATION_TOKEN_RE.search(suffix)
    max_search = cit_match.start() if cit_match else n

    idx = 0
    while idx < max_search:
        ch = suffix[idx]
        if ch in ("\n", "\r"):
            return idx
        elif ch in ("!", "?"):
            return idx + 1
        elif ch == ";":
            return idx
        elif ch == ".":
            is_followed_by_space = (idx + 1 == n) or suffix[idx + 1].isspace()
            is_followed_by_digit = (idx + 1 < n) and suffix[idx + 1].isdigit()
            if not is_followed_by_digit and is_followed_by_space:
                before_period = suffix[:idx].rstrip()
                if not _ABBREVIATION_RE.search(before_period):
                    return idx + 1
        idx += 1

    return max_search


def _find_backward_claim_boundary(prefix: str, max_chars: int = MAX_CLAIM_CHARS) -> int:
    """Find start index of the paragraph/block preceding a citation token.

    Step 1: Scans prefix for earlier citation tokens; if found, min_boundary is the
    end of the last citation match so citations in the same block do not bleed.
    Step 2: In search_region = prefix[min_boundary:], searches for the latest structural
    boundary (paragraph breaks, list bullets, numbered items, or markdown headers).
    Step 3: Bounds backward claim length to max_chars, advancing past the first
    sentence boundary in the window to avoid chopping mid-sentence.
    """
    n = len(prefix)
    if n == 0:
        return 0

    # Step 1: Find the last in-text citation match in prefix
    min_boundary = 0
    for m in _IN_TEXT_CITATION_TOKEN_RE.finditer(prefix):
        min_boundary = m.end()

    # Step 2: Search for the latest structural boundary end in search_region
    search_region = prefix[min_boundary:]
    all_matches = []
    for pat in _STRUCTURAL_BOUNDARY_RES:
        all_matches.extend(pat.finditer(search_region))

    if all_matches:
        start_idx = min_boundary + max(m.end() for m in all_matches)
    else:
        start_idx = min_boundary

    # Step 3: Bound claim length if exceeding max_chars
    if (n - start_idx) > max_chars:
        start_idx = n - max_chars
        fwd_offset = _find_forward_sentence_boundary(prefix[start_idx:])
        if fwd_offset > 0 and (n - (start_idx + fwd_offset)) >= MIN_CLAIM_CHARS:
            start_idx += fwd_offset

    return start_idx


def _clean_and_join_spans(backward: str, forward: str) -> str:
    """Join backward and forward spans into a clean, complete claim."""
    backward = backward.strip()
    forward = forward.strip()

    if not backward:
        claim = forward
    elif not forward:
        claim = backward
    else:
        if forward.startswith((",", ";", ":", ".")):
            claim = f"{backward}{forward}"
        else:
            claim = f"{backward} {forward}"

    # Normalize symbols: ≈ -> "about", ≥ -> "at least", ≤ -> "at most", → -> "to", — -> " - "
    claim = claim.replace("≈", "about")
    claim = claim.replace("≥", "at least")
    claim = claim.replace("≤", "at most")
    claim = claim.replace("→", "to")
    claim = claim.replace("—", " - ")

    # Strip inline bold/italic markers **, __, and backticks anywhere in the claim
    claim = claim.replace("**", "").replace("__", "").replace("`", "")

    claim = re.sub(r"\s+", " ", claim).strip()
    claim = _IN_TEXT_CITATION_TOKEN_RE.sub("", claim)
    claim = re.sub(r"\s+", " ", claim).strip()
    claim = re.sub(r"^(?:[\.\,\;\:\!\?\*\-\#\>\s]|\d+\.\s*)+", "", claim).strip()

    if claim and not claim.endswith((".", "!", "?")):
        claim = claim + "."

    return claim


def extract_claim_occurrences(
    text: str,
    total_chunks: int,
) -> list[dict[str, Any]]:
    """Extract claim text for each occurrence of cited chunk indices in document order.

    Args:
        text: Synthesized markdown text containing citation tokens.
        total_chunks: Number of available retrieved chunks for bounds validation.

    Returns:
        List of dicts with keys 'chunk_index', 'occurrence', and 'claim', where
        occurrence is the 1-based occurrence index for that specific chunk_index.
    """
    if not text or total_chunks <= 0:
        return []

    masked_text, placeholders = mask_text_for_extraction(text)
    occurrences: list[dict[str, Any]] = []
    occ_counters: dict[int, int] = {}

    for m in _IN_TEXT_CITATION_TOKEN_RE.finditer(masked_text):
        raw_content = m.group(1) if m.group(1) is not None else m.group(2)
        if not raw_content:
            continue
        parts = [p.strip() for p in re.split(r"[,;]", raw_content) if p.strip()]
        valid_indices: list[int] = []
        seen_in_token: set[int] = set()
        for p in parts:
            if p.isdigit():
                val = int(p)
                if 1 <= val <= total_chunks and val not in seen_in_token:
                    seen_in_token.add(val)
                    valid_indices.append(val)

        if not valid_indices:
            continue

        token_start = m.start()
        token_end = m.end()

        # Backward scan
        prefix = masked_text[:token_start]
        b_idx = _find_backward_claim_boundary(prefix)
        backward_span = prefix[b_idx:].strip()

        # Forward scan check
        forward_span = ""
        if len(backward_span) < MIN_CLAIM_CHARS or _AUTHOR_ONLY_RE.match(backward_span):
            suffix = masked_text[token_end:]
            f_idx = _find_forward_sentence_boundary(suffix)
            forward_span = suffix[:f_idx].strip()

        claim = _clean_and_join_spans(backward_span, forward_span)
        if len(claim) < 15:
            start_w = max(0, token_start - 100)
            end_w = min(len(masked_text), token_end + 100)
            fallback_span = masked_text[start_w:end_w].strip()
            claim = _clean_and_join_spans(fallback_span, "")

        claim = unmask_text(claim, placeholders)

        for idx in valid_indices:
            occ = occ_counters.get(idx, 0) + 1
            occ_counters[idx] = occ
            occurrences.append({
                "chunk_index": idx,
                "occurrence": occ,
                "claim": claim,
            })

    return occurrences


def extract_claims_for_citations(
    text: str,
    total_chunks: int,
) -> dict[int, str]:
    """Extract claim text for each cited chunk index in the synthesis text.

    Maintained for backward compatibility; selects the longest claim for each chunk.

    Args:
        text: Synthesized markdown text containing citation tokens.
        total_chunks: Number of available retrieved chunks for bounds validation.

    Returns:
        Dict mapping chunk index (1-based int) to the extracted claim text.
    """
    occurrences = extract_claim_occurrences(text, total_chunks)
    claims_by_index: dict[int, str] = {}
    for item in occurrences:
        idx = item["chunk_index"]
        claim = item["claim"]
        if idx not in claims_by_index or len(claim) > len(claims_by_index[idx]):
            claims_by_index[idx] = claim
    return claims_by_index


def prepare_source_text(
    chunk: dict[str, Any],
    claim: str | None = None,
    max_chars: int = MAX_SOURCE_TEXT_CHARS,
) -> str:
    """Format and bound chunk source text around claim keywords."""
    raw_text = str(chunk.get("text") or "").strip()
    if len(raw_text) <= max_chars:
        return raw_text

    if claim:
        words = set(re.findall(r"\b[a-zA-Z0-9]{4,}\b", claim.lower()))
        if words:
            step = min(500, max(100, max_chars // 3))
            best_score = -1
            best_pos = 0
            for pos in range(0, len(raw_text) - max_chars + 1, step):
                window = raw_text[pos : pos + max_chars].lower()
                score = sum(1 for w in words if w in window)
                if score > best_score:
                    best_score = score
                    best_pos = pos
            if best_score > 0:
                return raw_text[best_pos : best_pos + max_chars]

    return raw_text[:max_chars]


def _check_support_openrouter(
    claim: str,
    source_text: str,
    model: str = DEFAULT_OPENROUTER_MODEL,
    api_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> float | None:
    """Query OpenRouter Decisions API with fallback and fail-open handling."""
    effective_key = (
        api_key
        or get_secure_api_key(
            "openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")
        )
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPEN_ROUTER_API_KEY")
    )
    if not effective_key:
        logger.warning("No OpenRouter API key found for citation verification. Failing open.")
        return None

    headers = {
        "Authorization": f"Bearer {effective_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
        "X-OpenRouter-Title": "Zocrates Citation Verifier",
    }

    model_lower = model.lower()
    if "respan" in model_lower:
        payload = {
            "model": model,
            "state": f"Source text: {source_text}\n\nClaim: {claim}",
            "questions": {
                "support": {
                    "type": "noul",
                    "instructions": "Does the source text support the claim?",
                }
            },
        }
    else:
        payload = {
            "model": model,
            "state": {
                "source_text": source_text,
                "claim": claim,
            },
            "questions": {
                "support": {
                    "type": "noul",
                    "instructions": "Does the `source_text` support the `claim`?",
                }
            },
        }

    try:
        resp = requests.post(
            OPENROUTER_DECISIONS_URL,
            headers=headers,
            json=payload,
            timeout=timeout,
        )

        # Single retry on HTTP 429 with 250ms backoff
        if resp.status_code == 429:
            time.sleep(0.250)
            resp = requests.post(
                OPENROUTER_DECISIONS_URL,
                headers=headers,
                json=payload,
                timeout=timeout,
            )

        # Fallback to chat completions if 404 on alpha decisions endpoint
        if resp.status_code == 404:
            chat_payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are a citation verification model. Determine whether the source text supports the claim. "
                            "Respond with JSON in the form: {\"noul\": <probability between 0.0 and 1.0>}."
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"Source text:\n{source_text}\n\nClaim:\n{claim}",
                    },
                ],
                "temperature": 0.0,
                "max_tokens": 50,
            }
            chat_resp = requests.post(
                OPENROUTER_CHAT_URL,
                headers=headers,
                json=chat_payload,
                timeout=timeout,
            )
            if chat_resp.status_code == 200:
                try:
                    cdata = chat_resp.json()
                    content = cdata["choices"][0]["message"]["content"]
                    m = re.search(r'"noul"\s*:\s*([0-9]*\.?[0-9]+)', content)
                    if m:
                        val = float(m.group(1))
                        if math.isfinite(val):
                            return min(1.0, max(0.0, val))
                except Exception:
                    pass
            logger.warning("OpenRouter chat completions fallback failed. Failing open.")
            return None

        if resp.status_code != 200:
            logger.warning("OpenRouter decisions API returned HTTP %s. Failing open.", resp.status_code)
            return None

        res_data = resp.json()
        answers = res_data.get("answers", {})
        support_ans = answers.get("support", {})
        noul_val = support_ans.get("noul")

        if noul_val is not None:
            try:
                val = float(noul_val)
                if math.isfinite(val):
                    return min(1.0, max(0.0, val))
            except (ValueError, TypeError):
                pass

        logger.warning("Malformed 200 response from OpenRouter decision model (missing or non-numeric noul). Failing open.")
        return None

    except requests.Timeout:
        logger.warning("OpenRouter decision model timed out (%.1fs). Failing open.", timeout)
        return None
    except Exception as exc:
        logger.warning("OpenRouter decision model call failed: %s. Failing open.", exc)
        return None


def _probe_local_laya_health(health_url: str, timeout: float = DEFAULT_HEALTH_TIMEOUT_SEC) -> bool:
    """Fast health check probe against local Laya server."""
    try:
        resp = requests.get(health_url, timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _check_support_local_laya(
    claim: str,
    source_text: str,
    model: str = "laya",
    local_url: str = DEFAULT_LOCAL_URL,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    skip_health_probe: bool = False,
) -> float | None:
    """Query local Laya endpoint with fail-open health probe and error handling."""
    parsed = urllib.parse.urlparse(local_url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"

    if not skip_health_probe and not _probe_local_laya_health(health_url):
        logger.warning("Local Laya health probe failed (%s). Failing open.", health_url)
        return None

    effective_timeout = max(timeout, 10.0)

    payload = {
        "model": model,
        "state": {
            "source_text": source_text,
            "claim": claim,
        },
        "questions": {
            "support": {
                "type": "noul",
                "instructions": "Does the `source_text` support the `claim`?",
            }
        },
    }
    headers = {"Content-Type": "application/json"}

    try:
        resp = requests.post(local_url, headers=headers, json=payload, timeout=effective_timeout)
        if resp.status_code == 429:
            time.sleep(0.250)
            resp = requests.post(local_url, headers=headers, json=payload, timeout=effective_timeout)

        if resp.status_code != 200:
            if resp.status_code == 500:
                logger.warning(
                    "Local Laya returned HTTP 500: %s. This typically occurs when Laya attempts to run on an incompatible GPU or runs out of VRAM. Ensure Laya is started with LAYA_DEVICE=cpu (e.g. via Zocrates Preferences). Failing open.",
                    resp.text,
                )
            else:
                logger.warning("Local Laya returned HTTP %s. Failing open.", resp.status_code)
            return None

        res_data = resp.json()
        answers = res_data.get("answers", {})
        support_ans = answers.get("support", {})
        noul_val = support_ans.get("noul")

        if noul_val is not None:
            try:
                val = float(noul_val)
                if math.isfinite(val):
                    return min(1.0, max(0.0, val))
            except (ValueError, TypeError):
                pass

        logger.warning("Malformed 200 response from Local Laya (missing or non-numeric noul). Failing open.")
        return None

    except requests.Timeout:
        logger.warning("Local Laya request timed out (%.1fs). Failing open.", effective_timeout)
        return None
    except Exception as exc:
        logger.warning("Local Laya request failed: %s. Failing open.", exc)
        return None


def _check_support_local_laya_batch(
    items: list[tuple[Any, ...]],
    local_url: str = DEFAULT_LOCAL_URL,
    timeout: float = 15.0,
) -> dict[Any, float | None] | None:
    """Evaluate multiple citation claims concurrently via Laya's native /v1/systemone/batch endpoint.

    Supports both 3-tuples (chunk_idx, claim, source_text) and 4-tuples
    (chunk_idx, occ_idx, claim, source_text).
    """
    parsed = urllib.parse.urlparse(local_url)
    batch_url = f"{parsed.scheme}://{parsed.netloc}/v1/systemone/batch"
    states = []
    for item in items:
        if len(item) == 4:
            states.append({"source_text": item[3], "claim": item[2]})
        else:
            states.append({"source_text": item[2], "claim": item[1]})

    payload = {
        "states": states,
        "questions": {
            "support": {
                "type": "noul",
                "instructions": "Does the `source_text` support the `claim`?",
            }
        },
    }
    headers = {"Content-Type": "application/json"}
    try:
        resp = requests.post(batch_url, headers=headers, json=payload, timeout=max(timeout, 15.0))
        if resp.status_code == 200:
            res_data = resp.json()
            raw_results = res_data.get("results", [])
            out: dict[Any, float | None] = {}
            for item, row in zip(items, raw_results):
                key = (item[0], item[1]) if len(item) == 4 else item[0]
                ans = row.get("answers", {}).get("support", {})
                noul = ans.get("noul")
                if noul is not None:
                    try:
                        val = float(noul)
                        if math.isfinite(val):
                            out[key] = min(1.0, max(0.0, val))
                            continue
                    except (ValueError, TypeError):
                        pass
                out[key] = None
            return out
        logger.warning("Local Laya batch endpoint returned HTTP %s. Failing open.", resp.status_code)
        return None
    except Exception as exc:
        logger.warning("Local Laya batch endpoint query failed: %s. Failing open.", exc)
        return None


async def verify_citations_support(
    text: str,
    ranked_chunks: list[dict[str, Any]],
    config: dict[str, Any] | None = None,
    telemetry_out: dict[str, Any] | None = None,
) -> dict[int, list[float | None]]:
    """Verify citations in text against retrieved chunks using decision models.

    Args:
        text: Synthesized markdown response text containing citation tokens.
        ranked_chunks: Complete list of retrieved evidence chunks.
        config: Optional configuration dict from zocrates_config.json.
        telemetry_out: Optional dict to receive verification telemetry data.

    Returns:
        Dict mapping chunk index (1-based int) to list of support probabilities
        for each occurrence of that chunk index in document order ([p_occ1, p_occ2, ...]).
    """
    if not text or not ranked_chunks:
        return {}

    start_time = time.perf_counter()

    with _verification_span("citation_verification") as span:
        cfg = dict(config) if config is not None else load_zocrates_config()

        provider = str(cfg.get("citation_verification_provider") or "openrouter")
        if provider == "local_laya":
            model = "laya"
        else:
            model = str(cfg.get("citation_verification_model") or DEFAULT_OPENROUTER_MODEL)
        local_url = str(cfg.get("citation_verification_local_url") or DEFAULT_LOCAL_URL)
        timeout_sec = float(cfg.get("citation_verification_timeout_sec", DEFAULT_TIMEOUT_SEC))
        turn_budget_sec = float(cfg.get("citation_verification_turn_budget_sec", DEFAULT_TURN_BUDGET_SEC))
        verification_threshold = float(cfg.get("citation_verification_threshold", DEFAULT_THRESHOLD))
        execution_mode = "batch" if provider == "local_laya" else "thread_pool"
        endpoint = local_url if provider == "local_laya" else OPENROUTER_DECISIONS_URL

        occurrences = extract_claim_occurrences(text, len(ranked_chunks))
        if not occurrences:
            return {}

        # Initialize results mapping chunk_index -> list of None for each occurrence
        results: dict[int, list[float | None]] = {}
        for item in occurrences:
            c_idx = item["chunk_index"]
            if c_idx not in results:
                results[c_idx] = []
            results[c_idx].append(None)

        item_latencies: dict[tuple[int, int], float | None] = {}
        budget_exceeded = False

        # Build input metadata and assessments for span / telemetry
        input_assessments: list[dict[str, Any]] = []
        for item in occurrences:
            c_idx = item["chunk_index"]
            occ_idx = item["occurrence"]
            chunk = ranked_chunks[c_idx - 1] if (1 <= c_idx <= len(ranked_chunks)) else {}
            src = prepare_source_text(chunk, item["claim"])[:1500]
            input_assessments.append({
                "chunk_index": c_idx,
                "occurrence": occ_idx,
                "claim": item["claim"],
                "source_text": src,
                "title": str(chunk.get("title") or ""),
                "section": str(chunk.get("section") or chunk.get("heading") or ""),
                "authors": chunk.get("authors") if chunk.get("authors") is not None else [],
                "year": chunk.get("year") or "",
            })

        metadata_dict = {
            "provider": provider,
            "model": model,
            "endpoint": endpoint,
            "threshold": verification_threshold,
            "execution_mode": execution_mode,
            "turn_budget_sec": turn_budget_sec,
            "batch_size": len(occurrences),
        }

        # Set span input attributes
        try:
            _safe_set(span, "openinference.span.kind", "GUARDRAIL")
            _safe_set(span, "input.mime_type", "application/json")
            _safe_set(span, "input.value", json.dumps(input_assessments))
            _safe_set(span, "metadata", json.dumps(metadata_dict))
        except Exception as span_err:
            logger.debug("Failed recording input span attributes: %s", span_err)

        # Progressively initialize telemetry_out if provided
        if telemetry_out is not None:
            try:
                telemetry_out.clear()
                telemetry_out.update({
                    "schema_version": 1,
                    "provider": provider,
                    "model": model,
                    "endpoint": endpoint,
                    "threshold": verification_threshold,
                    "execution_mode": execution_mode,
                    "turn_budget_sec": turn_budget_sec,
                    "batch_size": len(occurrences),
                    "assessments": [],
                })
            except Exception as tel_err:
                logger.debug("Failed initializing telemetry_out: %s", tel_err)

        if provider == "local_laya":
            effective_budget = max(turn_budget_sec, 20.0, len(occurrences) * 2.5)
            parsed = urllib.parse.urlparse(local_url)
            health_url = f"{parsed.scheme}://{parsed.netloc}/health"
            if not _probe_local_laya_health(health_url):
                logger.warning("Local Laya health probe failed (%s). Failing open.", health_url)
            else:
                # Fast path: Native tensor-batched inference across all citation claim occurrences
                batch_items = [
                    (
                        item["chunk_index"],
                        item["occurrence"],
                        item["claim"],
                        prepare_source_text(
                            ranked_chunks[item["chunk_index"] - 1],
                            item["claim"],
                            max_chars=MAX_LOCAL_SOURCE_TEXT_CHARS,
                        ),
                    )
                    for item in occurrences
                ]
                batch_res = _check_support_local_laya_batch(
                    batch_items,
                    local_url=local_url,
                    timeout=effective_budget,
                )
                if batch_res is not None:
                    for item in occurrences:
                        c_idx = item["chunk_index"]
                        occ_idx = item["occurrence"]
                        prob = batch_res.get((c_idx, occ_idx))
                        results[c_idx][occ_idx - 1] = prob
                else:
                    if (time.perf_counter() - start_time) >= effective_budget:
                        budget_exceeded = True
                    # If batch inference timed out or failed, fail open cleanly without causing a cascading queue jam
                    logger.warning(
                        "Local Laya batch verification failed or timed out (budget: %.1fs). Failing open for %d citations.",
                        effective_budget,
                        len(occurrences),
                    )
        else:
            def _verify_one(chunk_idx: int, occ_idx: int, claim: str) -> tuple[int, int, float | None, float]:
                """Verify a single claim against its referenced chunk evidence.

                Args:
                    chunk_idx: 1-based index of the candidate evidence chunk.
                    occ_idx: 1-based occurrence number of the citation within the text.
                    claim: Extracted claim sentence to evaluate.

                Returns:
                    Tuple of (chunk_idx, occ_idx, support_probability, elapsed_ms).
                """
                t0 = time.perf_counter()
                try:
                    chunk = ranked_chunks[chunk_idx - 1]
                    source_text = prepare_source_text(chunk, claim)
                    if provider == "local_laya":
                        prob = _check_support_local_laya(
                            claim=claim,
                            source_text=source_text,
                            model=model,
                            local_url=local_url,
                            timeout=timeout_sec,
                            skip_health_probe=True,
                        )
                    else:
                        prob = _check_support_openrouter(
                            claim=claim,
                            source_text=source_text,
                            model=model,
                            timeout=timeout_sec,
                        )
                    elapsed_item_ms = (time.perf_counter() - t0) * 1000.0
                    return chunk_idx, occ_idx, prob, round(elapsed_item_ms, 2)
                except Exception as exc:
                    logger.warning(
                        "Unexpected error verifying chunk %s occ#%s: %s. Failing open.",
                        chunk_idx,
                        occ_idx,
                        exc,
                    )
                    elapsed_item_ms = (time.perf_counter() - t0) * 1000.0
                    return chunk_idx, occ_idx, None, round(elapsed_item_ms, 2)

            loop = asyncio.get_running_loop()
            num_workers = min(len(occurrences), 16)
            if num_workers > 0:
                with concurrent.futures.ThreadPoolExecutor(max_workers=num_workers) as executor:
                    futures = [
                        loop.run_in_executor(
                            executor,
                            _verify_one,
                            item["chunk_index"],
                            item["occurrence"],
                            item["claim"],
                        )
                        for item in occurrences
                    ]
                    try:
                        completed = await asyncio.wait_for(
                            asyncio.gather(*futures),
                            timeout=turn_budget_sec,
                        )
                        for chunk_idx, occ_idx, prob, item_lat in completed:
                            results[chunk_idx][occ_idx - 1] = prob
                            item_latencies[(chunk_idx, occ_idx)] = item_lat
                    except asyncio.TimeoutError:
                        budget_exceeded = True
                        logger.warning(
                            "Citation verification turn budget exceeded (%.1fs). Failing open for remaining citations.",
                            turn_budget_sec,
                        )

        # Build completed assessments and summary
        assessments: list[dict[str, Any]] = []
        kept_count = 0
        demoted_count = 0
        fail_open_count = 0

        for i, item in enumerate(occurrences):
            c_idx = item["chunk_index"]
            occ_idx = item["occurrence"]
            prob = results.get(c_idx, [])[occ_idx - 1] if (c_idx in results and len(results[c_idx]) >= occ_idx) else None

            if prob is None:
                verdict = "FAIL_OPEN"
                fail_open_count += 1
            elif prob >= verification_threshold:
                verdict = "KEPT"
                kept_count += 1
            else:
                verdict = "DEMOTED"
                demoted_count += 1

            assessment_entry = dict(input_assessments[i])
            assessment_entry.update({
                "support_probability": prob,
                "threshold": verification_threshold,
                "verdict": verdict,
                "latency_ms": item_latencies.get((c_idx, occ_idx)) if execution_mode == "thread_pool" else None,
            })
            assessments.append(assessment_entry)

        probs = [float(a["support_probability"]) for a in assessments if a.get("support_probability") is not None]
        mean_support = round(statistics.fmean(probs), 3) if probs else None
        std_support = round(statistics.stdev(probs), 3) if len(probs) >= 2 else (0.0 if len(probs) == 1 else None)

        wall_latency_ms = round((time.perf_counter() - start_time) * 1000.0, 2)
        summary = {
            "batch_size": len(occurrences),
            "kept": kept_count,
            "demoted": demoted_count,
            "fail_open": fail_open_count,
            "mean_support": mean_support,
            "std_support": std_support,
            "latency_ms": wall_latency_ms,
            "budget_exceeded": budget_exceeded,
        }

        # Progressively finalize telemetry_out
        if telemetry_out is not None:
            try:
                telemetry_out["assessments"] = assessments
                telemetry_out["summary"] = summary
                telemetry_out["latency_ms"] = wall_latency_ms
            except Exception as tel_err:
                logger.debug("Failed finalizing telemetry_out: %s", tel_err)

        # Set output attributes on span
        try:
            _safe_set(span, "output.mime_type", "application/json")
            _safe_set(span, "output.value", json.dumps({"summary": summary, "assessments": assessments}))
            _safe_set(span, "citation_verification.provider", provider)
            _safe_set(span, "citation_verification.model", model)
            _safe_set(span, "citation_verification.threshold", verification_threshold)
            _safe_set(span, "citation_verification.batch_size", len(occurrences))
            _safe_set(span, "citation_verification.kept", kept_count)
            _safe_set(span, "citation_verification.demoted", demoted_count)
            _safe_set(span, "citation_verification.fail_open", fail_open_count)
            _safe_set(span, "citation_verification.mean_support", mean_support)
            _safe_set(span, "citation_verification.std_support", std_support)
            _safe_set(span, "citation_verification.latency_ms", wall_latency_ms)
        except Exception as span_err:
            logger.debug("Failed setting span output attributes: %s", span_err)

        # Per-occurrence logging
        for idx in sorted(results.keys()):
            for occ_idx, p in enumerate(results[idx], start=1):
                p_str = f"{p:.4f}" if isinstance(p, (float, int)) else str(p)
                status = "KEPT" if (p is None or p >= verification_threshold) else "DEMOTED"
                logger.info(
                    "Citation verification [%d] occ#%d: p=%s (threshold=%.2f, status=%s)",
                    idx,
                    occ_idx,
                    p_str,
                    verification_threshold,
                    status,
                )

        # Summary line logging
        logger.info(
            "Citation verification complete: %d assessments in %.1fms (provider=%s, model=%s, mode=%s, threshold=%.2f, kept=%d, demoted=%d, fail_open=%d)",
            len(assessments),
            wall_latency_ms,
            provider,
            model,
            execution_mode,
            verification_threshold,
            kept_count,
            demoted_count,
            fail_open_count,
        )

        return results
