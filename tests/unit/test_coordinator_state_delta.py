"""Unit tests for dialogue endpoint state_delta resolution and active context items."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from zocrates.agents.common.prompts import format_active_items
from zocrates.agents.common.state_keys import USER_PROMPT, USER_RESEARCH_PROFILE_STR
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.rag.zotero_db import ZoteroDB, ZoteroItem


@pytest.fixture(autouse=True)
def reset_sse_app_status():
    """Reset sse_starlette AppStatus should_exit_event before each test."""
    import sse_starlette.sse

    sse_starlette.sse.AppStatus.should_exit_event = None
    yield
    sse_starlette.sse.AppStatus.should_exit_event = None


@pytest.fixture
def client():
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


def test_format_active_items():
    """Verify format_active_items handles item_key, key, None, and empty list."""
    assert format_active_items(None) == "No active paper is currently selected in the Zotero UI."
    assert format_active_items([]) == "No active paper is currently selected in the Zotero UI."

    # Using item_key
    items_with_item_key = [{"item_key": "ABC123", "title": "Paper One"}]
    formatted = format_active_items(items_with_item_key)
    assert '- [Key: ABC123] Title: "Paper One"' in formatted

    # Using key
    items_with_key = [{"key": "DEF456", "title": "Paper Two"}]
    formatted = format_active_items(items_with_key)
    assert '- [Key: DEF456] Title: "Paper Two"' in formatted


class TestDialogueEndpointStateDelta:
    """Test state_delta passed to runner.run_async in dialogue_endpoint."""

    def _setup_mock_runner(self):
        captured_state_deltas = []

        async def fake_run_async(*args, **kwargs):
            captured_state_deltas.append(kwargs.get("state_delta"))
            mock_event = MagicMock()
            mock_event.id = "evt-1"
            mock_event.author = "agent"
            mock_event.is_final_response.return_value = True
            mock_event.content.parts = [MagicMock(text="Hello", thought=None, function_call=None, function_response=None)]
            yield mock_event

        mock_runner = MagicMock()
        mock_runner.run_async = fake_run_async
        return mock_runner, captured_state_deltas

    def test_state_delta_with_item_selection(self, client):
        """Single or multiple item selection produces correct active_context_items and active_context_items_str."""
        mock_runner, captured = self._setup_mock_runner()
        mock_syncer = MagicMock()

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            app.state.runner = mock_runner
            res = client.post(
                "/chat",
                json={
                    "prompt": "Summarize this paper",
                    "session_id": "test-session-1",
                    "active_items": [
                        {"key": "ABCDEF", "title": "Attention Is All You Need", "type": "item"},
                        {"key": "GHIJKL", "title": "BERT Paper", "type": "item"},
                    ],
                },
            )
            assert res.status_code == 200
            assert len(captured) == 1
            sd = captured[0]

            # Verify ONLY required fields exist in state_delta
            assert set(sd.keys()) == {
                "active_context_items",
                "active_context_items_str",
                "focal_snippets_str",
                "snippets",
                USER_PROMPT,
                USER_RESEARCH_PROFILE_STR,
            }
            assert sd["snippets"] == []
            assert sd["focal_snippets_str"] == ""
            assert sd[USER_PROMPT] == "Summarize this paper"
            assert sd["active_context_items"] == [
                {"item_key": "ABCDEF", "title": "Attention Is All You Need"},
                {"item_key": "GHIJKL", "title": "BERT Paper"},
            ]
            expected_str = format_active_items(sd["active_context_items"], include_keys=False)
            assert sd["active_context_items_str"] == expected_str

    def test_state_delta_with_collection_selection(self, client):
        """Collection selection resolves member keys & titles, uses collection name in header, and chains format_active_items."""
        mock_runner, captured = self._setup_mock_runner()
        mock_syncer = MagicMock()
        mock_syncer.db.get_item_keys_in_collection.side_effect = lambda identifier: (
            ["ITEM1", "ITEM2"] if identifier in ("COL1", "Machine Learning") else []
        )
        mock_syncer.db.get_item_by_key.side_effect = lambda k: {
            "ITEM1": ZoteroItem(item_id=1, item_key="ITEM1", item_type="journalArticle", title="Paper 1"),
            "ITEM2": ZoteroItem(item_id=2, item_key="ITEM2", item_type="journalArticle", title="Paper 2"),
        }.get(k)

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            app.state.runner = mock_runner
            res = client.post(
                "/chat",
                json={
                    "prompt": "What are these papers about?",
                    "session_id": "test-session-2",
                    "active_items": [
                        {"key": "COL1", "title": "Collection: Machine Learning", "type": "collection"}
                    ],
                },
            )
            assert res.status_code == 200
            assert len(captured) == 1
            sd = captured[0]

            assert set(sd.keys()) == {
                "active_context_items",
                "active_context_items_str",
                "focal_snippets_str",
                "snippets",
                USER_PROMPT,
                USER_RESEARCH_PROFILE_STR,
            }
            assert sd["snippets"] == []
            assert sd["focal_snippets_str"] == ""
            assert sd[USER_PROMPT] == "What are these papers about?"
            assert sd["active_context_items"] == [
                {"item_key": "ITEM1", "title": "Paper 1"},
                {"item_key": "ITEM2", "title": "Paper 2"},
            ]

            # Header must use collection name ("Machine Learning") not collection key ("COL1")
            assert sd["active_context_items_str"].startswith("Active selection context: Zotero Collections (Machine Learning).\n")
            assert '- Title: "Paper 1"' in sd["active_context_items_str"]
            assert '- Title: "Paper 2"' in sd["active_context_items_str"]
            assert "[Key:" not in sd["active_context_items_str"]

    def test_state_delta_with_library_selection(self, client):
        """Whole library selection sets active_context_items=None and active_context_items_str to library context string."""
        mock_runner, captured = self._setup_mock_runner()
        mock_syncer = MagicMock()

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            app.state.runner = mock_runner
            res = client.post(
                "/chat",
                json={
                    "prompt": "Search my entire library",
                    "session_id": "test-session-3",
                    "active_items": [
                        {"key": "all", "title": "My Library", "type": "library"}
                    ],
                },
            )
            assert res.status_code == 200
            assert len(captured) == 1
            sd = captured[0]

            assert set(sd.keys()) == {
                "active_context_items",
                "active_context_items_str",
                "focal_snippets_str",
                "snippets",
                USER_PROMPT,
                USER_RESEARCH_PROFILE_STR,
            }
            assert sd["snippets"] == []
            assert sd["focal_snippets_str"] == ""
            assert sd[USER_PROMPT] == "Search my entire library"
            assert sd["active_context_items"] is None
            assert sd["active_context_items_str"] == "Active selection context: Entire Zotero Library."

    def test_state_delta_with_empty_selection(self, client):
        """Empty active_items sets active_context_items=[] and formatted default string."""
        mock_runner, captured = self._setup_mock_runner()
        mock_syncer = MagicMock()

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            app.state.runner = mock_runner
            res = client.post(
                "/chat",
                json={
                    "prompt": "Hello",
                    "session_id": "test-session-4",
                    "active_items": [],
                },
            )
            assert res.status_code == 200
            assert len(captured) == 1
            sd = captured[0]

            assert set(sd.keys()) == {
                "active_context_items",
                "active_context_items_str",
                "focal_snippets_str",
                "snippets",
                USER_PROMPT,
                USER_RESEARCH_PROFILE_STR,
            }
            assert sd["snippets"] == []
            assert sd["focal_snippets_str"] == ""
            assert sd[USER_PROMPT] == "Hello"
            assert sd["active_context_items"] == []
            assert sd["active_context_items_str"] == "No active paper is currently selected in the Zotero UI."

    def test_state_delta_with_mixed_collection_and_item(self, client):
        """Mixed collection and individual item selections are merged and deduplicated."""
        mock_runner, captured = self._setup_mock_runner()
        mock_syncer = MagicMock()
        mock_syncer.db.get_item_keys_in_collection.side_effect = lambda identifier: (
            ["KEY1", "KEY2"] if identifier in ("COL_A", "NLP") else []
        )
        mock_syncer.db.get_item_by_key.side_effect = lambda k: {
            "KEY1": ZoteroItem(item_id=1, item_key="KEY1", item_type="journalArticle", title="NLP Paper 1"),
            "KEY2": ZoteroItem(item_id=2, item_key="KEY2", item_type="journalArticle", title="NLP Paper 2"),
            "KEY3": ZoteroItem(item_id=3, item_key="KEY3", item_type="journalArticle", title="Vision Paper 3"),
        }.get(k)

        with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
            app.state.runner = mock_runner
            res = client.post(
                "/chat",
                json={
                    "prompt": "Compare NLP and Vision papers",
                    "session_id": "test-session-5",
                    "active_items": [
                        {"key": "COL_A", "title": "Collection: NLP", "type": "collection"},
                        # Duplicate KEY1 should be deduplicated
                        {"key": "KEY1", "title": "NLP Paper 1", "type": "item"},
                        # New standalone item
                        {"key": "KEY3", "title": "Vision Paper 3", "type": "item"},
                    ],
                },
            )
            assert res.status_code == 200
            assert len(captured) == 1
            sd = captured[0]

            assert set(sd.keys()) == {
                "active_context_items",
                "active_context_items_str",
                "focal_snippets_str",
                "snippets",
                USER_PROMPT,
                USER_RESEARCH_PROFILE_STR,
            }
            assert sd["snippets"] == []
            assert sd["focal_snippets_str"] == ""
            assert sd[USER_PROMPT] == "Compare NLP and Vision papers"
            assert sd["active_context_items"] == [
                {"item_key": "KEY1", "title": "NLP Paper 1"},
                {"item_key": "KEY2", "title": "NLP Paper 2"},
                {"item_key": "KEY3", "title": "Vision Paper 3"},
            ]
            assert sd["active_context_items_str"].startswith("Active selection context: Zotero Collections (NLP).\n")
            assert '- Title: "NLP Paper 1"' in sd["active_context_items_str"]
            assert '- Title: "NLP Paper 2"' in sd["active_context_items_str"]
            assert '- Title: "Vision Paper 3"' in sd["active_context_items_str"]
            assert "[Key:" not in sd["active_context_items_str"]


def test_search_corpus_scoping():
    """Verify search_corpus scoping rules: None/'all' searches whole library, 'active' searches state."""
    from zocrates.agents.common.state_keys import TEMP_RETRIEVAL_TOOL_COUNT
    from zocrates.agents.common.tools import search_corpus

    mock_syncer = MagicMock()
    mock_syncer.store.search.return_value = []
    mock_syncer.db.get_item.return_value = None

    mock_tool_context = MagicMock()
    mock_tool_context.state = {
        "active_context_items": [
            {"item_key": "KEY_A", "title": "Paper A"},
            {"item_key": "KEY_B", "title": "Paper B"},
        ]
    }

    with patch("zocrates.agents.common.toolkit.get_syncer", return_value=mock_syncer):
        # 1. default (omitted item_key) -> defaults to "active", extracting active context items from state
        res0 = search_corpus("query", limit=5, tool_context=mock_tool_context)
        assert res0["status"] == "success"
        mock_syncer.store.search.assert_called_with(
            query="query",
            final_k=5,
            item_key_filter=["KEY_A", "KEY_B"],
            chunk_type_filter=None,
            exclude_chunk_ids=None,
        )

        # 2. item_key=None -> entire library (no filter)
        res1 = search_corpus("query", limit=5, item_key=None, tool_context=mock_tool_context)
        assert res1["status"] == "success"
        mock_syncer.store.search.assert_called_with(
            query="query",
            final_k=5,
            item_key_filter=None,
            chunk_type_filter=None,
            exclude_chunk_ids=None,
        )

        # 3. item_key="all" -> entire library
        res3 = search_corpus("query", limit=5, item_key="all", tool_context=mock_tool_context)
        assert res3["status"] == "success"
        mock_syncer.store.search.assert_called_with(
            query="query",
            final_k=5,
            item_key_filter=None,
            chunk_type_filter=None,
            exclude_chunk_ids=None,
        )

        # 4. item_key="KEY_A" -> explicit filter (reset budget count first)
        mock_tool_context.state[TEMP_RETRIEVAL_TOOL_COUNT] = 0
        res4 = search_corpus("query", limit=5, item_key="KEY_A", tool_context=mock_tool_context)
        assert res4["status"] == "success"
        mock_syncer.store.search.assert_called_with(
            query="query",
            final_k=5,
            item_key_filter=["KEY_A"],
            chunk_type_filter=None,
            exclude_chunk_ids=None,
        )


def test_dialogue_endpoint_mode_ignored(client):
    """Verify chat endpoint ignores legacy mode parameter and does not set active_mode in runner state_delta."""
    captured_state_deltas = []

    async def fake_run_async(*args, **kwargs):
        captured_state_deltas.append(kwargs.get("state_delta"))
        mock_event = MagicMock()
        mock_event.id = "evt-1"
        mock_event.author = "agent"
        mock_event.is_final_response.return_value = True
        mock_event.content.parts = [MagicMock(text="Hello", thought=None, function_call=None, function_response=None)]
        yield mock_event

    mock_runner = MagicMock()
    mock_runner.run_async = fake_run_async
    mock_syncer = MagicMock()

    with patch("zocrates.fast_api_app.get_syncer", return_value=mock_syncer):
        app.state.runner = mock_runner
        res = client.post(
            "/chat",
            json={
                "prompt": "Discover new papers",
                "session_id": "test-session-disc",
                "mode": "discovery",
            },
        )
        assert res.status_code == 200
        assert len(captured_state_deltas) == 1
        assert "active_mode" not in captured_state_deltas[0]


def test_zotero_db_get_item_keys_in_collection(tmp_path):
    """Verify get_item_keys_in_collection matches either collection name or collection key."""
    import sqlite3

    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            key TEXT NOT NULL,
            itemTypeID INTEGER DEFAULT 1
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE collections (
            collectionID INTEGER PRIMARY KEY,
            collectionName TEXT NOT NULL,
            key TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE collectionItems (
            collectionID INTEGER,
            itemID INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE deletedItems (
            itemID INTEGER PRIMARY KEY
        )
        """
    )
    # Insert collection: name="Deep Learning", key="COL_DL"
    conn.execute("INSERT INTO collections VALUES (1, 'Deep Learning', 'COL_DL')")
    # Insert items: item1 (KEY1), item2 (KEY2), item3 deleted (KEY3)
    conn.execute("INSERT INTO items VALUES (10, 'KEY1', 1)")
    conn.execute("INSERT INTO items VALUES (20, 'KEY2', 1)")
    conn.execute("INSERT INTO items VALUES (30, 'KEY3', 1)")
    conn.execute("INSERT INTO deletedItems VALUES (30)")

    conn.execute("INSERT INTO collectionItems VALUES (1, 10)")
    conn.execute("INSERT INTO collectionItems VALUES (1, 20)")
    conn.execute("INSERT INTO collectionItems VALUES (1, 30)")
    conn.commit()
    conn.close()

    storage_dir = tmp_path / "storage"
    storage_dir.mkdir()
    zdb = ZoteroDB(sqlite_path=str(db_path), storage_dir=str(storage_dir))

    # Match by collectionName
    keys_by_name = zdb.get_item_keys_in_collection("Deep Learning")
    assert sorted(keys_by_name) == ["KEY1", "KEY2"]

    # Match by collection key
    keys_by_key = zdb.get_item_keys_in_collection("COL_DL")
    assert sorted(keys_by_key) == ["KEY1", "KEY2"]

    # Non-existent collection
    assert zdb.get_item_keys_in_collection("NonExistent") == []


def _make_resolver_syncer(item=None, attachments=None, notes=None, cache_dir=None):
    """Build a mocked LibrarySyncer for resolve_active_item_entry tests."""
    from pathlib import Path

    syncer = MagicMock()
    syncer.db.get_item_by_key.return_value = item
    syncer.db.get_attachments.return_value = attachments or []
    syncer.db.get_notes_for_item.return_value = notes or []
    syncer.processor._cache_dir = cache_dir or Path("/tmp/zocrates-cache")
    return syncer


def test_resolve_active_item_entry_enriched(tmp_path):
    """resolve_active_item_entry returns item dict enriched with attachment and note children."""
    from pathlib import Path

    from zocrates.agents.common.prompts import resolve_active_item_entry
    from zocrates.rag.zotero_db import ZoteroAttachment, ZoteroItem, ZoteroNote

    tmp_cache = tmp_path / "cache"
    (tmp_cache / "ATTACH1").mkdir(parents=True, exist_ok=True)
    (tmp_cache / "ATTACH1" / "document.md").write_text("# Att", encoding="utf-8")
    (tmp_cache / "MISSING_ATT").mkdir(parents=True)
    # Ensure ATTACH2 has no bundle directory at all (uncached).
    assert not (tmp_cache / "ATTACH2").exists()

    item = ZoteroItem(
        item_id=10, item_key="PARENT1", item_type="journalArticle", title="Parent Paper", year="2023"
    )
    att_cached = ZoteroAttachment(
        item_id=11,
        item_key="ATTACH1",
        parent_item_id=10,
        parent_item_key="PARENT1",
        content_type="application/pdf",
        path=Path("/fake/paper.pdf"),
        title="Full Text PDF (paper.pdf)",
    )
    att_uncached = ZoteroAttachment(
        item_id=12,
        item_key="ATTACH2",
        parent_item_id=10,
        parent_item_key="PARENT1",
        content_type="text/html",
        path=Path("/fake/snapshot.html"),
        title="Snapshot (snapshot.html)",
    )
    note = ZoteroNote(
        note_id=20,
        note_key="NOTE1",
        parent_item_id=10,
        parent_item_key="PARENT1",
        title="Reading Notes",
        note_html="<p>Short note content under fifty words.</p>",
    )

    syncer = _make_resolver_syncer(
        item=item,
        attachments=[att_cached, att_uncached],
        notes=[note],
        cache_dir=tmp_cache,
    )
    entry = resolve_active_item_entry(syncer, "PARENT1")

    assert entry["item_key"] == "PARENT1"
    assert entry["title"] == "Parent Paper"
    assert entry["year"] == "2023"
    assert len(entry["children"]) == 2

    att_child = entry["children"][0]
    assert att_child["item_key"] == "ATTACH1"
    assert att_child["is_note"] is False
    assert att_child["content_type"] == "application/pdf"
    assert att_child["title"] == "Full Text PDF (paper.pdf)"

    # Uncached attachment bundle is excluded from children.
    assert not any(c["item_key"] == "ATTACH2" for c in entry["children"])

    note_child = entry["children"][1]
    assert note_child["item_key"] == "NOTE1"
    assert note_child["is_note"] is True
    assert note_child["content_type"] == "text/note"
    assert note_child["word_count"] < 50
    assert note_child["inline_text"] is not None
    assert "Short note content" in note_child["inline_text"]


def test_resolve_active_item_entry_title_override_and_long_note():
    """Passing title overrides DB title; long notes get inline_text=None."""
    from pathlib import Path

    from zocrates.agents.common.prompts import resolve_active_item_entry
    from zocrates.rag.zotero_db import ZoteroItem, ZoteroNote

    item = ZoteroItem(item_id=5, item_key="K5", item_type="webpage", title="DB Title")
    long_note = ZoteroNote(
        note_id=30,
        note_key="LONGNOTE",
        parent_item_id=5,
        parent_item_key="K5",
        title="Long Note",
        note_html="<p>" + "word " * 60 + "</p>",
    )
    syncer = _make_resolver_syncer(item=item, notes=[long_note])

    entry = resolve_active_item_entry(syncer, "K5", title="UI Chip Title")
    assert entry["title"] == "UI Chip Title"
    assert entry["children"][0]["inline_text"] is None
    assert entry["children"][0]["word_count"] >= 50
    assert "year" not in entry  # item.year is None


def test_resolve_active_item_entry_graceful_error_handling():
    """DB lookups are wrapped: exceptions produce a minimal fallback entry."""
    from zocrates.agents.common.prompts import resolve_active_item_entry
    from zocrates.rag.zotero_db import ZoteroItem

    failing_syncer = MagicMock()
    failing_syncer.db.get_item_by_key.side_effect = RuntimeError("DB locked")

    entry = resolve_active_item_entry(failing_syncer, "GHOSTKEY")
    assert entry == {"item_key": "GHOSTKEY", "title": "Untitled"}

    # Missing item object: still returns a minimal dict and does not crash.
    missing_syncer = MagicMock()
    missing_syncer.db.get_item_by_key.return_value = None
    entry2 = resolve_active_item_entry(missing_syncer, "NOKEY")
    assert entry2 == {"item_key": "NOKEY", "title": "Untitled"}

    # Attachment query failure is contained.
    broken_att_syncer = MagicMock()
    broken_att_syncer.db.get_item_by_key.return_value = ZoteroItem(
        item_id=1, item_key="KEYA", item_type="journalArticle", title="Paper A"
    )
    broken_att_syncer.db.get_attachments.side_effect = Exception("boom")
    broken_att_syncer.db.get_notes_for_item.return_value = []
    entry3 = resolve_active_item_entry(broken_att_syncer, "KEYA")
    assert entry3["item_key"] == "KEYA"
    assert entry3["title"] == "Paper A"
    assert "children" not in entry3 or entry3["children"] == []
