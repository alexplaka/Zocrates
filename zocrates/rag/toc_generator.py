"""
OKF Document Bundle Table of Contents (ToC) and Asset Catalog Generator.

Extracts structured sections, figures, tables, and abstract/summary metadata
from converted Markdown documents and produces dual-format artifacts:
  - `toc.json` (Source of truth machine catalog)
  - `toc.md`   (Agent-readable overview)
"""

import html
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from zocrates.rag.chunker import _FRONTMATTER_RE
from zocrates.rag.line_indexer import LineIndex, char_offset_to_line

logger = logging.getLogger(__name__)

# ATX Heading Regex (matches # up to ######)
_HEADING_LINE_RE = re.compile(r"^( {0,3})(#{1,6})\s+(.+?)(?:\s+#+)?\s*$")

# Formatting Strippers
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_CODE_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_ASTERISK_RE = re.compile(r"\*\*([^*]+)\*\*")
_MD_BOLD_UNDERSCORE_RE = re.compile(r"(?<!\w)__([^_]+)__(?!\w)")
_MD_ITALIC_ASTERISK_RE = re.compile(r"\*([^*]+)\*")
_MD_ITALIC_UNDERSCORE_RE = re.compile(r"(?<!\w)_([^_]+)_(?!\w)")

# Figure & Table Regexes
_FIGURE_BLOCK_RE = re.compile(
    r"<figure\b([^>]*)>([\s\S]*?)</figure>", re.IGNORECASE | re.MULTILINE
)
_STANDALONE_IMG_RE = re.compile(
    r"!\[([^\]]*)\]\((images\/[^\s\)\"']+)(?:\s+[\"'].*?[\"'])?\)"
)
_SYNTH_CAPTION_RE = re.compile(
    r"<synthetic_caption\b[^>]*>([\s\S]*?)</synthetic_caption>", re.IGNORECASE
)
_FIGURE_CAPTION_LINE_RE = re.compile(
    r"^(?: {0,3})(?:\*\*|__)?(?:Figure|Fig\.?)\s*(\d+[^:]*[\.:]|\b)(?:\*\*|__)?\s*([^\n]+)$",
    re.IGNORECASE,
)
_TABLE_CAPTION_LINE_RE = re.compile(
    r"^(?: {0,3})(?:\*\*|__)?(?:Table|Tab\.?)\s*(\d+[^:]*[\.:]|\b)(?:\*\*|__)?\s*([^\n]+)$",
    re.IGNORECASE,
)
_GFM_TABLE_RE = re.compile(
    r"(?:^|\n)( {0,3}\|[^\n]+\|\s*\n {0,3}\|(?:\s*:?-+:?\s*\|)+\s*(?:\n {0,3}\|[^\n]+\|\s*)+)",
    re.MULTILINE,
)

MAX_CAPTION_CHARS = 100


def _clean_text_inline(text: str) -> str:
    """Strip markdown formatting, inline code, links, and HTML entities from a single line."""
    if not text:
        return ""
    cleaned = _MD_LINK_RE.sub(r"\1", text)
    cleaned = _MD_CODE_RE.sub(r"\1", cleaned)
    cleaned = _MD_BOLD_ASTERISK_RE.sub(r"\1", cleaned)
    cleaned = _MD_BOLD_UNDERSCORE_RE.sub(r"\1", cleaned)
    cleaned = _MD_ITALIC_ASTERISK_RE.sub(r"\1", cleaned)
    cleaned = _MD_ITALIC_UNDERSCORE_RE.sub(r"\1", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _truncate_caption(caption: str, max_chars: int = MAX_CAPTION_CHARS) -> str:
    """Truncate caption string to max_chars with clean ellipsis."""
    clean = _clean_text_inline(caption)
    if not clean:
        return ""
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."


def generate_toc(
    document_md: str,
    title: str | None = None,
    abstract: str | None = None,
    abstract_source: str = "zotero",
    item_key: str = "",
    parent_item_key: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Generate dual-format Table of Contents from a converted Markdown document.

    Args:
        document_md: Full converted Markdown text (including YAML frontmatter).
        title: Document title.
        abstract: Abstract or summary text.
        abstract_source: Source of abstract ('zotero', 'generated', 'extractive').
        item_key: Zotero item key.
        parent_item_key: Parent Zotero item key if item is an attachment.

    Returns:
        A 2-tuple of `(toc_json_dict, toc_md_text)`.
    """
    line_index = LineIndex(document_md)
    lines = document_md.splitlines(keepends=True)
    if not lines:
        lines = [""]

    total_lines = line_index.total_lines
    total_words = len(document_md.split())

    # -----------------------------------------------------------------------
    # 1. Frontmatter Detection (s0)
    # -----------------------------------------------------------------------
    sections: list[dict[str, Any]] = []
    fm_match = _FRONTMATTER_RE.match(document_md)
    fm_end_line = 0

    if fm_match:
        fm_raw = fm_match.group(0)
        fm_end_line = len(fm_raw.rstrip("\r\n").splitlines())
        fm_content = fm_match.group(1)
        if parent_item_key is None:
            try:
                fm_data = yaml.safe_load(fm_content)
                if isinstance(fm_data, dict) and "parent_item_key" in fm_data:
                    parent_item_key = fm_data.get("parent_item_key")
            except Exception:
                pass
        sections.append(
            {
                "id": "s0",
                "heading": "Frontmatter",
                "level": 0,
                "start_line": 1,
                "end_line": fm_end_line,
                "word_count": len(fm_content.split()),
            }
        )

    # -----------------------------------------------------------------------
    # 2. Section Extraction (s1, s2, ...)
    # -----------------------------------------------------------------------
    # Mask code blocks during heading discovery
    in_code_fence = False
    fence_char = ""
    discovered_headings: list[dict[str, Any]] = []

    for line_idx, line in enumerate(lines, start=1):
        # Only parse headings after frontmatter
        if line_idx <= fm_end_line:
            continue

        stripped_line = line.strip()
        if not in_code_fence:
            if stripped_line.startswith(("```", "~~~")):
                in_code_fence = True
                fence_char = stripped_line[:3]
                continue
        else:
            if stripped_line.startswith(fence_char):
                in_code_fence = False
            continue

        h_match = _HEADING_LINE_RE.match(line)
        if h_match:
            hashes = h_match.group(2)
            level = len(hashes)
            raw_title = h_match.group(3)
            clean_title = _clean_text_inline(raw_title)
            if clean_title:
                discovered_headings.append(
                    {
                        "heading": clean_title,
                        "level": level,
                        "start_line": line_idx,
                    }
                )

    if discovered_headings:
        for idx, h in enumerate(discovered_headings):
            # Section IDs start at s1 if no frontmatter (s0 is reserved for Frontmatter)
            s_id = f"s{len(sections) if fm_match else len(sections) + 1}"
            start_line = h["start_line"]
            if idx + 1 < len(discovered_headings):
                end_line = discovered_headings[idx + 1]["start_line"] - 1
            else:
                end_line = total_lines

            # Calculate word count in this section's slice
            section_slice = "".join(lines[start_line - 1 : end_line])
            word_count = len(section_slice.split())

            sections.append(
                {
                    "id": s_id,
                    "heading": h["heading"],
                    "level": h["level"],
                    "start_line": start_line,
                    "end_line": max(start_line, end_line),
                    "word_count": word_count,
                }
            )
    else:
        # Fallback for documents with no ATX headings (only if there is non-frontmatter body)
        if not fm_match or fm_end_line < total_lines:
            body_start = fm_end_line + 1 if (fm_match and fm_end_line < total_lines) else 1
            body_slice = "".join(lines[body_start - 1 : total_lines])
            s_id = f"s{len(sections) if fm_match else len(sections) + 1}"
            sections.append(
                {
                    "id": s_id,
                    "heading": "Document Body",
                    "level": 1,
                    "start_line": body_start,
                    "end_line": total_lines,
                    "word_count": len(body_slice.split()),
                }
            )

    # -----------------------------------------------------------------------
    # 3. Figure Extraction (f1, f2, ...)
    # -----------------------------------------------------------------------
    figures: list[dict[str, Any]] = []
    figure_spans: list[tuple[int, int]] = []

    # 3a. Match semantic figure blocks
    for m in _FIGURE_BLOCK_RE.finditer(document_md):
        attrs = m.group(1)
        body = m.group(2)
        start_char, end_char = m.span()
        start_line, end_line = line_index.span_to_lines(start_char, end_char)

        # Check if figure is an image or table
        is_table = 'data-type="table"' in attrs.lower() or 'data-type=\'table\'' in attrs.lower()
        if is_table:
            continue

        figure_spans.append((start_char, end_char))

        # Extract image path
        path = ""
        img_match = _STANDALONE_IMG_RE.search(body)
        if img_match:
            path = img_match.group(2)
        else:
            raw_p_match = re.search(r'(?:src=["\']|images/)([^\s"\'\)]+)', body)
            if raw_p_match:
                p_candidate = raw_p_match.group(1)
                path = p_candidate if p_candidate.startswith("images/") else f"images/{Path(p_candidate).name}"

        # Resolve caption using hierarchy: author caption -> synthetic caption -> alt text -> fallback
        resolved_caption = ""
        for line in body.splitlines():
            line_str = line.strip()
            if line_str.startswith("!["):
                continue
            cap_line_match = _FIGURE_CAPTION_LINE_RE.match(line_str)
            if cap_line_match:
                label = cap_line_match.group(1).strip()
                rest = cap_line_match.group(2).strip()
                prefix = f"Figure {label}" if not label.lower().startswith("fig") else label
                resolved_caption = f"{prefix} {rest}".strip() if label else rest
                break

        if not resolved_caption:
            synth_match = _SYNTH_CAPTION_RE.search(body)
            if synth_match:
                resolved_caption = synth_match.group(1).strip()

        if not resolved_caption and img_match:
            alt = img_match.group(1).strip()
            # Ignore generic alt texts
            if alt and not re.match(r"^(?:figure|image|picture)(?:\s*\d+)?(?:\s*\(page\s*\d+\))?$", alt, re.IGNORECASE):
                resolved_caption = alt

        fig_id = f"f{len(figures) + 1}"
        if not resolved_caption:
            resolved_caption = f"Figure {len(figures) + 1}"

        figures.append(
            {
                "id": fig_id,
                "caption": _truncate_caption(resolved_caption),
                "path": path,
                "start_line": start_line,
                "end_line": max(start_line, end_line),
            }
        )

    # 3b. Match standalone markdown images outside <figure> blocks
    for m in _STANDALONE_IMG_RE.finditer(document_md):
        start_char, end_char = m.span()
        # Check if inside any already-matched figure block
        inside_figure = any(s <= start_char and end_char <= e for s, e in figure_spans)
        if inside_figure:
            continue

        start_line, end_line = line_index.span_to_lines(start_char, end_char)
        alt = m.group(1).strip()
        path = m.group(2).strip()

        fig_id = f"f{len(figures) + 1}"
        caption = alt if (alt and not re.match(r"^(?:figure|image|picture)(?:\s*\d+)?$", alt, re.IGNORECASE)) else f"Figure {len(figures) + 1}"

        figures.append(
            {
                "id": fig_id,
                "caption": _truncate_caption(caption),
                "path": path,
                "start_line": start_line,
                "end_line": max(start_line, end_line),
            }
        )

    # -----------------------------------------------------------------------
    # 4. Table Extraction (t1, t2, ...)
    # -----------------------------------------------------------------------
    tables: list[dict[str, Any]] = []
    table_spans: list[tuple[int, int]] = []

    # 4a. Match semantic table blocks
    for m in _FIGURE_BLOCK_RE.finditer(document_md):
        attrs = m.group(1)
        body = m.group(2)
        is_table = 'data-type="table"' in attrs.lower() or 'data-type=\'table\'' in attrs.lower()
        if not is_table:
            continue

        start_char, end_char = m.span()
        start_line, end_line = line_index.span_to_lines(start_char, end_char)
        table_spans.append((start_char, end_char))

        # Extract table path
        path = None
        csv_match = re.search(r'data-csv=["\']([^"\']+)["\']', attrs)
        html_match = re.search(r'data-html=["\']([^"\']+)["\']', attrs)
        comment_match = re.search(r'<!--\s*artifacts:\s*(?:csv=([^\s]+)|html=([^\s]+))', body)
        link_match = re.search(r'\[(?:CSV|HTML)\]\((tables\/[^\)]+)\)', body)

        if csv_match:
            path = csv_match.group(1)
        elif html_match:
            path = html_match.group(1)
        elif comment_match:
            path = comment_match.group(1) or comment_match.group(2)
        elif link_match:
            path = link_match.group(1)

        # Resolve caption
        resolved_caption = ""
        for line in body.splitlines():
            line_str = line.strip()
            if line_str.startswith("![") or line_str.startswith("|"):
                continue
            cap_line_match = _TABLE_CAPTION_LINE_RE.match(line_str)
            if cap_line_match:
                label = cap_line_match.group(1).strip()
                rest = cap_line_match.group(2).strip()
                prefix = f"Table {label}" if not label.lower().startswith("tab") else label
                resolved_caption = f"{prefix} {rest}".strip() if label else rest
                break

        tbl_id = f"t{len(tables) + 1}"
        if not resolved_caption:
            resolved_caption = f"Table {len(tables) + 1}"

        tables.append(
            {
                "id": tbl_id,
                "caption": _truncate_caption(resolved_caption),
                "path": path,
                "start_line": start_line,
                "end_line": max(start_line, end_line),
            }
        )

    # 4b. Match standalone GFM Markdown tables outside <figure> blocks
    for m in _GFM_TABLE_RE.finditer(document_md):
        start_char, end_char = m.span()
        inside_table_figure = any(s <= start_char and end_char <= e for s, e in table_spans)
        if inside_table_figure:
            continue

        start_line, end_line = line_index.span_to_lines(start_char, end_char)

        # Check line above for table caption
        caption = ""
        if start_line > 1:
            prev_line = lines[start_line - 2].strip()
            if not prev_line.startswith("!["):
                cap_match = _TABLE_CAPTION_LINE_RE.match(prev_line)
                if cap_match:
                    caption = _clean_text_inline(prev_line)

        tbl_id = f"t{len(tables) + 1}"
        if not caption:
            caption = f"Table {len(tables) + 1}"

        tables.append(
            {
                "id": tbl_id,
                "caption": _truncate_caption(caption),
                "path": None,
                "start_line": start_line,
                "end_line": max(start_line, end_line),
            }
        )

    # -----------------------------------------------------------------------
    # 5. Build toc.json structure
    # -----------------------------------------------------------------------
    clean_abstract = (abstract or "").strip()
    toc_json: dict[str, Any] = {
        "version": 1,
        "item_key": item_key,
        "parent_item_key": parent_item_key,
        "title": title or "",
        "abstract": clean_abstract,
        "abstract_source": abstract_source,
        "total_lines": total_lines,
        "total_words": total_words,
        "sections": sections,
        "figures": figures,
        "tables": tables,
    }

    # -----------------------------------------------------------------------
    # 6. Render toc.md
    # -----------------------------------------------------------------------
    md_lines: list[str] = [
        f"# {title or 'Document'}",
        "",
        "## Abstract",
        clean_abstract if clean_abstract else "No abstract or summary available.",
        "",
        "## Table of Contents",
    ]

    for sec in sections:
        s_id = sec["id"]
        heading = sec["heading"]
        level = sec["level"]
        words = sec["word_count"]

        if level == 0:
            indent = ""
            heading_str = heading
        elif level <= 2:
            indent = ""
            heading_str = f"{'#' * level} {heading}"
        else:
            indent = "  " * (level - 2)
            heading_str = f"{'#' * level} {heading}"

        md_lines.append(f"{indent}- [{s_id}] {heading_str} (~{words} words)")

    md_lines.append("")
    md_lines.append("## Figures")
    if figures:
        for fig in figures:
            f_id = fig["id"]
            cap = fig["caption"]
            p = fig.get("path")
            ref = f" ({p})" if p else ""
            md_lines.append(f"- [{f_id}] {cap}{ref}")
    else:
        md_lines.append("None")

    md_lines.append("")
    md_lines.append("## Tables")
    if tables:
        for tbl in tables:
            t_id = tbl["id"]
            cap = tbl["caption"]
            p = tbl.get("path")
            ref = f" ({p})" if p else ""
            md_lines.append(f"- [{t_id}] {cap}{ref}")
    else:
        md_lines.append("None")

    md_lines.append("")
    toc_md = "\n".join(md_lines)

    return toc_json, toc_md


def save_toc_files(bundle_dir: Path, toc_json: dict[str, Any], toc_md: str) -> None:
    """Safely persist toc.json and toc.md inside bundle_dir using atomic writes.

    Writes to temporary files within the target bundle directory and renames
    via os.replace to prevent partial reads or corrupt states.

    Args:
        bundle_dir: OKF document bundle directory (e.g. corpus/<item_key>/).
        toc_json: Dictionary representation of toc.json.
        toc_md: String contents for toc.md.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)

    toc_md_path = bundle_dir / "toc.md"
    toc_json_path = bundle_dir / "toc.json"

    # Write toc.md atomically first
    tmp_md = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=bundle_dir,
        delete=False,
        prefix="toc_md_",
        suffix=".tmp",
    )
    try:
        tmp_md.write(toc_md)
        tmp_md.flush()
        os.fsync(tmp_md.fileno())
    finally:
        tmp_md.close()
    os.replace(tmp_md.name, toc_md_path)

    # Write toc.json atomically second
    tmp_json = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=bundle_dir,
        delete=False,
        prefix="toc_json_",
        suffix=".tmp",
    )
    try:
        tmp_json.write(json.dumps(toc_json, indent=2, ensure_ascii=False) + "\n")
        tmp_json.flush()
        os.fsync(tmp_json.fileno())
    finally:
        tmp_json.close()
    os.replace(tmp_json.name, toc_json_path)
