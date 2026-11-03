"""
Document summary generator utility with LiteLLM OpenRouter integration
and deterministic extractive lead text heuristics.
"""

import html
import logging
import re
from pathlib import Path
from typing import Any

import litellm

from zocrates.rag.bundle_catalog import strip_existing_frontmatter
from zocrates.config import (
    get_secure_api_key,
    load_zocrates_config,
)

logger = logging.getLogger(__name__)

DEFAULT_SUMMARY_MODEL = "openrouter/nvidia/nemotron-3.5-lightning:free"
DEFAULT_SUMMARY_MAX_INPUT_CHARS = 3500
DEFAULT_SUMMARY_MAX_TOKENS = 500
DEFAULT_SUMMARY_TIMEOUT_SECONDS = 10.0

SUMMARY_SYSTEM_PROMPT = (
    "You are an expert academic research assistant. "
    "Provide a concise, dense, factual summary (2-3 sentences, 100-150 words) of the following document. "
    "Focus on the primary research problem, core methodology/system architecture, and key findings or results. "
    "Do not include conversational filler, meta-commentary, or any thinking process/scratchpad. "
    "Output ONLY the final summary text directly."
)

# Regexes for extractive lead text heuristic
_IN_BODY_ABSTRACT_RE = re.compile(
    r"(?:^|\n)(?:#{1,6}\s+|(?:\*\*|__)\s*)Abstract(?:\s*(?:\*\*|__)|:|\s*\n)([\s\S]*?)(?=(?:\n#{1,6}\s+|\n<figure|\Z))",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", re.MULTILINE)
_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->", re.MULTILINE)
_FIGURE_BLOCK_RE = re.compile(r"<figure\b[^>]*>[\s\S]*?</figure>", re.IGNORECASE | re.MULTILINE)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_FORMAT_RE = re.compile(r"(\*\*|__|\*|_|`|~~)")
_BOILERPLATE_LINE_RE = re.compile(
    r"(@[a-zA-Z0-9._-]+\.[a-zA-Z]+|"
    r"\barxiv:\s*[\d.]+|"
    r"\bdoi:\s*10\.\d+|"
    r"\bdepartment of\b|"
    r"\buniversity\b|"
    r"\bfaculty of\b|"
    r"\bschool of\b|"
    r"\binstitute of\b|"
    r"\bcopyright\s*(?:©|\(c\)|20\d\d|19\d\d)|"
    r"\ball rights reserved\b|"
    r"\bcreative commons\b|"
    r"\bcc-by\b)",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"([.!?])(?:\s+|\Z)")


def _clean_markdown_prose(text: str) -> str:
    """Strip markdown links, formatting, and excess whitespace from a text snippet."""
    if not text:
        return ""
    # Convert [text](url) -> text
    cleaned = _MD_LINK_RE.sub(r"\1", text)
    # Strip formatting markers (*, **, _, __, `, ~~)
    cleaned = _MD_FORMAT_RE.sub("", cleaned)
    # Decode HTML entities
    cleaned = html.unescape(cleaned)
    # Collapse multiple whitespaces/newlines to single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _truncate_to_sentence_boundary(text: str, max_chars: int = 900, max_words: int = 160) -> str:
    """Truncate text at the last complete sentence boundary within char and word budgets."""
    cleaned = _clean_markdown_prose(text)
    if not cleaned:
        return ""

    words = cleaned.split()
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words])

    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars]

    # Find the last sentence end marker
    matches = list(_SENTENCE_END_RE.finditer(cleaned))
    if matches:
        last_match = matches[-1]
        truncated = cleaned[: last_match.end()].strip()
        if len(truncated.split()) >= 15:
            return truncated

    # If no clean sentence boundary found or too short, return cleaned text with trailing period if needed
    if cleaned and not cleaned.endswith((".", "!", "?")):
        return cleaned + "."
    return cleaned


def extract_lead_text(text: str) -> str:
    """Extract substantive lead text or in-body abstract deterministically without an LLM.

    Implements the 3-step heuristic:
      1. In-body 'Abstract' section detection (fast path).
      2. Prose paragraph accumulation & boilerplate filtering (general path).
      3. Safety fallback.

    Args:
        text: Converted Markdown document text (with or without frontmatter).

    Returns:
        Clean, informative extractive lead text string.
    """
    body = strip_existing_frontmatter(text).strip()
    if not body:
        return "No abstract or summary available."

    # -----------------------------------------------------------------------
    # Step 1: In-body "Abstract" section detection (Fast Path)
    # -----------------------------------------------------------------------
    abs_match = _IN_BODY_ABSTRACT_RE.search(body)
    if abs_match:
        abstract_candidate = abs_match.group(1).strip()
        cleaned_abs = _clean_markdown_prose(abstract_candidate)
        word_count = len(cleaned_abs.split())
        if 20 <= word_count <= 400:
            return _truncate_to_sentence_boundary(cleaned_abs, max_chars=1200, max_words=200)

    # -----------------------------------------------------------------------
    # Step 2: Substantive lead prose paragraph accumulation (General Path)
    # -----------------------------------------------------------------------
    # Mask non-prose blocks
    masked = _CODE_FENCE_RE.sub("\n\n", body)
    masked = _HTML_COMMENT_RE.sub("\n\n", masked)
    masked = _FIGURE_BLOCK_RE.sub("\n\n", masked)

    paragraphs = re.split(r"\n\s*\n+", masked)
    collected_paras: list[str] = []
    total_words = 0

    for para in paragraphs:
        p = para.strip()
        if not p:
            continue

        # Skip ATX headings
        if p.startswith("#"):
            continue

        # Skip short navigation lines, link lists, or breadcrumbs
        if len(p) < 40 and ("[" in p or ">" in p or "|" in p):
            continue

        lines = [line.strip() for line in p.splitlines() if line.strip()]
        # Check if entire paragraph is boilerplate / metadata
        is_boilerplate = False
        for line in lines:
            if _BOILERPLATE_LINE_RE.search(line):
                is_boilerplate = True
                break
        if is_boilerplate:
            continue

        clean_p = _clean_markdown_prose(p)
        words = clean_p.split()
        # Only take substantive prose paragraphs (>= 15 words)
        if len(words) >= 15:
            collected_paras.append(clean_p)
            total_words += len(words)
            if total_words >= 90:
                break

    if collected_paras:
        combined = " ".join(collected_paras)
        result = _truncate_to_sentence_boundary(combined, max_chars=900, max_words=150)
        if result and len(result.split()) >= 15:
            return result

    # -----------------------------------------------------------------------
    # Step 3: Safety Fallback
    # -----------------------------------------------------------------------
    return "No abstract or summary available."


def generate_summary(
    text: str,
    title: str | None = None,
    zotero_data_dir: Path | str | None = None,
    model: str | None = None,
    timeout: float = DEFAULT_SUMMARY_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Generate a concise document summary using LiteLLM with extractive fallback.

    Hierarchy:
      1. Model: Explicit override, configured `summary_model`, `llm_model`, or `openai/gpt-oss-20b` via OpenRouter.
      2. Offline / Error Fallback: Deterministic extractive lead text.

    Args:
        text: Converted Markdown text.
        title: Optional document title.
        zotero_data_dir: Optional Zotero data directory for loading config/keys.
        model: Optional explicit model override.
        timeout: Request timeout in seconds (default: 10s).

    Returns:
        A 2-tuple of `(summary_text, source_tag)` where source_tag is 'generated' or 'extractive'.
    """
    body = strip_existing_frontmatter(text).strip()
    if not body:
        return ("No abstract or summary available.", "extractive")

    # Guardrail: Truncate input to lead text budget (~3500 chars)
    lead_input = body[:DEFAULT_SUMMARY_MAX_INPUT_CHARS]

    # Resolve OpenRouter API key and model preferences
    cfg = load_zocrates_config(zotero_data_dir)
    api_key = get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))

    resolved_model = (
        model
        or cfg.get("summary_model")
        or DEFAULT_SUMMARY_MODEL
    )
    if not resolved_model.startswith("openrouter/") and not resolved_model.startswith(("ollama/", "ollama_chat/")):
        resolved_model = f"openrouter/{resolved_model}"

    is_local = resolved_model.startswith(("ollama/", "ollama_chat/"))

    if not api_key and not is_local:
        logger.debug("No OpenRouter API key configured; falling back to deterministic extractive lead text.")
        extractive = extract_lead_text(body)
        return extractive, "extractive"

    if api_key or is_local:
        doc_context = f"Title: {title}\n\n" if title else ""
        user_prompt = f"{doc_context}Document Text (first section):\n{lead_input}\n\nSummary:"

        messages = [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        try:
            logger.info("Attempting summary generation with %s ...", resolved_model)
            extra_body = {"reasoning": {"effort": "none"}} if resolved_model.startswith("openrouter/") else {}
            resp = litellm.completion(
                model=resolved_model,
                messages=messages,
                api_key=api_key,
                max_tokens=DEFAULT_SUMMARY_MAX_TOKENS,
                timeout=timeout,
                temperature=0.2,
                extra_body=extra_body,
            )
            raw_summary = (
                resp.choices[0].message.content
                if resp.choices and resp.choices[0].message
                else ""
            )
            cleaned = (raw_summary or "").strip()
            # Strip <think>...</think> tags (handling unclosed and pre-injected tags)
            cleaned = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", cleaned, flags=re.IGNORECASE).strip()
            cleaned = re.sub(r"^[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE).strip()
            # Strip "Here's a thinking process: ..." blocks
            cleaned = re.sub(
                r"(?i)(?:here'?s a thinking process|thinking process|thought process):[\s\S]*?(?=(?:\n\n[A-Z]|\n\n\*\*Summary|\n\nSummary|\Z))",
                "",
                cleaned,
            ).strip()
            # Strip leading "Summary:" label
            cleaned = re.sub(r"(?i)^\*?\*?(?:summary|final summary)\*?\*?:\s*", "", cleaned).strip()

            if cleaned and len(cleaned.split()) >= 8:
                # Ensure clean sentence ending
                if not cleaned.endswith((".", "!", "?")):
                    cleaned += "."
                return cleaned, "generated"
        except Exception as exc:
            logger.warning(
                "Summary generation failed with model %s: %s; falling back to extractive",
                resolved_model,
                exc,
            )

    # LLM attempt failed or no API key available: use deterministic extractive lead text
    logger.info("Using deterministic extractive lead text fallback for summary.")
    extractive = extract_lead_text(body)
    return extractive, "extractive"
