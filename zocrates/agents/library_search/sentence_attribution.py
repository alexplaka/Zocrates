"""Sentence-level attribution for claims using Option B (Pre-Segmented Numbered Spans).

Segments source passages into numbered atomic spans (preserving headings, table rows,
display math blocks) and evaluates which spans support an extracted claim using
nvidia/nemotron-3.5-lightning:free via OpenRouter.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from typing import Any

from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE
from zocrates.agents.library_search.citation_verifier import _ABBREVIATION_RE

logger = logging.getLogger(__name__)

SENTENCE_ATTRIBUTION_MODEL = "nvidia/nemotron-3.5-lightning:free"
SENTENCE_ATTRIBUTION_TIMEOUT_SEC = 20.0

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|(?:\s*:?-+:?\s*\|)+\s*$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+.*$")
_DISPLAY_MATH_RE = re.compile(r"\$\$[\s\S]+?\$\$")
_CODE_BLOCK_RE = re.compile(r"```[\s\S]*?```")
_SENTENCE_BOUNDARY_RE = re.compile(r'([.?!]+)(\s+)(?=[A-Z0-9"\'\(\[])')

ATTRIBUTION_SYSTEM_PROMPT = """You are an expert academic fact-checker. Your task is to identify which numbered sentence(s), table row(s), or equation(s) in a source passage directly support an extracted scientific claim.

Analyze the claim against the numbered sentences in the source passage.

Output MUST be a single valid JSON object strictly matching this schema:
{
  "supporting_indices": [<1-based integer indices of directly supporting sentences, or [] if none>],
  "unsupported_aspects": "<concise description of assertions from the claim not found in the text, or empty string if all facts are substantiated>"
}

Strict Rules:
1. Direct Factual Support Only: A sentence supports a claim ONLY if it explicitly asserts a specific fact, finding, parameter, or mechanism in the claim. Topical similarity, introductory remarks, or general thematic relevance is NOT support.
2. Strictly Minimal Attribution: Select ONLY the specific 1 to 3 key sentences that directly substantiate factual assertions. Do NOT select surrounding context, background, or preamble. NEVER select all or most sentences in a passage.
3. Quantitative & Parameter Rigor: If the claim asserts specific quantities, concentrations, volumes, reaction numbers, or model names (e.g., 20 μM, 0.0156 µm³, Pepke et al., ODE solver) and the text lacks those exact values or names, note those specific missing elements in "unsupported_aspects".
4. Natural Scholarly Explanation: In "unsupported_aspects", describe in clear, natural prose which specific factual assertions, mechanisms, or numbers from the claim are not found in the text. Refer naturally to "the text" or "the passage" without citing sentence numbers. If all facts are substantiated, set to "".
5. When No Sentences Support: If the passage discusses the same general topic but contains NO direct evidence for the specific assertions in the claim, return "supporting_indices": [] and explain what is missing in "unsupported_aspects".
6. Output ONLY the raw JSON object."""


def segment_source_text(text: str) -> list[str]:
    """Segment source_text into numbered atomic spans.

    Preserves Markdown headings, table rows (skipping divider rows), and display
    math blocks while splitting prose into sentences with academic abbreviation guards.

    Args:
        text: Raw source text or Markdown excerpt.

    Returns:
        List of atomic string spans.
    """
    if not text or not text.strip():
        return []

    # 1. Identify block regions (code blocks, display math) to keep them atomic
    blocks: list[tuple[int, int, str]] = []
    for m in _CODE_BLOCK_RE.finditer(text):
        blocks.append((m.start(), m.end(), m.group(0)))
    for m in _DISPLAY_MATH_RE.finditer(text):
        blocks.append((m.start(), m.end(), m.group(0)))
    blocks.sort(key=lambda x: x[0])

    # 2. Track line boundaries with character offsets
    lines: list[tuple[int, int, str]] = []
    cur = 0
    for line in text.splitlines(keepends=True):
        start = cur
        end = cur + len(line)
        lines.append((start, end, line))
        cur = end

    spans: list[str] = []
    i = 0
    while i < len(lines):
        start_pos, end_pos, line_str = lines[i]
        stripped = line_str.strip()
        if not stripped:
            i += 1
            continue

        # Check if line starts inside a protected block
        in_block = None
        for b_start, b_end, b_text in blocks:
            if b_start <= start_pos < b_end:
                in_block = (b_start, b_end, b_text)
                break
        if in_block:
            b_start, b_end, b_text = in_block
            spans.append(b_text.strip())
            while i < len(lines) and lines[i][0] < b_end:
                i += 1
            continue

        # Check Markdown heading
        if _HEADING_RE.match(stripped):
            spans.append(stripped)
            i += 1
            continue

        # Check Markdown table row
        if _TABLE_ROW_RE.match(stripped):
            if not _TABLE_SEP_RE.match(stripped):
                spans.append(stripped)
            i += 1
            continue

        # Gather contiguous prose lines
        prose_start = start_pos
        prose_end = end_pos
        i += 1
        while i < len(lines):
            next_start, next_end, next_line = lines[i]
            next_stripped = next_line.strip()
            if not next_stripped:
                break
            is_special = False
            for b_start, b_end, _ in blocks:
                if b_start <= next_start < b_end:
                    is_special = True
                    break
            if is_special or _HEADING_RE.match(next_stripped) or _TABLE_ROW_RE.match(next_stripped):
                break
            prose_end = next_end
            i += 1

        raw_prose = text[prose_start:prose_end]
        sent_start = 0
        for match in _SENTENCE_BOUNDARY_RE.finditer(raw_prose):
            punct = match.group(1)
            end_punct = match.end(1)
            prefix = raw_prose[sent_start:end_punct]
            if "." in punct:
                token = prefix[:-len(punct)].strip()
                if _ABBREVIATION_RE.search(token):
                    continue
            s = prefix.strip()
            if s:
                spans.append(s)
            sent_start = match.end()

        tail = raw_prose[sent_start:].strip()
        if tail:
            spans.append(tail)

    return spans


def format_spans_for_prompt(spans: list[str]) -> str:
    """Format numbered spans for model consumption.

    Args:
        spans: List of segmented text spans.

    Returns:
        Formatted string with 1-based indices [1]..[N].
    """
    lines = []
    for idx, span in enumerate(spans, 1):
        lines.append(f"[{idx}] {span}")
    return "\n".join(lines)


def parse_attribution_response(raw_text: str, total_spans: int) -> dict[str, Any]:
    """Parse and validate JSON attribution response from the LLM.

    Args:
        raw_text: Raw response string from LLM.
        total_spans: Total count of spans sent to LLM for index bounding.

    Returns:
        Dict with keys supporting_indices, unsupported_aspects.
    """
    text = (raw_text or "").strip()

    # Strip code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    parsed = None
    try:
        parsed = json.loads(text)
    except Exception:
        # Fallback: extract first JSON object
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception as e:
                logger.debug("Failed parsing extracted JSON from response: %s", e)

    if not isinstance(parsed, dict):
        return {
            "supporting_indices": [],
            "unsupported_aspects": "Failed to parse model evaluation",
        }

    raw_indices = parsed.get("supporting_indices")
    indices: list[int] = []
    if isinstance(raw_indices, list):
        for val in raw_indices:
            try:
                ival = int(val)
                if 1 <= ival <= total_spans and ival not in indices:
                    indices.append(ival)
            except (ValueError, TypeError):
                continue
    indices.sort()

    unsupported = str(parsed.get("unsupported_aspects") or "").strip()

    return {
        "supporting_indices": indices,
        "unsupported_aspects": unsupported,
    }


async def compute_sentence_attribution(
    claim: str,
    source_text: str,
    api_key: str | None = None,
    timeout: float = SENTENCE_ATTRIBUTION_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Compute sentence-level attribution for a claim against source text.

    Args:
        claim: Target claim statement to verify.
        source_text: Grounding source excerpt or chunk.
        api_key: OpenRouter API key.
        timeout: Timeout in seconds for model completion.

    Returns:
        Attribution result dictionary.
    """
    import litellm

    spans = segment_source_text(source_text)
    if not spans:
        return {
            "status": "ok",
            "supporting_indices": [],
            "spans": [],
            "unsupported_aspects": "Source passage is empty",
            "model": SENTENCE_ATTRIBUTION_MODEL,
            "latency_ms": 0.0,
        }

    formatted_spans = format_spans_for_prompt(spans)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": ATTRIBUTION_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Claim:\n{claim.strip()}\n\nNumbered Spans:\n{formatted_spans}\n\nEvaluation JSON:",
        },
    ]

    openrouter_model = f"openrouter/{SENTENCE_ATTRIBUTION_MODEL}"
    kwargs: dict[str, Any] = {
        "model": openrouter_model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 300,
        "extra_body": {"reasoning": {"effort": "none"}},
    }
    if api_key:
        kwargs["api_key"] = api_key

    t0 = time.perf_counter()
    try:
        response = await asyncio.wait_for(
            litellm.acompletion(**kwargs),
            timeout=timeout,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        raw_content = ""
        if response and response.choices and len(response.choices) > 0:
            choice = response.choices[0]
            if hasattr(choice, "message") and choice.message:
                raw_content = getattr(choice.message, "content", "") or ""
            elif isinstance(choice, dict):
                raw_content = choice.get("message", {}).get("content", "")

        parsed = parse_attribution_response(raw_content, len(spans))
        return {
            "status": "ok",
            "supporting_indices": parsed["supporting_indices"],
            "spans": spans,
            "unsupported_aspects": parsed["unsupported_aspects"],
            "model": SENTENCE_ATTRIBUTION_MODEL,
            "latency_ms": round(elapsed_ms, 2),
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        logger.warning("Sentence attribution completion failed: %s", exc)
        return {
            "status": "error",
            "supporting_indices": [],
            "spans": spans,
            "unsupported_aspects": f"Evaluation error: {exc}",
            "model": SENTENCE_ATTRIBUTION_MODEL,
            "latency_ms": round(elapsed_ms, 2),
        }


async def persist_sentence_attribution_to_session(
    session_id: str,
    invocation_id: str | None,
    chunk_index: int | None,
    occurrence: int | None,
    claim: str,
    attribution_data: dict[str, Any],
    session_service: Any,
    app_name: str = "zocrates",
) -> bool:
    """Locate matching assessment in session state and persist sentence attribution.

    Updates both in-memory session.state['retrieved_evidence'] and the SQLite
    sessions.db database when session_service is backed by DatabaseSessionService.

    Args:
        session_id: Target session identifier.
        invocation_id: Optional invocation ID scoping the turn.
        chunk_index: 1-based index of the chunk.
        occurrence: 1-based occurrence number of citation.
        claim: Claim text to match.
        attribution_data: Sentence attribution result dictionary.
        session_service: Active ADK session service.
        app_name: Application name for session service.

    Returns:
        True if an assessment was found and updated, False otherwise.
    """
    if not session_service or not session_id:
        return False

    try:
        raw_sess = session_service.get_session(
            app_name=app_name, user_id="default_user", session_id=session_id
        )
        sess = await raw_sess if inspect.isawaitable(raw_sess) else raw_sess
    except Exception as exc:
        logger.debug("Failed loading session %s for attribution persistence: %s", session_id, exc)
        return False

    if not sess or not getattr(sess, "state", None):
        return False

    # 1. Update in-memory session.state
    audit_history = list(sess.state.get(RETRIEVED_EVIDENCE) or [])
    updated = False
    for entry in reversed(audit_history):
        if invocation_id and entry.get("invocation_id") and entry.get("invocation_id") != invocation_id:
            continue
        tel = entry.get("citation_verification_telemetry") or {}
        assessments = tel.get("assessments") or []
        for a in assessments:
            matched = False
            if chunk_index and occurrence and a.get("chunk_index") == chunk_index and a.get("occurrence") == occurrence:
                matched = True
            elif a.get("claim") == claim:
                matched = True

            if matched:
                a["sentence_attribution"] = attribution_data
                updated = True
                break
        if updated:
            break

    # 2. Persist to SQLite sessions.db via DatabaseSessionService if supported
    if updated and hasattr(session_service, "_rollback_on_exception_session"):
        try:
            schema = session_service._get_schema_classes()
            async with session_service._rollback_on_exception_session() as sql_session:
                from sqlalchemy import select
                from sqlalchemy.orm.attributes import flag_modified

                stmt = (
                    select(schema.StorageSession)
                    .filter(schema.StorageSession.app_name == app_name)
                    .filter(schema.StorageSession.user_id == "default_user")
                    .filter(schema.StorageSession.id == session_id)
                )
                res = await sql_session.execute(stmt)
                row = res.scalar_one_or_none()
                if row and getattr(row, "state", None):
                    db_history = list(row.state.get(RETRIEVED_EVIDENCE) or [])
                    db_updated = False
                    for entry in reversed(db_history):
                        if invocation_id and entry.get("invocation_id") and entry.get("invocation_id") != invocation_id:
                            continue
                        tel = entry.get("citation_verification_telemetry") or {}
                        assessments = tel.get("assessments") or []
                        for a in assessments:
                            matched = False
                            if chunk_index and occurrence and a.get("chunk_index") == chunk_index and a.get("occurrence") == occurrence:
                                matched = True
                            elif a.get("claim") == claim:
                                matched = True
                            if matched:
                                a["sentence_attribution"] = attribution_data
                                db_updated = True
                                break
                        if db_updated:
                            break
                    if db_updated:
                        flag_modified(row, "state")
                        await sql_session.commit()
        except Exception as sql_exc:
            logger.warning("Failed persisting sentence attribution to sessions.db: %s", sql_exc)

    return updated
