"""Unit tests for the shared LineIndex utility."""

import pytest

from zocrates.rag.line_indexer import LineIndex, char_offset_to_line


def test_line_index_basic_multiline():
    text = "line 1\nline 2\nline 3\n"
    idx = LineIndex(text)

    assert idx.total_lines == 3
    # "line 1\n" is len 7 (0..6)
    assert idx.char_to_line(0) == 1
    assert idx.char_to_line(6) == 1  # newline of line 1
    assert idx.char_to_line(7) == 2  # start of line 2
    assert idx.char_to_line(13) == 2
    assert idx.char_to_line(14) == 3 # start of line 3
    assert idx.char_to_line(20) == 3


def test_line_index_span_to_lines():
    text = "first line\nsecond line\nthird line\nfourth line"
    idx = LineIndex(text)

    # Within first line
    assert idx.span_to_lines(0, 5) == (1, 1)
    # Span first two lines
    assert idx.span_to_lines(0, 15) == (1, 2)
    # Span across middle lines
    start_sec = text.index("second")
    end_third = text.index("third") + len("third line")
    assert idx.span_to_lines(start_sec, end_third) == (2, 3)


def test_line_index_empty_and_single_char():
    idx_empty = LineIndex("")
    assert idx_empty.total_lines == 1
    assert idx_empty.char_to_line(0) == 1
    assert idx_empty.span_to_lines(0, 0) == (1, 1)

    idx_one = LineIndex("a")
    assert idx_one.total_lines == 1
    assert idx_one.char_to_line(0) == 1
    assert idx_one.span_to_lines(0, 1) == (1, 1)


def test_line_index_crlf_windows_newlines():
    text = "Windows\r\nLine 2\r\nLine 3"
    idx = LineIndex(text)

    assert idx.total_lines == 3
    # "Windows\r\n" is 9 chars
    assert idx.char_to_line(0) == 1
    assert idx.char_to_line(8) == 1
    assert idx.char_to_line(9) == 2
    assert idx.span_to_lines(0, 10) == (1, 2)


def test_line_index_clamping_out_of_bounds():
    text = "hello\nworld"
    idx = LineIndex(text)

    assert idx.char_to_line(-5) == 1
    assert idx.char_to_line(9999) == 2
    assert idx.span_to_lines(-10, 1000) == (1, 2)


def test_char_offset_to_line_compat_helper():
    offsets = [0, 10, 25, 40]
    assert char_offset_to_line(offsets, 0) == 1
    assert char_offset_to_line(offsets, 5) == 1
    assert char_offset_to_line(offsets, 10) == 2
    assert char_offset_to_line(offsets, 24) == 2
    assert char_offset_to_line(offsets, 25) == 3
    assert char_offset_to_line(offsets, 50, total_lines=4) == 4


def test_line_index_inverted_span():
    text = "first line\nsecond line\nthird line"
    idx = LineIndex(text)
    assert idx.span_to_lines(15, 0) == (1, 2)


def test_existing_cached_bundles_toc_identity():
    """Verify that generate_toc produces 100% identical line numbers on any existing bundles."""
    import json
    from pathlib import Path
    from zocrates.rag.toc_generator import generate_toc

    zocrates_data = Path("/home/alex/Zotero/Zotero test-user/zocrates-data")
    cache_dir = zocrates_data / "corpus" if (zocrates_data / "corpus").exists() else zocrates_data / "markdown_cache"
    if not cache_dir.exists():
        pytest.skip("No local Zotero test user cache found.")

    toc_files = list(cache_dir.glob("*/toc.json"))
    if not toc_files:
        pytest.skip("No toc.json files found.")

    for toc_file in toc_files:
        doc_file = toc_file.parent / "document.md"
        if not doc_file.exists():
            continue

        with open(toc_file, encoding="utf-8") as f:
            existing_toc = json.load(f)

        document_md = doc_file.read_text(encoding="utf-8")
        generated_toc, _ = generate_toc(document_md, title=existing_toc.get("title", ""))

        # Check section line bounds
        existing_sections = {s["id"]: s for s in existing_toc.get("sections", [])}
        for gen_sec in generated_toc.get("sections", []):
            s_id = gen_sec["id"]
            if s_id in existing_sections:
                assert gen_sec["start_line"] == existing_sections[s_id]["start_line"]
                assert gen_sec["end_line"] == existing_sections[s_id]["end_line"]

        # Check figure line bounds
        existing_figs = {f.get("id"): f for f in existing_toc.get("figures", []) if f.get("id")}
        for gen_fig in generated_toc.get("figures", []):
            f_id = gen_fig.get("id")
            if f_id in existing_figs:
                assert gen_fig["start_line"] == existing_figs[f_id]["start_line"]
                assert gen_fig["end_line"] == existing_figs[f_id]["end_line"]

        # Check table line bounds
        existing_tables = {t.get("id"): t for t in existing_toc.get("tables", []) if t.get("id")}
        for gen_tab in generated_toc.get("tables", []):
            t_id = gen_tab.get("id")
            if t_id in existing_tables:
                assert gen_tab["start_line"] == existing_tables[t_id]["start_line"]
                assert gen_tab["end_line"] == existing_tables[t_id]["end_line"]

