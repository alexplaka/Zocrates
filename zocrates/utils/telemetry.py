import atexit
import json
import logging
import logging.handlers
import os
import re
import socket
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google.adk.cli.api_server import _setup_instrumentation_lib_if_installed
from opentelemetry.sdk.trace import SpanProcessor

_telemetry_configured: bool = False
_active_telemetry_endpoint: str | None = None
_adk_semconv_patched: bool = False


def _patch_adk_semconv_to_part() -> None:
    """Patch ADK experimental semconv to preserve thought=True on emitted message parts."""
    global _adk_semconv_patched
    if _adk_semconv_patched:
        return
    try:
        import google.adk.telemetry._experimental_semconv as semconv

        orig_to_part = semconv._to_part

        def patched_to_part(part: Any, idx: int, *args: Any, **kwargs: Any) -> Any:
            """Convert an ADK message part while preserving thought flag.

            Args:
                part: ADK content part to convert.
                idx: Index of the part within the message content.
                *args: Additional positional arguments forwarded to original _to_part.
                **kwargs: Additional keyword arguments forwarded to original _to_part.

            Returns:
                Converted semconv dictionary with 'thought' preserved if true.
            """
            res = orig_to_part(part, idx, *args, **kwargs)
            if res is not None and getattr(part, "thought", False):
                res["thought"] = True
            return res

        semconv._to_part = patched_to_part
        _adk_semconv_patched = True
        logging.debug("Patched google.adk.telemetry._experimental_semconv._to_part to preserve thoughts")
    except Exception as err:
        logging.warning("Could not patch ADK semconv _to_part: %s", err)



def _is_port_open(host: str, port: int, timeout: float = 0.1) -> bool:
    """Quickly check whether a TCP port is open on localhost."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            return s.connect_ex((host, port)) == 0
    except Exception:
        return False


def _set_span_attribute(span: Any, key: str, value: Any) -> None:
    """Safely attach or mutate an attribute on an active or ended OpenTelemetry span."""
    attrs = getattr(span, "_attributes", None)
    if attrs is not None:
        if hasattr(attrs, "_dict"):
            attrs._dict[key] = value
        elif hasattr(attrs, "_immutable"):
            attrs._immutable = False
            attrs[key] = value
            attrs._immutable = True
        elif isinstance(attrs, dict):
            attrs[key] = value

    if hasattr(span, "set_attribute"):
        try:
            span.set_attribute(key, value)
        except Exception:
            pass


_THINK_TAG_REGEX = re.compile(r"<think>(.*?)</think>(.*)", re.DOTALL)
_THINKING_PREFIX_REGEX = re.compile(
    r"^(?:(?:here'?s|here\s+is)\s+a\s+(?:thinking|thought)\s+process|thinking\s+process|thought\s+process):",
    re.IGNORECASE,
)


def _is_tool_call(part: dict[str, Any]) -> bool:
    """Check if a message part dictionary represents a tool or function call.

    Args:
        part: Message part dictionary to inspect.

    Returns:
        True if the part is a tool call or function call, False otherwise.
    """
    return (
        part.get("type") == "tool_call"
        or "function_call" in part
        or bool(part.get("function_call"))
    )


def _format_parts_with_labeled_thoughts(
    parts: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Separate and clearly label model thoughts/reasoning from final answers and tool calls.

    Returns:
        tuple of (output_value_string, enriched_parts_list)
    """
    if not parts:
        return "", []

    tool_indices = [i for i, p in enumerate(parts) if _is_tool_call(p)]
    has_tool_calls = bool(tool_indices)
    first_tool_idx = tool_indices[0] if has_tool_calls else -1
    last_tool_idx = tool_indices[-1] if has_tool_calls else -1
    has_text_after_tools = False
    if has_tool_calls:
        for p in parts[last_tool_idx + 1 :]:
            if not _is_tool_call(p):
                t = p.get("content") or p.get("text") or ""
                if isinstance(t, str) and t.strip():
                    has_text_after_tools = True
                    break
    is_preamble_before_tools = has_tool_calls and not has_text_after_tools

    thought_chunks: list[str] = []
    response_chunks: list[str] = []
    tool_call_parts: list[dict[str, Any]] = []
    tool_call_texts: list[str] = []

    in_think_tag = False

    for idx, part in enumerate(parts):
        if _is_tool_call(part):
            fc = part.get("function_call") or {}
            fn_name = part.get("name") or fc.get("name") or "tool"
            args = part.get("arguments") or fc.get("args") or {}
            args_str = (
                json.dumps(args, ensure_ascii=False)
                if isinstance(args, dict)
                else str(args)
            )
            tool_call_texts.append(f"⚙️ **Tool Call:** `{fn_name}`({args_str})")
            tool_call_parts.append(part)
            continue

        text = part.get("content") or part.get("text") or ""
        if not isinstance(text, str):
            text = str(text or "")

        thought_val = part.get("thought")
        is_explicit_thought = bool(thought_val)

        if in_think_tag:
            if "</think>" in text:
                before, after = text.split("</think>", 1)
                if before:
                    thought_chunks.append(before)
                in_think_tag = False
                if after:
                    if "<think>" in after:
                        m = _THINK_TAG_REGEX.search(after)
                        if m:
                            th = m.group(1)
                            ans = m.group(2)
                            if th:
                                thought_chunks.append(th)
                            if ans:
                                response_chunks.append(ans)
                        else:
                            th_before, th_after = after.split("<think>", 1)
                            if th_before:
                                response_chunks.append(th_before)
                            if th_after:
                                thought_chunks.append(th_after)
                            in_think_tag = True
                    else:
                        response_chunks.append(after)
            else:
                thought_chunks.append(text)
            continue

        if "<think>" in text:
            if "</think>" in text:
                m = _THINK_TAG_REGEX.search(text)
                if m:
                    th = m.group(1)
                    ans = m.group(2)
                    if th:
                        thought_chunks.append(th)
                    if ans:
                        response_chunks.append(ans)
                else:
                    before_think, after_think = text.split("<think>", 1)
                    if before_think:
                        response_chunks.append(before_think)
                    th_part, after_close = after_think.split("</think>", 1)
                    if th_part:
                        thought_chunks.append(th_part)
                    if after_close:
                        response_chunks.append(after_close)
            else:
                before_think, after_think = text.split("<think>", 1)
                if before_think:
                    response_chunks.append(before_think)
                if after_think:
                    thought_chunks.append(after_think)
                in_think_tag = True
            continue

        if is_explicit_thought:
            if isinstance(thought_val, str) and thought_val:
                thought_chunks.append(thought_val)
            elif text:
                thought_chunks.append(text)
        elif _THINKING_PREFIX_REGEX.search(text.strip()):
            thought_chunks.append(text)
        elif is_preamble_before_tools and idx < first_tool_idx:
            thought_chunks.append(text)
        else:
            if text:
                response_chunks.append(text)

    joined_thought = "".join(thought_chunks).strip()
    joined_response = "".join(response_chunks).strip()

    enriched_parts: list[dict[str, Any]] = []

    if joined_thought:
        enriched_parts.append({
            "type": "text",
            "content": f"### 💭 Thought Process\n\n{joined_thought}",
        })

    enriched_parts.extend(tool_call_parts)

    if joined_response:
        if joined_thought or tool_call_parts:
            enriched_parts.append({
                "type": "text",
                "content": f"### 💬 Response\n\n{joined_response}",
            })
        else:
            enriched_parts.append({
                "type": "text",
                "content": joined_response,
            })
    elif not joined_thought and not tool_call_parts and parts:
        enriched_parts = list(parts)

    sections: list[str] = []
    if joined_thought:
        sections.append(f"### 💭 Thought Process\n\n{joined_thought}")
    if tool_call_texts:
        sections.append("\n\n".join(tool_call_texts))
    if joined_response:
        if joined_thought or tool_call_texts:
            sections.append(f"### 💬 Response\n\n{joined_response}")
        else:
            sections.append(joined_response)

    output_value = "\n\n---\n\n".join(sections)
    return output_value, enriched_parts


def _summarize_merged_tool_response(resp_str: str) -> str:
    """Produce a concise summary for merged tool execution spans without state_delta bloat."""
    try:
        data = json.loads(resp_str)
        if isinstance(data, dict):
            parts = (
                data.get("content", {}).get("parts", [])
                if isinstance(data.get("content"), dict)
                else []
            )
            summaries: list[dict[str, Any]] = []
            for p in parts:
                fr = p.get("function_response")
                if fr and isinstance(fr, dict):
                    fn_name = fr.get("name", "tool")
                    call_id = fr.get("id")
                    fn_resp = fr.get("response", {})
                    status = (
                        fn_resp.get("status")
                        or fn_resp.get("retrieval_status")
                        or ("success" if "results" in fn_resp else "ok")
                    ) if isinstance(fn_resp, dict) else "ok"
                    summary_item: dict[str, Any] = {"tool": fn_name, "id": call_id, "status": status}
                    if isinstance(fn_resp, dict) and "retrieval_status" in fn_resp:
                        summary_item["info"] = fn_resp["retrieval_status"]
                    summaries.append(summary_item)
            if summaries:
                return json.dumps({"merged_tools": summaries}, ensure_ascii=False)
            if "actions" in data:
                cleaned = dict(data)
                cleaned.pop("actions", None)
                return json.dumps(cleaned, ensure_ascii=False)
    except Exception:
        pass
    return resp_str


class PhoenixEnrichmentSpanProcessor(SpanProcessor):
    """Enrich ADK spans with OpenInference attributes for Arize Phoenix observability."""

    def on_start(self, span: Any, parent_context: Any = None) -> None:
        """Handle span start event.

        Args:
            span: The OpenTelemetry span being started.
            parent_context: Optional parent context.
        """
        pass

    def on_end(self, span: Any) -> None:
        """Enrich OpenTelemetry span with OpenInference attributes upon span completion.

        Args:
            span: The OpenTelemetry span being ended.
        """
        attrs = getattr(span, "_attributes", None)
        if attrs is None:
            return

        name = getattr(span, "name", "")
        op = str(attrs.get("gen_ai.operation.name", ""))

        # 1. Tool execution spans (search_corpus, library_search, etc.)
        if name.startswith("execute_tool") or op == "execute_tool":
            _set_span_attribute(span, "openinference.span.kind", "TOOL")
            args = attrs.get("gcp.vertex.agent.tool_call_args")
            if args and args not in ("{}", "<not specified>"):
                _set_span_attribute(span, "tool.parameters", args)
                _set_span_attribute(span, "input.value", args)
                _set_span_attribute(span, "input.mime_type", "application/json")
                _set_span_attribute(span, "gen_ai.tool.call.arguments", args)

            resp = attrs.get("gcp.vertex.agent.tool_response")
            if resp and resp not in ("{}", "<not specified>"):
                if "(merged)" in name:
                    compact_resp = _summarize_merged_tool_response(resp)
                    _set_span_attribute(span, "output.value", compact_resp)
                    _set_span_attribute(span, "output.mime_type", "application/json")
                    _set_span_attribute(span, "gen_ai.tool.call.result", compact_resp)
                else:
                    _set_span_attribute(span, "output.value", resp)
                    _set_span_attribute(span, "output.mime_type", "application/json")
                    _set_span_attribute(span, "gen_ai.tool.call.result", resp)

        # 2. LLM inference spans (generate_content, call_llm)
        elif (
            name.startswith("generate_content")
            or name == "call_llm"
            or name == "predict_next_user_prompt"
            or name == "compact_session_history"
            or op == "generate_content"
            or attrs.get("openinference.span.kind") == "LLM"
        ):
            _set_span_attribute(span, "openinference.span.kind", "LLM")

            # Format and consolidate input messages & build clean input.value
            in_msgs_raw = attrs.get("gen_ai.input.messages")
            if in_msgs_raw and isinstance(in_msgs_raw, str):
                try:
                    in_msgs_list = json.loads(in_msgs_raw)
                    consolidated_input_msgs = []
                    input_turn_texts = []
                    for in_msg in in_msgs_list:
                        role = in_msg.get("role", "user")
                        msg_parts = in_msg.get("parts", [])
                        if role == "assistant":
                            out_val, en_parts = _format_parts_with_labeled_thoughts(msg_parts)
                            msg_copy = dict(in_msg)
                            msg_copy["parts"] = en_parts
                            consolidated_input_msgs.append(msg_copy)
                            if out_val:
                                input_turn_texts.append(f"[{role}] {out_val}")
                        else:
                            text_chunks = []
                            other_parts = []
                            for p in msg_parts:
                                t = p.get("content") or p.get("text") or ""
                                if isinstance(t, str) and t:
                                    text_chunks.append(t)
                                else:
                                    other_parts.append(p)
                            joined_t = "".join(text_chunks).strip()
                            new_parts = []
                            if joined_t:
                                new_parts.append({"type": "text", "content": joined_t})
                            new_parts.extend(other_parts)
                            msg_copy = dict(in_msg)
                            msg_copy["parts"] = new_parts if new_parts else msg_parts
                            consolidated_input_msgs.append(msg_copy)
                            if joined_t:
                                input_turn_texts.append(f"[{role}] {joined_t}")
                            elif other_parts:
                                input_turn_texts.append(f"[{role}] (tool/function call)")

                    if input_turn_texts and "input.value" not in attrs:
                        _set_span_attribute(span, "input.value", "\n\n".join(input_turn_texts))
                    if consolidated_input_msgs:
                        _set_span_attribute(
                            span,
                            "gen_ai.input.messages",
                            json.dumps(consolidated_input_msgs, ensure_ascii=False),
                        )
                except Exception:
                    pass
            elif "gcp.vertex.agent.llm_request" in attrs:
                req_str = attrs["gcp.vertex.agent.llm_request"]
                if req_str and req_str != "{}":
                    try:
                        req_data = json.loads(req_str)
                        contents = req_data.get("contents", [])
                        texts = []
                        converted_in_msgs = []
                        for c in contents:
                            role = c.get("role", "user")
                            c_parts = c.get("parts", [])
                            part_texts = [
                                p.get("text", "")
                                for p in c_parts
                                if isinstance(p, dict) and "text" in p and p.get("text")
                            ]
                            if part_texts:
                                joined = "".join(part_texts).strip()
                                if joined:
                                    texts.append(f"[{role}] {joined}")
                                    converted_in_msgs.append({
                                        "role": role,
                                        "parts": [{"type": "text", "content": joined}],
                                    })
                        if texts and "input.value" not in attrs:
                            _set_span_attribute(span, "input.value", "\n\n".join(texts))
                        if converted_in_msgs and "gen_ai.input.messages" not in attrs:
                            _set_span_attribute(
                                span,
                                "gen_ai.input.messages",
                                json.dumps(converted_in_msgs, ensure_ascii=False),
                            )
                    except Exception:
                        pass

            # Format output messages & label model thoughts
            out_msgs_raw = attrs.get("gen_ai.output.messages")
            if out_msgs_raw and isinstance(out_msgs_raw, str):
                try:
                    out_msgs_list = json.loads(out_msgs_raw)
                    all_output_values = []
                    enriched_msgs = []
                    for out_msg in out_msgs_list:
                        msg_parts = out_msg.get("parts", [])
                        out_val, en_parts = _format_parts_with_labeled_thoughts(msg_parts)
                        if out_val:
                            all_output_values.append(out_val)
                        msg_copy = dict(out_msg)
                        msg_copy["parts"] = en_parts
                        enriched_msgs.append(msg_copy)

                    if all_output_values:
                        _set_span_attribute(span, "output.value", "\n\n---\n\n".join(all_output_values))
                    if enriched_msgs:
                        _set_span_attribute(
                            span, "gen_ai.output.messages", json.dumps(enriched_msgs, ensure_ascii=False)
                        )
                except Exception:
                    pass
            elif "gcp.vertex.agent.llm_response" in attrs:
                resp_str = attrs["gcp.vertex.agent.llm_response"]
                if resp_str and resp_str != "{}":
                    try:
                        resp_data = json.loads(resp_str)
                        content = resp_data.get("content", {})
                        parts = content.get("parts", [])
                        out_val, en_parts = _format_parts_with_labeled_thoughts(parts)
                        if out_val:
                            _set_span_attribute(span, "output.value", out_val)
                        if en_parts and "gen_ai.output.messages" not in attrs:
                            _set_span_attribute(
                                span,
                                "gen_ai.output.messages",
                                json.dumps([{"role": "assistant", "parts": en_parts}], ensure_ascii=False),
                            )
                    except Exception:
                        pass

        # 3. Agent spans (coordinator_agent, etc.)
        elif name.startswith("invoke_agent") or op == "invoke_agent":
            _set_span_attribute(span, "openinference.span.kind", "AGENT")
            agent_data = attrs.get("gcp.vertex.agent.data")
            if agent_data and agent_data != "{}":
                try:
                    parsed = json.loads(agent_data)
                    texts = []
                    for item in parsed:
                        item_texts = [
                            part.get("text", "")
                            for part in item.get("parts", [])
                            if isinstance(part, dict) and part.get("text")
                        ]
                        if item_texts:
                            joined = "".join(item_texts).strip()
                            if joined:
                                texts.append(joined)
                    if texts:
                        _set_span_attribute(span, "input.value", "\n\n".join(texts))
                except Exception:
                    pass

        # 4. Chain / node / root invocation spans
        elif name.startswith("invoke_node") or name == "invocation":
            _set_span_attribute(span, "openinference.span.kind", "CHAIN")

        # 5. Evaluator spans (decision probes)
        elif (
            name == "compaction_decision_probe"
            or attrs.get("openinference.span.kind") == "EVALUATOR"
        ):
            _set_span_attribute(span, "openinference.span.kind", "EVALUATOR")


def get_active_telemetry_endpoint() -> str | None:
    """Return the active OTLP collector endpoint if tracing is enabled."""
    return _active_telemetry_endpoint


def flush_telemetry(timeout_millis: int = 5000) -> None:
    """Flush pending OpenTelemetry spans before process exit."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis)
    except Exception as exc:
        logging.debug("Telemetry flush error: %s", exc)


def setup_telemetry() -> str | None:
    """Configure GenAI prompt/response logging and distributed tracing via OpenTelemetry.

    Sets up tracing via a local OTLP collector (e.g. Arize Phoenix) if
    ``LOCAL_TRACING=true``, ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, or a local
    Phoenix server is running. When no local collector is available, tracing is
    left unconfigured (returns ``None``).

    When ``LOGS_BUCKET_NAME`` is set, prompt/response content capture is
    disabled (metadata-only logging mode for compliance).

    Returns:
        The active telemetry endpoint or ``LOGS_BUCKET_NAME`` if enabled,
        ``None`` otherwise.
    """
    global _telemetry_configured, _active_telemetry_endpoint
    if _telemetry_configured:
        return _active_telemetry_endpoint or os.environ.get("LOGS_BUCKET_NAME")

    # Ensure environment variables from .env are loaded
    load_dotenv()

    # Patch ADK experimental semconv to preserve thought=True in gen_ai.output.messages
    _patch_adk_semconv_to_part()

    # Capture full prompts/responses and tool payloads in trace span attributes for local debugging.
    os.environ.setdefault("ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "true")
    if os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").lower() in ("true", "1"):
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "SPAN_AND_EVENT"
    else:
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_AND_EVENT")

    os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "gen_ai_latest_experimental")
    os.environ.setdefault("ADK_EXPERIMENTAL_TELEMETRY", "true")

    bucket = os.environ.get("LOGS_BUCKET_NAME")
    capture_content = os.environ.get(
        "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "SPAN_AND_EVENT"
    )
    if bucket and capture_content != "false":
        logging.info(
            "Prompt-response logging enabled - mode: NO_CONTENT (metadata only, no prompts/responses)"
        )
        os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "NO_CONTENT"
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_UPLOAD_FORMAT", "jsonl")
        os.environ.setdefault("OTEL_INSTRUMENTATION_GENAI_COMPLETION_HOOK", "upload")
        commit_sha = os.environ.get("COMMIT_SHA", "dev")
        os.environ.setdefault(
            "OTEL_RESOURCE_ATTRIBUTES",
            f"service.namespace=zocrates,service.version={commit_sha}",
        )
        path = os.environ.get("GENAI_TELEMETRY_PATH", "completions")
        os.environ.setdefault(
            "OTEL_INSTRUMENTATION_GENAI_UPLOAD_BASE_PATH",
            f"gs://{bucket}/{path}",
        )
    else:
        logging.info(
            "Prompt-response logging disabled (set LOGS_BUCKET_NAME=gs://your-bucket and OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=NO_CONTENT to enable)"
        )

    # Check for local OTLP collector settings (e.g. Arize Phoenix)
    explicit_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "PHOENIX_COLLECTOR_ENDPOINT"
    )
    local_tracing_env = os.environ.get("LOCAL_TRACING", "").strip().lower()
    is_explicit_disabled = local_tracing_env in ("false", "0", "no")
    is_explicit_enabled = local_tracing_env in ("true", "1", "yes")

    local_endpoint: str | None = None
    if not is_explicit_disabled:
        if explicit_endpoint:
            local_endpoint = explicit_endpoint
        elif is_explicit_enabled:
            # If Phoenix HTTP port 6006 is open, or gRPC 4317 is not open, default to Phoenix HTTP endpoint
            if _is_port_open("127.0.0.1", 6006) or not _is_port_open("127.0.0.1", 4317):
                local_endpoint = "http://localhost:6006/v1/traces"
            else:
                local_endpoint = "http://localhost:4317"
        elif _is_port_open("127.0.0.1", 6006):
            # Auto-detect running Arize Phoenix instance on default port 6006
            local_endpoint = "http://localhost:6006/v1/traces"
        elif _is_port_open("127.0.0.1", 4317):
            # Auto-detect running OTLP gRPC collector on port 4317
            local_endpoint = "http://localhost:4317"

    local_otel_configured = False
    if local_endpoint:
        # Normalize Phoenix HTTP endpoint if port 6006 was passed without /v1/traces
        if ("6006" in local_endpoint or "v1/traces" in local_endpoint) and not local_endpoint.endswith("/v1/traces"):
            local_endpoint = f"{local_endpoint.rstrip('/')}/v1/traces"

        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            project_name = (
                os.environ.get("PHOENIX_PROJECT_NAME")
                or os.environ.get("OTEL_SERVICE_NAME")
                or "zocrates"
            )

            resource = Resource.create(
                attributes={
                    "service.name": "zocrates",
                    "service.namespace": "local-dev",
                    "openinference.project.name": project_name,
                }
            )

            # Select protocol based on endpoint structure
            if "v1/traces" in local_endpoint or "6006" in local_endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )
            else:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

            provider = TracerProvider(resource=resource)
            provider.add_span_processor(PhoenixEnrichmentSpanProcessor())
            exporter = OTLPSpanExporter(endpoint=local_endpoint)
            processor = BatchSpanProcessor(exporter)
            provider.add_span_processor(processor)
            trace.set_tracer_provider(provider)

            _active_telemetry_endpoint = local_endpoint
            _telemetry_configured = True
            atexit.register(flush_telemetry)

            logging.info(
                "Local OTLP tracing enabled. Exporting traces to: %s (project: %s)",
                local_endpoint,
                project_name,
            )
            local_otel_configured = True
        except Exception as err:
            logging.warning("Failed to initialize local OTLP tracing: %s", err)

    # Set up GenAI SDK instrumentation
    _setup_instrumentation_lib_if_installed()

    return bucket or (local_endpoint if local_otel_configured else None)


def setup_file_logging(log_dir: Path) -> None:
    """Attach a RotatingFileHandler to the root logger.

    Writes to ``<log_dir>/zocrates.log`` (10 MB per file, 5 backups).
    Safe to call multiple times — skips setup if the handler is already
    attached.

    Args:
        log_dir: Directory in which to create ``zocrates.log``. Must
            already exist (use ``get_log_dir()`` to ensure it is created).
    """
    log_path = log_dir / "zocrates.log"

    root_logger = logging.getLogger()

    # Avoid duplicate handlers if called more than once
    for handler in root_logger.handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler) and (
            getattr(handler, "baseFilename", None) == str(log_path)
        ):
            return

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    handler.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)
    logging.info("File logging enabled: %s", log_path)
