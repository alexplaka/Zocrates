"""Unit tests for zocrates.agents.common.pipeline_base shared scaffolding helpers."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.adk.events import Event
from google.genai import types

from zocrates.agents.common.pipeline_base import (
    coerce_to_text,
    enqueue_isolation_scope_closing_event,
    ensure_isolation_scope,
    get_pipeline_interrupt_ids,
    recover_isolation_scope,
)
from zocrates.agents.common.state_keys import HITL_SNAPSHOT
import zocrates.agents.library_search.search_pipeline as lib_pipeline
import zocrates.agents.online_search.search_pipeline as online_pipeline


# ---------------------------------------------------------------------------
# Test Helpers & Mocks
# ---------------------------------------------------------------------------


class MockInvocationContext:
    """Mock InvocationContext recording enqueued events."""

    def __init__(self, invocation_id: str = "inv-test-1", branch: str | None = None, session: Any = None):
        self.invocation_id = invocation_id
        self.branch = branch
        self.session = session
        self.isolation_scope: str | None = None
        self.enqueued_events: list[Event] = []

    async def _enqueue_event(self, event: Event) -> None:
        self.enqueued_events.append(event)


class MockWorkflowContext:
    """Mock ADK Context supporting state dict, invocation context, and isolation scope."""

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        invocation_context: Any = None,
        isolation_scope: str | None = None,
        agent_name: str | None = None,
        invocation_id: str | None = None,
        session: Any = None,
        resume_inputs: Any = None,
    ):
        self.state = state if state is not None else {}
        self._invocation_context = invocation_context
        self.isolation_scope = isolation_scope
        self.agent_name = agent_name
        self.invocation_id = invocation_id
        self.session = session
        self.resume_inputs = resume_inputs


# ---------------------------------------------------------------------------
# Tests for coerce_to_text
# ---------------------------------------------------------------------------


class TestCoerceToText:
    """Unit tests for coerce_to_text extracting text across response variants."""

    def test_coerce_to_text_str(self):
        assert coerce_to_text("Direct string response") == "Direct string response"

    def test_coerce_to_text_none(self):
        assert coerce_to_text(None) == ""

    def test_coerce_to_text_content_and_parts(self):
        part1 = types.Part.from_text(text="First paragraph. ")
        part2 = types.Part.from_text(text="Second paragraph.")
        content = types.Content(role="model", parts=[part1, part2])
        assert coerce_to_text(content) == "First paragraph. Second paragraph."

    def test_coerce_to_text_part_with_text(self):
        part = types.Part.from_text(text="Single part text")
        assert coerce_to_text(part) == "Single part text"

    def test_coerce_to_text_part_with_thought_ignored(self):
        part_thought = types.Part(thought=True, text="Internal thinking tokens that should not appear")
        assert coerce_to_text(part_thought) == ""

    def test_coerce_to_text_content_filters_thought_parts(self):
        thought_part = types.Part(thought=True, text="Reasoning trace")
        text_part = types.Part.from_text(text="Actual synthesized answer.")
        content = types.Content(role="model", parts=[thought_part, text_part])
        assert coerce_to_text(content) == "Actual synthesized answer."

    def test_coerce_to_text_dict_text(self):
        assert coerce_to_text({"text": "Answer from text key"}) == "Answer from text key"

    def test_coerce_to_text_dict_content(self):
        assert coerce_to_text({"content": "Answer from content key"}) == "Answer from content key"

    def test_coerce_to_text_dict_nested_content(self):
        assert coerce_to_text({"content": {"text": "Deeply nested text"}}) == "Deeply nested text"

    def test_coerce_to_text_dict_thought_ignored(self):
        assert coerce_to_text({"thought": True, "text": "Thought dict"}) == ""

    def test_coerce_to_text_object_with_output(self):
        obj = SimpleNamespace(output="Output from tool or step")
        assert coerce_to_text(obj) == "Output from tool or step"

    def test_coerce_to_text_object_with_nested_output(self):
        inner = SimpleNamespace(output="Nested output text")
        outer = SimpleNamespace(output=inner)
        assert coerce_to_text(outer) == "Nested output text"

    def test_coerce_to_text_object_with_text_attribute(self):
        obj = SimpleNamespace(text="From text attribute")
        assert coerce_to_text(obj) == "From text attribute"

    def test_coerce_to_text_object_with_content_attribute(self):
        content = types.Content(role="model", parts=[types.Part.from_text(text="From content attribute")])
        obj = SimpleNamespace(content=content)
        assert coerce_to_text(obj) == "From content attribute"

    def test_coerce_to_text_fallback(self):
        assert coerce_to_text(12345) == "12345"
        assert coerce_to_text(True) == "True"


# ---------------------------------------------------------------------------
# Tests for get_pipeline_interrupt_ids
# ---------------------------------------------------------------------------


class TestGetPipelineInterruptIds:
    """Unit tests for get_pipeline_interrupt_ids scanning resume_inputs and session events."""

    def test_resume_inputs_dict(self):
        ctx = MockWorkflowContext(
            resume_inputs={"interrupt_ext_1": {"action": "extend"}, "unrelated_key": 42}
        )
        assert get_pipeline_interrupt_ids(ctx) == {"interrupt_ext_1"}

    def test_resume_inputs_callable(self):
        ctx = MockWorkflowContext(
            resume_inputs=lambda: {"interrupt_ext_2": {"action": "synthesize"}}
        )
        assert get_pipeline_interrupt_ids(ctx) == {"interrupt_ext_2"}

    def test_resume_inputs_callable_exception_handled(self):
        def bad_callable():
            raise RuntimeError("Inputs not ready")

        ctx = MockWorkflowContext(resume_inputs=bad_callable)
        assert get_pipeline_interrupt_ids(ctx) == set()

    def test_session_events_long_running_tool_ids(self):
        ev = SimpleNamespace(
            invocation_id="inv-1",
            long_running_tool_ids=["interrupt_ext_1", "other_tool_id"],
            content=None,
        )
        session = SimpleNamespace(events=[ev])
        ctx = MockWorkflowContext(invocation_id="inv-1", session=session)
        assert get_pipeline_interrupt_ids(ctx) == {"interrupt_ext_1"}

    def test_session_events_function_call_parts(self):
        fc = SimpleNamespace(id="interrupt_ext_2")
        part = SimpleNamespace(function_call=fc)
        content = SimpleNamespace(parts=[part])
        ev = SimpleNamespace(
            invocation_id="inv-1",
            long_running_tool_ids=None,
            content=content,
        )
        session = SimpleNamespace(events=[ev])
        ctx = MockWorkflowContext(invocation_id="inv-1", session=session)
        assert get_pipeline_interrupt_ids(ctx) == {"interrupt_ext_2"}

    def test_invocation_id_filtering(self):
        ev_current = SimpleNamespace(
            invocation_id="inv-current",
            long_running_tool_ids=["interrupt_ext_current"],
            content=None,
        )
        ev_stale = SimpleNamespace(
            invocation_id="inv-prior",
            long_running_tool_ids=["interrupt_ext_stale"],
            content=None,
        )
        session = SimpleNamespace(events=[ev_stale, ev_current])
        ctx = MockWorkflowContext(invocation_id="inv-current", session=session)
        result = get_pipeline_interrupt_ids(ctx)
        assert "interrupt_ext_current" in result
        assert "interrupt_ext_stale" not in result

    def test_interrupt_ids_without_invocation_id_scans_all_events(self):
        ev1 = SimpleNamespace(
            invocation_id="inv-1",
            long_running_tool_ids=["interrupt_ext_1"],
            content=None,
        )
        ev2 = SimpleNamespace(
            invocation_id="inv-2",
            long_running_tool_ids=["interrupt_ext_2"],
            content=None,
        )
        session = SimpleNamespace(events=[ev1, ev2])
        ctx = MockWorkflowContext(invocation_id=None, session=session)
        ids = get_pipeline_interrupt_ids(ctx)
        assert ids == {"interrupt_ext_1", "interrupt_ext_2"}

    def test_fallback_to_invocation_context(self):
        ev = SimpleNamespace(
            invocation_id="inv-fallback",
            long_running_tool_ids=["interrupt_ext_3"],
            content=None,
        )
        session = SimpleNamespace(events=[ev])
        inv_ctx = MockInvocationContext(invocation_id="inv-fallback", session=session)
        ctx = MockWorkflowContext(invocation_context=inv_ctx)
        assert get_pipeline_interrupt_ids(ctx) == {"interrupt_ext_3"}

    def test_empty_returns_empty_set(self):
        ctx = MockWorkflowContext()
        assert get_pipeline_interrupt_ids(ctx) == set()


# ---------------------------------------------------------------------------
# Tests for recover_isolation_scope & ensure_isolation_scope
# ---------------------------------------------------------------------------


class TestScopeRecoveryAndEnsure:
    """Unit tests for recover_isolation_scope and ensure_isolation_scope."""

    def test_recover_already_set_on_ctx(self):
        ctx = MockWorkflowContext(isolation_scope="search_pipeline:fc-123")
        assert recover_isolation_scope(ctx, prefixes="search_pipeline:") == "search_pipeline:fc-123"

    def test_recover_from_hitl_snapshot(self):
        ctx = MockWorkflowContext(
            state={HITL_SNAPSHOT: {"isolation_scope": "search_pipeline:snapshot-scope"}}
        )
        assert recover_isolation_scope(ctx, prefixes="search_pipeline:") == "search_pipeline:snapshot-scope"

    def test_recover_from_session_events_matching_prefix(self):
        ev1 = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_library_pipeline:fc-match",
        )
        ev2 = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="coordinator_scope:fc-ignore",
        )
        session = SimpleNamespace(events=[ev2, ev1])
        ctx = MockWorkflowContext(invocation_id="inv-1", session=session)
        assert (
            recover_isolation_scope(
                ctx, prefixes=("search_library_pipeline:", "search_pipeline:")
            )
            == "search_library_pipeline:fc-match"
        )

    def test_recover_session_events_non_matching_prefix_returns_none(self):
        ev = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="unrelated_scope:fc-xyz",
        )
        session = SimpleNamespace(events=[ev])
        ctx = MockWorkflowContext(invocation_id="inv-1", session=session)
        assert (
            recover_isolation_scope(
                ctx, prefixes=("search_library_pipeline:", "search_pipeline:")
            )
            is None
        )

    def test_recover_with_custom_prefixes_tuple(self):
        ev = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_online_pipeline:fc-custom",
        )
        session = SimpleNamespace(events=[ev])
        ctx = MockWorkflowContext(invocation_id="inv-1", session=session)
        # With online search prefix only
        assert (
            recover_isolation_scope(ctx, prefixes=("search_online_pipeline:",))
            == "search_online_pipeline:fc-custom"
        )
        # With library search prefixes only, should not match
        assert (
            recover_isolation_scope(
                ctx, prefixes=("search_library_pipeline:", "search_pipeline:")
            )
            is None
        )

    def test_recover_with_custom_prefix_string(self):
        ev = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="prefix_str:fc-single",
        )
        session = SimpleNamespace(events=[ev])
        ctx = MockWorkflowContext(invocation_id="inv-1", session=session)
        assert recover_isolation_scope(ctx, prefixes="prefix_str:") == "prefix_str:fc-single"

    def test_recover_prioritizes_current_invocation_id(self):
        ev_old = SimpleNamespace(
            invocation_id="inv-old",
            isolation_scope="search_pipeline:old-scope",
        )
        ev_new = SimpleNamespace(
            invocation_id="inv-new",
            isolation_scope="search_pipeline:new-scope",
        )
        session = SimpleNamespace(events=[ev_old, ev_new])
        ctx = MockWorkflowContext(invocation_id="inv-new", session=session)
        assert recover_isolation_scope(ctx, prefixes="search_pipeline:") == "search_pipeline:new-scope"

    def test_ensure_isolation_scope_assigns_to_ctx_and_invocation_ctx(self):
        inv_ctx = MockInvocationContext(invocation_id="inv-1")
        ctx = MockWorkflowContext(
            state={HITL_SNAPSHOT: {"isolation_scope": "search_pipeline:fc-assigned"}},
            invocation_context=inv_ctx,
        )
        assert ctx.isolation_scope is None
        assert inv_ctx.isolation_scope is None

        recovered = ensure_isolation_scope(ctx, prefixes="search_pipeline:")
        assert recovered == "search_pipeline:fc-assigned"
        assert ctx.isolation_scope == "search_pipeline:fc-assigned"
        assert inv_ctx.isolation_scope == "search_pipeline:fc-assigned"

    def test_ensure_isolation_scope_handles_assignment_attribute_error_gracefully(self):
        class ReadOnlyContext:
            def __init__(self, scope: str | None = None):
                self._scope = scope

            @property
            def isolation_scope(self):
                return self._scope

            @isolation_scope.setter
            def isolation_scope(self, val):
                raise AttributeError("Read-only scope attribute")

        ctx = ReadOnlyContext(scope="search_pipeline:fixed")
        # Should not raise exception
        assert ensure_isolation_scope(ctx, prefixes="search_pipeline:") == "search_pipeline:fixed"


# ---------------------------------------------------------------------------
# Tests for enqueue_isolation_scope_closing_event
# ---------------------------------------------------------------------------


class TestEnqueueIsolationScopeClosingEvent:
    """Unit tests for enqueue_isolation_scope_closing_event emitting finish_task markers."""

    @pytest.mark.asyncio
    async def test_event_formation_and_payload(self):
        inv_ctx = MockInvocationContext(invocation_id="inv-close-123", branch="main")
        ctx = MockWorkflowContext(
            isolation_scope="search_pipeline:fc-999",
            agent_name="custom_pipeline_node",
            invocation_context=inv_ctx,
        )

        await enqueue_isolation_scope_closing_event(ctx, result="Task completed.")

        assert len(inv_ctx.enqueued_events) == 1
        ev = inv_ctx.enqueued_events[0]
        assert ev.author == "custom_pipeline_node"
        assert ev.isolation_scope == "search_pipeline:fc-999"
        assert ev.invocation_id == "inv-close-123"
        assert ev.branch == "main"
        assert ev.content.role == "tool"
        assert len(ev.content.parts) == 1
        part = ev.content.parts[0]
        assert part.function_response.name == "finish_task"
        assert part.function_response.response == {"result": "Task completed."}

    @pytest.mark.asyncio
    async def test_fallback_to_default_author(self):
        inv_ctx = MockInvocationContext(invocation_id="inv-close-456")
        ctx = MockWorkflowContext(
            isolation_scope="search_pipeline:fc-default-auth",
            agent_name=None,
            invocation_context=inv_ctx,
        )

        await enqueue_isolation_scope_closing_event(
            ctx,
            result="Task completed.",
            default_author="search_library_pipeline_node",
        )

        assert len(inv_ctx.enqueued_events) == 1
        assert inv_ctx.enqueued_events[0].author == "search_library_pipeline_node"

    @pytest.mark.asyncio
    async def test_no_isolation_scope_noop(self):
        inv_ctx = MockInvocationContext()
        ctx = MockWorkflowContext(isolation_scope=None, invocation_context=inv_ctx)

        await enqueue_isolation_scope_closing_event(ctx, result="Task completed.")
        assert len(inv_ctx.enqueued_events) == 0

    @pytest.mark.asyncio
    async def test_no_invocation_context_noop(self):
        ctx = MockWorkflowContext(isolation_scope="search_pipeline:fc-no-inv", invocation_context=None)
        # Should not raise exception
        await enqueue_isolation_scope_closing_event(ctx, result="Task completed.")

    @pytest.mark.asyncio
    async def test_enqueue_exception_handled_gracefully(self):
        inv_ctx = MockInvocationContext()
        inv_ctx._enqueue_event = AsyncMock(side_effect=RuntimeError("Channel closed"))
        ctx = MockWorkflowContext(
            isolation_scope="search_pipeline:fc-err",
            invocation_context=inv_ctx,
        )

        # Should log warning but not raise exception
        await enqueue_isolation_scope_closing_event(ctx, result="Task completed.")


# ---------------------------------------------------------------------------
# Tests for Module Integration & Aliases
# ---------------------------------------------------------------------------


class TestPipelineScaffoldingIntegration:
    """Verify library_search and online_search pipelines work seamlessly with extracted helpers."""

    def test_library_search_pipeline_delegates_to_base(self):
        # 1. _recover_isolation_scope accepts library search prefixes
        ev_lib = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_library_pipeline:fc-1",
        )
        ev_legacy = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_pipeline:fc-2",
        )
        ev_online = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_online_pipeline:fc-3",
        )

        ctx_lib = MockWorkflowContext(invocation_id="inv-1", session=SimpleNamespace(events=[ev_lib]))
        assert lib_pipeline._recover_isolation_scope(ctx_lib) == "search_library_pipeline:fc-1"

        ctx_legacy = MockWorkflowContext(invocation_id="inv-1", session=SimpleNamespace(events=[ev_legacy]))
        assert lib_pipeline._recover_isolation_scope(ctx_legacy) == "search_pipeline:fc-2"

        # Online search prefix must NOT match library_search pipeline recovery
        ctx_online = MockWorkflowContext(invocation_id="inv-1", session=SimpleNamespace(events=[ev_online]))
        assert lib_pipeline._recover_isolation_scope(ctx_online) is None

        # 3. _ensure_isolation_scope assigns scope
        inv_ctx = MockInvocationContext(invocation_id="inv-1")
        ctx_ensure = MockWorkflowContext(
            invocation_id="inv-1",
            session=SimpleNamespace(events=[ev_lib]),
            invocation_context=inv_ctx,
        )
        recovered = lib_pipeline._ensure_isolation_scope(ctx_ensure)
        assert recovered == "search_library_pipeline:fc-1"
        assert ctx_ensure.isolation_scope == "search_library_pipeline:fc-1"
        assert inv_ctx.isolation_scope == "search_library_pipeline:fc-1"

        # 4. _get_pipeline_interrupt_ids delegates
        ctx_int = MockWorkflowContext(resume_inputs={"interrupt_ext_1": {}})
        assert lib_pipeline._get_pipeline_interrupt_ids(ctx_int) == {"interrupt_ext_1"}

    @pytest.mark.asyncio
    async def test_library_search_pipeline_closing_event_author(self):
        inv_ctx = MockInvocationContext(invocation_id="inv-lib")
        ctx = MockWorkflowContext(
            isolation_scope="search_library_pipeline:fc-test",
            invocation_context=inv_ctx,
        )
        await lib_pipeline._enqueue_isolation_scope_closing_event(ctx, result="Task completed.")
        assert len(inv_ctx.enqueued_events) == 1
        assert inv_ctx.enqueued_events[0].author == "search_library_pipeline_node"

    def test_online_search_pipeline_delegates_to_base(self):
        # 1. _recover_isolation_scope accepts online search prefix only
        ev_online = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_online_pipeline:fc-onl",
        )
        ev_lib = SimpleNamespace(
            invocation_id="inv-1",
            isolation_scope="search_library_pipeline:fc-lib",
        )

        ctx_online = MockWorkflowContext(invocation_id="inv-1", session=SimpleNamespace(events=[ev_online]))
        assert online_pipeline._recover_isolation_scope(ctx_online) == "search_online_pipeline:fc-onl"

        # Library search prefix must NOT match online_search pipeline recovery
        ctx_lib = MockWorkflowContext(invocation_id="inv-1", session=SimpleNamespace(events=[ev_lib]))
        assert online_pipeline._recover_isolation_scope(ctx_lib) is None

        # 3. _ensure_isolation_scope assigns scope
        inv_ctx = MockInvocationContext(invocation_id="inv-1")
        ctx_ensure = MockWorkflowContext(
            invocation_id="inv-1",
            session=SimpleNamespace(events=[ev_online]),
            invocation_context=inv_ctx,
        )
        recovered = online_pipeline._ensure_isolation_scope(ctx_ensure)
        assert recovered == "search_online_pipeline:fc-onl"
        assert ctx_ensure.isolation_scope == "search_online_pipeline:fc-onl"
        assert inv_ctx.isolation_scope == "search_online_pipeline:fc-onl"

        # 4. _get_pipeline_interrupt_ids delegates
        ctx_int = MockWorkflowContext(resume_inputs={"interrupt_ext_2": {}})
        assert online_pipeline._get_pipeline_interrupt_ids(ctx_int) == {"interrupt_ext_2"}

    @pytest.mark.asyncio
    async def test_online_search_pipeline_closing_event_author(self):
        inv_ctx = MockInvocationContext(invocation_id="inv-onl")
        ctx = MockWorkflowContext(
            isolation_scope="search_online_pipeline:fc-test",
            invocation_context=inv_ctx,
        )
        await online_pipeline._enqueue_isolation_scope_closing_event(ctx, result="Task completed.")
        assert len(inv_ctx.enqueued_events) == 1
        assert inv_ctx.enqueued_events[0].author == "search_online_pipeline_node"
