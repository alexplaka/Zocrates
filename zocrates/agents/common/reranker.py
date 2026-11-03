"""Evidence reranking and budget trimming utilities for Zocrates research agents."""

import logging
from typing import Any

import httpx

from zocrates.config import (
    get_secure_api_key,
    load_zocrates_config,
)

logger = logging.getLogger(__name__)

HARD_WORD_CEILING: int = 7500
MAX_RERANK_CHUNKS: int = 15


def _trim_to_budget(
    chunks: list[dict[str, Any]], word_ceiling: int = HARD_WORD_CEILING
) -> list[dict[str, Any]]:
    """Trim lowest-ranked chunks so cumulative words <= word_ceiling.

    Preserves at least the first chunk even if it alone exceeds word_ceiling.
    """
    if not chunks:
        return []
    trimmed: list[dict[str, Any]] = []
    accumulated_words = 0
    for chunk in chunks:
        wc = chunk.get("word_count")
        if wc is None:
            wc = len(chunk.get("text", "").split())
        if trimmed and (accumulated_words + wc > word_ceiling):
            continue
        trimmed.append(chunk)
        accumulated_words += wc
    return trimmed


def is_substantive_chunk(chunk: dict[str, Any]) -> bool:
    """Determine whether a chunk contains substantive content rather than frontmatter or TOC.

    Returns False if:
      - chunk.get("chunk_type") == "frontmatter"
      - str(chunk.get("chunk_id", "")).endswith(":s0") or chunk.get("asset_id") == "s0"
      - Heading matches frontmatter or TOC: ('frontmatter', 'table of contents', 'contents', 'toc')
      - Text is pure YAML frontmatter (starts with '---', contains 'title:' or 'authors:', and has '\n---')
    Otherwise returns True.
    """
    if chunk.get("chunk_type") == "frontmatter":
        return False

    chunk_id = str(chunk.get("chunk_id") or "")
    if chunk_id.endswith(":s0") or chunk.get("asset_id") == "s0":
        return False

    heading = str(chunk.get("heading") or "").strip().lower()
    if heading in ("frontmatter", "table of contents", "contents", "toc"):
        return False

    text = str(chunk.get("text") or "")
    clean_text = text.strip()
    if clean_text.startswith(("---\n", "---\r\n")) and (
        "\ntitle:" in clean_text or "\nauthors:" in clean_text or clean_text.startswith("---\ntitle:") or clean_text.startswith("---\nauthors:")
    ) and "\n---" in clean_text:
        return False

    return True


def filter_substantive_evidence(
    chunks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Filter out non-substantive evidence chunks (frontmatter, TOC).

    Fallback guard: If filtering yields 2 or fewer substantive chunks (regardless
    of whether any remain), returns the original chunks so the synthesizer is not
    starved of evidence.  This preserves frontmatter chunks when there is not
    enough substantive content to stand on its own (e.g. the query only matched
    1-2 real sections alongside a frontmatter hit).
    """
    if not chunks:
        return []

    substantive = [c for c in chunks if is_substantive_chunk(c)]
    if len(substantive) <= 2:
        if len(chunks) > len(substantive):
            logger.debug(
                "Substantive filter left only %d chunk(s) (<= 2); falling back to original %d chunks.",
                len(substantive),
                len(chunks),
            )
        return chunks
    return substantive


async def rerank_evidence(
    query: str,
    chunks: list[dict[str, Any]],
    max_chunks: int | None = None,
    word_ceiling: int | None = None,
) -> list[dict[str, Any]]:
    """Rerank candidate chunks using OpenRouter cross-encoder with budget trimming and fallback."""
    chunks = filter_substantive_evidence(chunks)

    effective_max_chunks = max_chunks if max_chunks is not None else MAX_RERANK_CHUNKS
    effective_word_ceiling = word_ceiling if word_ceiling is not None else HARD_WORD_CEILING

    if not chunks:
        return []

    if len(chunks) == 1:
        single = dict(chunks[0])
        single["rerank_score"] = None
        return _trim_to_budget([single], effective_word_ceiling)

    api_key = get_secure_api_key()

    # Fail-open enforcement (Phase 7 onboarding, plan §5.1/§8): a missing or
    # invalid reranker model config must degrade to un-reranked retrieval with
    # a logged warning — never fail the turn or the search run.
    try:
        reranker_model_raw = load_zocrates_config().get("reranker_model")
    except Exception as cfg_exc:
        logger.warning(
            "Failed to read reranker_model from config (%s). Skipping cross-encoder rerank.",
            cfg_exc,
        )
        trimmed = [dict(c, rerank_score=None) for c in chunks][:effective_max_chunks]
        return _trim_to_budget(trimmed, effective_word_ceiling)

    if not isinstance(reranker_model_raw, str) or not reranker_model_raw.strip():
        logger.warning(
            "No reranker model configured (reranker_model unset or invalid). "
            "Skipping cross-encoder rerank; retrieval proceeds un-reranked."
        )
        trimmed = [dict(c, rerank_score=None) for c in chunks][:effective_max_chunks]
        return _trim_to_budget(trimmed, effective_word_ceiling)

    reranker_model = reranker_model_raw.strip()

    if not api_key:
        logger.debug(
            "No OpenRouter API key found. Skipping cross-encoder rerank."
        )
        trimmed = [dict(c, rerank_score=None) for c in chunks][:effective_max_chunks]
        return _trim_to_budget(trimmed, effective_word_ceiling)

    documents = [c.get("text", "") for c in chunks]
    payload = {
        "model": reranker_model,
        "query": query,
        "documents": documents,
        "top_n": len(documents),
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/rerank",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        results = data.get("results", [])
        score_by_idx = {
            r["index"]: r.get("relevance_score", 0.0) for r in results
        }

        ranked_chunks = []
        for idx, chunk in enumerate(chunks):
            chunk_copy = dict(chunk)
            chunk_copy["rerank_score"] = float(score_by_idx.get(idx, 0.0))
            ranked_chunks.append(chunk_copy)

        ranked_chunks.sort(
            key=lambda c: c.get("rerank_score", 0.0), reverse=True
        )
        top_candidates = ranked_chunks[:effective_max_chunks]
        return _trim_to_budget(top_candidates, effective_word_ceiling)

    except Exception as e:
        logger.warning(
            "OpenRouter rerank call failed (%s: %s). Falling back to original chunk order.",
            type(e).__name__,
            e,
        )
        trimmed = [dict(c, rerank_score=None) for c in chunks][:effective_max_chunks]
        return _trim_to_budget(trimmed, effective_word_ceiling)
