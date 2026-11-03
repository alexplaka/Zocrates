"""Shared scaffolding and scope management helpers for dynamic search workflows."""

import logging
from typing import Any

from google.adk.agents.context import Context
from google.adk.events import Event
from google.genai import types

from zocrates.agents.common.state_keys import HITL_SNAPSHOT

logger = logging.getLogger(__name__)


def coerce_to_text(response: Any) -> str:
    """Extract plain text string from string, Content, Part, dict, Event, or object.

    Extracts text parts while strictly suppressing reasoning/thought parts (where
    `thought=True`) and placing `parts` extraction ahead of `.text` to prevent
    internal thought tokens or raw object reprs from leaking into user-facing answers.
    """
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if getattr(response, "thought", False):
        return ""
    if hasattr(response, "output") and response.output is not None:
        return coerce_to_text(response.output)
    if hasattr(response, "parts") and response.parts:
        return "".join(
            getattr(p, "text", "")
            for p in response.parts
            if getattr(p, "text", None) and not getattr(p, "thought", False)
        )
    if hasattr(response, "text") and response.text:
        return str(response.text)
    if hasattr(response, "content"):
        return coerce_to_text(response.content)
    if isinstance(response, dict):
        if response.get("thought", False):
            return ""
        if "text" in response:
            return str(response["text"])
        if "content" in response:
            return coerce_to_text(response["content"])
    return str(response)


def get_pipeline_interrupt_ids(ctx: Context) -> set[str]:
    """Scan session events and ctx.resume_inputs to return set of all pipeline interrupt IDs."""
    interrupts: set[str] = set()
    resume_inputs = getattr(ctx, "resume_inputs", None)
    if callable(resume_inputs):
        try:
            resume_inputs = resume_inputs()
        except Exception:
            resume_inputs = None
    if isinstance(resume_inputs, dict):
        for k in resume_inputs:
            if isinstance(k, str) and k.startswith("interrupt_ext_"):
                interrupts.add(k)

    curr_inv_id = getattr(ctx, "invocation_id", None) or (
        getattr(ctx._invocation_context, "invocation_id", None)
        if hasattr(ctx, "_invocation_context")
        else None
    )
    session = getattr(ctx, "session", None)
    if not session and hasattr(ctx, "_invocation_context"):
        session = getattr(ctx._invocation_context, "session", None)
    if session and getattr(session, "events", None):
        for ev in session.events:
            if curr_inv_id and getattr(ev, "invocation_id", None) != curr_inv_id:
                continue
            if getattr(ev, "long_running_tool_ids", None):
                for int_id in ev.long_running_tool_ids:
                    if str(int_id).startswith("interrupt_ext_"):
                        interrupts.add(str(int_id))
            ev_content = getattr(ev, "content", None)
            if ev_content and getattr(ev_content, "parts", None):
                for p in ev_content.parts:
                    fc = getattr(p, "function_call", None)
                    if fc and getattr(fc, "id", None) and str(fc.id).startswith("interrupt_ext_"):
                        interrupts.add(str(fc.id))
    return interrupts


def recover_isolation_scope(
    ctx: Context,
    prefixes: tuple[str, ...] | list[str] | str,
) -> str | None:
    """Recover the search pipeline's isolation scope from snapshot or session events if unset.

    Args:
        ctx: Workflow execution context.
        prefixes: Required tuple, list, or string of acceptable isolation scope prefix(es)
            (e.g. `("search_library_pipeline:", "search_pipeline:")` or `("search_online_pipeline:",)`).
    """
    if getattr(ctx, "isolation_scope", None):
        return ctx.isolation_scope

    state = getattr(ctx, "state", None)
    if state:
        snapshot = state.get(HITL_SNAPSHOT)
        if isinstance(snapshot, dict) and snapshot.get("isolation_scope"):
            return snapshot["isolation_scope"]

    if isinstance(prefixes, str):
        prefixes_tuple = (prefixes,)
    else:
        prefixes_tuple = tuple(prefixes)

    # Fall back to scanning session events in reverse
    curr_inv_id = getattr(ctx, "invocation_id", None) or (
        getattr(ctx._invocation_context, "invocation_id", None)
        if hasattr(ctx, "_invocation_context")
        else None
    )
    session = getattr(ctx, "session", None)
    if not session and hasattr(ctx, "_invocation_context"):
        session = getattr(ctx._invocation_context, "session", None)
    if session and getattr(session, "events", None):
        if curr_inv_id:
            for event in reversed(session.events):
                if getattr(event, "invocation_id", None) == curr_inv_id:
                    scope = getattr(event, "isolation_scope", None)
                    if scope and str(scope).startswith(prefixes_tuple):
                        return str(scope)
        for event in reversed(session.events):
            scope = getattr(event, "isolation_scope", None)
            if scope and str(scope).startswith(prefixes_tuple):
                return str(scope)
    return None


def ensure_isolation_scope(
    ctx: Context,
    prefixes: tuple[str, ...] | list[str] | str,
) -> str | None:
    """Recover and assign isolation scope to ctx and its invocation context if unset.

    Args:
        ctx: Workflow execution context.
        prefixes: Required tuple, list, or string of acceptable isolation scope prefix(es).
    """
    scope = getattr(ctx, "isolation_scope", None) or recover_isolation_scope(ctx, prefixes=prefixes)
    if scope:
        try:
            ctx.isolation_scope = scope
        except (AttributeError, TypeError):
            pass
        if hasattr(ctx, "_invocation_context") and ctx._invocation_context is not None:
            try:
                ctx._invocation_context.isolation_scope = scope
            except (AttributeError, TypeError):
                pass
    return scope


async def enqueue_isolation_scope_closing_event(
    ctx: Context,
    *,
    result: str,
    default_author: str = "search_pipeline_node",
) -> None:
    """Emit a terminal ``finish_task`` event that closes ``ctx``'s isolation scope.

    ADK's ``Runner._find_active_task_scope`` treats any non-empty
    ``isolation_scope`` as an unclosed in-flight task agent awaiting user input
    unless an event carrying a ``finish_task`` FunctionResponse (result
    ``"Task completed."`` or ``"Task failed."``) is recorded for that scope.
    Stamping the pipeline's events with an isolation scope without explicit closure
    causes ADK to tag subsequent user turns with the stale scope, causing the coordinator
    (``isolation_scope=None``) to drop them. Emitting the closing event on the
    invocation context lets ``Runner._find_active_task_scope`` recognize the scope as finished.

    Args:
        ctx: The dynamically executed search pipeline context.
        result: Terminal ``finish_task`` result ("Task completed." or "Task failed.").
        default_author: Fallback author name if ctx.agent_name is unset.
    """
    if not getattr(ctx, "isolation_scope", None):
        return
    invocation_ctx = getattr(ctx, "_invocation_context", None)
    if invocation_ctx is None:
        return
    closing_event = Event(
        author=getattr(ctx, "agent_name", None) or default_author,
        isolation_scope=ctx.isolation_scope,
        invocation_id=getattr(invocation_ctx, "invocation_id", ""),
        branch=getattr(invocation_ctx, "branch", None),
        content=types.Content(
            role="tool",
            parts=[
                types.Part.from_function_response(
                    name="finish_task",
                    response={"result": result},
                )
            ],
        ),
    )
    # Never let a queueing failure mask or replace the synthesized turn result
    # or the original exception: this event is a best-effort scope-close marker.
    try:
        await invocation_ctx._enqueue_event(closing_event)
    except Exception as e:
        logger.warning("Failed to enqueue isolation scope closing event: %s", e)


__all__ = [
    "coerce_to_text",
    "get_pipeline_interrupt_ids",
    "recover_isolation_scope",
    "ensure_isolation_scope",
    "enqueue_isolation_scope_closing_event",
]
