"""OKF-inspired YAML frontmatter and catalog index.md generation module."""

import contextlib
import logging
import os
import re
import sqlite3
import threading
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

RELATION_TYPE_PRECEDENCE: dict[str, int] = {
    "human_curated_and_hybrid_search": 4,
    "human_curated": 3,
    "hybrid_search": 2,
    "shared_tags": 1,
}


def _compute_relative_source_path(
    source_path: str | Path | None, base_dir: Path
) -> str:
    """Compute relative POSIX path from base_dir to source_path with fallback.

    Args:
        source_path: Path to the source document on disk or URI string.
        base_dir: Base directory (e.g. cache_dir or bundle_dir).

    Returns:
        Relative path string with forward slashes, or normalized path/URI on error.
    """
    if not source_path or not str(source_path).strip():
        return ""
    str_source = str(source_path).strip()
    if "://" in str_source or str_source.startswith("zotero:"):
        return str_source
    if str_source.startswith("storage/"):
        return "../../../" + str_source
    if str_source.startswith("../../../storage/"):
        return str_source
    try:
        src_p = Path(source_path).resolve()
        base_p = base_dir.resolve()
        rel = os.path.relpath(src_p, base_p)
        return Path(rel).as_posix()
    except (ValueError, Exception):
        return Path(source_path).as_posix()


def normalize_to_storage_relative(
    source_path: str | Path | None,
    zotero_profile_dir: Path | None = None,
) -> str:
    """Normalize a source document path to storage-relative format or verbatim URI.

    Normalizes storage paths to 'storage/<item_key>/<filename>' (or 'storage/<item_key>').
    Virtual note URIs (zotero://note/...) and remote URIs are preserved verbatim.
    External linked files outside storage retain their normalized absolute POSIX paths.

    Args:
        source_path: File path on disk, URI, or storage path string.
        zotero_profile_dir: Optional root Zotero profile directory.

    Returns:
        Normalized storage-relative path string, URI, or absolute POSIX path.
    """
    if not source_path or not str(source_path).strip():
        return ""

    str_path = str(source_path).strip()
    if str_path.startswith("zotero://note/"):
        return str_path

    if "://" in str_path:
        if str_path.startswith("file://"):
            str_path = urllib.parse.unquote(str_path[7:])
            if str_path.startswith("/") and len(str_path) > 2 and str_path[2] == ":":
                str_path = str_path[1:]
        elif not str_path.startswith("zotero:"):
            return str_path

    # Decode any remaining percent-encoding (e.g. %20)
    str_path = urllib.parse.unquote(str_path)

    # Check if path is within zotero_profile_dir / "storage"
    if zotero_profile_dir is not None:
        try:
            storage_dir = (zotero_profile_dir / "storage").resolve()
            resolved = Path(str_path).resolve()
            if resolved.is_relative_to(storage_dir):
                rel = resolved.relative_to(zotero_profile_dir)
                return Path(rel).as_posix()
        except (ValueError, OSError):
            pass

    # Regex heuristic matching storage/<key>/<filename> or storage/<key>
    m = re.search(r"(?:^|[/\\])storage[/\\]([A-Za-z0-9_-]+)(?:[/\\](.+))?$", str_path)
    if m:
        key = m.group(1)
        rest = m.group(2)
        if rest:
            clean_rest = rest.replace("\\", "/").rstrip("/")
            return f"storage/{key}/{clean_rest}"
        return f"storage/{key}"

    # External linked file outside storage: retain normalized absolute POSIX path
    return Path(str_path).as_posix()


def resolve_source_path(
    stored_path: str | Path | None,
    zotero_profile_dir: Path,
) -> Path | str:
    """Resolve a stored source_path to a local physical Path or verbatim URI.

    Args:
        stored_path: Storage-relative path ('storage/...'), note URI, or absolute path.
        zotero_profile_dir: Root Zotero profile directory.

    Returns:
        Resolved physical Path on disk or URI string.
    """
    if not stored_path or not str(stored_path).strip():
        return ""

    str_stored = str(stored_path).strip()
    if str_stored.startswith("zotero://note/") or ("file:" not in str_stored and "://" in str_stored):
        return str_stored

    if str_stored.startswith("storage/"):
        unquoted = urllib.parse.unquote(str_stored)
        return (zotero_profile_dir / unquoted).resolve()

    p = Path(str_stored)
    if p.exists():
        return p.resolve()

    # Fallback heuristic: check if storage/<key>/<filename> exists in local zotero_profile_dir
    m = re.search(
        r"(?:^|[/\\])storage[/\\](?P<key>[A-Za-z0-9_-]+)(?:[/\\](?P<file>.+))?$",
        str_stored,
    )
    if m:
        key = m.group("key")
        filename = urllib.parse.unquote(m.group("file") or "")
        candidate = (
            (zotero_profile_dir / "storage" / key / filename)
            if filename
            else (zotero_profile_dir / "storage" / key)
        )
        if candidate.exists():
            return candidate.resolve()

    return p


_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n([\s\S]*?)\r?\n---\s*(?:\r?\n|$)")


def strip_existing_frontmatter(markdown: str) -> str:
    """Strip leading YAML frontmatter block if present.

    Args:
        markdown: Input Markdown string.

    Returns:
        Markdown content with leading YAML frontmatter removed.
    """
    m = _FRONTMATTER_RE.match(markdown)
    if m:
        return markdown[m.end() :].lstrip("\r\n")
    return markdown


def build_frontmatter(
    item_key: str,
    parent_item_type: str | None,
    title: str | None,
    collections: list[Any] | None,
    source_path: Path | str,
    source_hash: str,
    content_type: str,
    converter_id: str,
    converter_version: str,
    converted_at: str,
    parent_item_key: str | None = None,
    parse_quality_score: float | None = None,
    checkpoint_id: str | None = None,
    authors: list[str] | None = None,
    year: str | int | None = None,
    publication: str | None = None,
    doi: str | None = None,
    url: str | None = None,
    keywords: list[str] | None = None,
    bundle_dir: Path | None = None,
) -> str:
    """Build OKF-0.2 YAML frontmatter string.

    Args:
        item_key: Zotero item key.
        parent_item_type: Zotero item type (e.g., 'journalArticle'). Defaults to 'document'.
        title: Title of the parent item.
        collections: List of ZoteroCollection objects or dicts with name and key/collection_key.
        source_path: Absolute path to the source file on disk.
        source_hash: SHA-256 hex digest of the source file.
        content_type: MIME type of the source file.
        converter_id: Converter engine ID.
        converter_version: Converter engine version string.
        converted_at: ISO-8601 timestamp string.
        parent_item_key: Parent item key if item is an attachment.
        parse_quality_score: Optional parse quality score (e.g. 0.0 to 5.0 from Datalab).
        checkpoint_id: Optional converter checkpoint ID.
        authors: Optional list of creator/author name strings.
        year: Optional publication year (int or string).
        publication: Optional publication venue or publisher string.
        doi: Optional normalized DOI string.
        url: Optional source URL string.
        bundle_dir: Optional bundle directory (e.g. cache_dir / item_key). If provided,
            source.path is computed as a relative POSIX path from bundle_dir.

    Returns:
        Formatted YAML frontmatter block ending with '---\n'.
    """
    formatted_collections: list[dict[str, str]] = []
    if collections:
        for c in collections:
            if isinstance(c, dict):
                c_name = c.get("name", "")
                c_key = c.get("collection_key") or c.get("key", "")
                formatted_collections.append({"name": c_name, "collection_key": c_key})
            elif hasattr(c, "name") and hasattr(c, "key"):
                formatted_collections.append({"name": c.name, "collection_key": c.key})

    provenance_data: dict[str, Any] = {
        "converter_id": converter_id,
        "converter_version": converter_version,
        "converted_at": converted_at,
    }
    if parse_quality_score is not None:
        provenance_data["parse_quality_score"] = parse_quality_score
    if checkpoint_id is not None:
        provenance_data["checkpoint_id"] = checkpoint_id

    data: dict[str, Any] = {
        "type": parent_item_type or "document",
        "okf_version": "0.2",
        "item_key": item_key,
        "parent_item_key": parent_item_key,
        "title": title or "",
    }

    if authors:
        cleaned_authors = [a.strip() for a in authors if isinstance(a, str) and a.strip()]
        if cleaned_authors:
            data["authors"] = cleaned_authors

    if year is not None:
        if isinstance(year, int):
            data["year"] = year
        elif isinstance(year, str) and year.strip():
            if year.strip().isdigit():
                data["year"] = int(year.strip())
            else:
                data["year"] = year.strip()

    if publication and isinstance(publication, str) and publication.strip():
        data["publication"] = publication.strip()

    if doi and isinstance(doi, str) and doi.strip():
        data["doi"] = doi.strip()

    if url and isinstance(url, str) and url.strip():
        data["url"] = url.strip()

    if keywords:
        cleaned_keywords = [k.strip() for k in keywords if isinstance(k, str) and k.strip()]
        if cleaned_keywords:
            data["keywords"] = cleaned_keywords

    data["member_of_collections"] = formatted_collections
    rel_path = (
        _compute_relative_source_path(source_path, bundle_dir)
        if bundle_dir is not None
        else str(source_path)
    )
    data["source"] = {
        "path": rel_path,
        "hash": source_hash,
        "content_type": content_type,
    }
    data["provenance"] = provenance_data
    yaml_str = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_str}\n---\n"


def generate_item_index_md(
    item_dir: Path,
    item_key: str,
    title: str | None,
    converter_id: str,
    converter_version: str,
) -> None:
    """Generate item-level index.md catalog inside item_dir.

    Catalogs main document as well as all associated image and table assets.

    Args:
        item_dir: Bundle directory (corpus/<item_key>/).
        item_key: Zotero item key.
        title: Title of the parent item.
        converter_id: Converter engine ID.
        converter_version: Converter engine version string.
    """
    doc_path = item_dir / "document.md"
    doc_ref = (
        f"- **Main Document**: [{doc_path.name}](./{doc_path.name})"
        if doc_path.exists()
        else "- **Main Document**: None"
    )

    toc_md_path = item_dir / "toc.md"
    toc_json_path = item_dir / "toc.json"
    toc_ref = (
        f"- **Table of Contents**: [{toc_md_path.name}](./{toc_md_path.name}) ([JSON](./{toc_json_path.name}))"
        if toc_md_path.exists()
        else None
    )

    rel_md_path = item_dir / "item_relations.md"
    rel_ref = (
        "- **Related Items**: [item_relations.md](./item_relations.md)"
        if rel_md_path.exists()
        else None
    )

    image_extensions = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
    images_dir = item_dir / "images"
    image_links: list[str] = []
    if images_dir.exists() and images_dir.is_dir():
        for img_path in sorted(images_dir.iterdir()):
            if img_path.is_file() and img_path.suffix.lower() in image_extensions:
                rel = img_path.relative_to(item_dir).as_posix()
                image_links.append(f"- [{rel}](./{rel})")

    table_extensions = {".csv", ".html", ".md", ".json"}
    tables_dir = item_dir / "tables"
    table_links: list[str] = []
    if tables_dir.exists() and tables_dir.is_dir():
        for tbl_path in sorted(tables_dir.iterdir()):
            if tbl_path.is_file() and tbl_path.suffix.lower() in table_extensions:
                rel = tbl_path.relative_to(item_dir).as_posix()
                table_links.append(f"- [{rel}](./{rel})")

    lines = [
        f"# Item Catalog: {item_key}",
        "",
        f"- **Title**: {title or 'Untitled'}",
        f"- **Converter**: {converter_id} (v{converter_version})",
        doc_ref,
    ]
    if toc_ref:
        lines.append(toc_ref)
    if rel_ref:
        lines.append(rel_ref)
    lines.extend(["", "## Assets"])

    if not image_links and not table_links:
        lines.append("None")
    else:
        if image_links:
            lines.extend(["", "### Images", *image_links])
        if table_links:
            lines.extend(["", "### Tables", *table_links])

    lines.append("")
    index_path = item_dir / "index.md"
    index_path.write_text("\n".join(lines), encoding="utf-8")


def generate_item_relations_md(
    item_key: str,
    relations: list[dict[str, Any]],
    titles: dict[str, str] | None = None,
    cache_dir: Path | None = None,
) -> str:
    """Generate Markdown representation of an item's literature neighborhood.

    If relations is empty, returns an empty string (0 bytes for unconnected papers).
    Otherwise, coalesces multi-relation pairs and orders deterministically:
    ORDER BY score DESC, target_key ASC.

    Args:
        item_key: Source item key.
        relations: List of relation dictionaries (target_key, score, relation_type, ...).
        titles: Optional mapping of target_key to human-readable title.
        cache_dir: Optional markdown cache directory to verify if target bundle exists.

    Returns:
        Formatted Markdown string or empty string.
    """
    if not relations:
        return ""

    titles_map = titles or {}

    # Coalesce multi-relation pairs by target_key using strict display precedence:
    # human_curated_and_hybrid_search (1.0) > human_curated (1.0) > hybrid_search > shared_tags (0.70)
    best_by_target: dict[str, dict[str, Any]] = {}
    for r in relations:
        t_key = r.get("target_key", "")
        if not t_key:
            continue
        rel_type = str(r.get("relation_type", ""))
        score = float(r.get("score", 0.0))
        rank = (RELATION_TYPE_PRECEDENCE.get(rel_type, 0), score)

        if t_key not in best_by_target:
            best_by_target[t_key] = r
        else:
            prev = best_by_target[t_key]
            prev_rank = (
                RELATION_TYPE_PRECEDENCE.get(str(prev.get("relation_type", "")), 0),
                float(prev.get("score", 0.0)),
            )
            if rank > prev_rank:
                best_by_target[t_key] = r

    # Order by score DESC, target_key ASC
    sorted_rels = sorted(
        best_by_target.values(),
        key=lambda r: (-float(r.get("score", 0.0)), r.get("target_key", "")),
    )

    if not sorted_rels:
        return ""

    item_blocks: list[str] = []
    for r in sorted_rels:
        t_key = r.get("target_key", "")
        raw_title = titles_map.get(t_key)
        title = raw_title.strip() if raw_title and raw_title.strip() else t_key
        score = float(r.get("score", 0.0))
        rel_type = r.get("relation_type", "")
        score_str = "1.0" if abs(score - 1.0) < 1e-6 else f"{score:.2f}"

        has_bundle = bool(cache_dir and (cache_dir / t_key / "document.md").exists())
        if has_bundle:
            item_header = f"- [{t_key} - {title}](../{t_key}/document.md)"
        else:
            item_header = f"- **[{t_key}] {title}**"

        item_blocks.append(f"{item_header}\n  - Relation: `{rel_type}` (score: {score_str})")

    return f"# Related Items: {item_key}\n\n" + "\n\n".join(item_blocks) + "\n"


def regenerate_item_relations_md(
    item_key: str,
    cache_dir: Path,
    target_bundle_dir: Path | None = None,
) -> Path | None:
    """Regenerate item_relations.md and refresh index.md for a given bundle.

    Queries item_relations from conversion_meta.db for source_key = item_key.
    Also queries conversion_meta for target titles (fallback to target_key if not found).
    Target directory is target_bundle_dir if provided, else cache_dir / item_key.
    If target directory does not exist, returns None.
    Writes relations markdown atomically using .tmp_item_relations.md and os.replace (P1-4 pattern).
    Refreshes index.md catalog with the new/updated item_relations.md link.

    Args:
        item_key: Zotero item key.
        cache_dir: Markdown cache directory containing conversion_meta.db.
        target_bundle_dir: Optional bundle directory override (e.g. staging dir).

    Returns:
        Path to the materialized item_relations.md, or None if target directory does not exist.
    """
    bundle_dir = target_bundle_dir if target_bundle_dir is not None else (cache_dir / item_key)
    if not bundle_dir.exists():
        return None

    db_path = cache_dir / "conversion_meta.db"
    relations: list[dict[str, Any]] = []
    titles: dict[str, str] = {}
    self_title: str | None = None
    self_converter_id = "unknown"
    self_converter_version = "1.0"

    if db_path.exists():
        try:
            with contextlib.closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 5000;")
                try:
                    rows = conn.execute(
                        """
                        SELECT source_key, target_key, score, relation_type, match_evidence, updated_at
                        FROM item_relations
                        WHERE source_key = ?
                        ORDER BY score DESC, target_key ASC
                        """,
                        (item_key,),
                    ).fetchall()
                    relations = [dict(r) for r in rows]
                except sqlite3.OperationalError:
                    relations = []

                target_keys = {r["target_key"] for r in relations if r.get("target_key")}
                if target_keys:
                    placeholders = ",".join("?" for _ in target_keys)
                    try:
                        meta_rows = conn.execute(
                            f"SELECT item_key, title FROM conversion_meta WHERE item_key IN ({placeholders})",
                            tuple(target_keys),
                        ).fetchall()
                        for mr in meta_rows:
                            if mr["title"]:
                                titles[mr["item_key"]] = mr["title"]
                    except sqlite3.OperationalError:
                        pass

                try:
                    self_row = conn.execute(
                        "SELECT title, converter_id, converter_version FROM conversion_meta WHERE item_key = ?",
                        (item_key,),
                    ).fetchone()
                    if self_row:
                        self_title = self_row["title"]
                        self_converter_id = self_row["converter_id"] or self_converter_id
                        self_converter_version = self_row["converter_version"] or self_converter_version
                except sqlite3.OperationalError:
                    pass
        except Exception as exc:
            logger.warning("Failed to query conversion_meta.db for relations of %s: %s", item_key, exc)

    if not self_title and (bundle_dir / "document.md").exists():
        try:
            with (bundle_dir / "document.md").open("r", encoding="utf-8") as f:
                for _ in range(30):
                    line = f.readline()
                    if not line:
                        break
                    m = re.match(r"^title:\s*['\"]?(.*?)['\"]?\s*$", line.strip(), re.IGNORECASE)
                    if m:
                        self_title = m.group(1).strip()
                        break
        except Exception:
            pass

    content = generate_item_relations_md(item_key=item_key, relations=relations, titles=titles, cache_dir=cache_dir)

    target_path = bundle_dir / "item_relations.md"
    tmp_path = bundle_dir / f".tmp_{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex[:8]}_item_relations.md"
    try:
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target_path)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise exc

    # Refresh index.md catalog with the new/updated item_relations.md link
    generate_item_index_md(
        item_dir=bundle_dir,
        item_key=item_key,
        title=self_title,
        converter_id=self_converter_id,
        converter_version=self_converter_version,
    )

    return target_path


def generate_agents_md(cache_dir: Path) -> None:
    """Generate AGENTS.md navigation guide at the root of the corpus directory.

    Args:
        cache_dir: Corpus directory.
    """
    content = """# AGENTS.md - Zocrates Corpus Navigation Guide

Welcome to the Zocrates Document Corpus. This directory stores document bundles converted from Zotero attachments adhering to the **Open Knowledge Format (OKF v0.2)** standard.

---

## 1. Directory Structure

```
corpus/
├── index.md                      # Root Knowledge Base Catalog (all documents)
├── AGENTS.md                     # This navigation guide
├── conversion_meta.db            # SQLite metadata database (authoritative, WAL mode)
└── <item_key>/                   # Item-centric bundle directory (Zotero attachment key)
    ├── document.md               # OKF v0.2 Markdown document with YAML frontmatter
    ├── toc.json                  # Machine-parseable Table of Contents & asset index
    ├── toc.md                    # Agent-readable overview with Abstract and ToC
    ├── item_relations.md         # Related literature neighborhood (direct links, 0 bytes if orphan)
    ├── index.md                  # Item-level catalog linking local assets
    ├── images/                   # Extracted figures and rasterized diagrams (PNG, JPG, SVG)
    ├── tables/                   # Structured table extractions (CSV, HTML)
    ├── image_captions.json       # Sidecar storing VLM synthetic figure captions
    └── docling_document.json     # Parser AST sidecar (DO NOT read during normal navigation)
```

---

## 2. Agent Navigation Protocol

Follow this 5-step protocol to efficiently navigate and extract context without wasting tokens:

### Step 1: Catalog Discovery
Read `corpus/index.md` first to scan available items, titles, document types, and character counts. Identify relevant `item_key`s before opening individual files.

### Step 2: Frontmatter & ToC Triage
Open target `<item_key>/toc.md` (or inspect the YAML frontmatter of `document.md`) to read:
- Abstract or generated summary
- Section structure, line ranges, and word counts
- Available figures and tables

Evaluate relevance and identify specific sections *before* reading the entire document body.

### Step 3: Targeted Content Reading
Read specific sections using `[s0]`, `[s1]`, etc. or search for terms within `<item_key>/document.md`. Converted documents include:
- Section headers (ATX style `##`, `###`)
- Semantic figure tags (`<figure data-type="image" data-id="...">...<synthetic_caption>...</synthetic_caption></figure>`)
- Structured table containers (`<figure data-type="table">...`)

### Step 4: Asset Inspection
If figures, diagrams, or tabular data require detailed inspection:
- Check `<item_key>/index.md` or `toc.md` for relative links to all local assets.
- View image files in `<item_key>/images/` using multimodal tools or `get_figure_or_table`.
- Read CSV/HTML table files in `<item_key>/tables/`.

### Step 5: Relation & Literature Graph Traversal
If a document leaves unanswered questions or you need adjacent prior/follow-up work:
- Check `<item_key>/item_relations.md` (also cataloged in `<item_key>/index.md`).
- Traversal via relative links allows immediate navigation across related papers without burning tokens on full-library vector searches.
- If 0 bytes, the paper has no corroborated relations in the library (unconnected paper).

---

## 3. Strict Guardrails for Agents

* ⚠️ **Do NOT read or glob parser AST sidecars** (`docling_document.json`, `datalab_document.json`) during standard tasks. These are multi-megabyte machine parser ASTs intended only for debugging and will rapidly exhaust context windows. However, lightweight index files such as `toc.json`, `toc.md`, `item_relations.md`, and `image_captions.json` are safe to read (0-byte orphan `item_relations.md` files are intentional).
* ⚠️ **Avoid global recursive grep** across the entire `corpus/` directory. Always filter by `index.md` or target item bundles first.

---

## 4. Hybrid RAG Coordination

For broad conceptual or semantic queries across large libraries, use the Zocrates vector/FTS search API to retrieve top-matching chunk candidates and their associated `item_key`s, then navigate directly to the corresponding `<item_key>/` bundle on disk.

For full architectural details and design specifications, see the **[OKF Document Bundles & Agent Navigation Architecture](artifacts/okf_document_bundles_and_agent_navigation.md)** artifact.
"""
    agents_path = cache_dir / "AGENTS.md"
    agents_path.write_text(content.strip() + "\n", encoding="utf-8")


def generate_root_index_md(cache_dir: Path) -> None:
    """Regenerate root-level index.md catalog from conversion_meta.db.

    Args:
        cache_dir: Corpus directory containing conversion_meta.db.
    """
    db_path = cache_dir / "conversion_meta.db"
    if not db_path.exists():
        return

    lines = []
    with contextlib.closing(sqlite3.connect(str(db_path), timeout=5.0)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000;")
        rows = conn.execute(
            "SELECT item_key, title, parent_item_type, converter_id, converter_version, "
            "converted_at, source_path, char_count "
            "FROM conversion_meta ORDER BY converted_at DESC"
        ).fetchall()

        lines = [
            "# Zocrates Knowledge Base Catalog",
            "",
            f"Total Converted Documents: {len(rows)}",
            "",
            "| Item Key | Title | Type | Converter | Converted At | Source Path | Chars |",
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- |",
        ]

        title_pattern = re.compile(r"^title:\s*['\"]?(.*?)['\"]?\s*$", re.IGNORECASE)

        for r in rows:
            key = r["item_key"]
            raw_title = r["title"]

            # If title is missing in DB, attempt fast scan from document.md frontmatter
            if not raw_title or not str(raw_title).strip():
                doc_file = cache_dir / key / "document.md"
                if doc_file.exists():
                    try:
                        with doc_file.open("r", encoding="utf-8") as f:
                            for _ in range(30):
                                line = f.readline()
                                if not line:
                                    break
                                m = title_pattern.match(line.strip())
                                if m and m.group(1).strip():
                                    raw_title = m.group(1).strip()
                                    # Lazily backfill to DB
                                    conn.execute(
                                        "UPDATE conversion_meta SET title = ? WHERE item_key = ?",
                                        (raw_title, key),
                                    )
                                    conn.commit()
                                    break
                    except Exception:
                        pass

            if not raw_title or not str(raw_title).strip():
                raw_title = (r["source_path"] and Path(r["source_path"]).stem) or "Untitled"

            clean_title = (
                str(raw_title)
                .replace("|", "\\|")
                .replace("\r", " ")
                .replace("\n", " ")
                .strip()
            )
            p_type = r["parent_item_type"] or "document"
            conv = f"{r['converter_id']} (v{r['converter_version']})"
            conv_at = r["converted_at"]
            stored_path = (r["source_path"] or "").strip()
            if stored_path.startswith("zotero://note/"):
                rel_source = f"[Zotero Note]({stored_path})"
            elif stored_path.startswith("storage/"):
                rel_source = f"[Source Document](../../{stored_path})"
            elif stored_path.startswith("../../storage/"):
                rel_source = f"[Source Document]({stored_path})"
            elif stored_path:
                rel_source = f"[Source Document]({stored_path})"
            else:
                rel_source = ""
            count = r["char_count"]
            lines.append(
                f"| [{key}](./{key}/document.md) | {clean_title} | {p_type} | {conv} | {conv_at} | {rel_source} | {count} |"
            )

    lines.append("")
    root_index = cache_dir / "index.md"
    root_index.write_text("\n".join(lines), encoding="utf-8")

    # Keep AGENTS.md synchronized with root catalog
    generate_agents_md(cache_dir)
