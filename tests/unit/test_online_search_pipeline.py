"""Unit tests for the Online Literature Discovery Dynamic Workflow Pipeline."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.adk.events import EventActions
from google.adk.tools import ToolContext
from google.adk.workflow._errors import NodeInterruptedError

from zocrates.agents.common.state_keys import (
    CANDIDATE_PAPERS,
    HITL_SNAPSHOT,
    TEMP_EXTENSION_COUNT,
    TEMP_EXTENSION_REQUESTED,
    TEMP_ONLINE_CANDIDATES,
    TEMP_RETRIEVAL_TOOL_BUDGET,
    TEMP_RETRIEVAL_TOOL_COUNT,
    TEMP_SEARCH_REPORT,
)
from zocrates.agents.online_search.search_pipeline import (
    _enqueue_isolation_scope_closing_event,
    _ensure_isolation_scope,
    _recover_isolation_scope,
    build_online_coordinator_view,
    format_candidates_for_synthesizer,
    search_online_literature,
    search_online_pipeline_node,
)
from zocrates.agents.online_search.tools import (
    INITIAL_ONLINE_TOOL_BUDGET,
    CandidateRecommendation,
    _resolve_candidate_from_pool,
    conclude_online_search,
    request_search_extension,
    search_openalex,
)
from zocrates.discovery.models import CandidatePaper


# ---------------------------------------------------------------------------
# Test Helpers & Mocks
# ---------------------------------------------------------------------------


class MockToolContext:
    """Mock ADK ToolContext supporting state dict and actions."""

    def __init__(self, state: dict[str, Any] | None = None):
        self.state = state if state is not None else {}
        self.actions = EventActions()
        self.function_call_id = "fc-discovery-1234"
        self.run_node = AsyncMock()


class MockWorkflowContext:
    """Mock ADK Context supporting state dict, invocation context, and run_node dispatch."""

    def __init__(self, state: dict[str, Any] | None = None, invocation_context: Any = None):
        self.state = state if state is not None else {}
        self.run_node = AsyncMock()
        self._invocation_context = invocation_context
        self.isolation_scope = None
        self.agent_name = "search_online_pipeline_node"


class RecordingInvocationContext:
    """Mock InvocationContext that records enqueued events."""

    def __init__(self, subagents: dict[str, Any] | None = None):
        self.agent = SimpleNamespace(_subagents=subagents or {})
        self.invocation_id = "inv-discovery-5678"
        self.branch = None
        self.enqueued_events = []

    async def _enqueue_event(self, event):
        self.enqueued_events.append(event)


def _make_candidate(
    cid: str,
    title: str,
    year: int = 2024,
    authors: list[str] | None = None,
    is_oa: bool = True,
    citation_count: int = 25,
) -> CandidatePaper:
    return CandidatePaper(
        candidate_id=cid,
        title=title,
        authors=authors or ["Alice Smith", "Bob Jones"],
        year=year,
        venue="NeurIPS",
        doi=f"10.1234/{cid}",
        arxiv_id=f"2401.{cid}",
        abstract=f"Abstract for paper {title}.",
        is_oa=is_oa,
        citation_count=citation_count,
        primary_topic="Language Models",
        keywords=["speculative decoding", "transformers"],
    )


# ---------------------------------------------------------------------------
# 1. Candidate Formatting Tests
# ---------------------------------------------------------------------------


def test_format_candidates_for_synthesizer():
    """Format candidate papers into numbered blocks for synthesizer prompt."""
    candidates = [
        _make_candidate("c1", "Paper One", 2023),
        _make_candidate("c2", "Paper Two", 2024),
    ]
    formatted = format_candidates_for_synthesizer(candidates)
    assert "Candidate [1]" in formatted
    assert "Paper One" in formatted
    assert "Candidate [2]" in formatted
    assert "Paper Two" in formatted
    assert "Candidate ID: c1" in formatted
    assert "Candidate ID: c2" in formatted


def test_build_online_coordinator_view_empty_and_none_candidates():
    """Empty or None candidates returns stripped synthesis text without appending a header."""
    from zocrates.agents import online_search as online_search_pkg
    from zocrates.agents.online_search import search_pipeline as online_pipeline_mod

    assert "build_online_coordinator_view" in online_pipeline_mod.__all__
    assert "build_online_coordinator_view" in online_search_pkg.__all__
    assert online_search_pkg.build_online_coordinator_view is build_online_coordinator_view

    assert build_online_coordinator_view("  Clean synthesis body.  \n", []) == "Clean synthesis body."
    assert build_online_coordinator_view("  Clean synthesis body.  \n", None) == "Clean synthesis body."
    assert "# Discovered Candidate Papers" not in build_online_coordinator_view("Body", [])


def test_build_online_coordinator_view_idempotency():
    """Verify that calling build_online_coordinator_view twice or on text with existing candidate headers is idempotent."""
    c1 = _make_candidate("c1", "Test Paper", 2024, authors=["Alice Smith"])
    first_pass = build_online_coordinator_view("Synthesis body.", [c1])
    assert first_pass.count("# Discovered Candidate Papers") == 1

    second_pass = build_online_coordinator_view(first_pass, [c1])
    assert second_pass.count("# Discovered Candidate Papers") == 1
    assert second_pass == first_pass


def test_build_online_coordinator_view_author_formatting():
    """Verify author list formatting across 0, 1, 2, 3, and >3 authors."""
    c_empty = CandidatePaper(candidate_id="c0", title="No Authors Paper", authors=[], year=2024)
    c_one = _make_candidate("c1", "Single Author Paper", 2024, authors=["Alice Smith"])
    c_two = _make_candidate("c2", "Two Authors Paper", 2024, authors=["Alice Smith", "Bob Jones"])
    c_three = _make_candidate(
        "c3",
        "Three Authors Paper",
        2024,
        authors=["Alice Smith", "Bob Jones", "Carol White"],
    )
    c_many = _make_candidate(
        "c4",
        "Many Authors Paper",
        2024,
        authors=["Alice Smith", "Bob Jones", "Carol White", "Dan Brown"],
    )

    view = build_online_coordinator_view(
        "Synthesis [1] [2] [3] [4] [5].",
        [c_empty, c_one, c_two, c_three, c_many],
    )
    assert "- [1] Unknown Authors (2024). No Authors Paper." in view
    assert "- [2] Alice Smith (2024). Single Author Paper. In NeurIPS. DOI: 10.1234/c1" in view
    assert "- [3] Alice Smith & Bob Jones (2024). Two Authors Paper. In NeurIPS. DOI: 10.1234/c2" in view
    assert (
        "- [4] Alice Smith, Bob Jones, & Carol White (2024). Three Authors Paper. In NeurIPS. DOI: 10.1234/c3"
        in view
    )
    assert "- [5] Alice Smith et al. (2024). Many Authors Paper. In NeurIPS. DOI: 10.1234/c4" in view


def test_build_online_coordinator_view_formatting_rationale_and_omits_abstract():
    """Verify bibliographic fields, URL fallback, rationale sub-bullet, and strict abstract omission."""
    c1 = CandidatePaper(
        candidate_id="c1",
        title="  Speculative Drafting  ",
        authors=["Alice Smith", "Bob Jones"],
        year=2024,
        venue="NeurIPS",
        doi="10.1234/c1",
        abstract="CONFIDENTIAL LONG ABSTRACT ONE THAT MUST NOT BE INCLUDED.",
        tldr="  Introduces tree-structured speculative verification for 2.5x speedup.  ",
    )
    c2 = CandidatePaper(
        candidate_id="c2",
        title="Preprint Without Year Or DOI.",
        authors=["Carol White"],
        year=None,
        venue=None,
        doi=None,
        oa_pdf_url="https://arxiv.org/pdf/2401.99999.pdf",
        abstract="CONFIDENTIAL LONG ABSTRACT TWO THAT MUST NOT BE INCLUDED.",
        tldr=None,
    )
    c3 = CandidatePaper(
        candidate_id="c3",
        title="Paper With Dynamic PDF URL",
        authors=["Dan Brown"],
        year=2022,
        venue="ICML",
        doi=None,
        oa_pdf_url=None,
        tldr="   ",
    )
    c3.pdf_url = "https://example.org/c3.pdf"

    view = build_online_coordinator_view("  Overview citing [1], [2], and [3].  ", [c1, c2, c3])

    expected = (
        "Overview citing [1], [2], and [3].\n\n"
        "# Discovered Candidate Papers\n"
        "- [1] Alice Smith & Bob Jones (2024). Speculative Drafting. In NeurIPS. DOI: 10.1234/c1\n"
        "  - Rationale: Introduces tree-structured speculative verification for 2.5x speedup.\n"
        "- [2] Carol White (n.d.). Preprint Without Year Or DOI. URL: https://arxiv.org/pdf/2401.99999.pdf\n"
        "- [3] Dan Brown (2022). Paper With Dynamic PDF URL. In ICML. URL: https://example.org/c3.pdf"
    )
    assert view == expected
    assert "CONFIDENTIAL LONG ABSTRACT" not in view


# ---------------------------------------------------------------------------
# 2. Tool Budget Accounting & Notices Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_online_search_budget_accounting_and_notices():
    """Tool budget accounts calls accurately, emits notices on call 7 and 8, and halts on call 9."""
    mock_engine = MagicMock()
    mock_engine.search = AsyncMock(return_value=[_make_candidate("c1", "Test Paper")])

    ctx = MockToolContext(state={
        "discovery_engine": mock_engine,
        TEMP_RETRIEVAL_TOOL_BUDGET: 8,
    })

    # Calls 1 to 6: Allowed, no penultimate or budget exhausted notice
    for i in range(1, 7):
        res = await search_openalex(f"query {i}", tool_context=ctx)
        assert "Found 1 novel candidate paper" in res
        assert ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == i
        if i < 4:
            assert "[Notice:" not in res

    # Call 7: Penultimate notice (7/8 calls used, 1 remaining)
    res_7 = await search_openalex("query 7", tool_context=ctx)
    assert ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 7
    assert "7/8 retrieval tool calls used (1 call remaining)" in res_7

    # Call 8: Budget exhausted notice
    res_8 = await search_openalex("query 8", tool_context=ctx)
    assert ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 8
    assert "Retrieval tool budget exhausted (8/8)" in res_8

    # Call 9: Rejected due to budget ceiling
    res_9 = await search_openalex("query 9", tool_context=ctx)
    assert "Retrieval tool budget reached (8/8)" in res_9
    assert ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 8  # Not incremented


@pytest.mark.asyncio
async def test_search_openalex_recovers_from_serialized_string_discovery_engine():
    """search_openalex cleans up non-callable discovery_engine strings and uses get_default_discovery_engine."""
    from google.adk.sessions.state import State

    cand = _make_candidate("W5555", "Recovered Discovery Paper", 2024)
    mock_engine = MagicMock()
    mock_engine.search = AsyncMock(return_value=[cand])

    # 1. Plain dict state (simulating deserialized SQLite string repr)
    ctx_dict = MockToolContext(state={
        "discovery_engine": "<zocrates.discovery.engine.DiscoveryEngine object at 0x7f12345678>",
    })

    with patch(
        "zocrates.agents.online_search.tools.get_default_discovery_engine",
        return_value=mock_engine,
    ) as mock_get_engine:
        res = await search_openalex("speculative decoding", tool_context=ctx_dict)

    assert "Found 1 novel candidate paper" in res
    assert "Recovered Discovery Paper" in res
    mock_get_engine.assert_called_once()
    mock_engine.search.assert_awaited_once()
    assert "discovery_engine" not in ctx_dict.state
    assert isinstance(ctx_dict.state[TEMP_ONLINE_CANDIDATES]["W5555"], dict)
    assert ctx_dict.state[TEMP_ONLINE_CANDIDATES]["W5555"]["title"] == "Recovered Discovery Paper"

    # 2. Real ADK State object (which does not implement .pop)
    adk_state = State(
        {"discovery_engine": "<zocrates.discovery.engine.DiscoveryEngine object at 0x7f12345678>"},
        {},
    )
    ctx_adk = MockToolContext(state=adk_state)

    with patch(
        "zocrates.agents.online_search.tools.get_default_discovery_engine",
        return_value=mock_engine,
    ) as mock_get_engine_adk:
        res_adk = await search_openalex("speculative decoding", tool_context=ctx_adk)

    assert "Found 1 novel candidate paper" in res_adk
    mock_get_engine_adk.assert_called_once()
    assert ctx_adk.state.get("discovery_engine") is None


# ---------------------------------------------------------------------------
# 3. Termination & Extension Tools Tests
# ---------------------------------------------------------------------------


def test_conclude_online_search():
    """conclude_online_search saves unified candidates, search report, and sets skip_summarization."""
    c1 = _make_candidate("c1", "Paper One")
    c2 = _make_candidate("c2", "Paper Two")
    c3 = _make_candidate("c3", "Paper Three")
    ctx = MockToolContext(state={
        TEMP_ONLINE_CANDIDATES: {"c1": c1, "c2": c2, "c3": c3},
    })
    res = conclude_online_search(
        recommendations=[
            CandidateRecommendation(candidate_id="c1", rationale="Rationale for c1"),
            CandidateRecommendation(candidate_id="c2", rationale="Rationale for c2"),
            CandidateRecommendation(candidate_id="c3", rationale="Rationale for c3"),
        ],
        summary="High-impact draft model papers",
        tool_context=ctx,
    )
    assert "concluded with 3 candidate paper(s)" in res
    assert len(ctx.state["selected_candidates"]) == 3
    assert ctx.state["selected_candidates"][0].candidate_id == "c1"
    assert ctx.state["selected_candidates"][0].tldr == "Rationale for c1"
    assert ctx.state["selected_candidates"][1].candidate_id == "c2"
    assert ctx.state["selected_candidates"][1].tldr == "Rationale for c2"
    assert ctx.state["selected_candidates"][2].candidate_id == "c3"
    assert ctx.state["selected_candidates"][2].tldr == "Rationale for c3"
    assert ctx.state[CANDIDATE_PAPERS] == [c.to_dict() for c in ctx.state["selected_candidates"]]
    assert ctx.state[TEMP_SEARCH_REPORT]["summary"] == "High-impact draft model papers"
    assert ctx.state["temp_online_search_report"]["summary"] == "High-impact draft model papers"
    assert ctx.actions.skip_summarization is True


def test_conclude_online_search_structured_recommendations():
    """conclude_online_search accepts structured CandidateRecommendation instances and dicts."""
    c1 = _make_candidate("c1", "Paper One")
    c2 = _make_candidate("c2", "Paper Two")
    ctx = MockToolContext(state={
        TEMP_ONLINE_CANDIDATES: {"c1": c1, "c2": c2},
    })
    res = conclude_online_search(
        recommendations=[
            CandidateRecommendation(
                candidate_id="c1",
                rationale="Proposes novel speculative drafting tree architecture.",
            ),
            {
                "candidate_id": "c2",
                "rationale": "Empirical study on multi-token verification overhead.",
            },
        ],
        summary="Selected 2 key speculative decoding papers",
        tool_context=ctx,
    )
    assert "concluded with 2 candidate paper(s)" in res
    assert len(ctx.state["selected_candidates"]) == 2
    assert ctx.state["selected_candidates"][0].candidate_id == "c1"
    assert (
        ctx.state["selected_candidates"][0].tldr
        == "Proposes novel speculative drafting tree architecture."
    )
    assert ctx.state["selected_candidates"][1].candidate_id == "c2"
    assert (
        ctx.state["selected_candidates"][1].tldr
        == "Empirical study on multi-token verification overhead."
    )
    assert ctx.state[CANDIDATE_PAPERS] == [c.to_dict() for c in ctx.state["selected_candidates"]]
    assert ctx.state[TEMP_SEARCH_REPORT]["summary"] == "Selected 2 key speculative decoding papers"
    assert ctx.state["temp_online_search_report"]["summary"] == "Selected 2 key speculative decoding papers"
    assert ctx.actions.skip_summarization is True


def test_conclude_online_search_does_not_mutate_pool():
    """conclude_online_search creates fresh copies and does not mutate pool CandidatePaper objects."""
    c1 = _make_candidate("c1", "Paper One")
    original_tldr = c1.tldr
    ctx = MockToolContext(state={
        TEMP_ONLINE_CANDIDATES: {"c1": c1},
    })
    conclude_online_search(
        recommendations=[
            CandidateRecommendation(candidate_id="c1", rationale="New rationale"),
        ],
        tool_context=ctx,
    )
    assert ctx.state["selected_candidates"][0].tldr == "New rationale"
    assert c1.tldr == original_tldr


def test_conclude_online_search_edge_cases_and_unknown_ids():
    """conclude_online_search skips unknown pool IDs, preserves tldr when rationale empty, and handles empty input."""
    c1 = _make_candidate("c1", "Paper One")
    c1.tldr = "Original TLDR"
    ctx = MockToolContext(state={
        TEMP_ONLINE_CANDIDATES: {"c1": c1},
    })
    # 1. Unknown candidate ID and empty rationale
    res = conclude_online_search(
        recommendations=[
            CandidateRecommendation(candidate_id="unknown_99", rationale="Missing from pool"),
            CandidateRecommendation(candidate_id="c1", rationale="   "),  # whitespace rationale
        ],
        tool_context=ctx,
    )
    assert "concluded with 1 candidate paper(s)" in res
    assert len(ctx.state["selected_candidates"]) == 1
    assert ctx.state["selected_candidates"][0].candidate_id == "c1"
    # Original tldr preserved since rationale was empty
    assert ctx.state["selected_candidates"][0].tldr == "Original TLDR"

    # 2. Entirely empty recommendations list
    ctx_empty = MockToolContext(state={
        TEMP_ONLINE_CANDIDATES: {"c1": c1},
    })
    res_empty = conclude_online_search(recommendations=[], tool_context=ctx_empty)
    assert "concluded with 0 candidate paper(s)" in res_empty
    assert ctx_empty.state["selected_candidates"] == []
    assert ctx_empty.state[CANDIDATE_PAPERS] == []


def test_resolve_candidate_from_pool_all_tiers():
    """Verify _resolve_candidate_from_pool resolves via exact ID, short ID, ordinals, digits, int, DOI, and arXiv ID."""
    p1 = _make_candidate("https://openalex.org/W3128491823", "Attention Mechanisms")
    p1.openalex_id = "https://openalex.org/W3128491823"
    p1.doi = "10.1038/s41586-020-2649-2"
    p1.arxiv_id = "2401.12345"

    p2 = _make_candidate("https://openalex.org/W9999999999", "Quantization Techniques")
    p2.openalex_id = "https://openalex.org/W9999999999"
    p2.doi = "10.1145/1234567"
    p2.arxiv_id = "2305.67890"

    pool = {
        "https://openalex.org/W3128491823": p1,
        "https://openalex.org/W9999999999": p2,
    }

    # 1. Exact ID
    res = _resolve_candidate_from_pool("https://openalex.org/W3128491823", pool)
    assert res is not None and res.title == "Attention Mechanisms"

    # 2. Short OpenAlex ID
    res_short = _resolve_candidate_from_pool("W3128491823", pool)
    assert res_short is not None and res_short.title == "Attention Mechanisms"
    res_short_lower = _resolve_candidate_from_pool("w9999999999", pool)
    assert res_short_lower is not None and res_short_lower.title == "Quantization Techniques"

    # 3. Ordinal ("c1", "c2", "C1", "candidate 1")
    res_c1 = _resolve_candidate_from_pool("c1", pool)
    assert res_c1 is not None and res_c1.title == "Attention Mechanisms"
    res_c2 = _resolve_candidate_from_pool("C2", pool)
    assert res_c2 is not None and res_c2.title == "Quantization Techniques"
    res_cand1 = _resolve_candidate_from_pool("candidate 1", pool)
    assert res_cand1 is not None and res_cand1.title == "Attention Mechanisms"

    # 4. String digit ("1", "2")
    res_digit1 = _resolve_candidate_from_pool("1", pool)
    assert res_digit1 is not None and res_digit1.title == "Attention Mechanisms"
    res_digit2 = _resolve_candidate_from_pool("2", pool)
    assert res_digit2 is not None and res_digit2.title == "Quantization Techniques"

    # 5. Integer (1, 2)
    res_int1 = _resolve_candidate_from_pool(1, pool)
    assert res_int1 is not None and res_int1.title == "Attention Mechanisms"
    res_int2 = _resolve_candidate_from_pool(2, pool)
    assert res_int2 is not None and res_int2.title == "Quantization Techniques"

    # 6. DOI (raw, https://doi.org/, doi:)
    res_doi_raw = _resolve_candidate_from_pool("10.1038/s41586-020-2649-2", pool)
    assert res_doi_raw is not None and res_doi_raw.title == "Attention Mechanisms"
    res_doi_url = _resolve_candidate_from_pool("https://doi.org/10.1145/1234567", pool)
    assert res_doi_url is not None and res_doi_url.title == "Quantization Techniques"
    res_doi_prefix = _resolve_candidate_from_pool("doi:10.1038/s41586-020-2649-2", pool)
    assert res_doi_prefix is not None and res_doi_prefix.title == "Attention Mechanisms"

    # 7. arXiv ID (raw, arXiv:, https://arxiv.org/abs/)
    res_arxiv_raw = _resolve_candidate_from_pool("2401.12345", pool)
    assert res_arxiv_raw is not None and res_arxiv_raw.title == "Attention Mechanisms"
    res_arxiv_prefix = _resolve_candidate_from_pool("arXiv:2305.67890", pool)
    assert res_arxiv_prefix is not None and res_arxiv_prefix.title == "Quantization Techniques"
    res_arxiv_url = _resolve_candidate_from_pool("https://arxiv.org/abs/2401.12345", pool)
    assert res_arxiv_url is not None and res_arxiv_url.title == "Attention Mechanisms"

    # Edge cases
    assert _resolve_candidate_from_pool(None, pool) is None
    assert _resolve_candidate_from_pool("", pool) is None
    assert _resolve_candidate_from_pool("unknown_id", pool) is None
    assert _resolve_candidate_from_pool(99, pool) is None
    assert _resolve_candidate_from_pool("c99", pool) is None
    assert _resolve_candidate_from_pool("c1", {}) is None


def test_conclude_online_search_with_multi_tier_identifiers():
    """conclude_online_search resolves recommendations using mixed identifier formats."""
    p1 = _make_candidate("https://openalex.org/W111", "Paper One")
    p1.doi = "10.1000/1"
    p1.arxiv_id = "2401.00001"

    p2 = _make_candidate("https://openalex.org/W222", "Paper Two")
    p2.doi = "10.2000/2"
    p2.arxiv_id = "2402.00002"

    ctx = MockToolContext(state={
        TEMP_ONLINE_CANDIDATES: {
            "https://openalex.org/W111": p1,
            "https://openalex.org/W222": p2,
        },
    })
    res = conclude_online_search(
        recommendations=[
            CandidateRecommendation(candidate_id="c1", rationale="Rationale for paper 1 via ordinal."),
            {"candidate_id": "W222", "rationale": "Rationale for paper 2 via short ID."},
        ],
        summary="Mixed resolution test",
        tool_context=ctx,
    )
    assert "concluded with 2 candidate paper(s)" in res
    assert len(ctx.state["selected_candidates"]) == 2
    assert ctx.state["selected_candidates"][0].candidate_id == "https://openalex.org/W111"
    assert ctx.state["selected_candidates"][0].tldr == "Rationale for paper 1 via ordinal."
    assert ctx.state["selected_candidates"][1].candidate_id == "https://openalex.org/W222"
    assert ctx.state["selected_candidates"][1].tldr == "Rationale for paper 2 via short ID."



def test_request_search_extension():
    """request_search_extension sets extension requested flag, records rationale, and sets skip_summarization."""
    ctx = MockToolContext(state={TEMP_EXTENSION_COUNT: 0})
    res = request_search_extension(
        rationale="Need to explore speculative streaming techniques",
        tool_context=ctx,
    )
    assert "Online search extension requested" in res
    assert ctx.state[TEMP_EXTENSION_REQUESTED] is True
    assert ctx.state["temp_extension_requested"] is True
    assert ctx.state["extension_rationale"] == "Need to explore speculative streaming techniques"
    assert ctx.actions.skip_summarization is True


# ---------------------------------------------------------------------------
# 4. Programmatic Assembly & Pipeline Execution Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_online_pipeline_node_structured_recommendations():
    """Structured recommendations in selected_candidates directly populate CandidatePaper.tldr."""
    c1 = _make_candidate("c1", "Speculative Drafting", 2024)
    c2 = _make_candidate("c2", "KV Cache Quantization", 2023)
    c1.tldr = "Pioneering tree-based draft verification for fast inference."
    c2.tldr = "Sub-4-bit quantization preserving attention fidelity."

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-test-structured"

    synth_mock_output = (
        "Recent advances in speculative decoding [1] and KV cache compression [2] significantly "
        "reduce inference latency while maintaining output distribution fidelity."
    )

    async def mock_run_node(agent_or_node, *args, **kwargs):
        node_id = kwargs.get("run_id", "")
        if node_id == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": c1, "c2": c2}
            ctx.state["selected_candidates"] = [c1, c2]
            return "Search subagent completed"
        return synth_mock_output

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "speculative decoding")
    assert result.startswith(f"{synth_mock_output}\n\n# Discovered Candidate Papers\n")
    assert "- [1] Alice Smith & Bob Jones (2024). Speculative Drafting. In NeurIPS. DOI: 10.1234/c1" in result
    assert "  - Rationale: Pioneering tree-based draft verification for fast inference." in result
    assert "- [2] Alice Smith & Bob Jones (2023). KV Cache Quantization. In NeurIPS. DOI: 10.1234/c2" in result
    assert "  - Rationale: Sub-4-bit quantization preserving attention fidelity." in result
    assert "Abstract for paper" not in result
    assert "Recommended Papers for Import:" not in result

    # Check assembled candidates in state and rationales in tldr
    assert "candidates" in ctx.state
    assembled = ctx.state["candidates"]
    assert len(assembled) == 2
    assert assembled[0]["candidate_id"] == "c1"
    assert assembled[0]["tldr"] == "Pioneering tree-based draft verification for fast inference."
    assert assembled[1]["candidate_id"] == "c2"
    assert assembled[1]["tldr"] == "Sub-4-bit quantization preserving attention fidelity."
    assert ctx.state["selected_candidates"] == assembled
    assert ctx.state[CANDIDATE_PAPERS] == assembled


@pytest.mark.asyncio
async def test_search_online_pipeline_node_programmatic_assembly():
    """Synthesizer output recommended headers are stripped and selected_candidates are stamped."""
    c1 = _make_candidate("c1", "Speculative Drafting", 2024)
    c2 = _make_candidate("c2", "KV Cache Quantization", 2023)
    c1.tldr = "Core draft verification framework for edge acceleration."
    c2.tldr = "Scalable memory paging for speculative multi-sequence decoding."

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-test-1"

    synth_mock_output = (
        "Here is the synthesized overview of speculative decoding methods.\n\n"
        "### Recommended Papers for Import:\n"
        "- [1]: Core draft verification framework for edge acceleration.\n"
        "- [2]: Scalable memory paging for speculative multi-sequence decoding.\n"
    )

    async def mock_run_node(agent_or_node, *args, **kwargs):
        node_id = kwargs.get("run_id", "")
        if node_id == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": c1, "c2": c2}
            ctx.state["selected_candidates"] = [c1, c2]
            return "Search subagent completed"
        # Synthesizer returns recommendations
        return synth_mock_output

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "speculative decoding")
    assert result.startswith(
        "Here is the synthesized overview of speculative decoding methods.\n\n"
        "# Discovered Candidate Papers\n"
    )
    assert "- [1] Alice Smith & Bob Jones (2024). Speculative Drafting. In NeurIPS. DOI: 10.1234/c1" in result
    assert "  - Rationale: Core draft verification framework for edge acceleration." in result
    assert "- [2] Alice Smith & Bob Jones (2023). KV Cache Quantization. In NeurIPS. DOI: 10.1234/c2" in result
    assert "  - Rationale: Scalable memory paging for speculative multi-sequence decoding." in result
    assert "Recommended Papers for Import:" not in result

    # Check assembled candidates in state
    assert "candidates" in ctx.state
    assembled = ctx.state["candidates"]
    assert len(assembled) == 2
    assert assembled[0]["candidate_id"] == "c1"
    assert assembled[1]["candidate_id"] == "c2"
    assert ctx.state["selected_candidates"] == assembled
    assert ctx.state[CANDIDATE_PAPERS] == assembled


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "heading",
    ["## References", "# Cited Sources", "### Sources Consulted"],
)
async def test_search_online_pipeline_node_strips_trailing_reference_sections(heading):
    """Trailing References / Cited Sources / Sources Consulted sections are stripped from synthesis."""
    c1 = _make_candidate("c1", "Speculative Drafting", 2024)

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-test-refs"

    body = "Speculative decoding reduces latency [1]."
    synth_mock_output = f"{body}\n\n{heading}\n1. [1] Speculative Drafting (2024)\n"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        if kwargs.get("run_id", "") == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": c1}
            ctx.state["selected_candidates"] = [c1]
            return "Search subagent completed"
        return synth_mock_output

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "speculative decoding")
    assert result.startswith(f"{body}\n\n# Discovered Candidate Papers\n")
    assert "- [1] Alice Smith & Bob Jones (2024). Speculative Drafting. In NeurIPS. DOI: 10.1234/c1" in result
    assert "1. [1] Speculative Drafting (2024)" not in result
    assert "References" not in result
    assert "Cited Sources" not in result
    assert "Sources Consulted" not in result


@pytest.mark.asyncio
async def test_search_online_pipeline_node_programmatic_assembly_fallback():
    """When agent selected 0 candidates, falls back to top-10 from candidate pool."""
    c1 = _make_candidate("c1", "Paper Alpha", 2024)
    c2 = _make_candidate("c2", "Paper Beta", 2023)

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-test-2"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        if kwargs.get("run_id", "") == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": c1, "c2": c2}
            return "Search subagent completed"
        return "Synthesizer overview without explicit import list."

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "unstructured query")
    assert "# Discovered Candidate Papers" in result
    assert "- [1] Alice Smith & Bob Jones (2024). Paper Alpha. In NeurIPS. DOI: 10.1234/c1" in result
    assert "- [2] Alice Smith & Bob Jones (2023). Paper Beta. In NeurIPS. DOI: 10.1234/c2" in result
    assert "candidates" in ctx.state
    assert len(ctx.state["candidates"]) == 2
    assert ctx.state["candidates"][0]["candidate_id"] == "c1"
    assert ctx.state["candidates"][1]["candidate_id"] == "c2"
    assert ctx.state["selected_candidates"] == ctx.state["candidates"]
    assert ctx.state[CANDIDATE_PAPERS] == ctx.state["candidates"]


@pytest.mark.asyncio
async def test_search_online_pipeline_node_restores_selected_candidates_from_snapshot():
    """search_online_pipeline_node restores selected_candidates when resuming from a snapshot."""
    c1 = _make_candidate("c1", "Resumed Paper", 2024)
    c1.tldr = "Preserved Rationale"

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={
            HITL_SNAPSHOT: {
                "search_query": "resumed query",
                "candidates_pool": {"c1": c1.to_dict()},
                "selected_candidates": [c1.to_dict()],
                "search_report": {"summary": "Checkpoint summary"},
                "extension_count": 1,
                "tool_count": 8,
                "extension_requested": False,
                "ceilings": {"budget": 13},
                "pipeline_type": "online_search",
            },
        },
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-resume-test"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        return "Resumed synthesis output"

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "resumed query")
    assert result.startswith("Resumed synthesis output\n\n# Discovered Candidate Papers\n")
    assert "- [1] Alice Smith & Bob Jones (2024). Resumed Paper. In NeurIPS. DOI: 10.1234/c1" in result
    assert "  - Rationale: Preserved Rationale" in result
    assert "selected_candidates" in ctx.state
    assert len(ctx.state["selected_candidates"]) == 1
    assert ctx.state["selected_candidates"][0]["candidate_id"] == "c1"
    assert ctx.state["selected_candidates"][0]["tldr"] == "Preserved Rationale"
    assert ctx.state["candidates"] == ctx.state["selected_candidates"]


@pytest.mark.asyncio
async def test_fresh_turn_resets_ephemeral_state_and_prevents_candidate_leakage():
    """Fresh turn (no HITL snapshot) resets stale selected_candidates, pool, and tool count."""
    stale_cand = _make_candidate("stale_w1", "Stale Previous Turn Paper", 2021)
    stale_cand.tldr = "Stale rationale from Turn 1."

    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={
            TEMP_ONLINE_CANDIDATES: {"stale_w1": stale_cand.to_dict()},
            "temp_online_candidates": {"stale_w1": stale_cand.to_dict()},
            "selected_candidates": [stale_cand.to_dict()],
            CANDIDATE_PAPERS: [stale_cand.to_dict()],
            TEMP_RETRIEVAL_TOOL_COUNT: 8,
            "temp_retrieval_tool_count": 8,
            TEMP_SEARCH_REPORT: {"summary": "Turn 1 summary", "gaps": []},
            "temp_online_search_report": {"summary": "Turn 1 summary", "gaps": []},
            TEMP_EXTENSION_REQUESTED: True,
            "temp_extension_requested": True,
            "extension_rationale": "Stale rationale",
            "discovery_engine": "<zocrates.discovery.engine.DiscoveryEngine object at 0xdeadbeef>",
            "metadata": {"candidates": [stale_cand.to_dict()]},
        },
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-fresh-turn-reset"

    captured_synth_prompts: list[str] = []

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if run_id == "search_initial":
            # Verify stale state was already reset before search_initial executed
            assert ctx.state[TEMP_ONLINE_CANDIDATES] == {}
            assert ctx.state["temp_online_candidates"] == {}
            assert ctx.state["selected_candidates"] == []
            assert ctx.state[CANDIDATE_PAPERS] == []
            assert ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] == 0
            assert ctx.state["temp_retrieval_tool_count"] == 0
            assert ctx.state[TEMP_EXTENSION_REQUESTED] is False
            assert "discovery_engine" not in ctx.state
            # Simulate empty retrieval on Turn 2
            return "No candidates found in Turn 2."

        captured_synth_prompts.append(kwargs.get("node_input", ""))
        return "No relevant novel literature was discovered for this query."

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "nonexistent obscure topic")

    assert result == "No relevant novel literature was discovered for this query."
    assert "Stale Previous Turn Paper" not in result
    assert "# Discovered Candidate Papers" not in result
    assert len(captured_synth_prompts) == 1
    assert "No candidate papers found." in captured_synth_prompts[0]
    assert "Stale Previous Turn Paper" not in captured_synth_prompts[0]
    assert ctx.state["selected_candidates"] == []
    assert ctx.state[CANDIDATE_PAPERS] == []
    assert ctx.state["candidates"] == []
    assert ctx.state["metadata"]["candidates"] == []


@pytest.mark.asyncio
async def test_fresh_turn_adk_state_reconciliation_prevents_leakage_and_preserves_new_candidates():
    """Fresh turn with real ADK State clears Turn 1 data without shadowing Turn 2 subagent updates."""
    from google.adk.sessions.state import State

    stale_cand = _make_candidate("stale_w1", "Stale Turn 1 Paper", 2021)
    fresh_cand = _make_candidate("fresh_w2", "Fresh Turn 2 Paper", 2025)
    fresh_cand.tldr = "Fresh Turn 2 rationale."

    session_backing_dict: dict[str, Any] = {
        TEMP_ONLINE_CANDIDATES: {"stale_w1": stale_cand.to_dict()},
        "temp_online_candidates": {"stale_w1": stale_cand.to_dict()},
        "selected_candidates": [stale_cand.to_dict()],
        CANDIDATE_PAPERS: [stale_cand.to_dict()],
        TEMP_RETRIEVAL_TOOL_COUNT: 8,
        "discovery_engine": "<zocrates.discovery.engine.DiscoveryEngine object at 0x1234>",
    }
    adk_state = State(session_backing_dict, {})
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(state=adk_state, invocation_context=invocation_ctx)
    ctx.isolation_scope = "search_online_pipeline:fc-adk-state-reconcile"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        if kwargs.get("run_id", "") == "search_initial":
            # Child subagent tools commit to session_backing_dict (State._value), not parent State._delta
            child_state = State(session_backing_dict, {})
            child_state[TEMP_ONLINE_CANDIDATES] = {"fresh_w2": fresh_cand.to_dict()}
            child_state["selected_candidates"] = [fresh_cand]
            child_state[TEMP_RETRIEVAL_TOOL_COUNT] = 1
            return "Search subagent completed"
        return "Turn 2 synthesis citing [1]."

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "fresh turn 2 query")

    assert "Fresh Turn 2 Paper" in result
    assert "Fresh Turn 2 rationale." in result
    assert "Stale Turn 1 Paper" not in result
    assert ctx.state.get("discovery_engine") is None
    assert len(ctx.state["candidates"]) == 1
    assert ctx.state["candidates"][0]["candidate_id"] == "fresh_w2"
    assert ctx.state["candidates"][0]["tldr"] == "Fresh Turn 2 rationale."



# ---------------------------------------------------------------------------
# 5. Isolation Scope Closure Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_online_pipeline_node_closes_isolation_scope_on_success():
    """Dynamic workflow node enqueues finish_task with Task completed. upon normal completion."""
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-scope-success"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        return "Discovery synthesis result"

    ctx.run_node.side_effect = mock_run_node

    result = await search_online_pipeline_node._func(ctx, "quantum machine learning")
    assert result == "Discovery synthesis result"

    # Verify terminal scope closing event
    assert len(invocation_ctx.enqueued_events) == 1
    closing_event = invocation_ctx.enqueued_events[0]
    assert closing_event.author == "search_online_pipeline_node"
    assert closing_event.isolation_scope == "search_online_pipeline:fc-scope-success"
    assert closing_event.content.role == "tool"
    fr = closing_event.content.parts[0].function_response
    assert fr.name == "finish_task"
    assert fr.response == {"result": "Task completed."}


@pytest.mark.asyncio
async def test_search_online_pipeline_node_emits_task_failed_on_exception():
    """Dynamic workflow node enqueues finish_task with Task failed. when an unhandled exception occurs."""
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-scope-failure"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        raise RuntimeError("OpenAlex API network failure")

    ctx.run_node.side_effect = mock_run_node

    with pytest.raises(RuntimeError, match="OpenAlex API network failure"):
        await search_online_pipeline_node._func(ctx, "failure test")

    assert len(invocation_ctx.enqueued_events) == 1
    closing_event = invocation_ctx.enqueued_events[0]
    fr = closing_event.content.parts[0].function_response
    assert fr.name == "finish_task"
    assert fr.response == {"result": "Task failed."}


@pytest.mark.asyncio
async def test_search_online_pipeline_node_leaves_scope_open_on_interrupt():
    """When NodeInterruptedError is raised, the isolation scope closing event is NOT enqueued."""
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-scope-interrupt"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        raise NodeInterruptedError("Awaiting HITL user extension confirmation")

    ctx.run_node.side_effect = mock_run_node

    with pytest.raises(NodeInterruptedError):
        await search_online_pipeline_node._func(ctx, "interrupt test")

    # Scope closure must NOT be enqueued so the scope remains alive for /chat/resume
    assert len(invocation_ctx.enqueued_events) == 0


# ---------------------------------------------------------------------------
# 6. Tool Wrapper Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_online_literature_tool_wrapper():
    """search_online_literature sets skip_summarization and dispatches node under search_online_pipeline scope."""
    tool_ctx = MockToolContext()
    tool_ctx.function_call_id = "fc-call-9999"

    async def mock_run_node(node_callable, *args, **kwargs):
        assert kwargs.get("override_isolation_scope") == "search_online_pipeline:fc-call-9999"
        assert kwargs.get("node_input") == "speculative decoding preprints"
        return "Pipeline synthesis response"

    tool_ctx.run_node.side_effect = mock_run_node

    res = await search_online_literature("speculative decoding preprints", tool_ctx)
    assert res == "Pipeline synthesis response"
    assert tool_ctx.actions.skip_summarization is True


# ---------------------------------------------------------------------------
# 7. HITL Extension & Checkpoint Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_online_search_hitl_proactive_extension_and_snapshot():
    """Proactive extension request creates HITL snapshot and pauses at checkpoint."""
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-hitl-1"

    checkpoint_invocations = []

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if run_id == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": _make_candidate("c1", "Paper 1")}
            ctx.state[TEMP_EXTENSION_REQUESTED] = True
            ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 4
            ctx.state[TEMP_RETRIEVAL_TOOL_BUDGET] = 8
            ctx.state[TEMP_SEARCH_REPORT] = {
                "summary": "Found preliminary papers",
                "gaps": ["Need more survey papers"],
                "notes": "Exploring adjacent survey works",
                "source": "subagent_request",
            }
            return "Search initial completed"
        if "checkpoint" in run_id:
            checkpoint_invocations.append(kwargs)
            # User declines extension -> returns synthesize
            return {"action": "synthesize"}
        return "Synthesized answer"

    ctx.run_node.side_effect = mock_run_node

    await search_online_pipeline_node._func(ctx, "survey papers")

    assert len(checkpoint_invocations) == 1
    cp_input = checkpoint_invocations[0]["node_input"]
    assert cp_input["extension_number"] == 1
    assert cp_input["max_extensions"] == 2
    assert cp_input["current_calls"] == 4
    assert cp_input["missing_aspects"] == ["Need more survey papers"]
    assert cp_input["trigger_reason"] == "agent_request"
    assert cp_input["trigger_details"] == {
        "tool_calls_used": 4,
        "tool_budget": 8,
    }
    assert cp_input["agent_justification"] == "Exploring adjacent survey works"

    # When user chosen action is synthesize, pipeline concludes and clears turn snapshot
    assert ctx.state.get(HITL_SNAPSHOT) is None


@pytest.mark.asyncio
async def test_online_search_hitl_snapshot_saves_and_restores_selected_candidates():
    """Verify HITL_SNAPSHOT preserves and restores selected_candidates across pause/resume."""
    c1 = _make_candidate("c1", "Paper 1")
    c1.tldr = "Rationale 1"
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-hitl-snapshot-test"

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        if run_id == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": c1}
            ctx.state["selected_candidates"] = [c1]
            ctx.state[TEMP_EXTENSION_REQUESTED] = True
            ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 4
            ctx.state[TEMP_RETRIEVAL_TOOL_BUDGET] = 8
            ctx.state[TEMP_SEARCH_REPORT] = {
                "summary": "Found 1 candidate",
                "gaps": ["Need more works"],
                "notes": "",
                "source": "subagent_request",
            }
            return "Search initial completed"
        if "checkpoint" in run_id:
            snap = ctx.state[HITL_SNAPSHOT]
            assert "selected_candidates" in snap
            assert len(snap["selected_candidates"]) == 1
            assert snap["selected_candidates"][0]["candidate_id"] == "c1"
            assert snap["selected_candidates"][0]["tldr"] == "Rationale 1"
            assert snap["trigger_reason"] == "agent_request"
            assert snap["trigger_details"] == {"tool_calls_used": 4, "tool_budget": 8}
            assert snap["agent_justification"] == ""
            return {"action": "synthesize"}
        return "Synthesized result"

    ctx.run_node.side_effect = mock_run_node
    await search_online_pipeline_node._func(ctx, "test query")



@pytest.mark.asyncio
async def test_online_search_hitl_budget_expansion_on_user_approval():
    """User approving extend_budget increases budget by 5 and triggers extension search node."""
    invocation_ctx = RecordingInvocationContext()
    c1 = _make_candidate("c1", "Paper 1")
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-hitl-expand"

    node_runs = []

    async def mock_run_node(agent_or_node, *args, **kwargs):
        run_id = kwargs.get("run_id", "")
        node_runs.append((run_id, kwargs.get("node_input")))
        if run_id == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {"c1": c1}
            ctx.state[TEMP_EXTENSION_REQUESTED] = True
            ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 8
            ctx.state[TEMP_RETRIEVAL_TOOL_BUDGET] = 8
            ctx.state[TEMP_SEARCH_REPORT] = {
                "summary": "Initial 8 calls used",
                "gaps": ["Unaddressed edge SLM decoding methods"],
                "notes": "Budget reached",
                "source": "subagent_request",
            }
            return "Search initial completed"
        if run_id == "checkpoint_0":
            return {"action": "extend_budget", "guidance": "Focus on 2024 ArXiv preprints"}
        elif run_id == "online_search_extension_1":
            # Simulate extension search adding candidate c2
            c2 = _make_candidate("c2", "Edge SLM Paper")
            ctx.state[TEMP_ONLINE_CANDIDATES]["c2"] = c2
            return "Extension search executed"
        return (
            "Here is the overview.\n\n"
            "### Recommended Papers for Import:\n"
            "- [1]: Foundational paper.\n"
            "- [2]: Edge SLM paper.\n"
        )

    ctx.run_node.side_effect = mock_run_node

    await search_online_pipeline_node._func(ctx, "edge slm decoding")

    run_ids = [r[0] for r in node_runs]
    assert "checkpoint_0" in run_ids
    assert "online_search_extension_1" in run_ids

    # Extension prompt checks
    ext_input = next(r[1] for r in node_runs if r[0] == "online_search_extension_1")
    assert "Deep Research Extension Granted (Extension 1 of 2)" in ext_input
    assert "Focus on 2024 ArXiv preprints" in ext_input
    assert "Unaddressed edge SLM decoding methods" in ext_input

    # Both candidates mapped
    assert len(ctx.state["candidates"]) == 2


@pytest.mark.asyncio
async def test_online_search_hitl_reactive_budget_exhaustion_trigger():
    """Budget exhaustion (8 calls) with unaddressed gaps triggers reactive checkpoint."""
    invocation_ctx = RecordingInvocationContext()
    ctx = MockWorkflowContext(
        state={},
        invocation_context=invocation_ctx,
    )
    ctx.isolation_scope = "search_online_pipeline:fc-reactive"

    checkpoint_called = False
    checkpoint_input = None

    async def mock_run_node(agent_or_node, *args, **kwargs):
        nonlocal checkpoint_called, checkpoint_input
        run_id = kwargs.get("run_id", "")
        if run_id == "search_initial":
            ctx.state[TEMP_ONLINE_CANDIDATES] = {}
            ctx.state[TEMP_EXTENSION_REQUESTED] = False
            ctx.state[TEMP_RETRIEVAL_TOOL_COUNT] = 8
            ctx.state[TEMP_RETRIEVAL_TOOL_BUDGET] = 8
            ctx.state[TEMP_SEARCH_REPORT] = {
                "summary": "Exhausted 8 calls with no solid matches",
                "gaps": ["Alternative keyword search needed"],
                "notes": "Reactive trigger test",
                "source": "budget_exhaustion",
            }
            return "Search initial completed"
        if "checkpoint" in run_id:
            checkpoint_called = True
            checkpoint_input = kwargs.get("node_input")
            return {"action": "synthesize"}
        return "Synthesizer fallback response"

    ctx.run_node.side_effect = mock_run_node

    await search_online_pipeline_node._func(ctx, "obscure topic")
    assert checkpoint_called is True
    assert checkpoint_input["trigger_reason"] == "tool_budget_exhausted"
    assert checkpoint_input["trigger_details"] == {"tool_calls_used": 8, "tool_budget": 8}
    assert checkpoint_input["agent_justification"] is None


def test_candidates_invocation_id_scoping_prevents_leakage_in_session_formatting():
    """Verify format_session_for_ui scopes candidates strictly to matching invocation_id."""
    from zocrates.utils.session_formatting import format_session_for_ui
    from google.adk.events import Event
    from google.genai import types

    cand = _make_candidate("W9001", "Scoped Candidate Paper", 2024).to_dict()

    ev1_user = Event(
        id="e1",
        author="user",
        invocation_id="inv-1",
        content=types.Content(parts=[types.Part.from_text(text="Search online for quantum")]),
    )
    ev1_asst = Event(
        id="e2",
        author="coordinator_agent",
        invocation_id="inv-1",
        content=types.Content(parts=[types.Part.from_text(text="Here are recommended quantum papers.")]),
    )

    ev2_user = Event(
        id="e3",
        author="user",
        invocation_id="inv-2",
        content=types.Content(parts=[types.Part.from_text(text="What is 2+2?")]),
    )
    ev2_asst = Event(
        id="e4",
        author="coordinator_agent",
        invocation_id="inv-2",
        content=types.Content(parts=[types.Part.from_text(text="4.")]),
    )

    mock_session = SimpleNamespace(
        id="sess-scoped-cand",
        events=[ev1_user, ev1_asst, ev2_user, ev2_asst],
        state={
            "candidates": [cand],
            "candidates_invocation_id": "inv-1",
        },
    )

    formatted = format_session_for_ui(mock_session)
    asst_messages = [m for m in formatted["messages"] if m["role"] == "assistant"]
    assert len(asst_messages) == 2

    # Turn 1 should have candidates attached
    assert asst_messages[0]["invocation_id"] == "inv-1"
    assert asst_messages[0]["candidates"] == [cand]

    # Turn 2 must NOT have stale candidates from Turn 1 attached
    assert asst_messages[1]["invocation_id"] == "inv-2"
    assert asst_messages[1]["candidates"] is None

