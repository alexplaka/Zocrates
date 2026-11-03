"""
Zero-dependency 5-feature statistical domain keyword extractor and tag retrieval.

Extracts high-precision keyphrases deterministically from academic text and structural
digests in < 0.2 ms on CPU using standard library primitives only (re, math, collections).
Supplements extracted keyphrases with native and user tags from Zotero SQLite.
"""

import logging
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

# Workflow tags that provide zero topical signal
GENERIC_TAGS: set[str] = {
    "read",
    "to-read",
    "toread",
    "unread",
    "favorite",
    "important",
    "_reading",
}

# Standard English stopwords plus academic functional stopwords
STOPWORDS: set[str] = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "aren",
    "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by",
    "can", "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him", "himself",
    "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me", "more", "most",
    "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other", "our",
    "ours", "ourselves", "out", "over", "own", "same", "she", "should", "so", "some", "such", "than",
    "that", "the", "their", "theirs", "them", "themselves", "then", "there", "these", "they", "this",
    "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were", "what", "when",
    "where", "which", "while", "who", "whom", "why", "with", "would", "you", "your", "yours", "yourself",
    # Academic functional stopwords (IMRAD & rhetorical framing)
    "study", "propose", "paper", "article", "present", "show", "using", "based", "approach", "novel",
    "results", "method", "methods", "methodology", "work", "one", "two", "three", "also", "however", "although",
    "introduction", "background", "discussion", "conclusion", "conclusions", "references", "preliminaries",
    "et", "al", "fig", "figure", "table", "section", "sec", "given", "let", "may", "see", "since", "well",
    "http", "https", "com", "org", "www", "html", "pdf", "url", "like", "get", "make",
}

# Generic academic section headers to discard from structural digests
GENERIC_SECTION_HEADERS: set[str] = {
    "abstract", "introduction", "background", "preliminaries", "related work",
    "methods", "method", "materials and methods", "methodology",
    "results", "discussion", "results and discussion", "conclusion", "conclusions",
    "references", "bibliography", "acknowledgments", "acknowledgements",
    "data availability", "code availability", "online content", "supplementary material",
    "appendix", "overview", "setup", "table of contents", "author contributions",
}


def clean_markdown_for_kw(text: str) -> str:
    """Strip code blocks, URLs, HTML tags, LaTeX math formulas, and link markup.

    Args:
        text: Raw Markdown text.

    Returns:
        Cleaned text suitable for keyword extraction.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"\$\$?[^\$]+\$\$?", " ", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return text


def is_generic_section_header(h: str) -> bool:
    """Check if a section heading is standard academic boilerplate (IMRAD, References, etc.).

    Args:
        h: Raw header string (e.g. '## 1. Introduction').

    Returns:
        True if the header is generic and should be excluded.
    """
    clean = re.sub(r"^[0-9.\s#\-]+", "", h).strip().lower()
    return clean in GENERIC_SECTION_HEADERS or len(clean) < 3


def extract_structural_digest(doc_text: str, max_lead: int = 2000) -> str:
    """Extract lead abstract/intro plus topical H1/H2/H3 section headers.

    Filters out standard academic IMRAD boilerplate headers so only substantive
    topical section headings are retained alongside lead text.

    Args:
        doc_text: Full document markdown string.
        max_lead: Maximum character length for lead prose.

    Returns:
        Structural digest string combining lead text and topical section headers.
    """
    # Strip YAML frontmatter if present
    body = re.sub(r"^---\n.*?\n---\n", "", doc_text, flags=re.DOTALL)
    cleaned = clean_markdown_for_kw(body)

    # Extract section headers
    headers = re.findall(r"^#{1,3}\s+(.+)$", cleaned, flags=re.MULTILINE)
    topical_headers = [h.strip() for h in headers if not is_generic_section_header(h)]

    # Extract lead paragraphs
    lead = cleaned[:max_lead].strip()
    header_block = "\n".join(topical_headers)

    return f"{lead}\n\n{header_block}" if header_block else lead


def extract_statistical_keywords(title: str, text: str, max_keywords: int = 6) -> list[str]:
    """Pure-Python 5-feature statistical keyphrase extractor (RAKE + YAKE principles).

    Extracts high-precision compound keyphrases in < 0.2 ms on CPU using:
    1. Co-occurrence degree / frequency ratio.
    2. Casing & acronym multipliers (1.8x ALL_CAPS, 1.3x proper nouns).
    3. Title positional boost (2.0x).
    4. Sentence spread log-multiplier.
    5. Length normalization favoring 2-3 word technical noun phrases.

    Args:
        title: Document title.
        text: Text to extract from (e.g. structural digest or abstract).
        max_keywords: Maximum number of keywords to return.

    Returns:
        List of extracted keyphrase strings.
    """
    full_text = f"{title}. {text}" if title else text
    sentences = [s.strip() for s in re.split(r"[\n.!?]+", full_text) if s.strip()]
    if not sentences:
        return []

    title_words = set(re.findall(r"\b[A-Za-z0-9\-_]+\b", title.lower())) if title else set()
    sentence_phrases: list[tuple[str, ...]] = []
    word_casing_boost: dict[str, float] = {}
    word_sentence_counts: dict[str, set[int]] = defaultdict(set)

    for sent_idx, sent in enumerate(sentences):
        raw_tokens = re.findall(r"\b[A-Za-z0-9\-_]+\b", sent)
        curr_phrase: list[str] = []
        for raw_tok in raw_tokens:
            clean_tok = raw_tok.lower()
            if clean_tok not in STOPWORDS and not clean_tok.isdigit() and len(clean_tok) > 2:
                curr_phrase.append(clean_tok)
                # Feature 1: Casing & Acronym boost
                if raw_tok.isupper() and len(raw_tok) >= 2:
                    word_casing_boost[clean_tok] = max(word_casing_boost.get(clean_tok, 1.0), 1.8)
                elif raw_tok[0].isupper() and sent_idx > 0:
                    word_casing_boost[clean_tok] = max(word_casing_boost.get(clean_tok, 1.0), 1.3)
                else:
                    word_casing_boost.setdefault(clean_tok, 1.0)
                word_sentence_counts[clean_tok].add(sent_idx)
            else:
                if curr_phrase and len(curr_phrase) <= 4:
                    sentence_phrases.append(tuple(curr_phrase))
                curr_phrase = []
        if curr_phrase and len(curr_phrase) <= 4:
            sentence_phrases.append(tuple(curr_phrase))

    if not sentence_phrases:
        return []

    word_freq: Counter[str] = Counter()
    word_degree: Counter[str] = Counter()
    for phrase in sentence_phrases:
        length = len(phrase)
        for word in phrase:
            word_freq[word] += 1
            word_degree[word] += length

    term_scores: dict[str, float] = {}
    for w in word_freq:
        base_rake = word_degree[w] / word_freq[w]
        casing = word_casing_boost.get(w, 1.0)
        pos_boost = 2.0 if w in title_words else 1.0
        spread = 1.0 + math.log(1 + len(word_sentence_counts[w]))
        term_scores[w] = base_rake * casing * pos_boost * spread

    phrase_scores: dict[str, float] = {}
    for phrase in sentence_phrases:
        p_str = " ".join(phrase)
        length_factor = 1.0 if len(phrase) in (2, 3) else (0.85 if len(phrase) == 1 else 0.75)
        raw_sum = sum(term_scores[w] for w in phrase)
        phrase_scores[p_str] = raw_sum * length_factor

    sorted_phrases = sorted(phrase_scores.items(), key=lambda x: -x[1])
    results: list[str] = []
    seen_words: set[str] = set()
    for p_str, _ in sorted_phrases:
        words = set(p_str.split())
        if not (words & seen_words):
            results.append(p_str)
            seen_words.update(words)
        elif len(words - seen_words) >= 2:
            results.append(p_str)
            seen_words.update(words)
        if len(results) >= max_keywords:
            break

    return results


def get_item_tags(
    zotero_dir: Path | str,
    item_id: int | None = None,
    item_key: str | None = None,
    tag_type: int | None = None,
) -> list[str]:
    """Retrieve user and automatic tags for an item from zotero.sqlite.

    Excludes generic workflow tags (e.g. 'read', 'unread', 'to-read', 'favorite').
    If item is an attachment, also resolves tags from its parent item.

    Args:
        zotero_dir: Path to the root Zotero data directory.
        item_id: Optional integer itemID in zotero.sqlite.
        item_key: Optional string key in zotero.sqlite (items.key).
        tag_type: Optional integer tag type filter (0 = user/manual tag, 1 = automatic tag).
            If None (default), returns all tag types.

    Returns:
        List of non-generic tag name strings.
    """
    if item_id is None and item_key is None:
        return []

    zotero_path = Path(zotero_dir)
    sqlite_path = zotero_path / "zotero.sqlite"
    if not sqlite_path.exists():
        return []

    tags: list[str] = []
    seen: set[str] = set()

    try:
        from zocrates.rag.zotero_db import _open_db

        with _open_db(sqlite_path) as conn:
            target_item_ids: list[int] = []
            if item_id is not None:
                target_item_ids.append(item_id)
                try:
                    parent_row = conn.execute(
                        "SELECT parentItemID FROM itemAttachments WHERE itemID = ?",
                        (item_id,),
                    ).fetchone()
                    if parent_row and parent_row["parentItemID"]:
                        target_item_ids.append(parent_row["parentItemID"])
                except Exception:
                    pass
            elif item_key is not None:
                try:
                    row = conn.execute(
                        "SELECT itemID FROM items WHERE key = ?",
                        (item_key,),
                    ).fetchone()
                    if row and row["itemID"]:
                        target_item_ids.append(row["itemID"])
                        try:
                            parent_row = conn.execute(
                                "SELECT parentItemID FROM itemAttachments WHERE itemID = ?",
                                (row["itemID"],),
                            ).fetchone()
                            if parent_row and parent_row["parentItemID"]:
                                target_item_ids.append(parent_row["parentItemID"])
                        except Exception:
                            pass
                except Exception:
                    pass

            if not target_item_ids:
                return []

            placeholders = ",".join("?" for _ in target_item_ids)
            sql_params: list[Any] = list(target_item_ids)
            type_clause = ""
            if tag_type is not None:
                cols = {c[1] for c in conn.execute("PRAGMA table_info(itemTags)").fetchall()}
                if "type" in cols:
                    type_clause = " AND it.type = ?"
                    sql_params.append(tag_type)

            sql = f"""
                SELECT t.name
                FROM itemTags it
                JOIN tags t ON it.tagID = t.tagID
                WHERE it.itemID IN ({placeholders}){type_clause}
            """
            rows = conn.execute(sql, sql_params).fetchall()
            for r in rows:
                tag_name = str(r["name"]).strip()
                if tag_name.lower() not in GENERIC_TAGS and tag_name.lower() not in seen:
                    tags.append(tag_name)
                    seen.add(tag_name.lower())
    except Exception as exc:
        logger.warning("Could not fetch tags from %s: %s", sqlite_path, exc)

    return tags


def build_search_query(
    title: str,
    keywords: list[str],
    tags: list[str] | None = None,
) -> str:
    """Build a deduplicated, high-signal search query combining title, keywords, and tags.

    Guarantees high-IDF lexical coverage without repeating words or bloating the query.

    Args:
        title: Document title.
        keywords: Extracted statistical keywords.
        tags: Optional user or automatic Zotero tags.

    Returns:
        Deduplicated space-separated query string capped at 35 terms.
    """
    clean_title = re.sub(r"[^\w\s\-]", " ", title or "").strip()
    title_words = [w for w in clean_title.split() if len(w) > 1]

    seen_lower = {w.lower() for w in title_words}
    query_parts = list(title_words)

    extra_items = list(keywords or [])
    if tags:
        extra_items.extend(tags)

    for item in extra_items:
        clean_item = re.sub(r"[^\w\s\-]", " ", str(item)).strip()
        item_words = [w for w in clean_item.split() if len(w) > 1]
        new_words = [w for w in item_words if w.lower() not in seen_lower]
        if new_words:
            query_parts.extend(new_words)
            seen_lower.update(w.lower() for w in new_words)

    return " ".join(query_parts[:35])
