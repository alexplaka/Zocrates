"""Compatibility patches for Google ADK runtime and workflow execution."""

import logging
from typing import Any

logger = logging.getLogger(__name__)

_adk_request_input_context_filter_patched: bool = False


def patch_adk_request_input_context_filter() -> None:
    """Patch ADK contents filter to exclude adk_request_input function calls/responses from agent LLM context.

    In dynamic workflows with Human-In-The-Loop (HITL), RequestInput emits an Event
    with a function call named 'adk_request_input' and the user resume response emits
    a function response named 'adk_request_input'. While ADK natively filters out
    internal framework events like 'adk_framework' and 'adk_request_credential' from
    being included in an agent's LLM context, it omits 'adk_request_input'. This causes
    ADK's _fencing module to wrap the checkpoint call and response in cross-agent
    fencing (<<<BEGIN_QUOTED_AGENT_CONTENT>>>) and inject them into search_subagent's
    prompt during extension passes.

    This patch ensures 'adk_request_input' events are filtered out from LLM context
    generation, preserving clean continuation prompts for subagents.
    """
    global _adk_request_input_context_filter_patched
    if _adk_request_input_context_filter_patched:
        return

    try:
        import google.adk.flows.llm_flows.contents as adk_contents

        orig_should_include = adk_contents._should_include_event_in_context

        def _is_request_input_event(event: Any) -> bool:
            """Check if an event contains an adk_request_input tool call or response.

            Args:
                event: The event to inspect for request-input parts.

            Returns:
                True if the event contains an adk_request_input call/response, False otherwise.
            """
            content = getattr(event, "content", None)
            parts = getattr(content, "parts", None) if content else None
            if not parts:
                return False
            for part in parts:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None) == "adk_request_input":
                    return True
                fr = getattr(part, "function_response", None)
                if fr and getattr(fr, "name", None) == "adk_request_input":
                    return True
            return False

        def patched_should_include_event_in_context(
            current_branch: str | None,
            event: Any,
            isolation_scope: str | None = None,
            *,
            include_thoughts: bool = False,
            **kwargs: Any,
        ) -> bool:
            """Filter out adk_request_input events before delegating to the original ADK context filter.

            Args:
                current_branch: The current execution branch in the session flow.
                event: The session event being evaluated for inclusion in LLM context.
                isolation_scope: Active isolation scope identifier, if any.
                include_thoughts: Whether to retain agent reasoning/thought parts.
                **kwargs: Additional parameters forwarded to the original ADK filter.

            Returns:
                False if event is an adk_request_input event, otherwise the original filter decision.
            """
            if _is_request_input_event(event):
                return False
            return orig_should_include(
                current_branch,
                event,
                isolation_scope=isolation_scope,
                include_thoughts=include_thoughts,
                **kwargs,
            )

        adk_contents._should_include_event_in_context = patched_should_include_event_in_context
        _adk_request_input_context_filter_patched = True
        logger.debug(
            "Patched google.adk.flows.llm_flows.contents._should_include_event_in_context to filter adk_request_input"
        )
    except Exception as err:
        logger.warning("Could not patch ADK contents _should_include_event_in_context: %s", err)


_compaction_subagent_isolation_patched: bool = False


def _has_compaction_for_active_turn(session_events: list[Any]) -> bool:
    """Check if a compaction event has already been recorded for the currently active turn."""
    latest_user_ts = None
    for e in reversed(session_events):
        if e.author == "user" and not (getattr(e, "actions", None) and getattr(e.actions, "compaction", None)):
            latest_user_ts = getattr(e, "timestamp", None)
            break
    if latest_user_ts is None:
        return False
    for e in session_events:
        if getattr(e, "actions", None) and getattr(e.actions, "compaction", None):
            ts = getattr(e, "timestamp", 0.0)
            if ts >= latest_user_ts:
                return True
    return False


def patch_compaction_subagent_isolation() -> None:
    """Ensure token compaction only executes for the root coordinator agent, never for subagents,
    and eliminate duplicate post-invocation compaction runs.

    In multi-agent systems, subagents (e.g. library_search_agent, online_search_agent)
    execute inside isolated pipelines. If token-threshold compaction runs inside subagents,
    it mistakenly compacts the parent session's history before every internal tool call,
    causing massive latency overhead and redundant compaction cycles.

    Additionally, Google ADK 2.8.0 contains an upstream context propagation bug where
    `BaseAgent._create_invocation_context` performs a shallow `model_copy()` of InvocationContext,
    causing `token_compaction_checked = True` to only be set on the child context and never reaching
    the root context in `runners.py`. This causes runners.py to pass `skip_token_compaction=False` to
    `_run_post_invocation_compaction`, triggering a second, completely redundant compaction cycle.
    This patch links parent contexts and guards `_run_compaction_for_token_threshold` so compaction
    never executes more than once per turn.
    """
    global _compaction_subagent_isolation_patched
    if _compaction_subagent_isolation_patched:
        return

    try:
        from google.adk.agents.base_agent import BaseAgent
        import google.adk.apps.compaction as adk_app_compaction
        import google.adk.flows.llm_flows.compaction as adk_compaction

        # 1. Link child InvocationContext to parent context so flags propagate back to root runner
        orig_create_invocation_context = BaseAgent._create_invocation_context

        def patched_create_invocation_context(agent_self: Any, parent_context: Any) -> Any:
            """Create child invocation context and attach a reference to its parent context.

            Args:
                agent_self: The agent instance creating the invocation context.
                parent_context: The parent invocation context to link against.

            Returns:
                The created child invocation context with `_parent_context` attached.
            """
            child = orig_create_invocation_context(agent_self, parent_context)
            child._parent_context = parent_context
            return child

        BaseAgent._create_invocation_context = patched_create_invocation_context

        # 2. Patch CompactionRequestProcessor to isolate token compaction to root coordinator
        # and propagate token_compaction_checked flag to parent contexts
        orig_run_async = adk_compaction.CompactionRequestProcessor.run_async

        async def patched_run_async(self: Any, invocation_context: Any, llm_request: Any) -> Any:
            """Execute compaction request processor only for root coordinator agent instances.

            Args:
                self: The CompactionRequestProcessor instance.
                invocation_context: Current invocation context.
                llm_request: The pending LLM request.

            Yields:
                Events yielded by the original compaction request processor.
            """
            # Skip if running inside an isolation scope (subagent pipeline)
            if getattr(invocation_context, "isolation_scope", None) is not None:
                return
                yield

            # Skip if agent is a subagent (only coordinator_agent compacts session history)
            agent = getattr(invocation_context, "agent", None)
            if agent and getattr(agent, "name", None) not in ("coordinator_agent", "coordinator"):
                return
                yield

            async for event in orig_run_async(self, invocation_context, llm_request):
                yield event

            # Propagate token_compaction_checked back to root invocation context in runner loop
            if getattr(invocation_context, "token_compaction_checked", False):
                curr = getattr(invocation_context, "_parent_context", None)
                while curr is not None:
                    curr.token_compaction_checked = True
                    curr = getattr(curr, "_parent_context", None)

        adk_compaction.CompactionRequestProcessor.run_async = patched_run_async
        adk_compaction.request_processor.run_async = patched_run_async.__get__(
            adk_compaction.request_processor, adk_compaction.CompactionRequestProcessor
        )

        # 3. Guard post-invocation token-threshold compaction against duplicate execution
        orig_token_compaction = adk_app_compaction._run_compaction_for_token_threshold

        async def patched_run_compaction_for_token_threshold(
            app: Any, session: Any, session_service: Any
        ) -> bool:
            """Guard post-invocation token-threshold compaction against duplicate execution per turn.

            Args:
                app: The ADK application instance.
                session: The active session being evaluated for compaction.
                session_service: Session service handling session persistence.

            Returns:
                True if compaction was executed, False if skipped or not triggered.
            """
            if session and getattr(session, "events", None) and _has_compaction_for_active_turn(session.events):
                logger.debug(
                    "Skipping duplicate post-invocation token-threshold compaction: "
                    "a compaction event already exists for the active turn."
                )
                return False
            return await orig_token_compaction(app, session, session_service)

        adk_app_compaction._run_compaction_for_token_threshold = patched_run_compaction_for_token_threshold

        # 4. Patch Runner._run_post_invocation_compaction:
        # - Suppress compaction when invocation is interrupted mid-turn for HITL (e.g. search_budget_checkpoint)
        # - Always skip token-threshold compaction in post-invocation since it is strictly evaluated pre-invocation,
        #   preventing duplicate runs, empty Phoenix spans, and unnecessary end-of-turn latency.
        from google.adk.runners import Runner

        orig_post_compaction = Runner._run_post_invocation_compaction

        async def patched_post_invocation_compaction(
            runner_self: Any,
            *,
            session: Any,
            skip_token_compaction: bool,
        ) -> None:
            """Run adaptive post-turn compaction and suppress redundant post-invocation compaction.

            Args:
                runner_self: The Runner instance executing post-invocation steps.
                session: The active session.
                skip_token_compaction: Whether to skip token-threshold compaction.
            """
            if session and getattr(session, "events", None):
                last_ev = session.events[-1]
                if last_ev.content and any(getattr(p, "function_call", None) for p in last_ev.content.parts):
                    logger.debug("Skipping post-invocation compaction: invocation was interrupted mid-turn for HITL.")
                    return
                if getattr(last_ev, "actions", None) and (
                    getattr(last_ev.actions, "requested_tool_confirmations", None)
                    or getattr(last_ev.actions, "requested_auth_configs", None)
                ):
                    logger.debug("Skipping post-invocation compaction: invocation awaiting tool confirmation.")
                    return

            try:
                from zocrates.agents.common.compaction import run_adaptive_post_invocation_compaction

                await run_adaptive_post_invocation_compaction(runner_self, session)
            except Exception as exc:
                logger.warning("Adaptive post-invocation compaction failed open: %s", exc)

            await orig_post_compaction(
                runner_self,
                session=session,
                skip_token_compaction=True,
            )

        Runner._run_post_invocation_compaction = patched_post_invocation_compaction

        _compaction_subagent_isolation_patched = True
        logger.debug("Patched ADK CompactionRequestProcessor, Runner, and post-invocation token compaction guards")
    except Exception as err:
        logger.warning("Could not patch ADK CompactionRequestProcessor: %s", err)


_adk_active_task_scope_rewind_patched: bool = False


def patch_adk_active_task_scope_rewind() -> None:
    """Patch ADK _find_active_task_scope to filter out rewound events before checking active scopes.

    When an in-flight subagent pipeline (which stamps events with an isolation_scope,
    e.g. 'search_library_pipeline:xyz' or 'search_online_pipeline:xyz') is rewound via
    rewind_before_invocation_id, its unfinished isolation scope remains in session.events.
    ADK's native _find_active_task_scope walks session.events directly without taking rewinds
    into account, causing subsequent user turns to mistakenly be stamped with the dead
    subagent's isolation scope.

    This patch ensures _find_active_task_scope evaluates active task scopes against
    the live events filtered by _apply_rewinds(session.events).
    """
    global _adk_active_task_scope_rewind_patched
    if _adk_active_task_scope_rewind_patched:
        return

    try:
        import google.adk.runners as adk_runners
        from google.adk.flows.llm_flows.contents import _apply_rewinds

        orig_find_active_task_scope = adk_runners._find_active_task_scope

        class _RewoundSessionProxy:
            """Lightweight proxy exposing rewound events while preserving session attributes."""

            def __init__(self, session: Any, live_events: list[Any]) -> None:
                """Initialize session proxy wrapping live non-rewound events.

                Args:
                    session: The underlying ADK session instance.
                    live_events: Filtered list of active session events after applying rewinds.
                """
                self._session = session
                self.events = live_events

            def __getattr__(self, name: str) -> Any:
                """Delegate attribute lookups to the underlying session instance.

                Args:
                    name: Attribute name to resolve on the underlying session.

                Returns:
                    The attribute value from the underlying session.
                """
                return getattr(self._session, name)

        def patched_find_active_task_scope(session: Any) -> tuple[str, str] | None:
            """Find the active task scope by filtering out rewound events first.

            Args:
                session: The ADK session to inspect for an active task scope.

            Returns:
                A tuple of (scope_id, task_name) if an active scope exists, otherwise None.
            """
            if not session or getattr(session, "events", None) is None:
                return None
            live_events = _apply_rewinds(session.events)
            proxy = _RewoundSessionProxy(session, live_events)
            return orig_find_active_task_scope(proxy)

        adk_runners._find_active_task_scope = patched_find_active_task_scope
        _adk_active_task_scope_rewind_patched = True
        logger.debug(
            "Patched google.adk.runners._find_active_task_scope to respect _apply_rewinds"
        )
    except Exception as err:
        logger.warning("Could not patch ADK _find_active_task_scope: %s", err)


