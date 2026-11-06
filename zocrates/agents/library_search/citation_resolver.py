"""Deterministic citation and reference resolver for Zocrates syntheses."""

import logging
import re
from pathlib import Path
from typing import Any

from zocrates.agents.common.prompts import _format_authors_clean, _format_year_clean

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_EXTRACTIVE_QUOTE_CHARS",
    "resolve_citations_and_references",
    "build_coordinator_view",
]

MAX_EXTRACTIVE_QUOTE_CHARS: int | None = None

# Regex patterns for citations:
# Standard brackets with optional E/Chunk prefix: [1], [E 1], [Chunk 2], [1, 2], [1; 3]
_BRACKET_CITATION_RE = re.compile(
    r"\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]"
)
# Fullwidth brackets from Asian / reasoning models: 【1】, 【1, 2】
_FULLWIDTH_CITATION_RE = re.compile(
    r"【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】"
)
# Combined in-text numeric chunk citation token regex
_IN_TEXT_CITATION_TOKEN_RE = re.compile(
    r"\[(?:E\s?|Chunk\s?)?(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)\]|【(\d{1,2}(?:\s*[,;]\s*\d{1,2})*)】"
)

# Code block masking regex patterns to prevent collision with array indexes / code syntax
_CODE_BLOCK_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)")

# Image Markdown masking regex: ![alt](url) — must be masked before bracket-citation
# processing because _BRACKET_CITATION_RE would otherwise match bare numbers in
# image alt text (e.g. ![3](zocrates-img://...) would rewrite the '3' as a citation).
# Masking (placeholder + restore) is preferred over a negative lookbehind for
# consistency with the existing code-block masking pass.
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_DEF_RE = re.compile(r"^\s*\[\d+\]:\s*\S+.*$", re.MULTILINE)


def _read_publication_from_bundle(bundle_dir: Path) -> str | None:
    """Read publication venue from bundle document.md YAML frontmatter."""
    doc_path = bundle_dir / "document.md"
    if not doc_path.exists():
        return None
    try:
        with open(doc_path, "r", encoding="utf-8") as f:
            first = f.readline()
            if not first.startswith("---"):
                return None
            for line in f:
                if line.startswith("---"):
                    break
                if line.startswith("publication:"):
                    parts = line.split(":", 1)
                    if len(parts) > 1:
                        pub = parts[1].strip().strip('"').strip("'")
                        if pub:
                            return pub
    except Exception as exc:
        logger.debug("Failed reading publication from %s: %s", doc_path, exc)
    return None


def _resolve_publication(
    item_key: str,
    attachment_key: str | None = None,
    zotero_dir: Path | None = None,
    cache: dict[str, str] | None = None,
) -> str:
    """Resolve publication venue for an item from bundle frontmatter or zotero_db."""
    if not item_key:
        return ""
    if cache is not None and item_key in cache:
        return cache[item_key]

    pub = ""
    if zotero_dir is not None:
        try:
            from zocrates.config import get_corpus_dir, resolve_zotero_data_dir

            resolved_dir = resolve_zotero_data_dir(zotero_dir)
            if resolved_dir:
                cache_dir = get_corpus_dir(resolved_dir)
                # Check item_key bundle first
                p = _read_publication_from_bundle(cache_dir / item_key)
                if p:
                    pub = p
                elif attachment_key:
                    p = _read_publication_from_bundle(cache_dir / attachment_key)
                    if p:
                        pub = p

                # If still empty, check ZoteroDB
                if not pub:
                    sqlite_path = resolved_dir / "zotero.sqlite"
                    if sqlite_path.exists():
                        from zocrates.rag.zotero_db import ZoteroDB

                        zdb = ZoteroDB(sqlite_path=sqlite_path, storage_dir=resolved_dir / "storage")
                        item = zdb.get_item_by_key(item_key)
                        if item and getattr(item, "publication", None):
                            pub = str(item.publication).strip()
        except Exception as exc:
            logger.debug("Error resolving publication for %s: %s", item_key, exc)

    if cache is not None:
        cache[item_key] = pub
    return pub


def _resolve_item_metadata_fallback(
    item_key: str,
    zotero_dir: Path | None = None,
    cache: dict[str, tuple[str, str, str]] | None = None,
) -> tuple[str, str, str]:
    """Fallback to resolve clean (authors, year, title) from bundle frontmatter or Zotero DB."""
    if cache is not None and item_key in cache:
        return cache[item_key]

    resolved_authors = ""
    resolved_year = ""
    resolved_title = ""

    try:
        from zocrates.agents.common.toolkit import _resolve_bundle_dir
        bundle_res = _resolve_bundle_dir(item_key)
        if bundle_res:
            doc_md = bundle_res[0] / "document.md"
            if doc_md.exists():
                import yaml
                import re
                text_start = doc_md.read_text(encoding="utf-8")
                if text_start.startswith("---"):
                    parts = text_start.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1])
                        if isinstance(fm, dict):
                            if fm.get("authors"):
                                resolved_authors = _format_authors_clean(fm["authors"]) or ""
                            if fm.get("year"):
                                resolved_year = _format_year_clean(fm["year"]) or ""
                            elif fm.get("date"):
                                m = re.search(r"\b(19\d\d|20\d\d)\b", str(fm["date"]))
                                if m:
                                    resolved_year = m.group(1)
                            if fm.get("title") and str(fm["title"]).strip().lower() not in ("snapshot", "full text", "attachment"):
                                resolved_title = str(fm["title"]).strip()
    except Exception:
        pass

    res = (resolved_authors, resolved_year, resolved_title)
    if cache is not None:
        cache[item_key] = res
    return res


def _format_doc_citation(d: dict[str, Any]) -> tuple[str, str]:
    """Return (bold_meta, title_and_pub)."""
    raw_auth = d.get("authors")
    auth = _format_authors_clean(raw_auth) if raw_auth else ""
    raw_yr = d.get("year")
    yr = _format_year_clean(raw_yr) if raw_yr else ""
    title = str(d.get("title") or "Untitled Document").strip()
    pub = str(d.get("publication") or "").strip()

    if auth and yr:
        meta = f"**{auth} ({yr})**"
    elif auth:
        meta = f"**{auth}**"
    elif yr:
        meta = f"**({yr})**"
    else:
        meta = ""

    rest_parts = [f"*{title}*"]
    if pub:
        rest_parts.append(f"{pub}")
    rest = ". ".join(rest_parts) + "."
    return meta, rest


def resolve_citations_and_references(
    text: str,
    ranked_chunks: list[dict[str, Any]],
    zotero_dir: Path | None = None,
    references_enabled: bool = True,
    considered_disclosure: bool = True,
    citations_enabled: bool = True,
    support_probabilities: dict[int, float | None] | dict[int, list[float | None]] | None = None,
    verification_threshold: float = 0.60,
) -> dict[str, Any]:
    """Parse in-text citation tokens, bounds check evidence, and assemble references.

    Args:
        text: Synthesizer agent markdown output containing citation tokens.
        ranked_chunks: Complete list of retrieved evidence chunks passed to the
            synthesizer, ordered by descending rerank score.
        zotero_dir: Optional root Zotero data directory for DB / bundle enrichment.
        references_enabled: Whether to assemble and append the references section.
        considered_disclosure: Whether to append Tier 2 (Other Considered Sources).
        citations_enabled: Whether in-text numeric citation tokens should be parsed
            and preserved. When False, all bracket citation tokens are stripped from
            the text and ``cited_indices_order`` is forced empty, which triggers the
            zero-citation fallback so every retrieved document appears in Tier 1 of
            the references section (when ``references_enabled`` is True).

    Returns:
        Dict with keys:
            - ``enriched_text``: Markdown text with validated in-text citation tokens
              and appended two-tier references.
            - ``citations``: List of citation metadata dicts for all cited chunks.
            - ``references``: List of document metadata dicts with tier attribute (1 or 2).
            - ``cited_chunk_ids``: List of chunk IDs cited in the text.
    """
    if not text or not ranked_chunks:
        return {
            "enriched_text": text or "",
            "citations": [],
            "references": [],
            "cited_chunk_ids": [],
        }

    total_chunks = len(ranked_chunks)

    # 1. Mask code fences and inline code spans to prevent collisions
    code_placeholders: list[str] = []

    def _mask_code(m: re.Match) -> str:
        """Replace a code fence or inline code span with a unique mask placeholder.

        Args:
            m: Regular expression match object containing the matched code text.

        Returns:
            Placeholder string representing the masked code block.
        """
        idx = len(code_placeholders)
        code_placeholders.append(m.group(0))
        return f"@@ZOCRATES_CODE_SPAN_{idx}@@"

    masked_text = _CODE_BLOCK_RE.sub(_mask_code, text)

    # 1b. Mask image Markdown ![alt](url) to prevent _BRACKET_CITATION_RE from
    #     matching bare numbers inside image alt text (e.g. ![3](zocrates-img://...)).
    image_placeholders: list[str] = []

    def _mask_image(m: re.Match) -> str:
        """Replace a Markdown image syntax span with a unique mask placeholder.

        Args:
            m: Regular expression match object containing the image Markdown syntax.

        Returns:
            Placeholder string representing the masked image.
        """
        idx = len(image_placeholders)
        image_placeholders.append(m.group(0))
        return f"@@ZOCRATES_IMAGE_SPAN_{idx}@@"

    masked_text = _MD_IMAGE_RE.sub(_mask_image, masked_text)

    # 1c. Mask markdown link reference definitions: [1]: http...
    link_def_placeholders: list[str] = []

    def _mask_link_def(m: re.Match) -> str:
        """Replace a Markdown link reference definition with a unique mask placeholder.

        Args:
            m: Regular expression match object containing the link definition.

        Returns:
            Placeholder string representing the masked link definition.
        """
        idx = len(link_def_placeholders)
        link_def_placeholders.append(m.group(0))
        return f"@@ZOCRATES_LINK_DEF_SPAN_{idx}@@"

    masked_text = _MD_LINK_DEF_RE.sub(_mask_link_def, masked_text)

    # 2. Scan and bounds check citations
    cited_indices_order: list[int] = []
    chunk_hit_counts: dict[int, int] = {}
    pre_filtered_valid_indices: set[int] = set()

    # Normalize support_probabilities:
    # Accept both flat dict[int, float | None] (legacy) and dict[int, list[float | None]] (per-occurrence).
    # If flat, normalize to [prob] * total_occurrences.
    norm_support_probs: dict[int, list[float | None]] | None = None
    if support_probabilities is not None:
        is_per_occurrence = any(isinstance(v, list) for v in support_probabilities.values())
        if is_per_occurrence:
            norm_support_probs = {
                k: (list(v) if isinstance(v, list) else [v])
                for k, v in support_probabilities.items()
            }
        else:
            # Count total occurrences per chunk in masked_text
            chunk_occ_counts: dict[int, int] = {}
            for m in _IN_TEXT_CITATION_TOKEN_RE.finditer(masked_text):
                raw = m.group(1) if m.group(1) is not None else m.group(2)
                if not raw:
                    continue
                parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
                seen_in_token: set[int] = set()
                for p in parts:
                    if p.isdigit():
                        val = int(p)
                        if 1 <= val <= total_chunks and val not in seen_in_token:
                            seen_in_token.add(val)
                            chunk_occ_counts[val] = chunk_occ_counts.get(val, 0) + 1
            norm_support_probs = {
                k: [v] * max(chunk_occ_counts.get(k, 1), 1)
                for k, v in support_probabilities.items()
            }

    occ_counters: dict[int, int] = {}
    occ_records: dict[int, list[dict[str, Any]]] = {}

    if not citations_enabled:
        # Strip all in-text citation tokens without recording any indices
        masked_text = _IN_TEXT_CITATION_TOKEN_RE.sub("", masked_text)
    else:
        def _process_match(m: re.Match) -> str:
            """Process and filter citation tokens against verification probabilities.

            Args:
                m: Regular expression match object containing matched citation bracket numbers.

            Returns:
                Formatted surviving citation token string, or empty string if pruned.
            """
            raw_content = m.group(1) if m.group(1) is not None else m.group(2)
            if not raw_content:
                return ""
            parts = [p.strip() for p in re.split(r"[,;]", raw_content) if p.strip()]
            valid_nums: list[int] = []
            seen_in_token: set[int] = set()

            for p in parts:
                if not p.isdigit():
                    continue
                val = int(p)
                if not (1 <= val <= total_chunks):
                    continue
                if val in seen_in_token:
                    continue
                seen_in_token.add(val)

                pre_filtered_valid_indices.add(val)
                occ_idx = occ_counters.get(val, 0)
                occ_counters[val] = occ_idx + 1
                occ_num = occ_idx + 1

                prob: float | None = None
                if norm_support_probs is not None:
                    p_list = norm_support_probs.get(val)
                    if p_list is not None and occ_idx < len(p_list):
                        prob = p_list[occ_idx]
                    elif p_list is not None and len(p_list) > 0:
                        prob = p_list[-1]

                survived = not (prob is not None and prob < verification_threshold)
                p_rounded = round(float(prob), 3) if isinstance(prob, (float, int)) else prob
                occ_records.setdefault(val, []).append({
                    "occurrence": occ_num,
                    "support_probability": p_rounded,
                    "survived": survived,
                })

                if not survived:
                    continue

                valid_nums.append(val)
                chunk_hit_counts[val] = chunk_hit_counts.get(val, 0) + 1
                if val not in cited_indices_order:
                    cited_indices_order.append(val)

            if not valid_nums:
                # Out of bounds, invalid, or all filtered: strip completely
                return ""

            return f"[{', '.join(str(n) for n in valid_nums)}]"

        # Replace citation tokens in single pass in document order
        masked_text = _IN_TEXT_CITATION_TOKEN_RE.sub(_process_match, masked_text)

    # 3. Unmask code blocks
    for i, code_str in enumerate(code_placeholders):
        masked_text = masked_text.replace(f"@@ZOCRATES_CODE_SPAN_{i}@@", code_str)

    # 3b. Unmask image Markdown
    for i, img_str in enumerate(image_placeholders):
        masked_text = masked_text.replace(f"@@ZOCRATES_IMAGE_SPAN_{i}@@", img_str)

    # 3c. Unmask markdown link reference definitions
    for i, link_def_str in enumerate(link_def_placeholders):
        masked_text = masked_text.replace(f"@@ZOCRATES_LINK_DEF_SPAN_{i}@@", link_def_str)

    # Clean whitespace left by stripped citations (e.g. "claim  text" or "claim .")
    cleaned_text = re.sub(r"[ \t]+([.,;!?])", r"\1", masked_text)
    cleaned_text = re.sub(r"[ \t]{2,}", " ", cleaned_text)

    # 4. Zero-citation fallback detection
    # Also triggers when citations_enabled=False (cited_indices_order is always empty)
    # BLOCKER 1 RESOLUTION: When support_probabilities is provided and all citations fail
    # verification, do NOT trigger zero_citation_fallback! Set tier1_keys = [] and place
    # all candidate documents in tier2_keys (## Other Considered Sources), preserving
    # U = C ∪ R without falsely promoting unsupported documents.
    if support_probabilities is not None and len(pre_filtered_valid_indices) > 0 and len(cited_indices_order) == 0:
        zero_citation_fallback = False
    elif not citations_enabled:
        zero_citation_fallback = True
    else:
        zero_citation_fallback = (len(cited_indices_order) == 0)

    # 5. Extract metadata per cited chunk and cited chunk IDs
    citations: list[dict[str, Any]] = []
    cited_chunk_ids: list[str] = []
    pub_cache: dict[str, str] = {}
    meta_cache: dict[str, tuple[str, str, str]] = {}

    for idx in cited_indices_order:
        chunk = ranked_chunks[idx - 1]
        cid = str(chunk.get("chunk_id") or "")
        if cid and cid not in cited_chunk_ids:
            cited_chunk_ids.append(cid)

        item_key = str(chunk.get("item_key") or "")
        att_key = chunk.get("attachment_key")
        title = str(chunk.get("title") or "Untitled Document").strip()
        auth = _format_authors_clean(chunk.get("authors")) or ""
        year = _format_year_clean(chunk.get("year")) or ""

        if not auth or not year or not title or title.lower() in ("snapshot", "full text", "attachment", "untitled document"):
            fb_auth, fb_year, fb_title = _resolve_item_metadata_fallback(item_key, zotero_dir=zotero_dir, cache=meta_cache)
            if not auth and fb_auth:
                auth = fb_auth
            if not year and fb_year:
                year = fb_year
            if (not title or title.lower() in ("snapshot", "full text", "attachment", "untitled document")) and fb_title:
                title = fb_title

        sec = str(chunk.get("heading") or "").strip()
        raw_text = str(chunk.get("text") or "").strip()
        if MAX_EXTRACTIVE_QUOTE_CHARS and len(raw_text) > MAX_EXTRACTIVE_QUOTE_CHARS:
            quote = raw_text[:MAX_EXTRACTIVE_QUOTE_CHARS] + "..."
        else:
            quote = raw_text
        pub = _resolve_publication(item_key, att_key, zotero_dir=zotero_dir, cache=pub_cache)

        citation_dict: dict[str, Any] = {
            "index": idx,
            "token": f"[{idx}]",
            "chunk_id": cid,
            "item_key": item_key,
            "attachment_key": att_key,
            "title": title,
            "authors": auth,
            "year": year,
            "section": sec,
            "quote": quote,
            "publication": pub,
        }
        recs = occ_records.get(idx, [])
        citation_dict["occurrences"] = recs

        if norm_support_probs is not None:
            surviving_probs = [
                r["support_probability"]
                for r in recs
                if r["survived"] and r["support_probability"] is not None
            ]
            if surviving_probs:
                max_surviving_prob = max(surviving_probs)
            else:
                all_probs = [
                    r["support_probability"]
                    for r in recs
                    if r["support_probability"] is not None
                ]
                max_surviving_prob = max(all_probs) if all_probs else None

            citation_dict["support_probability"] = max_surviving_prob

        citations.append(citation_dict)

    # 6. Two-tier source partitioning
    # Group all documents across ranked_chunks
    doc_order: list[str] = []
    docs_by_key: dict[str, dict[str, Any]] = {}

    for idx, chunk in enumerate(ranked_chunks, start=1):
        k = str(chunk.get("item_key") or "")
        if not k:
            continue
        if k not in docs_by_key:
            doc_order.append(k)
            title = str(chunk.get("title") or "Untitled Document").strip()
            auth = _format_authors_clean(chunk.get("authors")) or ""
            year = _format_year_clean(chunk.get("year")) or ""

            if not auth or not year or not title or title.lower() in ("snapshot", "full text", "attachment", "untitled document"):
                fb_auth, fb_year, fb_title = _resolve_item_metadata_fallback(k, zotero_dir=zotero_dir, cache=meta_cache)
                if not auth and fb_auth:
                    auth = fb_auth
                if not year and fb_year:
                    year = fb_year
                if (not title or title.lower() in ("snapshot", "full text", "attachment", "untitled document")) and fb_title:
                    title = fb_title

            att_key = chunk.get("attachment_key")
            pub = _resolve_publication(k, att_key, zotero_dir=zotero_dir, cache=pub_cache)
            docs_by_key[k] = {
                "item_key": k,
                "title": title,
                "authors": auth,
                "year": year,
                "publication": pub,
                "attachment_key": att_key,
                "chunk_indices": [],
            }
        docs_by_key[k]["chunk_indices"].append(idx)

    tier1_keys: list[str] = []
    tier2_keys: list[str] = []

    if zero_citation_fallback:
        # Promote all documents to Tier 1
        tier1_keys = list(doc_order)
    else:
        # Tier 1: Documents with >= 1 cited chunk in text (in order of first citation)
        for idx in cited_indices_order:
            chunk = ranked_chunks[idx - 1]
            k = str(chunk.get("item_key") or "")
            if k and k not in tier1_keys:
                tier1_keys.append(k)

        # Tier 2: Remaining documents with 0 cited chunks
        for k in doc_order:
            if k not in tier1_keys:
                tier2_keys.append(k)

    references: list[dict[str, Any]] = []
    for k in tier1_keys:
        d = dict(docs_by_key[k])
        d["tier"] = 1
        references.append(d)

    for k in tier2_keys:
        d = dict(docs_by_key[k])
        d["tier"] = 2
        references.append(d)

    # 7. Markdown Assembly
    if not references_enabled:
        return {
            "enriched_text": cleaned_text,
            "citations": citations,
            "references": [],
            "cited_chunk_ids": cited_chunk_ids,
        }

    # Strip any trailing LLM-hallucinated reference headers to avoid duplication
    cleaned_text = re.sub(
        r"\n{1,3}#{1,3}\s*(?:Cited Sources|References|Sources consulted)[\s\S]*$",
        "",
        cleaned_text,
        flags=re.IGNORECASE,
    ).rstrip()

    sections: list[str] = [cleaned_text]

    # Tier 1 Markdown
    if tier1_keys:
        t1_lines = ["# Cited Sources"]
        for k in tier1_keys:
            d = docs_by_key[k]
            meta, rest = _format_doc_citation(d)
            prefix = f"{meta}. " if meta else ""
            link = f'<span class="zotero-source-link" data-item-key="{k}">{prefix}{rest}</span>'
            t1_lines.append(f"- {link}")
        sections.append("\n".join(t1_lines))

    # Tier 2 Markdown
    if tier2_keys and considered_disclosure:
        cnt = len(tier2_keys)
        plural = "source" if cnt == 1 else "sources"
        t2_lines = [
            "## Other Considered Sources",
            '<details class="considered-sources">',
            f"  <summary>{cnt} additional {plural} analyzed by search agent</summary>",
            "  <ul>",
        ]
        for k in tier2_keys:
            d = docs_by_key[k]
            auth = d.get("authors")
            yr = d.get("year")
            title = d.get("title") or "Untitled Document"
            pub = d.get("publication")

            if auth and yr:
                meta = f"<strong>{auth} ({yr})</strong>"
            elif auth:
                meta = f"<strong>{auth}</strong>"
            elif yr:
                meta = f"<strong>({yr})</strong>"
            else:
                meta = ""

            prefix = f"{meta}. " if meta else ""
            pub_str = f". {pub}" if pub else ""
            link = f'<span class="zotero-source-link" data-item-key="{k}">{prefix}<em>{title}</em>{pub_str}.</span>'
            t2_lines.append(f"    <li>{link}</li>")
        t2_lines.append("  </ul>")
        t2_lines.append("</details>")
        sections.append("\n".join(t2_lines))

    enriched_text = "\n\n".join(sections)

    return {
        "enriched_text": enriched_text,
        "citations": citations,
        "references": references,
        "cited_chunk_ids": cited_chunk_ids,
    }


def build_coordinator_view(
    text: str,
    citations: list[dict[str, Any]],
    references: list[dict[str, Any]],
) -> str:
    """Build a clean, human-readable Markdown representation for the coordinator agent.

    Substitutes in-text numeric chunk citation tokens ([1], [1, 2], etc.) with
    natural parenthetical author-year citations, masks code blocks, strips
    trailing HTML/reference blocks, and appends a clean Markdown two-tier
    bibliography without HTML tags or internal item keys.

    Args:
        text: Response text (may contain numeric citation tokens or existing sources).
        citations: List of citation dicts containing metadata (index, authors, year, title).
        references: List of reference dicts containing document metadata (tier, authors, year, title, publication).

    Returns:
        Clean Markdown string with parenthetical citations and clean Markdown bibliography.
    """
    clean_text = text or ""
    # a. Strip any trailing reference headers/sections from text
    clean_text = re.sub(
        r"(?:\n+|^\s*)<div\s+class=[\"']zocrates-sources-section[\"'][\s\S]*$",
        "",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(
        r"(?:\n{1,3}|^\s*)#{1,3}\s*(?:Cited Sources|References|Sources consulted)[\s\S]*$",
        "",
        clean_text,
        flags=re.IGNORECASE,
    ).rstrip()

    # b. Protect fenced code blocks (```...``` or ~~~...~~~) and inline code (`...`)
    code_placeholders: list[str] = []

    def _mask_code(m: re.Match) -> str:
        """Replace code blocks with unique placeholders during academic citation formatting.

        Args:
            m: Regular expression match object for the matched code block.

        Returns:
            Placeholder string representing the masked code.
        """
        idx = len(code_placeholders)
        code_placeholders.append(m.group(0))
        return f"@@ZOCRATES_CODE_MASK_{idx}@@"

    masked_text = _CODE_BLOCK_RE.sub(_mask_code, clean_text)

    # c. Substitute in-text numeric chunk citation tokens
    citations_list = citations or []

    def _replace_citation_token(m: re.Match) -> str:
        """Replace numeric citation tokens with formatted author-year citations.

        Args:
            m: Regular expression match object containing numeric citation tokens.

        Returns:
            Formatted parenthetical author-year citation string.
        """
        raw_content = m.group(1) if m.group(1) is not None else m.group(2)
        if not raw_content:
            return ""
        parts = [p.strip() for p in re.split(r"[,;]", raw_content) if p.strip()]
        indices: list[int] = []
        for p in parts:
            if p.isdigit():
                indices.append(int(p))

        labels: list[str] = []
        for idx in indices:
            c = None
            for item in citations_list:
                c_idx = item.get("index")
                if c_idx is not None and (c_idx == idx or str(c_idx) == str(idx)):
                    c = item
                    break
            if not c:
                continue

            authors = _format_authors_clean(c.get("authors")) if c.get("authors") else ""
            year = _format_year_clean(c.get("year")) if c.get("year") else ""
            title = str(c.get("title") or "").strip()

            if authors and year:
                lbl = f"{authors}, {year}"
            elif authors:
                lbl = authors
            elif year:
                lbl = str(year)
            elif title:
                lbl = title
            else:
                lbl = ""

            if lbl and lbl not in labels:
                labels.append(lbl)

        if labels:
            joined = "; ".join(labels)
            return f"({joined})"
        return ""

    masked_text = _IN_TEXT_CITATION_TOKEN_RE.sub(_replace_citation_token, masked_text)
    masked_text = re.sub(r"[ \t]+([.,;:!?])", r"\1", masked_text)
    masked_text = re.sub(r"[ \t]{2,}", " ", masked_text)

    # d. Restore masked code blocks
    for i, code_str in enumerate(code_placeholders):
        masked_text = masked_text.replace(f"@@ZOCRATES_CODE_MASK_{i}@@", code_str)

    # e. If references is non-empty, assemble clean Markdown bibliography
    t1_lines: list[str] = []
    t2_lines: list[str] = []

    if references:
        tier1_refs: list[dict[str, Any]] = []
        tier2_refs: list[dict[str, Any]] = []

        for ref in references:
            t = ref.get("tier")
            if t == 2 or t == "2":
                tier2_refs.append(ref)
            else:
                tier1_refs.append(ref)

        if tier1_refs:
            t1_lines.append("# Cited Sources")
            for ref in tier1_refs:
                meta, rest = _format_doc_citation(ref)
                entry = f"{meta}. {rest}" if meta else rest
                t1_lines.append(f"- {entry}")

        if tier2_refs:
            t2_lines.append("## Other Considered Sources")
            for ref in tier2_refs:
                meta, rest = _format_doc_citation(ref)
                entry = f"{meta}. {rest}" if meta else rest
                t2_lines.append(f"- {entry}")

    sections: list[str] = []
    if masked_text.strip():
        sections.append(masked_text.strip())
    if t1_lines:
        sections.append("\n".join(t1_lines))
    if t2_lines:
        sections.append("\n".join(t2_lines))

    # f. Return resulting string
    return "\n\n".join(sections)

