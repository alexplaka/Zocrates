"""Zocrates FastMCP Server definition.

Exposes 4 core academic retrieval tools, resources for reading OKF bundles
and knowledge catalogs, and prompt templates for structured literature review.

STRICT INVARIANT:
Stdout is reserved strictly for JSON-RPC 2.0 protocol frames.
All logging, diagnostic messages, and tracebacks MUST route exclusively to sys.stderr.
"""

from typing import Any

from mcp.server.fastmcp import FastMCP

from zocrates.mcp.backend_client import BackendClient

mcp = FastMCP("zocrates-library")
backend_client = BackendClient()


# ---------------------------------------------------------------------------
# FastMCP Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search_corpus(
    query: str,
    item_key: str | list[str] | None = None,
    chunk_type: str | list[str] | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Hybrid (dense vector + FTS5 BM25) search over the indexed Zotero library with compound pre-filtering.

    Supports filtering by item_key(s) and chunk_type ('frontmatter', 'figure', 'table', 'equation', 'code', 'text').
    Searching chunk_type='frontmatter' discovers paper metadata, abstracts, and bibliographic details.
    Results include cosine_similarity and rrf_score.

    Args:
        query: Natural language research query or keyword query.
        item_key: Optional item key or list of keys to restrict search scope.
        chunk_type: Optional chunk type or list of chunk types to filter by.
        limit: Number of results to return (clamped between 1 and 20 by backend).

    Returns:
        Dictionary containing search status and list of matching chunk results with metadata.
    """
    return backend_client.search(
        query=query,
        item_key=item_key,
        chunk_type=chunk_type,
        limit=limit,
    )


@mcp.tool()
def get_document_toc(item_key: str) -> dict[str, Any]:
    """Retrieve the human- and agent-readable Table of Contents, Title, Abstract/Summary, Sections outline with word counts, and Figures/Tables catalog from toc.md.

    Automatically resolves parent item keys to converted child attachments (PDF-first priority) and materializes notes on demand.

    Args:
        item_key: Unique Zotero item key (e.g., 'ABCD1234').

    Returns:
        Dictionary containing table of contents content, title, authors, year, and sections.
    """
    return backend_client.get_toc(item_key=item_key)


@mcp.tool()
def read_document(
    item_key: str,
    asset_id: str | list[str] | None = None,
    page: int | None = None,
) -> dict[str, Any]:
    """Read full document Markdown or targeted sections, figures, or tables from the OKF bundle.

    Accepts exact asset IDs from toc.md ('s0', 's1', 'f1', 't1'), a list of IDs, or fuzzy heading names.
    When asset_id is omitted, returns the document in ~3000-word pages.
    Tables return raw CSV structured content when available; figures return synthetic captions and asset file paths.

    Args:
        item_key: Unique Zotero item key (e.g., 'ABCD1234').
        asset_id: Optional asset ID (e.g., 's1', 't1'), list of IDs, or fuzzy heading name.
        page: Optional 1-indexed page number if reading the full document paginated.

    Returns:
        Dictionary containing document assets with content, headings, word counts, and intersecting chunk IDs.
    """
    return backend_client.read_document(item_key=item_key, asset_id=asset_id, page=page)


@mcp.tool()
def get_related_items(item_key: str, ceiling: int = 25) -> dict[str, Any]:
    """Discover verified related papers and literature connections from the Zocrates knowledge graph for a given item.

    Returns target item keys, titles, relation types (human_curated, hybrid_search, shared_tags), and similarity scores.

    Args:
        item_key: Unique Zotero item key (e.g., 'ABCD1234').
        ceiling: Maximum number of related papers to return (clamped between 1 and 50).

    Returns:
        Dictionary containing verified relations and total count.
    """
    return backend_client.get_relations(item_key=item_key, ceiling=ceiling)


# ---------------------------------------------------------------------------
# FastMCP Resources
# ---------------------------------------------------------------------------


@mcp.resource("zotero://library/index", mime_type="text/markdown")
def get_library_index() -> str:
    """Return the root-level knowledge base catalog index.md listing all converted documents."""
    res = backend_client.get_catalog()
    if res.get("status") == "success":
        return str(res.get("content", ""))
    return f"Error retrieving library index: {res.get('message', 'Unknown error')}"


@mcp.resource("zotero://items/{item_key}/document.md", mime_type="text/markdown")
def get_document_markdown(item_key: str) -> str:
    """Return the complete OKF document Markdown for a given item key."""
    res = backend_client.read_document(item_key=item_key)
    if res.get("status") == "success":
        assets = res.get("assets", [])
        if assets and isinstance(assets, list):
            contents = [
                str(a.get("content", ""))
                for a in assets
                if isinstance(a, dict) and a.get("content")
            ]
            if contents:
                return "\n\n".join(contents)
        return f"(No readable document content found for {item_key}. Use get_document_toc to inspect document structure.)"
    return f"Error reading document: {res.get('message', 'Unknown error')}"


@mcp.resource("zotero://items/{item_key}/relations", mime_type="text/markdown")
def get_document_relations(item_key: str) -> str:
    """Return literature neighborhood relations for a given item key as a Markdown table."""
    res = backend_client.get_relations(item_key=item_key)
    if res.get("status") == "success":
        related = res.get("related_items", [])
        if not related:
            return f"# Related Documents for {item_key}\n\nNo verified relations found in the knowledge graph."
        lines = [
            f"# Related Documents for {item_key}",
            "",
            f"Total Relations: {res.get('total_count', len(related))}",
            "",
            "| Item Key | Title | Relation Type | Score |",
            "| :--- | :--- | :--- | :--- |",
        ]
        for item in related:
            k = item.get("item_key", "")
            t = (item.get("title") or "Untitled").replace("|", "\\|")
            rtype = item.get("relation_type", "")
            score = item.get("score")
            score_str = f"{score:.3f}" if isinstance(score, (int, float)) else "N/A"
            lines.append(f"| {k} | {t} | {rtype} | {score_str} |")
        return "\n".join(lines)
    return f"Error retrieving relations: {res.get('message', 'Unknown error')}"


# ---------------------------------------------------------------------------
# FastMCP Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def literature_review(topic: str, max_papers: int = 5) -> str:
    """Guides an AI agent through a systematic 4-step literature review workflow over the local Zotero library."""
    papers_limit = max(1, min(max_papers, 20))
    search_limit = max(5, min(papers_limit * 2, 20))
    return f"""Please conduct a rigorous academic literature review on the topic: "{topic}".

Follow this structured 4-step research methodology:

### Step 1: Broad & Targeted Retrieval
- Call `search_corpus(query="{topic}", limit={search_limit})` to identify candidate papers.
- Optionally run a search with `chunk_type="frontmatter"` to discover paper abstracts, creators, and publication metadata.

### Step 2: Metadata & Structure Triage
- For the top candidate papers (up to {papers_limit}), inspect their structure using `get_document_toc(item_key)`.
- Review their titles, authors, publication years, abstracts, and section outlines to select the most relevant sources.
- Check `get_related_items(item_key)` for connected works in the knowledge graph.

### Step 3: Targeted Reading & Evidence Extraction
- Use `read_document(item_key=..., asset_id=...)` to deeply read relevant sections (e.g., 's1', 's2'), methodology, findings, or specific tables ('t1') and figures ('f1').
- Avoid reading entire papers end-to-end unless necessary; prioritize high-evidence sections.

### Step 4: Grounded Academic Synthesis
- Synthesize the evidence directly addressing "{topic}".
- Compare and contrast methodologies, empirical findings, and theoretical perspectives across papers.
- Attribute every claim to its source using explicit Zotero item keys (e.g., `[ITEM_KEY]`).
- Conclude with identified research gaps and directions for future inquiry.
"""
