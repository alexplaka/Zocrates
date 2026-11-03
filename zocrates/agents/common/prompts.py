"""Common prompt templates, formatters, and citation guidelines for Zocrates agents."""

import html
import re
import urllib.parse
from typing import Any


def format_active_items(
    active_items: list[dict[str, Any]] | None,
    include_keys: bool = True,
) -> str:
    """Format active context items from session state for insertion into agent prompts.

    Supports a hierarchical format when a parent item has multiple converted
    attachments and/or child notes.  When a parent has exactly one converted
    attachment and no notes the output is byte-identical to the legacy flat
    format so existing prompts and tests are unaffected.

    Args:
        active_items: List of item dictionaries representing active selections in
            Zotero UI.  Each entry may optionally include a ``children`` list
            of attachment/note dicts (see ``fast_api_app.py``).
        include_keys: Whether to include ``[Key: ...]``, ``[Attachment Key: ...]``,
            and ``[Note Key: ...]`` identifiers.  Set to ``False`` for
            synthesizer/coordinator prompts to hide internal keys.

    Returns:
        Formatted multi-line Markdown string.
    """
    if not active_items:
        return "No active paper is currently selected in the Zotero UI."

    lines: list[str] = []
    for item in active_items:
        key = item.get("item_key") or item.get("key", "unknown")
        title = item.get("title", "Untitled")

        year = item.get("year", "")
        year_str = f" ({year})" if year else ""
        children: list[dict[str, Any]] = item.get("children") or []
        has_notes = any(c.get("is_note") for c in children)
        use_hierarchy = len(children) > 1 or has_notes

        if use_hierarchy:
            # --- Parent line ---
            if include_keys:
                lines.append(f'- [Key: {key}] Title: "{title}"{year_str}')
            else:
                lines.append(f'- Title: "{title}"{year_str}')

            # --- Child lines ---
            for child in children:
                c_key = child.get("item_key", "unknown")
                c_title = child.get("title", "Untitled")
                c_type = child.get("content_type", "")

                if child.get("is_note"):
                    word_count = child.get("word_count", 0)
                    inline_text: str | None = child.get("inline_text")
                    if inline_text:
                        # Short note: inline content as blockquote.
                        if include_keys:
                            lines.append(
                                f'  - [Note Key: {c_key}] "{c_title}" ({word_count} words) [text/note]:'
                            )
                        else:
                            lines.append(f'  - Note: "{c_title}":')
                        for inline_line in inline_text.splitlines():
                            lines.append(f"    > {inline_line}")
                    else:
                        # Longer note: reference only.
                        if include_keys:
                            lines.append(
                                f'  - [Note Key: {c_key}] "{c_title}" (~{word_count} words) [text/note]'
                            )
                        else:
                            lines.append(f'  - Note: "{c_title}" [text/note]')
                else:
                    # Attachment child.
                    if include_keys:
                        lines.append(f'  - [Attachment Key: {c_key}] "{c_title}" [{c_type}]')
                    else:
                        lines.append(f'  - Attachment: "{c_title}" [{c_type}]')
        else:
            # --- Flat format (backward-compatible, single attachment or no children) ---
            if include_keys:
                lines.append(f'- [Key: {key}] Title: "{title}"{year_str}')
            else:
                lines.append(f'- Title: "{title}"{year_str}')

    return "\n".join(lines)


def resolve_active_item_entry(
    syncer: Any,
    key: str,
    title: str | None = None,
) -> dict[str, Any]:
    """Resolve a Zotero item key into an enriched active-context entry dict.

    Queries the syncer DB for the parent item's converted attachments and child
    notes, producing a ``children`` list compatible with ``format_active_items``.
    Attachment entries are only included when their OKF bundle (``document.md``)
    exists on disk.  Child notes are summarized non-LLM: notes shorter than 50
    words are inlined into ``inline_text`` (capped at 300 chars), longer notes
    carry ``inline_text=None`` and are referenced by key/word count only.

    Args:
        syncer: The active ``LibrarySyncer`` (needed for ``.db`` and
            ``.processor`` access).
        key: Zotero item key of the parent item.
        title: Optional human-readable title override; falls back to the DB
            item title, then ``"Untitled"``.

    Returns:
        Enriched item dict with ``item_key``, ``title``, optional ``year``,
        and optional ``children``.  Any syncer/DB failure yields a minimal
        ``{"item_key": key, "title": doc_title}`` fallback.
    """
    doc_title = title or "Untitled"
    item_obj = None
    try:
        item_obj = syncer.db.get_item_by_key(key)
        if not doc_title or doc_title == "Untitled":
            doc_title = item_obj.title if item_obj and item_obj.title else "Untitled"
    except Exception:
        if not doc_title or doc_title == "Untitled":
            doc_title = "Untitled"

    children: list[dict[str, Any]] = []
    if item_obj is not None:
        try:
            for att in syncer.db.get_attachments(item_obj.item_id):
                att_bundle = syncer.processor._cache_dir / att.item_key / "document.md"
                if att_bundle.exists():
                    children.append({
                        "item_key": att.item_key,
                        "title": att.title or (att.path.name if att.path else "Attachment"),
                        "content_type": att.content_type,
                        "is_note": False,
                    })
        except Exception:
            pass

        try:
            for n in syncer.db.get_notes_for_item(item_obj.item_id):
                raw_text = re.sub(r"<[^>]+>", " ", n.note_html)
                plain_text = html.unescape(raw_text)
                plain_text = re.sub(r"\s+", " ", plain_text).strip()
                words = len(plain_text.split())
                if words < 50:
                    inline_text: str | None = plain_text
                    if len(inline_text) > 300:
                        inline_text = inline_text[:300].rstrip() + "\u2026"
                else:
                    inline_text = None
                children.append({
                    "item_key": n.note_key,
                    "title": n.title or "Note",
                    "content_type": "text/note",
                    "is_note": True,
                    "word_count": words,
                    "inline_text": inline_text,
                })
        except Exception:
            pass

    entry: dict[str, Any] = {"item_key": key, "title": doc_title}
    if item_obj is not None and getattr(item_obj, "year", None) and isinstance(item_obj.year, str):
        entry["year"] = item_obj.year
    if children:
        entry["children"] = children
    return entry


def _clean_author_name(name: str) -> str:
    """Extract surname or primary identifier from a formatted author name.

    Args:
        name: Raw author name string, potentially formatted as "Last, First".

    Returns:
        Cleaned surname or trimmed author name string.
    """
    s = name.strip()
    if "," in s:
        last = s.split(",", 1)[0].strip()
        if last:
            return last
    return s


def _format_authors_clean(raw_authors: Any) -> str | None:
    """Format authors list cleanly in APA style (1 author: 'Author', 2: 'A & B', >2: 'A et al.')."""
    if isinstance(raw_authors, (list, tuple)):
        clean = [_clean_author_name(str(a)) for a in raw_authors if a and str(a).strip()]
        if not clean:
            return None
        if len(clean) == 1:
            return clean[0]
        if len(clean) == 2:
            return f"{clean[0]} & {clean[1]}"
        return f"{clean[0]} et al."
    if isinstance(raw_authors, str) and raw_authors.strip():
        return raw_authors.strip()
    return None


def _format_year_clean(raw_year: Any) -> str | None:
    """Format year cleanly, omitting empty or 'N/A' values."""
    if raw_year is None:
        return None
    y_str = str(raw_year).strip()
    if not y_str or y_str.upper() == "N/A":
        return None
    return y_str


def format_retrieved_evidence_str(evidence_chunks: list[dict[str, Any]] | None) -> str:
    """Format accumulated evidence chunks into a clean XML-tagged block.

    Suitable for passing into agent prompts or sufficiency evaluators.
    Encloses chunks inside <retrieved_evidence> and <chunk index="..." ...>
    tags to prevent Markdown boundary collisions, eliminate duplicate titles/headings,
    and suppress internal chunk IDs and scores from the prompt representation.

    Args:
        evidence_chunks: List of serialized evidence chunk dictionaries.

    Returns:
        XML string summarizing all retrieved evidence chunks.
    """
    if not evidence_chunks:
        return "<retrieved_evidence>\nNo evidence retrieved.\n</retrieved_evidence>"

    chunk_blocks: list[str] = []

    for idx, c in enumerate(evidence_chunks, start=1):
        title = str(c.get("title") or "").strip()
        heading = str(c.get("heading") or "").strip()
        ctype = str(c.get("chunk_type") or "text").strip()
        text = str(c.get("text") or "").strip()

        # Attribute construction
        attr_parts = [f'index="{idx}"']

        doc_name = title if title else "Untitled Document"
        attr_parts.append(f'title="{html.escape(doc_name, quote=True)}"')
        auth_str = _format_authors_clean(c.get("authors"))
        if auth_str:
            attr_parts.append(f'authors="{html.escape(auth_str, quote=True)}"')
        year_str = _format_year_clean(c.get("year"))
        if year_str:
            attr_parts.append(f'year="{html.escape(year_str, quote=True)}"')
        if heading:
            attr_parts.append(f'section="{html.escape(heading, quote=True)}"')
        if ctype:
            attr_parts.append(f'type="{html.escape(ctype, quote=True)}"')

        attr_str = " " + " ".join(attr_parts)

        # Body construction
        body_parts = []
        item_key_val = str(c.get("item_key") or "").strip()
        if text:
            # Rewrite any local relative image links in chunk text (e.g. ![Fig](images/foo.jpg))
            # to fully qualified zocrates-img://{item_key}/images/foo.jpg URIs so the synthesizer
            # always copies an actionable URI even when referencing inline section figures.
            if item_key_val and ("images/" in text or "./images/" in text):
                text = re.sub(
                    r"(!\[[^\]]*\]\()\s*(?:\./)?(images/[^)\s]+)\)",
                    rf"\1zocrates-img://{item_key_val}/\2)",
                    text,
                )
            body_parts.append(text)
        if c.get("image_path"):
            raw_image_path = str(c["image_path"]).strip()
            if item_key_val:
                # URL-encode the path component for resilience against spaces
                # in PDF-extracted filenames (Markdown link syntax breaks on
                # unencoded spaces).  safe="/" preserves path separators.
                encoded_path = urllib.parse.quote(raw_image_path, safe="/")
                display_uri = f"zocrates-img://{item_key_val}/{encoded_path}"
            else:
                # item_key missing: fall back to raw relative path so the chunk
                # is still informative, but no displayable URI will be emitted.
                display_uri = raw_image_path
            body_parts.append(f'<figure_image path="{html.escape(display_uri, quote=True)}" />')
        if c.get("structured_content"):
            body_parts.append(f'<table_csv>\n{str(c["structured_content"]).strip()}\n</table_csv>')

        body = "\n".join(body_parts)
        if body:
            chunk_blocks.append(f'  <chunk{attr_str}>\n{body}\n  </chunk>')
        else:
            chunk_blocks.append(f'  <chunk{attr_str} />')

    return "<retrieved_evidence>\n" + "\n".join(chunk_blocks) + "\n</retrieved_evidence>"


def strip_snippet_keys(snippet_xml: str) -> str:
    """Programmatically strip item key attributes from snippet XML tags.

    Leaves all other XML attributes (e.g. paper, page, authors)
    and snippet text content intact.

    Args:
        snippet_xml: XML-formatted snippet string.

    Returns:
        Snippet XML string with all ' key="..."' attributes removed.
    """
    if not snippet_xml:
        return ""
    return re.sub(r'\s+key="[^"]*"', "", snippet_xml)
