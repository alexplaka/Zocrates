"""Unit tests for Phase 5: Retrieval Agent Integration & Guardrails for Graph Paper Relations.

Validates:
- get_document_toc cycle suppression within a turn
- get_document_toc cycle suppression via canonical resolved_key (parent <-> attachment)
- read_document unblocked even when temp:seen_toc_item_keys contains target key
- search_subagent prompt documentation for get_related_items, **Related Items**: N, and budget
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zocrates.agents.common.state_keys import (
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_SEEN_TOC_ITEM_KEYS,
)
from zocrates.agents.common.tools import get_document_toc, read_document
from zocrates.agents.library_search.prompts import (
    LIBRARY_SEARCH_AGENT_INSTRUCTION,
)


class MockToolContext:
    """Mock ADK ToolContext for testing invocation state and cycle suppression."""

    def __init__(self, state: dict | None = None):
        self.state = state if state is not None else {}
        self.actions = MagicMock()


def test_get_document_toc_cycle_suppression():
    """Verify first call succeeds and second call on same turn returns already_inspected without re-reading."""
    mock_ctx = MockToolContext({TEMP_RETRIEVAL_TOOL_COUNT: 0})

    with patch("zocrates.agents.common.tools.get_document_toc_core") as mock_core:
        mock_core.return_value = {
            "status": "success",
            "item_key": "DOC1",
            "toc_content": "## Table of Contents\n- [s0] Abstract\n- [s1] Intro\n",
            "title": "Doc Title",
        }

        # First call: succeeds and registers key in temp:seen_toc_item_keys
        res1 = get_document_toc("DOC1", tool_context=mock_ctx)
        assert res1["status"] == "success"
        assert mock_core.call_count == 1
        assert "DOC1" in mock_ctx.state[TEMP_SEEN_TOC_ITEM_KEYS]
        assert mock_ctx.state[TEMP_SEEN_TOC_ITEM_KEYS] == ["DOC1"]

        # Second call: suppressed before reading disk
        res2 = get_document_toc("DOC1", tool_context=mock_ctx)
        assert res2["status"] == "already_inspected"
        assert res2["item_key"] == "DOC1"
        assert "TOC for DOC1 was already retrieved in this turn" in res2["message"]
        assert "Refer to prior context or use read_document for specific sections." in res2["message"]

        # Verify disk reading core was NOT invoked again
        assert mock_core.call_count == 1


def test_get_document_toc_cycle_suppression_resolved_key():
    """Calling by parent key then attachment key (or vice-versa) triggers already_inspected."""
    # Direction 1: Parent key first, then attachment key
    mock_ctx1 = MockToolContext({TEMP_RETRIEVAL_TOOL_COUNT: 0})

    with patch("zocrates.agents.common.tools._resolve_bundle_dir") as mock_resolve, \
         patch("zocrates.agents.common.tools.get_document_toc_core") as mock_core:
        mock_resolve.side_effect = lambda k: (Path("/fake/ATTACH1"), "ATTACH1", None)
        mock_core.return_value = {
            "status": "success",
            "item_key": "ATTACH1",
            "toc_content": "## Table of Contents\n- [s0] Abstract\n",
            "title": "Paper Title",
        }

        res_parent = get_document_toc("PARENT1", tool_context=mock_ctx1)
        assert res_parent["status"] == "success"
        assert mock_core.call_count == 1
        seen_keys = mock_ctx1.state[TEMP_SEEN_TOC_ITEM_KEYS]
        assert "PARENT1" in seen_keys
        assert "ATTACH1" in seen_keys

        # Second call with attachment key -> triggers cycle suppression
        res_attach = get_document_toc("ATTACH1", tool_context=mock_ctx1)
        assert res_attach["status"] == "already_inspected"
        assert res_attach["item_key"] == "ATTACH1"
        assert "TOC for ATTACH1 was already retrieved in this turn." in res_attach["message"]
        assert mock_core.call_count == 1

    # Direction 2: Attachment key first, then parent key (vice-versa)
    mock_ctx2 = MockToolContext({TEMP_RETRIEVAL_TOOL_COUNT: 0})

    with patch("zocrates.agents.common.tools._resolve_bundle_dir") as mock_resolve, \
         patch("zocrates.agents.common.tools.get_document_toc_core") as mock_core:
        mock_resolve.side_effect = lambda k: (Path("/fake/ATTACH1"), "ATTACH1", None)
        mock_core.return_value = {
            "status": "success",
            "item_key": "ATTACH1",
            "toc_content": "## Table of Contents\n- [s0] Abstract\n",
            "title": "Paper Title",
        }

        res_attach = get_document_toc("ATTACH1", tool_context=mock_ctx2)
        assert res_attach["status"] == "success"
        assert mock_core.call_count == 1
        assert "ATTACH1" in mock_ctx2.state[TEMP_SEEN_TOC_ITEM_KEYS]

        # Second call with parent key -> resolves to ATTACH1 which is in seen_keys -> suppressed
        res_parent = get_document_toc("PARENT1", tool_context=mock_ctx2)
        assert res_parent["status"] == "already_inspected"
        assert res_parent["item_key"] == "PARENT1"
        assert "TOC for PARENT1 was already retrieved in this turn." in res_parent["message"]
        assert mock_core.call_count == 1


def test_read_document_remains_unblocked():
    """Verify read_document succeeds and is not blocked even when temp:seen_toc_item_keys contains that key."""
    mock_ctx = MockToolContext(
        {
            TEMP_RETRIEVAL_TOOL_COUNT: 0,
            TEMP_SEEN_TOC_ITEM_KEYS: ["DOC_SEEN", "PARENT_SEEN"],
        }
    )

    with patch("zocrates.agents.common.tools.read_document_core") as mock_read_core:
        mock_read_core.return_value = {
            "status": "success",
            "item_key": "DOC_SEEN",
            "assets": [
                {
                    "asset_id": "s1",
                    "title": "Methods",
                    "text": "Detailed methods content.",
                    "word_count": 50,
                }
            ],
            "intersecting_chunk_ids": [],
        }

        res = read_document("DOC_SEEN", asset_id="s1", tool_context=mock_ctx)
        assert res["status"] == "success"
        assert res["item_key"] == "DOC_SEEN"
        assert len(res["assets"]) == 1
        assert res["assets"][0]["asset_id"] == "s1"
        assert mock_read_core.call_count == 1


def test_search_subagent_prompt_includes_related_items():
    """Verify LIBRARY_SEARCH_AGENT_INSTRUCTION documents get_related_items, **Related Items**: N, and budget inclusions."""
    # 1. Available Tools documentation
    assert "`get_related_items(item_key)`:" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "Retrieves verified semantically related and user-curated connected papers" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "'human_curated_and_hybrid_search'" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "confidence scores (0.50 - 1.0)" in LIBRARY_SEARCH_AGENT_INSTRUCTION

    # 2. Retrieval Strategy Guidelines documentation
    assert "**Related Items**: N" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "Paper Relations & Literature Graph Traversal" in LIBRARY_SEARCH_AGENT_INSTRUCTION
    assert "get_related_items(item_key)" in LIBRARY_SEARCH_AGENT_INSTRUCTION

    # 3. Bounded Retrieval Rules & Termination Protocol budget inclusion
    budget_line = (
        "- Strict Retrieval Tool Budget: You operate under a finite budget of retrieval tool calls "
        "(search_corpus, get_document_toc, read_document, get_related_items)."
    )
    assert budget_line in LIBRARY_SEARCH_AGENT_INSTRUCTION
