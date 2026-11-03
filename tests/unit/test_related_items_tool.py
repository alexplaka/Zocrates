"""Unit tests for get_related_items tool in app.agents.common.tools (Phase 2).

Tests budget enforcement, invalid key handling, bundle resolution,
orphan zero-state, multi-relation ordering & coalescing, parent key resolution,
budget notice propagation, and 25-relation internal ceiling.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.agents.common.state_keys import (
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_SEARCH_REPORT,
)
from zocrates.agents.common.tools import get_related_items
from zocrates.agents.common.toolkit import get_related_items_core
from zocrates.rag.document_processor import _open_meta_db


class MockToolContext:
    """Mock ADK ToolContext for retrieval budget and session state testing."""

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}
        self.actions = MagicMock()


@pytest.fixture
def test_cache(tmp_path: Path):
    """Set up a temporary markdown cache with initialized conversion_meta.db."""
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    with _open_meta_db(cache_dir) as conn:
        conn.commit()
    return cache_dir


def _setup_mock_syncer(cache_dir: Path):
    """Create a mock syncer pointing to cache_dir."""
    mock_processor = MagicMock()
    mock_processor._cache_dir = cache_dir

    mock_db = MagicMock()
    mock_db.get_note_by_key.return_value = None
    mock_db.get_item_by_key.return_value = None

    mock_syncer = MagicMock()
    mock_syncer.processor = mock_processor
    mock_syncer.db = mock_db
    return mock_syncer


class TestGetRelatedItemsTool:
    """Comprehensive test suite for get_related_items tool."""

    def test_invalid_item_key(self):
        """Invalid or traversal item keys return an error status."""
        for invalid_key in ("", "../etc/passwd", "INVALID KEY", "KEY!@#", None):
            res = get_related_items(invalid_key)  # type: ignore[arg-type]
            assert res["status"] == "error"
            assert "Invalid item key" in res["message"]

    def test_budget_exhausted(self, test_cache: Path):
        """When turn retrieval budget is exhausted, call is blocked."""
        mock_ctx = MockToolContext(
            {
                TEMP_RETRIEVAL_TOOL_COUNT: 10,
                TEMP_RETRIEVAL_TOOL_BUDGET: 10,
            }
        )
        res = get_related_items("DOC1", tool_context=mock_ctx)
        assert res["status"] == "budget_exhausted"
        assert "Retrieval tool budget reached" in res["error"]

    def test_bundle_not_found(self, test_cache: Path):
        """When document bundle does not exist on disk, returns not found error."""
        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items("NOTFOUND1")
            assert res["status"] == "error"
            assert "not found or not yet converted" in res["message"]

    def test_orphan_paper_returns_empty_relations(self, test_cache: Path):
        """Paper with a converted bundle but 0 relations returns empty list."""
        bundle_dir = test_cache / "ORPHAN1"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Orphan Paper\n", encoding="utf-8")

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items("ORPHAN1")

        assert res["status"] == "success"
        assert res["item_key"] == "ORPHAN1"
        assert res["total_count"] == 0
        assert res["related_items"] == []

    def test_multiple_relations_ordering_and_coalescing(self, test_cache: Path):
        """Verifies score DESC, target_key ASC ordering, multi-relation coalescing, and parent key resolution."""
        # 1. Create bundles for source and targets
        for key in ("SRC1", "TARGET_A", "TARGET_B", "TARGET_C"):
            b_dir = test_cache / key
            b_dir.mkdir(parents=True, exist_ok=True)
            (b_dir / "document.md").write_text(f"# Paper {key}\n", encoding="utf-8")

        # 2. Seed conversion_meta and item_relations
        with _open_meta_db(test_cache) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                    content_type, parent_item_type, converted_at, source_hash, char_count, title, parent_item_key)
                VALUES
                    ('SRC1', 'test', '1.0', 'p1', 'doc', 'item', 'now', 'h1', 10, 'Source Paper', 'PARENT_SRC'),
                    ('TARGET_A', 'test', '1.0', 'p2', 'doc', 'item', 'now', 'h2', 10, 'Paper Alpha', 'PARENT_A'),
                    ('TARGET_B', 'test', '1.0', 'p3', 'doc', 'item', 'now', 'h3', 10, 'Paper Beta', 'PARENT_B'),
                    ('TARGET_C', 'test', '1.0', 'p4', 'doc', 'item', 'now', 'h4', 10, 'Paper Gamma', NULL)
                """
            )
            conn.execute(
                """
                INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
                VALUES
                    ('SRC1', 'TARGET_B', 0.70, 'shared_tags', '{}', 'now'),
                    ('SRC1', 'TARGET_B', 0.85, 'hybrid_search', '{}', 'now'),
                    ('SRC1', 'TARGET_A', 0.95, 'human_curated', '{}', 'now'),
                    ('SRC1', 'TARGET_C', 0.85, 'hybrid_search', '{}', 'now')
                """
            )
            conn.commit()

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items("SRC1")

        assert res["status"] == "success"
        assert res["item_key"] == "SRC1"
        assert res["total_count"] == 3
        items = res["related_items"]

        # 1st item: TARGET_A (score 0.95)
        assert items[0]["item_key"] == "TARGET_A"
        assert items[0]["title"] == "Paper Alpha"
        assert items[0]["relation_type"] == "human_curated"
        assert items[0]["score"] == 0.95
        assert items[0]["parent_item_key"] == "PARENT_A"

        # 2nd item: TARGET_B (coalesced score 0.85, relation_type hybrid_search; B < C alphabetically)
        assert items[1]["item_key"] == "TARGET_B"
        assert items[1]["title"] == "Paper Beta"
        assert items[1]["relation_type"] == "hybrid_search"
        assert items[1]["score"] == 0.85
        assert items[1]["parent_item_key"] == "PARENT_B"

        # 3rd item: TARGET_C (score 0.85; NULL parent_item_key)
        assert items[2]["item_key"] == "TARGET_C"
        assert items[2]["title"] == "Paper Gamma"
        assert items[2]["relation_type"] == "hybrid_search"
        assert items[2]["score"] == 0.85
        assert items[2]["parent_item_key"] is None

    def test_parent_key_resolution_and_notice(self, test_cache: Path):
        """Resolving parent item key maps to attachment key and propagates disambiguation notice."""
        bundle_dir = test_cache / "ATT001"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Att Document\n", encoding="utf-8")

        mock_syncer = _setup_mock_syncer(test_cache)
        # Simulate _resolve_bundle_dir returning resolved attachment key and disambiguation notice
        with patch(
            "zocrates.agents.common.toolkit._resolve_bundle_dir",
            return_value=(bundle_dir, "ATT001", "Parent item has multiple attachments; resolved to ATT001."),
        ):
            res = get_related_items("PARENT_ITEM")

        assert res["status"] == "success"
        assert res["item_key"] == "ATT001"
        assert res["notice"] == "Parent item has multiple attachments; resolved to ATT001."

    def test_budget_notice_propagation(self, test_cache: Path):
        """Penultimate tool budget warning is propagated in budget_notice field."""
        bundle_dir = test_cache / "DOC1"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Document One\n", encoding="utf-8")

        mock_ctx = MockToolContext(
            {
                TEMP_RETRIEVAL_TOOL_COUNT: 8,
                TEMP_RETRIEVAL_TOOL_BUDGET: 10,
                TEMP_SEARCH_REPORT: None,
            }
        )

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items("DOC1", tool_context=mock_ctx)

        assert res["status"] == "success"
        assert mock_ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 9
        assert "budget_notice" in res
        assert "9/10" in res["budget_notice"]

    def test_ceiling_25_relations(self, test_cache: Path):
        """Tool enforces internal ceiling of 25 relations when many edges exist."""
        bundle_dir = test_cache / "HUB_PAPER"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Hub Paper\n", encoding="utf-8")

        with _open_meta_db(test_cache) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                    content_type, parent_item_type, converted_at, source_hash, char_count, title)
                VALUES ('HUB_PAPER', 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, 'Hub Paper')
                """
            )
            # Insert 30 distinct relations
            for i in range(1, 31):
                t_key = f"TARGET_{i:02d}"
                conn.execute(
                    """
                    INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                        content_type, parent_item_type, converted_at, source_hash, char_count, title)
                    VALUES (?, 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, ?)
                    """,
                    (t_key, f"Target Paper {i}"),
                )
                conn.execute(
                    """
                    INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
                    VALUES ('HUB_PAPER', ?, ?, 'hybrid_search', '{}', 'now')
                    """,
                    (t_key, 0.50 + (i * 0.01)),
                )
            conn.commit()

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items("HUB_PAPER")

        assert res["status"] == "success"
        assert res["total_count"] == 25
        assert len(res["related_items"]) == 25
        # Highest score was TARGET_30 with 0.80
        assert res["related_items"][0]["item_key"] == "TARGET_30"
        assert res["related_items"][0]["score"] == 0.8

    def test_strict_display_precedence_coalescing(self, test_cache: Path):
        """Verifies strict display precedence:
        human_curated_and_hybrid_search > human_curated > hybrid_search > shared_tags.
        E.g. hybrid_search (0.55) wins over shared_tags (0.70).
        """
        bundle_dir = test_cache / "PREC_SRC"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Precedence Paper\n", encoding="utf-8")

        for key in ("TARGET_X", "TARGET_Y"):
            b_dir = test_cache / key
            b_dir.mkdir(parents=True, exist_ok=True)
            (b_dir / "document.md").write_text(f"# Paper {key}\n", encoding="utf-8")

        with _open_meta_db(test_cache) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                    content_type, parent_item_type, converted_at, source_hash, char_count, title)
                VALUES
                    ('PREC_SRC', 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, 'Src'),
                    ('TARGET_X', 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, 'Paper X'),
                    ('TARGET_Y', 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, 'Paper Y')
                """
            )
            # TARGET_X has both shared_tags (0.70) and hybrid_search (0.55) -> hybrid_search must win
            conn.execute(
                """
                INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
                VALUES
                    ('PREC_SRC', 'TARGET_X', 0.70, 'shared_tags', '{}', 'now'),
                    ('PREC_SRC', 'TARGET_X', 0.55, 'hybrid_search', '{}', 'now')
                """
            )
            # TARGET_Y has both human_curated (1.0) and human_curated_and_hybrid_search (1.0) -> dual must win
            conn.execute(
                """
                INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
                VALUES
                    ('PREC_SRC', 'TARGET_Y', 1.0, 'human_curated', '{}', 'now'),
                    ('PREC_SRC', 'TARGET_Y', 1.0, 'human_curated_and_hybrid_search', '{}', 'now')
                """
            )
            conn.commit()

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items("PREC_SRC")

        assert res["status"] == "success"
        by_key = {it["item_key"]: it for it in res["related_items"]}
        assert by_key["TARGET_X"]["relation_type"] == "hybrid_search"
        assert by_key["TARGET_X"]["score"] == 0.55
        assert by_key["TARGET_Y"]["relation_type"] == "human_curated_and_hybrid_search"
        assert by_key["TARGET_Y"]["score"] == 1.0

    def test_get_document_toc_related_items_injection(self, test_cache: Path):
        """Verifies get_document_toc injects **Related Items**: N dynamically into toc_content
        without storing it in tool_context.state[CACHED_TOCS].
        """
        from zocrates.agents.common.state_keys import CACHED_TOCS
        from zocrates.agents.common.tools import get_document_toc

        bundle_dir = test_cache / "TOC_SRC"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        doc_md = "# TOC Paper\n\nSome lead intro.\n\n## Section One\nBody content.\n"
        (bundle_dir / "document.md").write_text(doc_md, encoding="utf-8")
        toc_md = "# TOC Paper\n\n**Abstract** (zotero):\nSome lead intro.\n\n## Table of Contents\n- [s1] ## Section One (~2 words)\n"
        (bundle_dir / "toc.md").write_text(toc_md, encoding="utf-8")

        with _open_meta_db(test_cache) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                    content_type, parent_item_type, converted_at, source_hash, char_count, title)
                VALUES ('TOC_SRC', 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, 'TOC Paper')
                """
            )
            conn.execute(
                """
                INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
                VALUES
                    ('TOC_SRC', 'N1', 0.88, 'hybrid_search', '{}', 'now'),
                    ('TOC_SRC', 'N2', 1.0, 'human_curated', '{}', 'now')
                """
            )
            conn.commit()

        mock_syncer = _setup_mock_syncer(test_cache)
        mock_ctx = MockToolContext({CACHED_TOCS: {}})

        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_document_toc("TOC_SRC", tool_context=mock_ctx)

        assert res["status"] == "success"
        toc_content = res["toc_content"]
        assert "**Related Items**: 2" in toc_content
        assert toc_content.index("**Related Items**: 2") < toc_content.index("## Table of Contents")

        # Verify cached outline does NOT contain the dynamic Related Items count
        cached_entry = mock_ctx.state[CACHED_TOCS].get("TOC_SRC")
        assert cached_entry is not None
        assert "**Related Items**" not in cached_entry["outline_text"]


class TestGetRelatedItemsCore:
    """Targeted unit tests for get_related_items_core pure retrieval function."""

    def test_invalid_item_key(self):
        """Invalid keys return an error dictionary."""
        for invalid_key in ("", "../etc/passwd", "BAD KEY", "KEY!@#", None):
            res = get_related_items_core(invalid_key)  # type: ignore[arg-type]
            assert res["status"] == "error"
            assert "Invalid item key" in res["message"]

    def test_bundle_not_found(self, test_cache: Path):
        """When document bundle does not exist on disk, returns not found error."""
        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res = get_related_items_core("NOTFOUND_CORE")
            assert res["status"] == "error"
            assert "not found or not yet converted" in res["message"]

    def test_custom_ceiling_parameter(self, test_cache: Path):
        """Verifies get_related_items_core respects custom ceiling."""
        bundle_dir = test_cache / "CORE_HUB"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Core Hub\n", encoding="utf-8")

        with _open_meta_db(test_cache) as conn:
            conn.execute(
                """
                INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                    content_type, parent_item_type, converted_at, source_hash, char_count, title)
                VALUES ('CORE_HUB', 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, 'Core Hub')
                """
            )
            for i in range(1, 11):
                t_key = f"TARGET_{i:02d}"
                conn.execute(
                    """
                    INSERT INTO conversion_meta (item_key, converter_id, converter_version, source_path,
                        content_type, parent_item_type, converted_at, source_hash, char_count, title)
                    VALUES (?, 'test', '1.0', 'p', 'doc', 'item', 'now', 'h', 10, ?)
                    """,
                    (t_key, f"Target {i}"),
                )
                conn.execute(
                    """
                    INSERT INTO item_relations (source_key, target_key, score, relation_type, match_evidence, updated_at)
                    VALUES ('CORE_HUB', ?, ?, 'hybrid_search', '{}', 'now')
                    """,
                    (t_key, 0.50 + (i * 0.02)),
                )
            conn.commit()

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
            res_default = get_related_items_core("CORE_HUB")
            assert res_default["status"] == "success"
            assert res_default["total_count"] == 10
            assert len(res_default["related_items"]) == 10

            res_custom = get_related_items_core("CORE_HUB", ceiling=5)
            assert res_custom["status"] == "success"
            assert res_custom["total_count"] == 5
            assert len(res_custom["related_items"]) == 5
            assert res_custom["related_items"][0]["item_key"] == "TARGET_10"

    def test_database_error_handling(self, test_cache: Path):
        """Database errors during relations query are cleanly handled with error status/message."""
        bundle_dir = test_cache / "ERR_ITEM"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "document.md").write_text("# Error Item\n", encoding="utf-8")

        mock_syncer = _setup_mock_syncer(test_cache)
        with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer), \
             patch("sqlite3.connect", side_effect=Exception("Database lock failure")):
            res = get_related_items_core("ERR_ITEM")
            assert res["status"] == "error"
            assert "Database error querying relations" in res["message"] or "Database lock failure" in res["message"]

