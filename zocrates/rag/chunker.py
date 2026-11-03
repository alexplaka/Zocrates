"""
Chunking utilities for Markdown documents.

Strategy:
  1. Split by Markdown headers (# → ## → ###) to create logical section chunks.
  2. If a section exceeds CHUNK_MAX_CHARS, recursively split by paragraph,
     then by sentence, until all sub-chunks are within the size limit.

Each chunk is returned as a ChunkResult with the section heading path,
character offsets, and the source document title.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from zocrates.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS
from zocrates.rag.line_indexer import LineIndex


@dataclass
class ChunkResult:
    """A single text chunk produced from a Markdown document."""

    text: str
    heading_path: list[str]  # e.g. ["Introduction", "Background"]
    chunk_index: int  # position within this document's chunk list
    document_title: str = ""  # The Zotero item title of the source document
    chunk_type: str = "text"  # "frontmatter" | "figure" | "table" | "equation" | "code" | "text"
    image_path: str | None = None
    caption_source: str | None = None
    vlm_model: str | None = None
    line_start: int = 1
    line_end: int = 1

    @property
    def heading_str(self) -> str:
        """Return the heading path as a ' > '-delimited breadcrumb string."""
        return " > ".join(self.heading_path) if self.heading_path else ""


# ---------------------------------------------------------------------------
# Header-based splitter and block maskers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~)", re.MULTILINE)
_FIGURE_RE = re.compile(r"(<figure\b[^>]*>[\s\S]*?</figure>)", re.IGNORECASE | re.MULTILINE)
_SYNTH_CAPTION_RE = re.compile(r"<synthetic_caption\b([^>]*)>([\s\S]*?)</synthetic_caption>", re.IGNORECASE)
_MD_IMG_RE = re.compile(r"!\[(.*?)\]\((.*?)(?:\s+[\"'].*?[\"'])?\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)


def _normalize_image_path(raw_path: str) -> str:
    """Normalize local file URIs, relative paths, or filenames to standard relative paths."""
    if not raw_path:
        return ""
    clean = raw_path.strip()
    if clean.startswith("file://"):
        parsed = urlparse(clean)
        path_str = unquote(parsed.path)
        if "images/" in path_str:
            idx = path_str.find("images/")
            return path_str[idx:]
        return f"images/{Path(path_str).name}"
    if "images/" in clean:
        idx = clean.find("images/")
        return clean[idx:]
    return clean


def _mask_code_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Replace fenced code blocks with opaque placeholders.

    Prevents ``#``-prefixed lines inside code fences from being matched by
    the ATX header regex during splitting.

    Args:
        text: Raw Markdown string that may contain fenced code blocks.

    Returns:
        A 2-tuple of ``(masked_text, masks)`` where ``masked_text`` has each
        fenced block replaced by a unique ``__CODE_BLOCK_N__`` token, and
        ``masks`` maps each token back to the original block content.
    """
    masks = {}

    def replacer(m: re.Match) -> str:
        """Replace matched code fence with placeholder token.

        Args:
            m: Regex match object representing the code fence.

        Returns:
            Placeholder string token.
        """
        key = f"__CODE_BLOCK_{len(masks)}__"
        masks[key] = m.group(0)
        return key

    masked = _FENCE_RE.sub(replacer, text)
    return masked, masks


def _unmask_code_blocks(text: str, masks: dict[str, str]) -> str:
    """Restore fenced code blocks from their placeholder tokens.

    Args:
        text: Text containing ``__CODE_BLOCK_N__`` placeholder tokens.
        masks: Mapping returned by ``_mask_code_blocks`` that associates each
            token with its original code block content.

    Returns:
        Text with all placeholder tokens replaced by their original content.
    """
    for key, original in masks.items():
        text = text.replace(key, original)
    return text


def _mask_semantic_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Replace <figure>...</figure> blocks with opaque placeholders.

    Prevents figure/table contents, newlines, and headers from being fragmented across chunks.

    Args:
        text: Raw Markdown string containing semantic <figure> blocks.

    Returns:
        A 2-tuple of ``(masked_text, masks)`` with placeholders.
    """
    masks = {}

    def replacer(m: re.Match) -> str:
        """Replace matched semantic figure block with placeholder token.

        Args:
            m: Regex match object representing the figure block.

        Returns:
            Placeholder string token.
        """
        key = f"__SEMANTIC_FIGURE_{len(masks)}__"
        masks[key] = m.group(0)
        return key

    masked = _FIGURE_RE.sub(replacer, text)
    return masked, masks


def _unmask_semantic_blocks(text: str, masks: dict[str, str]) -> str:
    """Restore <figure>...</figure> blocks from their placeholder tokens.

    Args:
        text: Text containing ``__SEMANTIC_FIGURE_N__`` placeholder tokens.
        masks: Mapping returned by ``_mask_semantic_blocks``.

    Returns:
        Text with all placeholder tokens replaced by original content.
    """
    for key, original in masks.items():
        text = text.replace(key, original)
    return text


def _mask_all_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Mask both code fences and semantic figure blocks.

    Args:
        text: Input Markdown text.

    Returns:
        A 2-tuple of ``(masked_text, combined_masks)``.
    """
    code_masked, code_masks = _mask_code_blocks(text)
    fig_masked, fig_masks = _mask_semantic_blocks(code_masked)
    return fig_masked, {**code_masks, **fig_masks}


def _unmask_all_blocks(text: str, masks: dict[str, str]) -> str:
    """Unmask both code fences and semantic figure blocks.

    Args:
        text: Masked Markdown text.
        masks: Combined dictionary of placeholders and original text.

    Returns:
        Unmasked Markdown text.
    """
    # Unmask in reverse of masking order (semantic figures first, then code fences)
    for key, original in reversed(list(masks.items())):
        text = text.replace(key, original)
    return text


# Matches Markdown ATX headers: # H1, ## H2, ### H3 (we go down to level 3)
_HEADER_RE = re.compile(r"^(#{1,3})\s+(.+)$", re.MULTILINE)


def _split_by_headers(text: str) -> list[tuple[list[str], str]]:
    """
    Split Markdown text into sections by ATX headers up to level 3.

    Masks code and semantic blocks before scanning so that ``#``-prefixed lines inside
    fences are not treated as section boundaries. Preamble text before the
    first header is included as a section with an empty heading path.

    Args:
        text: Raw Markdown string to split.

    Returns:
        List of ``(heading_path, content)`` pairs where ``heading_path`` is
        an ordered list of ancestor header titles and ``content`` is the
        stripped body text for that section.
    """
    masked_text, masks = _mask_all_blocks(text)
    matches = list(_HEADER_RE.finditer(masked_text))
    if not matches:
        return [([], text.strip())]

    sections: list[tuple[list[str], str]] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title)

    for i, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()

        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(masked_text)
        body = masked_text[start:end].strip()

        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))

        path = [h for _, h in heading_stack]
        if body:
            sections.append((path, _unmask_all_blocks(body, masks)))

    # Preamble before the first header
    preamble = masked_text[: matches[0].start()].strip()
    if preamble:
        sections.insert(0, ([], _unmask_all_blocks(preamble, masks)))

    return sections


# ---------------------------------------------------------------------------
# Recursive character splitter (fallback for oversized sections)
# ---------------------------------------------------------------------------

_PARAGRAPH_SEP = re.compile(r"\n{2,}")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


def _recursive_split(
    text: str, max_chars: int, overlap: int, masks: dict[str, str] | None = None
) -> list[str]:
    """
    Recursively split text that is too large for a single chunk.

    Applies a waterfall of splitting strategies: paragraph boundaries first
    (``\n\n``), then sentence boundaries (``[.!?] + whitespace``), then hard
    character slices as a last resort.

    Preserves code blocks and semantic <figure> blocks as indivisible units.

    Args:
        text: Input text to split.
        max_chars: Maximum character count per output chunk.
        overlap: Number of characters repeated at the start of each subsequent
            chunk to preserve cross-boundary context.
        masks: Optional dict mapping placeholder tokens to original block content.

    Returns:
        List of text chunks, each at most ``max_chars`` characters long.

    Raises:
        ValueError: If ``overlap`` is greater than or equal to ``max_chars``.
    """
    if overlap >= max_chars:
        raise ValueError(f"overlap ({overlap}) must be less than max_chars ({max_chars})")

    if masks is None:
        masked_text, combined_masks = _mask_all_blocks(text)
    else:
        masked_text = text
        combined_masks = masks

    effective_len = _get_effective_len(masked_text, combined_masks)
    if effective_len <= max_chars and not combined_masks:
        return [text]
    if effective_len <= max_chars:
        return [_unmask_all_blocks(masked_text, combined_masks)]

    paragraphs = [p.strip() for p in _PARAGRAPH_SEP.split(masked_text) if p.strip()]
    if len(paragraphs) > 1:
        merged = _merge_splits(paragraphs, max_chars, overlap, "\n\n", combined_masks)
        return [_unmask_all_blocks(c, combined_masks) for c in merged]

    sentences = [s.strip() for s in _SENTENCE_END.split(masked_text) if s.strip()]
    if len(sentences) > 1:
        merged = _merge_splits(sentences, max_chars, overlap, " ", combined_masks)
        return [_unmask_all_blocks(c, combined_masks) for c in merged]

    chunks: list[str] = []
    start = 0
    while start < len(masked_text):
        end = start + max_chars
        chunk_slice = masked_text[start:end]
        chunks.append(_unmask_all_blocks(chunk_slice, combined_masks))
        start = end - overlap if overlap else end
    return chunks


def _get_effective_len(text: str, masks: dict[str, str] | None) -> int:
    """Calculate the real character count of a text fragment including unmasked content."""
    if not masks:
        return len(text)
    length = len(text)
    for k, v in masks.items():
        if k in text:
            length = length - len(k) + len(v)
    return length


def _merge_splits(
        parts: list[str],
        max_chars: int,
        overlap: int,
        sep: str,
        masks: dict[str, str] | None = None,
) -> list[str]:
    """
    Greedily merge a list of text fragments into chunks up to max_chars.

    Accumulates parts until the next part would exceed the limit, then emits
    the current chunk and carries forward an overlap window of trailing parts.

    Args:
        parts: Pre-split text fragments to merge.
        max_chars: Maximum character count per merged chunk.
        overlap: Overlap character budget carried between consecutive chunks.
        sep: Separator string used when joining parts (e.g. ``"\\n\\n"`` or ``" "``).
        masks: Optional dict mapping placeholder tokens to original block content.

    Returns:
        List of merged text chunks.
    """
    result: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for part in parts:
        part_len = _get_effective_len(part, masks)
        sep_len = len(sep) if current_parts else 0

        # If a single part exceeds max_chars
        if part_len > max_chars:
            # If it's a masked atomic block (e.g. __SEMANTIC_FIGURE_N__ or __CODE_BLOCK_N__),
            # keep it intact as its own standalone chunk instead of slicing it
            if masks and any(part == k for k in masks):
                if current_parts:
                    result.append(sep.join(current_parts))
                    current_parts = []
                    current_len = 0
                result.append(part)
                continue

            if current_parts:
                result.append(sep.join(current_parts))
                current_parts = []
                current_len = 0
            result.extend(_recursive_split(part, max_chars, overlap, masks=masks))
            continue

        if current_len + sep_len + part_len > max_chars and current_parts:
            result.append(sep.join(current_parts))
            overlap_parts: list[str] = []
            overlap_len = 0
            for prev in reversed(current_parts):
                prev_len = _get_effective_len(prev, masks)
                if overlap_len + len(sep) + prev_len <= overlap:
                    overlap_parts.insert(0, prev)
                    overlap_len += len(sep) + prev_len
                else:
                    break
            current_parts = overlap_parts
            current_len = sum(_get_effective_len(p, masks) for p in current_parts) + (
                len(sep) * (len(current_parts) - 1) if current_parts else 0
            )

        current_parts.append(part)
        current_len += sep_len + part_len

    if current_parts:
        result.append(sep.join(current_parts))

    return result


# ---------------------------------------------------------------------------
# Public API & Chunk Type Classification
# ---------------------------------------------------------------------------


_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)")


def _extract_leading_frontmatter(markdown: str) -> tuple[str | None, str]:
    """Extract leading YAML frontmatter block if present.

    Returns:
        A tuple of (frontmatter_block, remaining_markdown).
        If no frontmatter is found, returns (None, markdown).
    """
    m = _FRONTMATTER_RE.match(markdown)
    if m:
        fm_block = f"---\n{m.group(1).strip()}\n---"
        remaining = markdown[m.end() :].lstrip("\r\n")
        return fm_block, remaining
    return None, markdown


def determine_chunk_type(chunk_text: str) -> str:
    """Determine the semantic classification of a text chunk.

    Taxonomy:
      - "frontmatter": YAML frontmatter block (---...---)
      - "table": Tabular data (<figure data-type="table"> or markdown table)
      - "figure": Images and captions (<figure data-type="image"> or standalone `![alt](url)`)
      - "code": Fenced code blocks (```...``` or ~~~...~~~)
      - "equation": Math derivations with display LaTeX ($$...$$, \\[...\\], \\begin{equation})
      - "text": Default prose/narrative paragraphs

    Priority:
      1. YAML frontmatter block (closed --- delimiters)
      2. Explicit semantic wrapper tags (<figure data-type="table|image">)
      3. Whole-block code fences (protects code examples containing table/image syntax)
      4. GFM markdown table syntax (delimiter row: |---|)
      5. Standalone markdown image syntax (`![alt](url)`)
      6. Display LaTeX equations ($$...$$, \\[...\\], \\begin{...})
      7. Default prose narrative

    Args:
        chunk_text: Text content of the chunk.

    Returns:
        One of the six canonical string literals.
    """
    stripped = chunk_text.strip()
    if not stripped:
        return "text"

    # 1. YAML frontmatter block
    if (stripped.startswith("---\n") or stripped.startswith("---\r\n")) and _FRONTMATTER_RE.match(stripped):
        return "frontmatter"

    # 2. Explicit semantic wrapper tags
    if '<figure data-type="table"' in chunk_text or "<figure data-type='table'" in chunk_text:
        return "table"
    if '<figure data-type="image"' in chunk_text or "<figure data-type='image'" in chunk_text:
        return "figure"

    # 2. Whole-block code fences
    if (stripped.startswith("```") and stripped.endswith("```")) or (
        stripped.startswith("~~~") and stripped.endswith("~~~")
    ):
        return "code"

    # 3. GFM markdown table detection (delimiter row required: |---| or |:---| or | --- |)
    if (
        "\n|---" in chunk_text
        or "\n|:--" in chunk_text
        or "\n| ---" in chunk_text
        or "\n|  ---" in chunk_text
        or "\n| :--" in chunk_text
    ):
        return "table"
    if stripped.startswith("|") and (
        "|---" in stripped or "|:--" in stripped or "| ---" in stripped or "| :--" in stripped
    ):
        return "table"

    # 4. Standalone markdown image detection
    if stripped.startswith("![") and "](" in stripped:
        return "figure"

    # 5. Math equation detection (display LaTeX equations)
    if (
        "$$" in chunk_text
        or r"\[" in chunk_text
        or r"\begin{equation}" in chunk_text
        or r"\begin{align}" in chunk_text
        or r"\begin{gather}" in chunk_text
        or r"\begin{aligned}" in chunk_text
    ):
        return "equation"

    # 6. Default prose
    return "text"


@dataclass
class _ExtractedFigure:
    """Extracted figure or table metadata and line boundaries within markdown."""

    anchor: int
    text: str
    chunk_type: str
    image_path: str | None = None
    caption_source: str | None = None
    vlm_model: str | None = None
    line_start: int = 1
    line_end: int = 1


def chunk_markdown(
    markdown: str,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
    document_title: str = "",
) -> list[ChunkResult]:
    """
    Chunk a Markdown document into semantically coherent pieces.

    Splits by ATX headers (H1–H3) first, isolates figures and tables as
    standalone atomic chunks with outer <figure> tags stripped, stitches
    interrupted narrative prose across figure boundaries, and recursively
    splits oversized narrative prose by paragraph and sentence up to ``max_chars``.

    Preserves document order via anchor interleaving.

    Args:
        markdown: Raw Markdown string to chunk.
        max_chars: Maximum character count per chunk. Defaults to
            ``CHUNK_MAX_CHARS`` from config.
        overlap_chars: Characters repeated at chunk boundaries for context
            continuity. Defaults to ``CHUNK_OVERLAP_CHARS`` from config.
        document_title: Zotero item title attached to every ``ChunkResult``.

    Returns:
        Ordered list of ``ChunkResult`` objects, one per non-empty sub-chunk.
    """
    # 0. Build line index on verbatim raw markdown before any masking or splitting
    line_index = LineIndex(markdown)

    fm_block, remaining_body = _extract_leading_frontmatter(markdown)
    chunks: list[ChunkResult] = []
    idx = 0

    if fm_block:
        fm_match = _FRONTMATTER_RE.match(markdown)
        if fm_match:
            fm_raw = fm_match.group(0)
            fm_end_line = len(fm_raw.rstrip("\r\n").splitlines())
        else:
            fm_end_line = len(fm_block.rstrip("\r\n").splitlines())

        chunks.append(
            ChunkResult(
                text=fm_block,
                heading_path=[],
                chunk_index=idx,
                document_title=document_title,
                chunk_type="frontmatter",
                line_start=1,
                line_end=max(1, fm_end_line),
            )
        )
        idx += 1

    sections = _split_by_headers(remaining_body)
    doc_search_cursor = 0

    for heading_path, body in sections:
        if not body.strip():
            continue

        # 1. Mask fenced code blocks before scanning for figures
        code_masked_body, code_masks = _mask_code_blocks(body)

        # 2. Extract all <figure> blocks and record their anchor positions
        extracted_figures: list[_ExtractedFigure] = []
        for match in _FIGURE_RE.finditer(code_masked_body):
            raw_fig_tag = match.group(0)
            anchor_pos = match.start()

            # Unmask any code placeholders inside figure if present
            unmasked_fig_tag = _unmask_code_blocks(raw_fig_tag, code_masks)
            orig_verbatim_fig_tag = unmasked_fig_tag

            # Determine type from attributes
            if re.search(r'data-type=["\']table["\']', unmasked_fig_tag, re.IGNORECASE):
                fig_type = "table"
            else:
                fig_type = "figure"

            # Extract image path if present
            image_path = None
            if fig_type == "figure":
                md_img = _MD_IMG_RE.search(unmasked_fig_tag)
                html_img = _HTML_IMG_RE.search(unmasked_fig_tag)
                if md_img:
                    image_path = _normalize_image_path(md_img.group(2))
                elif html_img:
                    image_path = _normalize_image_path(html_img.group(1))

            # Extract synthetic caption and determine caption source & model
            caption_source = None
            vlm_model = None
            synth_match = _SYNTH_CAPTION_RE.search(unmasked_fig_tag)
            if synth_match:
                attrs = synth_match.group(1)
                synth_text = synth_match.group(2).strip()

                src_attr = re.search(r'data-source=[\"\']([^\"\']+)[\"\']', attrs, re.IGNORECASE)
                if src_attr:
                    caption_source = src_attr.group(1)
                else:
                    if any(cue in synth_text for cue in ("**Type**:", "Type:", "**Key Elements**:", "**Takeaway**:", "Takeaway:")):
                        caption_source = "vlm"
                    else:
                        caption_source = "datalab"

                model_attr = re.search(r'data-model=[\"\']([^\"\']+)[\"\']', attrs, re.IGNORECASE)
                if model_attr:
                    vlm_model = model_attr.group(1)

                # Format synthetic caption into clean Markdown [Visual Summary]
                unmasked_fig_tag = _SYNTH_CAPTION_RE.sub(
                    lambda m: f"\n\n[Visual Summary]:\n{m.group(2).strip()}", unmasked_fig_tag
                )

            # Determine figure start/end line in the source markdown using the verbatim tag
            fig_start_char = markdown.find(orig_verbatim_fig_tag, doc_search_cursor)
            if fig_start_char == -1:
                fig_start_char = markdown.find(orig_verbatim_fig_tag)
            if fig_start_char != -1:
                fig_end_char = fig_start_char + len(orig_verbatim_fig_tag)
                fig_l_start, fig_l_end = line_index.span_to_lines(fig_start_char, fig_end_char)
            else:
                fig_l_start = 1
                fig_l_end = 1
            fig_l_end = max(fig_l_start, fig_l_end)

            # Strip all HTML wrapper tags from the figure chunk text
            inner_content = re.sub(
                r"^<figure\b[^>]*>", "", unmasked_fig_tag, flags=re.IGNORECASE
            ).strip()
            inner_content = re.sub(
                r"</figure>$", "", inner_content, flags=re.IGNORECASE
            ).strip()
            inner_content = re.sub(
                r"</?figcaption\b[^>]*>", "", inner_content, flags=re.IGNORECASE
            ).strip()
            inner_content = re.sub(
                r"</?synthetic_caption\b[^>]*>", "", inner_content, flags=re.IGNORECASE
            ).strip()

            if inner_content:
                clean_inner = re.sub(r"\n{3,}", "\n\n", inner_content).strip()
                extracted_figures.append(
                    _ExtractedFigure(
                        anchor=anchor_pos,
                        text=clean_inner,
                        chunk_type=fig_type,
                        image_path=image_path if fig_type == "figure" else None,
                        caption_source=caption_source if fig_type == "figure" else None,
                        vlm_model=vlm_model if fig_type == "figure" else None,
                        line_start=fig_l_start,
                        line_end=fig_l_end,
                    )
                )

        # 3. Stitch narrative prose by replacing figures with empty string and collapsing redundant blank lines
        stitched_masked_body = _FIGURE_RE.sub("", code_masked_body)
        stitched_masked_body = re.sub(r"\n{3,}", "\n\n", stitched_masked_body)
        stitched_body = _unmask_code_blocks(stitched_masked_body, code_masks).strip()

        # 4. Split stitched narrative prose into size-bounded sub-chunks
        prose_sub_chunks: list[str] = []
        if stitched_body:
            prose_sub_chunks = _recursive_split(stitched_body, max_chars, overlap_chars)

        # 5. Interleave prose chunks and figure chunks to preserve document flow
        pending_figs = list(extracted_figures)
        search_cursor = 0
        is_first_prose = True

        for sub in prose_sub_chunks:
            sub_str = re.sub(r"\n{3,}", "\n\n", sub.strip()).strip()
            if not sub_str:
                continue

            # Locate position of this prose chunk in code_masked_body
            non_empty_lines = [l.strip() for l in sub_str.splitlines() if l.strip()]
            first_line = non_empty_lines[0][:40] if non_empty_lines else ""
            last_line = non_empty_lines[-1][-40:] if non_empty_lines else ""

            prose_start = (
                code_masked_body.find(first_line, search_cursor)
                if first_line
                else search_cursor
            )
            if prose_start == -1:
                prose_start = search_cursor
            else:
                search_cursor = prose_start

            prose_end = (
                code_masked_body.find(last_line, search_cursor)
                if last_line
                else search_cursor
            )
            if prose_end == -1:
                prose_end = prose_start + len(sub_str)
            else:
                prose_end += len(last_line)
                search_cursor = prose_end

            # Map prose start/end lines in verbatim markdown
            p_start = (
                markdown.find(first_line, doc_search_cursor)
                if first_line
                else doc_search_cursor
            )
            if p_start == -1 and first_line:
                p_start = markdown.find(first_line)
            if p_start == -1:
                p_start = doc_search_cursor

            p_end = (
                markdown.find(last_line, p_start)
                if last_line
                else p_start
            )
            if p_end == -1 and last_line:
                p_end = markdown.find(last_line)
            if p_end == -1:
                p_end = p_start + len(sub_str)
            else:
                p_end = p_end + len(last_line)

            p_line_start, p_line_end = line_index.span_to_lines(p_start, p_end)

            # If this is the first prose chunk under an ATX heading, encompass the heading line
            if is_first_prose and heading_path:
                h_matches = list(
                    re.finditer(
                        r"^(#{1,6})\s+" + re.escape(heading_path[-1]),
                        markdown[:p_start],
                        re.MULTILINE,
                    )
                )
                if h_matches:
                    h_line = line_index.char_to_line(h_matches[-1].start())
                    p_line_start = min(p_line_start, h_line)
                is_first_prose = False

            p_line_end = max(p_line_start, p_line_end)
            doc_search_cursor = max(doc_search_cursor, p_start + 1)

            # Emit figures that appeared before or at the start of this prose chunk
            while pending_figs and pending_figs[0].anchor <= prose_start:
                fig = pending_figs.pop(0)
                chunks.append(
                    ChunkResult(
                        text=fig.text,
                        heading_path=heading_path,
                        chunk_index=idx,
                        document_title=document_title,
                        chunk_type=fig.chunk_type,
                        image_path=fig.image_path,
                        caption_source=fig.caption_source,
                        vlm_model=fig.vlm_model,
                        line_start=fig.line_start,
                        line_end=fig.line_end,
                    )
                )
                idx += 1

            # Emit the prose chunk
            prose_type = determine_chunk_type(sub_str)
            prose_img_path = None
            prose_cap_source = None
            if prose_type == "figure":
                md_img = _MD_IMG_RE.search(sub_str)
                html_img = _HTML_IMG_RE.search(sub_str)
                if md_img:
                    prose_img_path = _normalize_image_path(md_img.group(2))
                elif html_img:
                    prose_img_path = _normalize_image_path(html_img.group(1))

            chunks.append(
                ChunkResult(
                    text=sub_str,
                    heading_path=heading_path,
                    chunk_index=idx,
                    document_title=document_title,
                    chunk_type=prose_type,
                    image_path=prose_img_path,
                    caption_source=prose_cap_source,
                    line_start=p_line_start,
                    line_end=p_line_end,
                )
            )
            idx += 1

            # Emit figures that appeared within this prose chunk span
            while pending_figs and pending_figs[0].anchor <= prose_end:
                fig = pending_figs.pop(0)
                chunks.append(
                    ChunkResult(
                        text=fig.text,
                        heading_path=heading_path,
                        chunk_index=idx,
                        document_title=document_title,
                        chunk_type=fig.chunk_type,
                        image_path=fig.image_path,
                        caption_source=fig.caption_source,
                        vlm_model=fig.vlm_model,
                        line_start=fig.line_start,
                        line_end=fig.line_end,
                    )
                )
                idx += 1

        # Emit any trailing figures in the section
        while pending_figs:
            fig = pending_figs.pop(0)
            chunks.append(
                ChunkResult(
                    text=fig.text,
                    heading_path=heading_path,
                    chunk_index=idx,
                    document_title=document_title,
                    chunk_type=fig.chunk_type,
                    image_path=fig.image_path,
                    caption_source=fig.caption_source,
                    vlm_model=fig.vlm_model,
                    line_start=fig.line_start,
                    line_end=fig.line_end,
                )
            )
            idx += 1

    return chunks
