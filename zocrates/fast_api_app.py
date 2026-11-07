"""
FastAPI Server for the Zocrates research assistant.

Exposes REST endpoints for registering the Zotero directory, performing syncs,
checking server status, and a streaming Server-Sent Events (SSE) chat endpoint.
"""

import anyio
import asyncio
import base64
import contextlib
import errno
import gc
import inspect
import json
import logging
import os
import re
import secrets
import shutil
import sys
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# Propagate docling and transformers loggers to root handlers
for _logger_name in ("docling", "docling_core", "docling_parse", "transformers"):
    _sub_logger = logging.getLogger(_logger_name)
    _sub_logger.setLevel(logging.INFO)
    _sub_logger.propagate = True

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.genai import types
from sse_starlette.sse import EventSourceResponse

from zocrates import __version__
from zocrates.agents.common.adk_compat import (
    patch_adk_active_task_scope_rewind,
    patch_adk_request_input_context_filter,
    patch_compaction_subagent_isolation,
)

# Apply runtime compatibility patches
patch_adk_request_input_context_filter()
patch_compaction_subagent_isolation()
patch_adk_active_task_scope_rewind()

from zocrates.agents.common.prompts import format_active_items, resolve_active_item_entry
from zocrates.agents.common.state_keys import (
    HITL_SNAPSHOT,
    RESEARCH_TURN_HISTORY,
    USER_PROMPT,
    USER_RESEARCH_PROFILE_STR,
)
from zocrates.agents.coordinator.prompts import format_user_research_profile_context
from zocrates.utils import services
from zocrates.utils.session_formatting import (
    format_session_for_ui,
    parse_finish_reason as _parse_finish_reason,
    parse_usage_metadata as _parse_usage_metadata,
)
from zocrates.utils.cost_calculator import calculate_event_cost
from zocrates.utils.telemetry import setup_file_logging, setup_telemetry
from zocrates.config import (
    ALLOW_ORIGINS,
    DEFAULT_CITATION_VERIFICATION_MODEL,
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_NUM_RETRIES,
    DEFAULT_LLM_TEMPERATURE,
    DEFAULT_ENABLE_PREDICTIVE_GHOST_INPUT,
    DEFAULT_PREDICTIVE_PROMPT_MODEL,
    _write_auth_sidecar,
    check_config_freshness,
    get_auth_token,
    get_conversion_timeout_seconds,
    get_log_dir,
    is_mcp_server_enabled,
)
from zocrates.rag.service_manager import get_syncer, initialize_services
from zocrates.schemas.sync_request import (
    SyncRequest,
)
from zocrates.schemas.register_request import RegisterRequest
from zocrates.schemas.diagnostic_request import DiagnosticReportRequest
from zocrates.schemas.llm_request import (
    LLMTestRequest,
    OpenAlexTestRequest,
    OpenRouterKeyTestRequest,
    SuggestPromptRequest,
)
from zocrates.schemas.config_request import ConfigRequest
from zocrates.schemas.decision_request import DecisionModelTestRequest
from zocrates.schemas.rag_request import EmbeddingTestRequest, RAGResetRequest, EmbeddingModelChangeRequest, \
    BatchIndexRequest
from zocrates.schemas.item_request import ItemDeleteRequest, SnippetItem
from zocrates.schemas.chat_request import ChatRequest, ResumeChatRequest
from zocrates.schemas.graph_request import GraphRecomputeRequest
from zocrates.schemas.convert_request import ConverterChangeRequest, ConverterTestRequest, ConvertRequest, \
    BatchConvertRequest
from zocrates.schemas.session_request import CreateSessionRequest, RewindSessionRequest
from zocrates.schemas.discover_request import DiscoverSearchRequest, DiscoverResolveRequest, DiscoverPrepareBatchRequest, \
    DiscoverCleanupBatchRequest
from zocrates.schemas.mcp_request import MCPSearchRequest, MCPReadRequest
from zocrates.rag.graph_engine import evaluate_and_persist_edges
from zocrates.discovery.engine import get_default_discovery_engine
from zocrates.discovery.staging import (
    cleanup_stale_staging_directories,
    download_and_stage_pdf,
    get_staging_root,
)
from zocrates.agents.common.toolkit import (
    get_document_toc_core,
    get_related_items_core,
    read_document_core,
    search_corpus_core,
)
from zocrates.rag.bundle_catalog import generate_root_index_md

_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+\Z")


def _validate_item_key(item_key: str) -> bool:
    """Validate item key to prevent path traversal."""
    return bool(item_key and _SAFE_KEY_RE.match(item_key))


_EDGE_TASKS: set[asyncio.Task] = set()


def _dispatch_edge_evaluation(item_key: str, syncer: Any) -> None:
    """Dispatch background edge evaluation with strong reference retention and error logging."""
    async def _runner() -> None:
        """Evaluate paper relationships and persist edges in the background."""
        await asyncio.to_thread(evaluate_and_persist_edges, item_key, syncer, True)

    task = asyncio.create_task(_runner())
    _EDGE_TASKS.add(task)

    def _on_done(t: asyncio.Task) -> None:
        """Handle completion of background edge evaluation task.

        Args:
            t: The completed asyncio Task instance.
        """
        _EDGE_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc:
                logger.warning("Background edge evaluation failed for %s: %s", item_key, exc)

    task.add_done_callback(_on_done)

# ---------------------------------------------------------------------------
# Setup and configuration
# ---------------------------------------------------------------------------

load_dotenv()
setup_telemetry()

logger = logging.getLogger(__name__)

AGENT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Process Lifecycle & Parent Watchdog
# ---------------------------------------------------------------------------


def _safe_cleanup_snapshots() -> None:
    try:
        from zocrates.rag.zotero_db import _cleanup_sqlite_snapshots

        _cleanup_sqlite_snapshots()
    except Exception:
        pass


def _parent_watchdog(
    parent_pid: int,
    poll_interval: float = 2.0,
    exit_fn: Any = os._exit,
) -> None:
    """Monitor parent Zotero process and self-terminate backend when parent process exits."""
    logger.info("Parent watchdog started monitoring PID %d", parent_pid)
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, parent_pid)
        if not handle:
            logger.info("Parent process %d handle unavailable. Exiting backend.", parent_pid)
            _safe_cleanup_snapshots()
            exit_fn(0)
            return
        try:
            timeout_ms = max(1, int(poll_interval * 1000))
            while True:
                # 0x00000102 is WAIT_TIMEOUT
                ret = kernel32.WaitForSingleObject(handle, timeout_ms)
                if ret != 0x00000102:
                    logger.info("Parent Zotero process %d terminated. Exiting backend.", parent_pid)
                    _safe_cleanup_snapshots()
                    exit_fn(0)
                    return
        finally:
            kernel32.CloseHandle(handle)
    else:
        while True:
            time.sleep(poll_interval)
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                logger.info("Parent Zotero process %d terminated. Exiting backend.", parent_pid)
                _safe_cleanup_snapshots()
                exit_fn(0)
                return
            except PermissionError:
                pass
            except OSError as exc:
                if exc.errno == errno.ESRCH:
                    logger.info("Parent Zotero process %d terminated. Exiting backend.", parent_pid)
                    _safe_cleanup_snapshots()
                    exit_fn(0)
                    return


def _trigger_shutdown() -> None:
    """Terminate the process after sending the shutdown response."""
    logger.info("Executing scheduled backend shutdown.")

    def _force_exit() -> None:
        _safe_cleanup_snapshots()
        os._exit(0)

    try:
        import signal

        if hasattr(signal, "SIGTERM") and sys.platform != "win32":
            threading.Timer(2.0, _force_exit).start()
            _safe_cleanup_snapshots()
            os.kill(os.getpid(), signal.SIGTERM)
            return
    except Exception as exc:
        logger.warning("Error signaling SIGTERM: %s. Forcing exit.", exc)
    _safe_cleanup_snapshots()
    os._exit(0)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage the FastAPI application lifespan: initialize resources on startup and clean up on shutdown.

    On startup: imports the ADK app and root agent, auto-initializes services if
    ``ZOTERO_DATA_DIR`` is set and valid, spawns the parent watchdog if
    ``ZOTERO_PID`` is set, constructs the ``Runner`` (binding it to the registered
    session and artifact services), and stores it on ``app.state`` for use by
    the ``/chat`` endpoint.

    On shutdown: closes the active ``LibrarySyncer`` to release Chroma and
    database connections.

    Args:
        app: The FastAPI application instance whose ``state`` is mutated.

    Yields:
        Control to the application while it is running.
    """
    from zocrates.agents.app import app as adk_app

    # Auto-initialize services if ZOTERO_DATA_DIR is provided
    zotero_dir_env = os.getenv("ZOTERO_DATA_DIR")
    if zotero_dir_env:
        data_path = Path(zotero_dir_env).expanduser().resolve()
        sqlite_path = data_path / "zotero.sqlite"
        storage_path = data_path / "storage"
        if data_path.exists() and sqlite_path.exists() and storage_path.is_dir():
            try:
                syncer = initialize_services(data_path)
                setup_file_logging(get_log_dir(data_path))
                app.state.zotero_data_dir = str(data_path)
                get_auth_token(data_path)
                logger.info("Auto-initialized services from ZOTERO_DATA_DIR: %s", data_path)

                # Run synchronous startup maintenance before runner traffic begins (fail-open)
                try:
                    from zocrates.utils.session_maintenance import run_session_database_maintenance
                    from zocrates.config import get_session_maintenance_config

                    session_service = services.get_session_service()
                    maintenance_res = await run_session_database_maintenance(
                        zotero_data_dir=data_path,
                        session_service=session_service,
                        config=get_session_maintenance_config(data_path),
                        active_session_ids=getattr(app.state, "active_chat_session_ids", None),
                        app_name=adk_app.name,
                    )
                    logger.info("Startup session database maintenance complete: %s", maintenance_res)
                except Exception as maint_err:
                    logger.warning("Startup session maintenance encountered non-fatal error: %s", maint_err)

                # Launch non-blocking background metadata staleness sweep
                sweep_task = asyncio.create_task(asyncio.to_thread(syncer.sync_metadata_staleness))
                app.state.metadata_sweep_task = sweep_task

                def _log_sweep_result(t: asyncio.Task) -> None:
                    """Log completion or failure of startup metadata staleness sweep.

                    Args:
                        t: The completed asyncio Task instance.
                    """
                    try:
                        res = t.result()
                        logger.info("Startup metadata staleness sweep complete: %s", res)
                    except Exception as sweep_err:
                        logger.warning("Startup metadata staleness sweep encountered error: %s", sweep_err)

                sweep_task.add_done_callback(_log_sweep_result)
            except Exception as exc:
                logger.warning("Failed to auto-initialize services from ZOTERO_DATA_DIR %s: %s", data_path, exc)

    runner = Runner(
        app=adk_app,
        session_service=services.get_session_service(),
        artifact_service=services.get_artifact_service(),
        auto_create_session=True,
    )
    app.state.runner = runner
    app.state.agent_app_name = adk_app.name
    app.state.active_chat_session_ids = set()

    # Start parent watchdog if ZOTERO_PID is present and > 0
    zotero_pid_env = os.getenv("ZOTERO_PID")
    if zotero_pid_env and zotero_pid_env.strip().isdigit():
        parent_pid = int(zotero_pid_env.strip())
        if parent_pid > 0:
            watchdog_thread = threading.Thread(
                target=_parent_watchdog,
                args=(parent_pid,),
                daemon=True,
                name="ZoteroParentWatchdog",
            )
            watchdog_thread.start()
            app.state.parent_watchdog_thread = watchdog_thread
            logger.info("Spawned parent watchdog thread monitoring ZOTERO_PID %d", parent_pid)

    # Auto-start local Laya daemon if configured
    try:
        from zocrates.config import load_zocrates_config
        cfg = load_zocrates_config()
        if (
            cfg.get("citation_verification_enabled")
            and cfg.get("citation_verification_provider") == "local_laya"
            and cfg.get("citation_verification_auto_start_local", True)
        ):
            from zocrates.rag.decision_service import is_laya_installed, start_laya_daemon
            if is_laya_installed():
                port = int(cfg.get("citation_verification_local_port", 8005))
                await asyncio.to_thread(start_laya_daemon, port=port)
    except Exception as exc:
        logger.warning("Could not auto-start Laya daemon on startup: %s", exc)

    # Clean up stale discovery staging directories (> 2 hours old)
    try:
        removed = cleanup_stale_staging_directories()
        if removed > 0:
            logger.info("Cleaned up %d stale discovery staging directories on startup.", removed)
    except Exception as exc:
        logger.warning("Could not sweep stale discovery staging directories: %s", exc)

    yield

    # Shutdown cleanup
    try:
        from zocrates.config import load_zocrates_config
        from zocrates.rag.decision_service import stop_laya_daemon
        cfg = load_zocrates_config()
        port = int(cfg.get("citation_verification_local_port", 8005))
        await asyncio.to_thread(stop_laya_daemon, port=port)
    except Exception as exc:
        logger.warning("Error stopping Laya daemon during shutdown: %s", exc)

    try:
        from zocrates.rag.service_manager import get_syncer

        syncer = get_syncer()
        syncer.close()
        logger.info("Zocrates backend shutdown complete.")
    except RuntimeError:
        pass  # Not initialized, nothing to clean up
    except Exception as exc:
        logger.warning("Error during shutdown cleanup: %s", exc)

    _safe_cleanup_snapshots()


# ---------------------------------------------------------------------------
# FastAPI Initialization
# ---------------------------------------------------------------------------

app = FastAPI(
    title="zocrates",
    description="API for interacting with Zocrates",
    lifespan=lifespan,
)
app.state.active_chat_session_ids = set()

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_origin_regex=r"^(chrome://zocrates|moz-extension://.*)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _extract_request_token(request: Request) -> str | None:
    """Extract authentication token from Authorization header, X-Zocrates-Auth header, or query parameter."""
    auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    custom_header = request.headers.get("X-Zocrates-Auth") or request.headers.get("x-zocrates-auth")
    if custom_header:
        return custom_header.strip()
    # Query parameter 'token' is strictly allowed only for GET /assets/ and GET /graph/view
    if request.url.path.startswith("/assets/") or request.url.path == "/graph/view":
        query_token = request.query_params.get("token")
        if query_token:
            return query_token.strip()
    return None


def _is_request_authenticated(request: Request) -> bool:
    """Return True if the request carries a valid Zocrates authentication token."""
    provided = _extract_request_token(request)
    if not provided:
        return False
    expected = get_auth_token()
    if not expected:
        return False
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def verify_auth(request: Request) -> None:
    """FastAPI dependency enforcing valid local authentication token."""
    if not _is_request_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


class _HealthCheckAccessFilter(logging.Filter):
    """Suppress successful GET /health heartbeat access lines from uvicorn.access."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            method, path, status_code = args[1], str(args[2]).split("?", 1)[0], args[4]
            try:
                if method == "GET" and path == "/health" and int(status_code) < 400:
                    return False
            except (TypeError, ValueError):
                pass
        return True


logging.getLogger("uvicorn.access").addFilter(_HealthCheckAccessFilter())


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Log all incoming HTTP requests and outgoing responses."""
    is_health_probe = request.method == "GET" and request.url.path == "/health"
    start_time = time.perf_counter()
    url_path = request.url.path
    if request.url.query:
        safe_query = re.sub(r"(^|[?&])(token=)[^&]+", r"\1\2[REDACTED]", request.url.query, flags=re.IGNORECASE)
        url_path += f"?{safe_query}"

    if not is_health_probe:
        logger.info("--> %s %s", request.method, url_path)
    try:
        response = await call_next(request)
        if not is_health_probe:
            process_time = (time.perf_counter() - start_time) * 1000.0
            logger.info(
                "<-- %s %s [%d] (%.2fms)",
                request.method,
                url_path,
                response.status_code,
                process_time,
            )
        return response
    except Exception as exc:
        process_time = (time.perf_counter() - start_time) * 1000.0
        logger.error(
            "<-- %s %s [EXCEPTION: %s] (%.2fms)",
            request.method,
            url_path,
            exc,
            process_time,
        )
        raise


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle request validation errors with human-readable detail and structured error list."""
    error_messages = []
    for err in exc.errors():
        loc = " -> ".join(str(part) for part in err.get("loc", []))
        msg = err.get("msg", "Invalid value")
        if loc:
            error_messages.append(f"{loc}: {msg}")
        else:
            error_messages.append(msg)
    detail_str = "; ".join(error_messages) if error_messages else "Request validation failed"

    logger.warning(
        "Validation error on %s %s: %s",
        request.method,
        request.url.path,
        detail_str,
    )
    return JSONResponse(
        status_code=422,
        content={
            "detail": detail_str,
            "errors": jsonable_encoder(exc.errors()),
        },
    )


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Asset Serving Endpoint  (P2 — Response Image Display)
# ---------------------------------------------------------------------------

_ASSET_EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}


@app.get("/assets/{item_key}/{asset_path:path}", dependencies=[Depends(verify_auth)])
async def serve_bundle_asset(item_key: str, asset_path: str) -> Response:
    """Serve a bundle asset file (image) for a given Zotero item key.

    Resolves ``item_key`` to the markdown-cache bundle directory that is already
    managed by the active ``LibrarySyncer``.  The path is URL-decoded before
    resolution and validated against the bundle root to prevent traversal.

    Only the extensions in ``_ASSET_EXTENSION_MEDIA_TYPES`` (.png, .jpg,
    .jpeg, .gif, .webp, .svg) are served; anything else returns 404.

    Returns:
        Raw image bytes with the appropriate Content-Type and a
        ``Cache-Control: max-age=3600`` header (bundles are content-addressed
        by SHA-256 staleness tracking so the response is effectively immutable).

    Raises:
        HTTPException 404: on unknown/invalid key, traversal attempt, missing
            bundle or file, or disallowed file extension.
    """
    # 1. Validate item_key to the existing alphanumeric-key grammar
    if not _validate_item_key(item_key):
        raise HTTPException(status_code=404, detail="Unknown item key.")

    # 2. Resolve cache root directly from the active syncer (LBYL — no fallback
    #    to on-demand note materialization; this is a pure byte-serving endpoint)
    try:
        syncer = get_syncer()
        cache_dir: Path = syncer.processor._cache_dir
    except RuntimeError:
        raise HTTPException(status_code=404, detail="Backend not registered.")

    bundle_dir = cache_dir / item_key
    if not bundle_dir.is_dir():
        try:
            from zocrates.agents.common.toolkit import _resolve_bundle_dir
            resolved_info = _resolve_bundle_dir(item_key)
            if resolved_info:
                bundle_dir = resolved_info[0]
            else:
                raise HTTPException(status_code=404, detail="Bundle not found.")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=404, detail="Bundle not found.")

    # 3. URL-decode the path segment (the evidence formatter percent-encodes
    #    spaces and other reserved characters for URL safety)
    decoded_path = urllib.parse.unquote(asset_path)

    # 4. Resolve and validate: block traversal attempts and absolute overrides
    resolved = (bundle_dir / decoded_path).resolve()
    if not resolved.is_relative_to(bundle_dir.resolve()):
        raise HTTPException(status_code=404, detail="Asset path not permitted.")

    # 5. Extension allowlist with explicit media_type mapping
    suffix = resolved.suffix.lower()
    media_type = _ASSET_EXTENSION_MEDIA_TYPES.get(suffix)
    if media_type is None:
        raise HTTPException(status_code=404, detail="File type not served.")

    # 6. File existence check
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail="Asset not found.")

    # 7. Serve bytes with cache header (bundles are SHA-256 content-addressed)
    return Response(
        content=resolved.read_bytes(),
        media_type=media_type,
        headers={"Cache-Control": "max-age=3600"},
    )


def _status_zotero_data_dir_hint() -> str | None:
    """Best-effort Zotero data directory for the unregistered /status branch.

    The Zotero launcher's port-probe attach classifier compares this field
    against ``Zotero.DataDirectory.dir``; the registered branch reports
    ``str(syncer._zotero_data_dir)``, so the unregistered branch must too.
    Resolution order: auto-init app state -> ZOTERO_DATA_DIR env -> discovery.
    Returns ``None`` when nothing can be resolved (field still present so the
    classifier sees an explicit miss rather than an absent key).
    """
    state_dir = getattr(app.state, "zotero_data_dir", None)
    if state_dir:
        return str(state_dir)
    env_dir = os.getenv("ZOTERO_DATA_DIR")
    if env_dir:
        return env_dir
    try:
        from zocrates.config import get_zocrates_data_dir

        return str(get_zocrates_data_dir().parent)
    except Exception:  # noqa: BLE001 - hint is best-effort by design
        return None


def _maybe_hot_swap_agent_models(zotero_dir: Path | str | None, context_label: str) -> None:
    """Silently hot-swap coordinator agent models if zocrates_config.json changed on disk."""
    if not zotero_dir:
        return
    try:
        if check_config_freshness(zotero_dir):
            from zocrates.agents.app import set_agent_models
            from zocrates.llm import get_llm_model

            new_model = get_llm_model(Path(zotero_dir) if zotero_dir else None)
            set_agent_models(new_model, zotero_data_dir=zotero_dir)
            logger.info("Silently hot-swapped agent model from updated config on %s", context_label)
    except Exception as exc:
        from zocrates.config import rearm_config_freshness

        rearm_config_freshness()
        logger.warning("Config freshness check failed on %s: %s", context_label, exc)


@app.get("/health")
async def health(request: Request) -> dict:
    """Fast zero-I/O heartbeat endpoint for sidebar liveness and model hot-swapping."""
    try:
        syncer = get_syncer()
        zotero_dir: str | None = str(syncer._zotero_data_dir)
        registered = True
    except RuntimeError:
        syncer = None
        zotero_dir = _status_zotero_data_dir_hint()
        registered = False

    if not _is_request_authenticated(request):
        return {
            "status": "ready" if registered else "not_registered",
            "zotero_data_dir": zotero_dir,
        }

    target_dir = syncer._zotero_data_dir if registered and syncer is not None else zotero_dir
    _maybe_hot_swap_agent_models(target_dir, "health check")

    from zocrates.config import load_zocrates_config

    cfg = load_zocrates_config(target_dir)
    return {
        "status": "ready" if registered else "not_registered",
        "zotero_data_dir": str(syncer._zotero_data_dir) if registered and syncer is not None else zotero_dir,
        "model": cfg.get("llm_model") or DEFAULT_LLM_MODEL,
        "llm_provider": cfg.get("llm_provider") or "openrouter_llm",
    }


@app.get("/status")
async def status(request: Request) -> dict:
    """Retrieve the current initialization, hardware specs, and index status of Zocrates.

    Returns:
        Dict with keys ``status``, ``zotero_data_dir``, ``cached_documents_count``,
        ``cached_document_keys``, ``model``, ``hardware``, and ``embedding_info``
        when authenticated, or a minimal safe probe payload when unauthenticated.
        ``status`` is ``"ready"`` if registered, ``"not_registered"`` otherwise.
    """
    project_dir = str(Path(__file__).resolve().parent.parent)
    if not _is_request_authenticated(request):
        try:
            syncer = get_syncer()
            zotero_dir = str(syncer._zotero_data_dir)
            registered = True
        except RuntimeError:
            zotero_dir = _status_zotero_data_dir_hint()
            registered = False
        return {
            "status": "ready" if registered else "not_registered",
            "zotero_data_dir": zotero_dir,
            "version": __version__,
        }

    from zocrates.rag.hardware import (
        get_docling_hardware_assessment,
        get_system_hardware_info,
    )

    hw_info = get_system_hardware_info().to_dict()
    hw_info["docling_assessment"] = get_docling_hardware_assessment()

    from zocrates.config import (
        get_auto_render_mermaid_diagrams,
        get_auto_spawn_backend,
        get_backend_port,
        get_collection_name,
        get_compaction_config,
        get_conversion_timeout_seconds,
        get_datalab_api_key,
        get_datalab_merge_cross_page,
        get_datalab_non_pdf_fallback,
        get_docling_mode,
        get_enable_vlm_captioning,
        get_force_cpu_pref,
        get_ollama_num_ctx,
        get_openalex_api_key,
        get_scholar_polite_email,
        get_session_maintenance_config,
        get_vlm_captioning_backup_model,
        get_vlm_captioning_local_model,
        get_vlm_captioning_model,
        get_vlm_captioning_provider,
        get_vlm_max_images_per_doc,
        is_mcp_server_enabled,
        load_zocrates_config,
    )

    try:
        syncer = get_syncer()
        cfg = load_zocrates_config(syncer._zotero_data_dir)
        force_cpu = get_force_cpu_pref(syncer._zotero_data_dir, cfg=cfg)
        project_dir = str(Path(__file__).resolve().parent.parent)

        # Pre-populate dynamic config
        mode = os.getenv("EMBEDDING_MODE", cfg.get("embedding_provider", "local"))
        req_model = "jinaai/jina-embeddings-v5-text-nano"

        cached_keys = syncer.processor.list_cached_keys()
        embedder = syncer.store._embedder

        from zocrates.utils.session_maintenance import get_session_storage_stats
        session_db_path = Path(syncer._zotero_data_dir) / "zocrates-data" / "sessions.db"
        sess_service = services.get_session_service()
        app_name = getattr(app.state, "agent_app_name", "zocrates")
        try:
            session_storage_stats = await get_session_storage_stats(
                db_path=session_db_path,
                session_service=sess_service,
                app_name=app_name,
                user_id="default_user",
            )
        except Exception as exc:
            logger.debug("Failed to get session storage stats for status: %s", exc)
            session_storage_stats = {
                "size_bytes": 0,
                "session_count": 0,
                "freelist_pages": 0,
                "total_pages": 0,
                "page_size": 4096,
            }

        emb_info = {
            "mode": mode,
            "active_model": getattr(embedder, "_model_name", req_model),
            "requested_model": req_model,
            "dimension": getattr(embedder, "dimension", 768),
            "device": "Cloud API" if mode == "cloud" else "CPU",
            "is_fallback": False,
            "validation_error": None,
            "active_collection": syncer.store.active_collection_name,
            "force_cpu": True,
        }

        return {
            "status": "ready",
            "version": __version__,
            "zotero_data_dir": str(syncer._zotero_data_dir),
            "project_dir": project_dir,
            "cached_documents_count": len(cached_keys),
            "cached_document_keys": cached_keys,
            "model": cfg.get("llm_model") or DEFAULT_LLM_MODEL,
            "llm_provider": cfg.get("llm_provider") or "openrouter_llm",
            "document_converter_id": cfg.get("document_converter_id", "markitdown"),
            "document_converter_mode": cfg.get("document_converter_mode", "local"),
            "conversion_timeout_seconds": get_conversion_timeout_seconds(syncer._zotero_data_dir, cfg=cfg),
            "datalab_non_pdf_fallback": get_datalab_non_pdf_fallback(syncer._zotero_data_dir, cfg=cfg),
            "datalab_merge_cross_page": get_datalab_merge_cross_page(syncer._zotero_data_dir, cfg=cfg),
            "has_datalab_key": bool(get_datalab_api_key(syncer._zotero_data_dir)),
            "has_openalex_api_key": bool(get_openalex_api_key(syncer._zotero_data_dir)),
            "docling_mode": get_docling_mode(syncer._zotero_data_dir, cfg=cfg),
            "enable_vlm_captioning": get_enable_vlm_captioning(syncer._zotero_data_dir, cfg=cfg),
            "vlm_captioning_provider": get_vlm_captioning_provider(syncer._zotero_data_dir, cfg=cfg),
            "vlm_captioning_model": get_vlm_captioning_model(syncer._zotero_data_dir, cfg=cfg),
            "vlm_captioning_backup_model": get_vlm_captioning_backup_model(syncer._zotero_data_dir, cfg=cfg),
            "vlm_captioning_local_model": get_vlm_captioning_local_model(syncer._zotero_data_dir, cfg=cfg),
            "vlm_max_images_per_doc": get_vlm_max_images_per_doc(syncer._zotero_data_dir, cfg=cfg),
            "auto_render_mermaid_diagrams": get_auto_render_mermaid_diagrams(syncer._zotero_data_dir, cfg=cfg),
            "auto_spawn_backend": get_auto_spawn_backend(syncer._zotero_data_dir, cfg=cfg),
            "backend_port": get_backend_port(syncer._zotero_data_dir, cfg=cfg),
            "enable_mcp_server": is_mcp_server_enabled(syncer._zotero_data_dir, cfg=cfg),
            "scholar_polite_email": get_scholar_polite_email(syncer._zotero_data_dir, cfg=cfg),
            "user_research_profile": str(cfg.get("user_research_profile") or ""),
            "ollama_num_ctx": get_ollama_num_ctx(syncer._zotero_data_dir, cfg=cfg),
            "citation_verification_enabled": bool(cfg.get("citation_verification_enabled", False)),
            "citation_verification_provider": str(cfg.get("citation_verification_provider", "openrouter")),
            "citation_verification_model": str(cfg.get("citation_verification_model", DEFAULT_CITATION_VERIFICATION_MODEL)),
            "citation_verification_threshold": float(cfg.get("citation_verification_threshold", 0.60)),
            "citation_verification_local_url": str(cfg.get("citation_verification_local_url", "http://127.0.0.1:8005/v1/systemone")),
            "citation_verification_local_port": int(cfg.get("citation_verification_local_port", 8005)),
            "citation_verification_auto_start_local": bool(cfg.get("citation_verification_auto_start_local", True)),
            "citation_verification_timeout_sec": float(cfg.get("citation_verification_timeout_sec", 3.0)),
            "citation_verification_turn_budget_sec": float(cfg.get("citation_verification_turn_budget_sec", 10.0)),
            **get_compaction_config(syncer._zotero_data_dir, cfg=cfg),
            **get_session_maintenance_config(syncer._zotero_data_dir, cfg=cfg),
            "session_storage_stats": session_storage_stats,
            "hardware": hw_info,
            "embedding_info": emb_info,
        }
    except RuntimeError:
        cfg = load_zocrates_config()
        force_cpu = get_force_cpu_pref(cfg=cfg)
        project_dir = str(Path(__file__).resolve().parent.parent)

        # Pre-populate dynamic config
        mode = os.getenv("EMBEDDING_MODE", cfg.get("embedding_provider", "local"))
        req_model = "jinaai/jina-embeddings-v5-text-nano"

        dim = 768
        emb_info = {
            "mode": mode,
            "active_model": req_model,
            "requested_model": req_model,
            "dimension": dim,
            "device": "Cloud API" if mode == "cloud" else "CPU",
            "is_fallback": False,
            "validation_error": None,
            "active_collection": get_collection_name(req_model, dim),
            "force_cpu": True,
        }

        return {
            "status": "not_registered",
            "version": __version__,
            "project_dir": project_dir,
            "message": "Call /register with your Zotero data directory to initialize.",
            # Always report the data-dir hint so the Zotero launcher's port-probe
            # attach classifier can identify OUR backend even before /register
            # runs (a missing field was misclassified as a foreign service).
            "zotero_data_dir": _status_zotero_data_dir_hint(),
            "model": cfg.get("llm_model") or DEFAULT_LLM_MODEL,
            "llm_provider": cfg.get("llm_provider") or "openrouter_llm",
            "document_converter_id": cfg.get("document_converter_id", "markitdown"),
            "document_converter_mode": cfg.get("document_converter_mode", "local"),
            "datalab_non_pdf_fallback": get_datalab_non_pdf_fallback(cfg=cfg),
            "datalab_merge_cross_page": get_datalab_merge_cross_page(cfg=cfg),
            "has_datalab_key": bool(get_datalab_api_key()),
            "has_openalex_api_key": bool(get_openalex_api_key()),
            "docling_mode": get_docling_mode(cfg=cfg),
            "enable_vlm_captioning": get_enable_vlm_captioning(cfg=cfg),
            "vlm_captioning_provider": get_vlm_captioning_provider(cfg=cfg),
            "vlm_captioning_model": get_vlm_captioning_model(cfg=cfg),
            "vlm_captioning_backup_model": get_vlm_captioning_backup_model(cfg=cfg),
            "vlm_captioning_local_model": get_vlm_captioning_local_model(cfg=cfg),
            "vlm_max_images_per_doc": get_vlm_max_images_per_doc(cfg=cfg),
            "auto_render_mermaid_diagrams": get_auto_render_mermaid_diagrams(cfg=cfg),
            "auto_spawn_backend": get_auto_spawn_backend(cfg=cfg),
            "backend_port": get_backend_port(cfg=cfg),
            "enable_mcp_server": is_mcp_server_enabled(cfg=cfg),
            "scholar_polite_email": get_scholar_polite_email(cfg=cfg),
            "user_research_profile": str(cfg.get("user_research_profile") or ""),
            "ollama_num_ctx": get_ollama_num_ctx(cfg=cfg),
            "citation_verification_enabled": bool(cfg.get("citation_verification_enabled", False)),
            "citation_verification_provider": str(cfg.get("citation_verification_provider", "openrouter")),
            "citation_verification_model": str(cfg.get("citation_verification_model", DEFAULT_CITATION_VERIFICATION_MODEL)),
            "citation_verification_threshold": float(cfg.get("citation_verification_threshold", 0.60)),
            "citation_verification_local_url": str(cfg.get("citation_verification_local_url", "http://127.0.0.1:8005/v1/systemone")),
            "citation_verification_local_port": int(cfg.get("citation_verification_local_port", 8005)),
            "citation_verification_auto_start_local": bool(cfg.get("citation_verification_auto_start_local", True)),
            "citation_verification_timeout_sec": float(cfg.get("citation_verification_timeout_sec", 3.0)),
            "citation_verification_turn_budget_sec": float(cfg.get("citation_verification_turn_budget_sec", 10.0)),
            **get_compaction_config(cfg=cfg),
            **get_session_maintenance_config(cfg=cfg),
            "session_storage_stats": {
                "size_bytes": 0,
                "session_count": 0,
                "freelist_pages": 0,
                "total_pages": 0,
                "page_size": 4096,
            },
            "hardware": hw_info,
            "embedding_info": emb_info,
        }


@app.post("/register", dependencies=[Depends(verify_auth)])
async def register(request: RegisterRequest) -> dict:
    """Register the user's Zotero Data Directory and initialize pipeline services.

    Validates the directory structure and initializes all pipeline services.

    Raises:
        HTTPException: 400 if the directory, ``zotero.sqlite``, or ``storage/``
            subdirectory do not exist; 500 on unexpected initialization failure.

    Returns:
        Dict with keys ``status``, ``message``, and ``data_directory``.
    """
    path = Path(request.zotero_data_dir).expanduser().resolve()
    if not path.exists():
        raise HTTPException(
            status_code=400, detail=f"The specified directory does not exist: {path}"
        )

    sqlite_path = path / "zotero.sqlite"
    if not sqlite_path.exists():
        raise HTTPException(
            status_code=400,
            detail=f"Zotero SQLite database file not found at {sqlite_path}",
        )

    storage_dir = path / "storage"
    if not storage_dir.is_dir():
        raise HTTPException(
            status_code=400,
            detail=f"Path does not appear to be a valid Zotero data directory: "
                   f"missing 'storage/' subdirectory at {path}",
        )

    try:
        if request.scholar_polite_email and request.scholar_polite_email.strip():
            from zocrates.config import update_config
            update_config({"scholar_polite_email": request.scholar_polite_email.strip()}, zotero_data_dir=path)

        if request.openalex_api_key and request.openalex_api_key.strip():
            from zocrates.config import save_openalex_api_key
            save_openalex_api_key(request.openalex_api_key.strip())

        if request.openrouter_api_key and request.openrouter_api_key.strip():
            from zocrates.config import save_secure_api_key
            save_secure_api_key(
                request.openrouter_api_key.strip(),
                key_name="openrouter_api_key",
                env_var="OPENROUTER_API_KEY",
            )

        if request.datalab_api_key and request.datalab_api_key.strip():
            from zocrates.config import save_datalab_api_key
            save_datalab_api_key(request.datalab_api_key.strip())

        syncer = initialize_services(path)
        setup_file_logging(get_log_dir(path))
        auth_file = path / "zocrates-data" / "backend.auth"
        if not auth_file.exists():
            _write_auth_sidecar(auth_file, get_auth_token())

        # Refresh Runner to use registered DatabaseSessionService
        from zocrates.agents.app import app as adk_app
        session_service = services.get_session_service()

        # Run synchronous startup maintenance before runner traffic begins (fail-open)
        try:
            from zocrates.utils.session_maintenance import run_session_database_maintenance
            from zocrates.config import get_session_maintenance_config
            maintenance_res = await run_session_database_maintenance(
                zotero_data_dir=path,
                session_service=session_service,
                config=get_session_maintenance_config(path),
                active_session_ids=getattr(app.state, "active_chat_session_ids", None),
                app_name=adk_app.name,
            )
            logger.info("Startup session database maintenance complete: %s", maintenance_res)
        except Exception as maint_err:
            logger.warning("Startup session maintenance encountered non-fatal error: %s", maint_err)

        app.state.runner = Runner(
            app=adk_app,
            session_service=session_service,
            artifact_service=services.get_artifact_service(),
            auto_create_session=True,
        )
        app.state.agent_app_name = adk_app.name

        # Launch non-blocking background metadata staleness sweep
        sweep_task = asyncio.create_task(asyncio.to_thread(syncer.sync_metadata_staleness))
        app.state.metadata_sweep_task = sweep_task

        def _log_sweep_result(t: asyncio.Task) -> None:
            """Log completion or failure of registration metadata staleness sweep.

            Args:
                t: The completed asyncio Task instance.
            """
            try:
                res = t.result()
                logger.info("Startup metadata staleness sweep complete: %s", res)
            except Exception as sweep_err:
                logger.warning("Startup metadata staleness sweep encountered error: %s", sweep_err)

        sweep_task.add_done_callback(_log_sweep_result)

        return {
            "status": "success",
            "message": "Zocrates backend registered successfully.",
            "data_directory": str(path),
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to initialize services: {e}"
        ) from e


@app.get("/converters", dependencies=[Depends(verify_auth)])
async def list_converters() -> dict:
    """List all registered document converter strategies and their active status."""
    from zocrates.config import (
        get_datalab_api_key,
        get_document_converter_id,
        get_document_converter_mode,
        load_zocrates_config,
    )
    from zocrates.rag.document_converters import (
        DatalabConverter,
        DirectReadConverter,
        DoclingLocalConverter,
        MarkItDownConverter,
    )

    try:
        syncer = get_syncer()
        converters_list = syncer.processor._converters
        active_id = (
                syncer.processor._document_converter_id
                or get_document_converter_id(syncer._zotero_data_dir)
                or "markitdown"
        )
        converter_mode = get_document_converter_mode(syncer._zotero_data_dir)
        datalab_key_present = bool(get_datalab_api_key(syncer._zotero_data_dir))
    except RuntimeError:
        converters_list = [
            DirectReadConverter(),
            DoclingLocalConverter(),
            MarkItDownConverter(),
            DatalabConverter(),
        ]
        cfg = load_zocrates_config()
        active_id = cfg.get("document_converter_id", "markitdown")
        converter_mode = cfg.get("document_converter_mode", "local")
        datalab_key_present = bool(get_datalab_api_key())

    converters_info = [
        {
            "id": c.converter_id,
            "version": c.converter_version,
            "is_remote": getattr(c, "is_remote", False),
            "has_api_key": datalab_key_present if c.converter_id == "datalab" else True,
            "is_active": (
                    c.converter_id == active_id
                    or (c.converter_id == "docling_local" and active_id == "docling")
            ),
        }
        for c in converters_list
    ]

    return {
        "status": "success",
        "document_converter_id": active_id,
        "document_converter_mode": converter_mode,
        "has_datalab_key": datalab_key_present,
        "converters": converters_info,
    }


@app.post("/converters/test-connection", dependencies=[Depends(verify_auth)])
async def test_converter_connection(request: ConverterTestRequest) -> dict:
    """Test connection and credentials for a document converter."""
    from zocrates.config import get_datalab_api_key

    conv_id = request.converter_id
    if conv_id == "datalab":
        key = request.datalab_api_key or request.api_key or get_datalab_api_key()
        if not key or not str(key).strip():
            return {
                "status": "error",
                "message": "Datalab API key is missing. Please provide an API key.",
            }
        try:
            try:
                from datalab_sdk import DatalabAuthenticationError, DatalabClient
            except ImportError:
                from datalab_sdk import DatalabClient  # type: ignore
                DatalabAuthenticationError = ()  # type: ignore

            client = DatalabClient(api_key=str(key).strip())
            # Perform zero-credit authenticated live check against Datalab API
            try:
                await asyncio.to_thread(client.list_files, limit=1)
                return {
                    "status": "success",
                    "message": "Connected to Datalab API successfully. API key is valid.",
                }
            except Exception as net_err:
                # If network check failed due to invalid auth, report exact error
                err_str = str(net_err)
                err_lower = err_str.lower()
                is_auth_error = (
                    isinstance(net_err, DatalabAuthenticationError)
                    or type(net_err).__name__ == "DatalabAuthenticationError"
                    or "invalid api key" in err_lower
                    or "access token" in err_lower
                    or "unauthorized" in err_lower
                    or "401" in err_str
                    or "forbidden" in err_lower
                )
                if is_auth_error:
                    return {
                        "status": "error",
                        "message": f"Datalab API key rejected (Unauthorized): {err_str}",
                    }
                # Otherwise, if offline or temporary network issue, indicate SDK configured
                return {
                    "status": "success",
                    "message": f"Datalab SDK initialized with provided key (network probe notice: {err_str})",
                }
        except Exception as e:
            err_str = str(e)
            err_lower = err_str.lower()
            if (
                type(e).__name__ == "DatalabAuthenticationError"
                or "invalid api key" in err_lower
                or "access token" in err_lower
                or "unauthorized" in err_lower
                or "401" in err_str
                or "forbidden" in err_lower
            ):
                return {
                    "status": "error",
                    "message": f"Datalab API key rejected (Unauthorized): {err_str}",
                }
            return {
                "status": "error",
                "message": f"Datalab connection test failed: {e}",
            }
    elif conv_id in ("markitdown", "direct_read", "docling_local"):
        return {
            "status": "success",
            "message": f"Converter '{conv_id}' is available locally.",
        }
    else:
        return {
            "status": "error",
            "message": f"Unknown converter ID '{conv_id}'.",
        }


@app.get("/items/{item_key}/status", dependencies=[Depends(verify_auth)])
async def get_item_status(item_key: str) -> dict:
    """Get conversion and vector store status for a given Zotero item key."""
    try:
        syncer = get_syncer()

        attachments_info = []
        item = syncer.db.get_item_by_key(item_key)
        resolved_title = item.title if item and item.title else item_key
        resolved_parent_title = item.title if item and item.title else ""

        if item:
            atts = syncer.db.get_attachments(item.item_id)
            for att in atts:
                ak = att.item_key
                c_cached = syncer.processor.is_cached(ak)
                c_indexed = syncer.store.is_ingested(ak)
                c_meta = syncer.processor.get_metadata(ak)
                c_failures = syncer.processor.get_failures(ak)
                if c_meta:
                    c_meta["markdown_path"] = str(syncer.processor.cache_dir / ak / "document.md")
                doc_title = resolved_title or (att.path.name if att.path else ak)
                attachments_info.append({
                    "item_key": ak,
                    "parent_item_key": item_key,
                    "title": doc_title,
                    "parent_title": resolved_parent_title,
                    "filename": att.path.name if att.path else ak,
                    "content_type": att.content_type,
                    "is_cached": c_cached,
                    "is_indexed": c_indexed,
                    "metadata": c_meta,
                    "failed_converters": c_failures,
                })

        if not attachments_info:
            att = syncer.db.get_attachment_by_key(item_key)
            if att:
                ak = att.item_key
                c_cached = syncer.processor.is_cached(ak)
                c_indexed = syncer.store.is_ingested(ak)
                c_meta = syncer.processor.get_metadata(ak)
                c_failures = syncer.processor.get_failures(ak)
                if c_meta:
                    c_meta["markdown_path"] = str(syncer.processor.cache_dir / ak / "document.md")

                p_item = None
                if att.parent_item_key:
                    p_item = syncer.db.get_item_by_key(att.parent_item_key)
                elif att.parent_item_id:
                    p_item = syncer.db.get_item(att.parent_item_id)

                p_title = p_item.title if p_item and p_item.title else ""
                doc_title = p_title or (att.path.name if att.path else ak)
                resolved_title = doc_title
                resolved_parent_title = p_title

                attachments_info.append({
                    "item_key": ak,
                    "parent_item_key": att.parent_item_key or (p_item.item_key if p_item else ""),
                    "title": doc_title,
                    "parent_title": p_title,
                    "filename": att.path.name if att.path else ak,
                    "content_type": att.content_type,
                    "is_cached": c_cached,
                    "is_indexed": c_indexed,
                    "metadata": c_meta,
                    "failed_converters": c_failures,
                })

        primary = attachments_info[0] if attachments_info else {}
        is_cached = any(a["is_cached"] for a in attachments_info) if attachments_info else False
        is_indexed = any(a["is_indexed"] for a in attachments_info) if attachments_info else False
        meta = primary.get("metadata")
        failed_converters = primary.get("failed_converters") if primary else syncer.processor.get_failures(item_key)

    except RuntimeError:
        is_cached = False
        is_indexed = False
        meta = None
        failed_converters = {}
        attachments_info = []
        resolved_title = item_key
        resolved_parent_title = ""

    return {
        "status": "success",
        "item_key": item_key,
        "title": resolved_title,
        "parent_title": resolved_parent_title,
        "is_cached": is_cached,
        "is_indexed": is_indexed,
        "metadata": meta,
        "failed_converters": failed_converters or {},
        "attachments": attachments_info,
    }


@app.get("/library/conversion-status", dependencies=[Depends(verify_auth)])
def get_library_conversion_status() -> dict:
    """Get aggregate conversion status and list of unconverted documents across the entire library."""
    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    raw_attachments = syncer.db.get_all_convertible_attachments()
    meta_map = syncer.processor.get_all_metadata()

    # Deduplicate: if an item has multiple attachments, prioritize PDF attachment
    grouped_by_parent: dict[str, list[dict[str, Any]]] = {}
    standalone: list[dict[str, Any]] = []

    for att in raw_attachments:
        if att["parent_key"]:
            grouped_by_parent.setdefault(att["parent_key"], []).append(att)
        else:
            standalone.append(att)

    primary_attachments: list[dict[str, Any]] = []
    for _p_key, att_list in grouped_by_parent.items():
        pdf_atts = [
            a
            for a in att_list
            if a["content_type"] == "application/pdf"
               or a["path"].suffix.lower() == ".pdf"
        ]
        primary = pdf_atts[0] if pdf_atts else att_list[0]
        primary_attachments.append(primary)

    primary_attachments.extend(standalone)

    total_documents = len(primary_attachments)
    ingested_keys = syncer.store.get_all_ingested_item_keys()

    indexed_count = 0
    pending_index_count = 0
    unconverted_count = 0
    converted_count = 0
    converter_counts = {
        "Datalab (Cloud)": 0,
        "Docling (Local)": 0,
        "MarkItDown (Local)": 0,
        "Other": 0,
    }
    unconverted_items: list[dict[str, Any]] = []
    pending_index_items: list[dict[str, Any]] = []

    for att in primary_attachments:
        ak = att["attachment_key"]
        meta = meta_map.get(ak)
        title = att["parent_title"] or att["filename"] or ak
        is_indexed = ak in ingested_keys

        if meta:
            converted_count += 1
            cid = meta.get("converter_id", "")
            if cid == "datalab":
                converter_counts["Datalab (Cloud)"] += 1
            elif cid in ("docling_local", "docling"):
                converter_counts["Docling (Local)"] += 1
            elif cid == "markitdown":
                converter_counts["MarkItDown (Local)"] += 1
            else:
                converter_counts["Other"] += 1

            if is_indexed:
                indexed_count += 1
            else:
                pending_index_count += 1
                pending_index_items.append({
                    "key": ak,
                    "parent_key": att["parent_key"],
                    "title": title,
                    "filename": att["filename"],
                    "content_type": att["content_type"],
                    "converter_id": meta.get("converter_id"),
                })
        else:
            unconverted_count += 1
            unconverted_items.append({
                "key": ak,
                "parent_key": att["parent_key"],
                "title": title,
                "filename": att["filename"],
                "content_type": att["content_type"],
            })

    return {
        "status": "success",
        "total_documents": total_documents,
        "indexed_count": indexed_count,
        "pending_index_count": pending_index_count,
        "unconverted_count": unconverted_count,
        "converted_count": converted_count,
        "converter_counts": converter_counts,
        "unconverted_items": unconverted_items,
        "pending_index_items": pending_index_items,
    }


@app.post("/convert/{item_key}", dependencies=[Depends(verify_auth)])
async def convert_item(item_key: str, request: ConvertRequest | None = None) -> dict:
    """Convert a single item using the specified or active converter strategy.

    When ``force=False`` (default), checks whether the item is already cached
    with the target converter and identical source file hash. If so, returns
    ``{"status": "skipped", ...}`` without re-converting.

    When ``force=True`` or when un-converted/stale, destroys any existing Markdown
    bundle for the item, converts fresh, writes the bundle + frontmatter, and
    immediately re-indexes the vector store.
    """
    from zocrates.rag.chunker import chunk_markdown
    from zocrates.config import (
        CHUNK_MAX_CHARS,
        CHUNK_OVERLAP_CHARS,
        get_document_converter_id,
    )
    from zocrates.rag.document_processor import UnsupportedConversionError

    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    attachment = syncer.db.get_attachment_by_key(item_key)
    if not attachment or not attachment.path or not attachment.path.exists():
        # Retry with brief backoff in case WAL checkpoint is finishing
        for _ in range(4):
            await asyncio.sleep(0.3)
            attachment = syncer.db.get_attachment_by_key(item_key)
            if attachment and attachment.path and attachment.path.exists():
                break

    if not attachment or not attachment.path or not attachment.path.exists():
        # Fallback: check if storage/<item_key>/ contains an attachment file directly
        storage_dir = getattr(syncer.db, "_storage_dir", None)
        if storage_dir and (storage_dir / item_key).is_dir():
            candidates = [
                f for f in (storage_dir / item_key).iterdir()
                if f.is_file() and not f.name.startswith(".")
            ]
            if candidates:
                target_file = sorted(
                    candidates,
                    key=lambda f: 0 if f.suffix.lower() == ".pdf" else 1,
                )[0]
                from zocrates.rag.zotero_db import ZoteroAttachment

                attachment = ZoteroAttachment(
                    item_id=0,
                    item_key=item_key,
                    parent_item_id=None,
                    parent_item_key=None,
                    content_type=(
                        "application/pdf"
                        if target_file.suffix.lower() == ".pdf"
                        else "application/octet-stream"
                    ),
                    path=target_file,
                )

    if not attachment or not attachment.path or not attachment.path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No valid attachment file found on disk for item key '{item_key}'",
        )

    requested_id = request.converter_id if request else None
    active_id = str(
        requested_id
        or syncer.processor._document_converter_id
        or get_document_converter_id(syncer._zotero_data_dir)
        or "markitdown"
    )

    parent_item = None
    if attachment.parent_item_id:
        parent_item = syncer.db.get_item(attachment.parent_item_id)
    elif attachment.parent_item_key:
        parent_item = syncer.db.get_item_by_key(attachment.parent_item_key)

    p_type = parent_item.item_type if (parent_item and isinstance(getattr(parent_item, "item_type", None), str)) else None
    p_title = parent_item.title if (parent_item and isinstance(getattr(parent_item, "title", None), str)) else item_key
    p_colls = parent_item.collections if (parent_item and isinstance(getattr(parent_item, "collections", None), list)) else None
    p_authors = parent_item.creators if (parent_item and isinstance(getattr(parent_item, "creators", None), list)) else None
    p_year = parent_item.year if (parent_item and isinstance(getattr(parent_item, "year", None), (str, int))) else None
    p_pub = parent_item.publication if (parent_item and isinstance(getattr(parent_item, "publication", None), str)) else None
    p_doi = parent_item.doi if (parent_item and isinstance(getattr(parent_item, "doi", None), str)) else None
    p_url = parent_item.url if (parent_item and isinstance(getattr(parent_item, "url", None), str)) else None
    p_abstract = parent_item.abstract if (parent_item and isinstance(getattr(parent_item, "abstract", None), str)) else None

    target_cache_key = (
        attachment.parent_item_key if attachment.parent_item_key else attachment.item_key
    )
    target_item_id = (
        attachment.parent_item_id if attachment.parent_item_id else attachment.item_id
    )

    is_pdf = attachment.content_type == "application/pdf" or (
            attachment.path and attachment.path.suffix.lower() == ".pdf"
    )
    effective_id = active_id
    if active_id == "datalab" and not is_pdf:
        effective_id = syncer.processor.datalab_non_pdf_fallback or "markitdown"

    force = request.force if request else False
    if not force and syncer.processor.is_cached(attachment.item_key):
        from zocrates.rag.document_processor import _file_sha256

        meta = syncer.processor.get_metadata(attachment.item_key)
        if meta and meta.get("converter_id") == effective_id:
            try:
                current_hash = _file_sha256(attachment.path)
                if meta.get("source_hash") == current_hash:
                    logger.info(
                        "Skipping already converted item %s ('%s') with %s",
                        attachment.item_key,
                        p_title,
                        effective_id,
                    )
                    return {
                        "status": "skipped",
                        "message": f"Already converted with {effective_id}",
                        "item_key": attachment.item_key,
                        "parent_item_key": attachment.parent_item_key,
                        "title": p_title,
                        "converter_id": effective_id,
                        "already_converted": True,
                    }
            except Exception as exc:
                logger.warning(
                    "Failed to verify source hash for %s: %s",
                    attachment.item_key,
                    exc,
                )

    try:
        from zocrates.config import get_conversion_timeout_seconds

        timeout_sec = get_conversion_timeout_seconds(syncer._zotero_data_dir)
        markdown = await asyncio.wait_for(
            asyncio.to_thread(
                syncer.processor.convert_with,
                item_key=attachment.item_key,
                converter_id=effective_id,
                source_path=attachment.path,
                content_type=attachment.content_type,
                parent_item_type=p_type,
                title=p_title,
                collections=p_colls,
                parent_item_key=attachment.parent_item_key,
                authors=p_authors,
                year=p_year,
                publication=p_pub,
                doi=p_doi,
                url=p_url,
                abstract=p_abstract,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError as exc:
        timeout_msg = (
            f"Conversion timed out after {timeout_sec}s with engine '{effective_id}'. "
            "For large or math-heavy PDFs on CPU, try Docling Fast Mode or MarkItDown."
        )
        try:
            from zocrates.rag.document_processor import _file_sha256

            source_hash = _file_sha256(attachment.path)
            syncer.processor.record_failure(
                attachment.item_key, effective_id, timeout_msg, source_hash
            )
        except Exception:
            pass
        raise HTTPException(
            status_code=504,
            detail={
                "error": "conversion_timeout",
                "message": timeout_msg,
                "converter_id": effective_id,
                "item_key": attachment.item_key,
            },
        ) from exc
    except UnsupportedConversionError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_content_type",
                "message": str(exc),
                "converter_id": effective_id,
                "item_key": attachment.item_key,
            },
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_converter",
                "message": str(exc),
                "converter_id": effective_id,
                "item_key": attachment.item_key,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "conversion_failed",
                "message": str(exc),
                "converter_id": effective_id,
                "item_key": attachment.item_key,
            },
        ) from exc

    # Post-conversion VLM figure enrichment pass
    try:
        from zocrates.rag.vlm_captioner import enrich_document_bundle

        bundle_dir = syncer.processor._cache_dir / attachment.item_key
        enriched = await asyncio.to_thread(
            enrich_document_bundle,
            bundle_dir,
            syncer._zotero_data_dir,
        )
        if enriched:
            markdown = enriched
    except Exception as exc:
        logger.warning(
            "VLM figure enrichment failed for %s: %s; proceeding with base markdown",
            attachment.item_key,
            exc,
        )

    # Update ToC on final post-enrichment markdown
    try:
        from zocrates.rag.toc_generator import generate_toc, save_toc_files

        bundle_dir = syncer.processor._cache_dir / attachment.item_key
        meta = syncer.processor.get_metadata(attachment.item_key)
        abs_text = (meta.get("abstract") if meta else None) or p_abstract
        abs_src = (meta.get("abstract_source") if meta else None) or ("zotero" if p_abstract else "extractive")
        t_json, t_md = generate_toc(
            markdown,
            title=p_title,
            abstract=abs_text,
            abstract_source=abs_src,
            item_key=attachment.item_key,
            parent_item_key=attachment.parent_item_key,
        )
        save_toc_files(bundle_dir, t_json, t_md)
    except Exception as exc:
        logger.warning(
            "Post-enrichment ToC generation failed for %s: %s",
            attachment.item_key,
            exc,
        )

    # Immediate re-indexing into vector store
    chunks = chunk_markdown(
        markdown,
        max_chars=CHUNK_MAX_CHARS,
        overlap_chars=CHUNK_OVERLAP_CHARS,
        document_title=p_title,
    )
    if chunks:
        meta_dict: dict[str, Any] = {
            "content_type": attachment.content_type,
        }
        if attachment.parent_item_key:
            meta_dict["parent_item_key"] = attachment.parent_item_key
        if attachment.parent_item_id:
            meta_dict["parent_item_id"] = attachment.parent_item_id
        else:
            meta_dict["is_standalone"] = True

        syncer.store.ingest_document(
            attachment.item_key,
            attachment.parent_item_id or attachment.item_id,
            chunks,
            meta_dict,
        )
        syncer.processor.update_indexed_at(attachment.item_key)
        _dispatch_edge_evaluation(attachment.item_key, syncer)

    return {
        "status": "success",
        "item_key": attachment.item_key,
        "converter_id": effective_id,
        "char_count": len(markdown),
        "chunk_count": len(chunks),
    }


@app.get("/cost-estimate", dependencies=[Depends(verify_auth)])
async def cost_estimate(
        converter_id: str = "datalab",
        item_keys: str | None = None,
        force: bool = False,
) -> dict:
    """Estimate page count and USD cost for a paid converter across targeted items."""
    from zocrates.rag.document_processor import _file_sha256
    from zocrates.rag.pdf_utils import count_pdf_pages

    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    COST_PER_PAGE_MAP = {
        "markitdown": 0.0,
        "docling_local": 0.0,
        "datalab": 0.004,
        "gemini_vision": 0.0005,
        "google_document_ai": 0.0015,
    }
    cost_per_page = COST_PER_PAGE_MAP.get(converter_id, 0.0)

    target_keys = (
        [k.strip() for k in item_keys.split(",") if k.strip()]
        if item_keys
        else None
    )

    logger.info(
        "Cost estimate requested for converter='%s', item_keys=%s, force=%s",
        converter_id,
        target_keys,
        force,
    )

    attachments = []
    if target_keys:
        for k in target_keys:
            att = syncer.db.get_attachment_by_key(k)
            if not att or not att.path or not att.path.exists():
                parent_item = syncer.db.get_item_by_key(k)
                if parent_item:
                    atts = syncer.db.get_attachments(parent_item.item_id)
                    if atts:
                        pdf_atts = [
                            a
                            for a in atts
                            if (
                                       a.content_type == "application/pdf"
                                       or (a.path and a.path.suffix.lower() == ".pdf")
                               )
                               and a.path
                               and a.path.exists()
                        ]
                        if pdf_atts:
                            att = pdf_atts[0]
                        else:
                            valid_atts = [a for a in atts if a.path and a.path.exists()]
                            if valid_atts:
                                att = valid_atts[0]
            if att and att.path and att.path.exists():
                attachments.append(att)
    else:
        raw_attachments = syncer.db.get_all_convertible_attachments()
        grouped_by_parent: dict[str, list[dict[str, Any]]] = {}
        standalone: list[dict[str, Any]] = []

        for a in raw_attachments:
            if a["parent_key"]:
                grouped_by_parent.setdefault(a["parent_key"], []).append(a)
            else:
                standalone.append(a)

        for _p_key, att_list in grouped_by_parent.items():
            pdf_atts = [
                a
                for a in att_list
                if a["content_type"] == "application/pdf"
                   or a["path"].suffix.lower() == ".pdf"
            ]
            primary = pdf_atts[0] if pdf_atts else att_list[0]
            from zocrates.rag.zotero_db import ZoteroAttachment
            attachments.append(
                ZoteroAttachment(
                    item_id=primary["attachment_id"],
                    item_key=primary["attachment_key"],
                    parent_item_id=primary["parent_item_id"],
                    parent_item_key=primary["parent_key"],
                    content_type=primary["content_type"],
                    path=primary["path"],
                )
            )

        for s in standalone:
            from zocrates.rag.zotero_db import ZoteroAttachment
            attachments.append(
                ZoteroAttachment(
                    item_id=s["attachment_id"],
                    item_key=s["attachment_key"],
                    parent_item_id=None,
                    parent_item_key=None,
                    content_type=s["content_type"],
                    path=s["path"],
                )
            )

    already_converted = []
    eligible_attachments = []

    for att in attachments:
        is_pdf = att.content_type == "application/pdf" or (
                att.path and att.path.suffix.lower() == ".pdf"
        )
        if converter_id == "datalab" and not is_pdf:
            effective_id = syncer.processor.datalab_non_pdf_fallback or "markitdown"
        else:
            effective_id = converter_id

        if not force:
            meta = syncer.processor.get_metadata(att.item_key)
            if meta and meta.get("converter_id") == effective_id:
                try:
                    current_hash = _file_sha256(att.path)
                    if meta.get("source_hash") == current_hash:
                        already_converted.append(att)
                        continue
                except Exception:
                    pass
        eligible_attachments.append(att)

    total_pages = sum(
        count_pdf_pages(att.path)
        for att in eligible_attachments
        if att.path
        and (
                att.content_type == "application/pdf"
                or att.path.suffix.lower() == ".pdf"
        )
    )
    estimated_cost = round(total_pages * cost_per_page, 4)

    from zocrates.config import (
        get_datalab_merge_cross_page,
        get_enable_vlm_captioning,
        get_vlm_captioning_backup_model,
        get_vlm_captioning_local_model,
        get_vlm_captioning_model,
        get_vlm_captioning_provider,
        get_vlm_max_images_per_doc,
    )

    merge_cross_page = get_datalab_merge_cross_page(syncer._zotero_data_dir)
    enable_vlm = get_enable_vlm_captioning(syncer._zotero_data_dir)
    vlm_info: dict[str, Any] = {
        "enabled": enable_vlm,
    }
    if enable_vlm:
        vlm_prov = get_vlm_captioning_provider(syncer._zotero_data_dir)
        vlm_model = get_vlm_captioning_model(syncer._zotero_data_dir)
        vlm_local = get_vlm_captioning_local_model(syncer._zotero_data_dir)
        is_free = (vlm_prov == "ollama") or (":free" in (vlm_model or "").lower())
        active_model = (vlm_local if vlm_prov == "ollama" and vlm_local else vlm_model) or "default"
        vlm_info.update({
            "provider": vlm_prov,
            "model": active_model,
            "cloud_model": vlm_model,
            "backup_model": get_vlm_captioning_backup_model(syncer._zotero_data_dir),
            "local_model": vlm_local,
            "max_images_per_doc": get_vlm_max_images_per_doc(syncer._zotero_data_dir),
            "is_free_tier": is_free,
        })

    logger.info(
        "Cost estimate result: %d target docs, %d already converted, %d eligible docs (%d pages, $%.4f USD), vlm_enabled=%s",
        len(attachments),
        len(already_converted),
        len(eligible_attachments),
        total_pages,
        estimated_cost,
        enable_vlm,
    )

    return {
        "status": "success",
        "converter_id": converter_id,
        "datalab_merge_cross_page": merge_cross_page,
        "target_documents": len(attachments),
        "already_converted_count": len(already_converted),
        "eligible_documents": len(eligible_attachments),
        "total_pages": total_pages,
        "cost_per_page_usd": cost_per_page,
        "estimated_cost_usd": estimated_cost,
        "vlm_captioning": vlm_info,
    }


@app.post("/batch-convert", dependencies=[Depends(verify_auth)])
async def batch_convert(
        batch_req: BatchConvertRequest,
        raw_request: Request,
) -> EventSourceResponse:
    """Batch re-convert documents with Server-Sent Events (SSE) progress streaming.

    Processes documents serially to avoid CPU/VRAM thrashing and cloud rate limits.
    """
    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    from zocrates.rag.chunker import chunk_markdown
    from zocrates.config import (
        CHUNK_MAX_CHARS,
        CHUNK_OVERLAP_CHARS,
        CONVERSION_TIMEOUT_SECONDS,
        get_conversion_timeout_seconds,
        get_document_converter_id,
    )
    from zocrates.rag.document_processor import _file_sha256

    active_id = str(
        batch_req.converter_id
        or syncer.processor._document_converter_id
        or get_document_converter_id(syncer._zotero_data_dir)
        or "markitdown"
    )

    try:
        syncer.processor._get_converter_by_id(active_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unknown_converter",
                "message": f"Converter '{active_id}' is not registered.",
                "converter_id": active_id,
            },
        ) from exc

    logger.info(
        "Batch convert requested for %d keys with converter='%s' (force=%s): %s",
        len(batch_req.item_keys),
        active_id,
        batch_req.force,
        batch_req.item_keys,
    )

    # Resolve items and attachments
    targets: list[dict[str, Any]] = []
    for key in batch_req.item_keys:
        clean_key = key.strip()
        if not clean_key:
            continue

        att = syncer.db.get_attachment_by_key(clean_key)
        parent_item = None
        if att:
            if att.parent_item_id:
                parent_item = syncer.db.get_item(att.parent_item_id)
            elif att.parent_item_key:
                parent_item = syncer.db.get_item_by_key(att.parent_item_key)
            title = (
                parent_item.title
                if (parent_item and isinstance(getattr(parent_item, "title", None), str))
                else (att.path.name if att.path else clean_key)
            )
            targets.append({
                "attachment": att,
                "parent_item": parent_item,
                "title": title,
                "original_key": clean_key,
            })
        else:
            parent_item = syncer.db.get_item_by_key(clean_key)
            if parent_item:
                atts = syncer.db.get_attachments(parent_item.item_id)
                primary_att = None
                if atts:
                    pdf_atts = [
                        a
                        for a in atts
                        if (
                                   a.content_type == "application/pdf"
                                   or (a.path and a.path.suffix.lower() == ".pdf")
                           )
                           and a.path
                           and a.path.exists()
                    ]
                    if pdf_atts:
                        primary_att = pdf_atts[0]
                    else:
                        valid_atts = [a for a in atts if a.path and a.path.exists()]
                        if valid_atts:
                            primary_att = valid_atts[0]
                title = parent_item.title if isinstance(getattr(parent_item, "title", None), str) else clean_key
                targets.append({
                    "attachment": primary_att,
                    "parent_item": parent_item,
                    "title": title,
                    "original_key": clean_key,
                })
            else:
                targets.append({
                    "attachment": None,
                    "parent_item": None,
                    "title": clean_key,
                    "original_key": clean_key,
                })

    async def sse_batch_generator() -> AsyncIterator[dict[str, Any]]:
        """Stream batch document conversion progress events over Server-Sent Events.

        Yields:
            Dictionary event payloads formatted for EventSourceResponse.
        """
        total = len(targets)
        processed = 0
        skipped = 0
        failed = 0
        failed_items: list[dict[str, Any]] = []

        logger.info(
            "Starting batch conversion loop for %d target documents with engine '%s'",
            total,
            active_id,
        )

        if total == 0:
            yield {
                "event": "complete",
                "data": json.dumps({
                    "total": 0,
                    "processed": 0,
                    "skipped": 0,
                    "failed": 0,
                    "failed_items": [],
                }),
            }
            return

        for idx, target in enumerate(targets, 1):
            if await raw_request.is_disconnected():
                logger.info("Client disconnected during batch convert. Stopping loop.")
                break

            att = target["attachment"]
            parent_item = target["parent_item"]
            title = target["title"]
            orig_key = target["original_key"]

            if not att or not att.path or not att.path.exists():
                failed += 1
                err_msg = f"No valid attachment file found on disk for key '{orig_key}'"
                logger.error("[%d/%d] Attachment error for '%s' (%s): %s", idx, total, title, orig_key, err_msg)
                failed_items.append({"item_key": orig_key, "title": title, "error": err_msg})
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "processed": processed,
                        "skipped": skipped,
                        "failed": failed,
                        "total": total,
                        "current_index": idx,
                        "current_key": orig_key,
                        "current_title": title,
                        "status": "failed",
                        "error": err_msg,
                    }),
                }
                continue

            # Determine effective converter (route non-PDFs to fallback for Datalab)
            is_pdf = att.content_type == "application/pdf" or (att.path and att.path.suffix.lower() == ".pdf")
            if active_id == "datalab" and not is_pdf:
                effective_id = syncer.processor.datalab_non_pdf_fallback or "markitdown"
            else:
                effective_id = active_id

            # Check if already converted with effective converter (incremental filtering)
            if not batch_req.force:
                meta = syncer.processor.get_metadata(att.item_key)
                if meta and meta.get("converter_id") == effective_id:
                    try:
                        current_hash = _file_sha256(att.path)
                        if meta.get("source_hash") == current_hash:
                            skipped += 1
                            logger.info("[%d/%d] Skipping already converted item %s ('%s') with %s", idx, total,
                                        att.item_key, title, effective_id)
                            yield {
                                "event": "progress",
                                "data": json.dumps({
                                    "processed": processed,
                                    "skipped": skipped,
                                    "failed": failed,
                                    "total": total,
                                    "current_index": idx,
                                    "current_key": att.item_key,
                                    "current_title": title,
                                    "status": "skipped",
                                    "message": f"Already converted with {effective_id}",
                                    "error": None,
                                }),
                            }
                            continue
                    except Exception:
                        pass

            # Execute conversion serially
            p_type = parent_item.item_type if (parent_item and isinstance(getattr(parent_item, "item_type", None), str)) else None
            p_title = parent_item.title if (parent_item and isinstance(getattr(parent_item, "title", None), str)) else title
            p_colls = parent_item.collections if (parent_item and isinstance(getattr(parent_item, "collections", None), list)) else None
            p_authors = parent_item.creators if (parent_item and isinstance(getattr(parent_item, "creators", None), list)) else None
            p_year = parent_item.year if (parent_item and isinstance(getattr(parent_item, "year", None), (str, int))) else None
            p_pub = parent_item.publication if (parent_item and isinstance(getattr(parent_item, "publication", None), str)) else None
            p_doi = parent_item.doi if (parent_item and isinstance(getattr(parent_item, "doi", None), str)) else None
            p_url = parent_item.url if (parent_item and isinstance(getattr(parent_item, "url", None), str)) else None
            p_abstract = parent_item.abstract if (parent_item and isinstance(getattr(parent_item, "abstract", None), str)) else None

            logger.info("[%d/%d] Converting item %s ('%s') using engine '%s' (effective: '%s')...", idx, total,
                        att.item_key, p_title, active_id, effective_id)
            yield {
                "event": "progress",
                "data": json.dumps({
                    "processed": processed,
                    "skipped": skipped,
                    "failed": failed,
                    "total": total,
                    "current_index": idx,
                    "current_key": att.item_key,
                    "current_title": title,
                    "status": "converting",
                    "error": None,
                }),
            }

            try:
                markdown = await asyncio.wait_for(
                    asyncio.to_thread(
                        syncer.processor.convert_with,
                        item_key=att.item_key,
                        converter_id=effective_id,
                        source_path=att.path,
                        content_type=att.content_type,
                        parent_item_type=p_type,
                        title=p_title,
                        collections=p_colls,
                        parent_item_key=att.parent_item_key,
                        authors=p_authors,
                        year=p_year,
                        publication=p_pub,
                        doi=p_doi,
                        url=p_url,
                        abstract=p_abstract,
                    ),
                    timeout=get_conversion_timeout_seconds(syncer._zotero_data_dir),
                )

                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "processed": processed,
                        "skipped": skipped,
                        "failed": failed,
                        "total": total,
                        "current_index": idx,
                        "current_key": att.item_key,
                        "current_title": title,
                        "status": "indexing",
                        "error": None,
                    }),
                }

                # Post-conversion VLM figure enrichment pass
                try:
                    from zocrates.rag.vlm_captioner import enrich_document_bundle

                    bundle_dir = syncer.processor._cache_dir / att.item_key
                    enriched = await asyncio.to_thread(
                        enrich_document_bundle,
                        bundle_dir,
                        syncer._zotero_data_dir,
                    )
                    if enriched:
                        markdown = enriched
                except Exception as exc:
                    logger.warning(
                        "VLM figure enrichment failed for %s: %s; proceeding with base markdown",
                        att.item_key,
                        exc,
                    )

                # Update ToC on final post-enrichment markdown
                try:
                    from zocrates.rag.toc_generator import generate_toc, save_toc_files

                    bundle_dir = syncer.processor._cache_dir / att.item_key
                    meta = syncer.processor.get_metadata(att.item_key)
                    abs_text = (meta.get("abstract") if meta else None) or p_abstract
                    abs_src = (meta.get("abstract_source") if meta else None) or ("zotero" if p_abstract else "extractive")
                    t_json, t_md = generate_toc(
                        markdown,
                        title=p_title,
                        abstract=abs_text,
                        abstract_source=abs_src,
                        item_key=att.item_key,
                        parent_item_key=att.parent_item_key,
                    )
                    save_toc_files(bundle_dir, t_json, t_md)
                except Exception as exc:
                    logger.warning(
                        "Post-enrichment ToC generation failed for %s: %s",
                        att.item_key,
                        exc,
                    )

                chunks = chunk_markdown(
                    markdown,
                    max_chars=CHUNK_MAX_CHARS,
                    overlap_chars=CHUNK_OVERLAP_CHARS,
                    document_title=p_title,
                )
                if chunks:
                    meta_dict = {
                        "content_type": att.content_type,
                    }
                    if att.parent_item_key:
                        meta_dict["parent_item_key"] = att.parent_item_key
                    if att.parent_item_id:
                        meta_dict["parent_item_id"] = att.parent_item_id
                    else:
                        meta_dict["is_standalone"] = True

                    await asyncio.to_thread(
                        syncer.store.ingest_document,
                        att.item_key,
                        att.parent_item_id or att.item_id,
                        chunks,
                        meta_dict,
                    )
                    syncer.processor.update_indexed_at(att.item_key)
                    _dispatch_edge_evaluation(att.item_key, syncer)

                processed += 1
                logger.info(
                    "[%d/%d] Successfully converted and indexed item %s ('%s') into %d chunks (%d chars)",
                    idx,
                    total,
                    att.item_key,
                    p_title,
                    len(chunks),
                    len(markdown),
                )
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "processed": processed,
                        "skipped": skipped,
                        "failed": failed,
                        "total": total,
                        "current_index": idx,
                        "current_key": att.item_key,
                        "current_title": title,
                        "status": "completed",
                        "error": None,
                    }),
                }

            except asyncio.TimeoutError:
                t_sec = get_conversion_timeout_seconds(syncer._zotero_data_dir)
                timeout_msg = (
                    f"Conversion timed out after {t_sec}s with engine '{effective_id}'."
                )
                try:
                    source_hash = _file_sha256(att.path)
                    syncer.processor.record_failure(
                        att.item_key, effective_id, timeout_msg, source_hash
                    )
                except Exception:
                    pass
                failed += 1
                logger.error("[%d/%d] Conversion timeout on item %s ('%s'): %s", idx, total, att.item_key, p_title,
                             timeout_msg)
                failed_items.append({"item_key": att.item_key, "title": title, "error": timeout_msg})
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "processed": processed,
                        "skipped": skipped,
                        "failed": failed,
                        "total": total,
                        "current_index": idx,
                        "current_key": att.item_key,
                        "current_title": title,
                        "status": "failed",
                        "error": timeout_msg,
                    }),
                }
            except Exception as exc:
                err_msg = str(exc)
                try:
                    source_hash = _file_sha256(att.path)
                    syncer.processor.record_failure(
                        att.item_key, effective_id, err_msg, source_hash
                    )
                except Exception:
                    pass
                failed += 1
                logger.error("[%d/%d] Conversion failed on item %s ('%s') with engine '%s': %s", idx, total,
                             att.item_key, p_title, effective_id, err_msg, exc_info=True)
                failed_items.append({"item_key": att.item_key, "title": title, "error": err_msg})
                yield {
                    "event": "progress",
                    "data": json.dumps({
                        "processed": processed,
                        "skipped": skipped,
                        "failed": failed,
                        "total": total,
                        "current_index": idx,
                        "current_key": att.item_key,
                        "current_title": title,
                        "status": "failed",
                        "error": err_msg,
                    }),
                }

        logger.info(
            "Batch conversion finished: %d processed, %d skipped, %d failed out of %d total targets",
            processed,
            skipped,
            failed,
            total,
        )

        yield {
            "event": "complete",
            "data": json.dumps({
                "total": total,
                "processed": processed,
                "skipped": skipped,
                "failed": failed,
                "failed_items": failed_items,
            }),
        }

    return EventSourceResponse(sse_batch_generator())


@app.post("/batch-index", dependencies=[Depends(verify_auth)])
async def batch_index(
        batch_req: BatchIndexRequest,
        raw_request: Request,
) -> EventSourceResponse:
    """Batch index cached documents into the vector store with Server-Sent Events (SSE) progress streaming."""
    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    from zocrates.rag.chunker import chunk_markdown
    from zocrates.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS

    targets: list[dict[str, Any]] = []

    if batch_req.all_pending:
        raw_attachments = syncer.db.get_all_convertible_attachments()
        meta_map = syncer.processor.get_all_metadata()
        ingested_keys = syncer.store.get_all_ingested_item_keys()

        for att in raw_attachments:
            ak = att["attachment_key"]
            if ak in meta_map and ak not in ingested_keys:
                md_path = syncer.processor.cache_dir / ak / "document.md"
                if md_path.exists():
                    parent_item = syncer.db.get_item_by_key(att["parent_key"]) if att["parent_key"] else None
                    title = parent_item.title if parent_item and parent_item.title else (att["filename"] or ak)
                    targets.append({
                        "item_key": ak,
                        "parent_item_key": att["parent_key"] or "",
                        "parent_item": parent_item,
                        "content_type": att.get("content_type"),
                        "title": title,
                        "md_path": md_path,
                    })
    elif batch_req.keys:
        for key in batch_req.keys:
            clean_key = key.strip()
            if not clean_key:
                continue
            md_path = syncer.processor.cache_dir / clean_key / "document.md"
            if md_path.exists():
                att = syncer.db.get_attachment_by_key(clean_key)
                parent_item = None
                if att and att.parent_item_id:
                    parent_item = syncer.db.get_item(att.parent_item_id)
                elif att and att.parent_item_key:
                    parent_item = syncer.db.get_item_by_key(att.parent_item_key)
                title = parent_item.title if parent_item and parent_item.title else (
                    att.path.name if att and att.path else clean_key)
                targets.append({
                    "item_key": clean_key,
                    "parent_item_key": (att.parent_item_key if att else "") or (
                        parent_item.item_key if parent_item else ""),
                    "parent_item": parent_item,
                    "content_type": att.content_type if att else None,
                    "title": title,
                    "md_path": md_path,
                })
            else:
                parent_item = syncer.db.get_item_by_key(clean_key)
                if parent_item:
                    atts = syncer.db.get_attachments(parent_item.item_id)
                    for att in atts:
                        a_md = syncer.processor.cache_dir / att.item_key / "document.md"
                        if a_md.exists():
                            title = parent_item.title or att.item_key
                            targets.append({
                                "item_key": att.item_key,
                                "parent_item_key": clean_key,
                                "parent_item": parent_item,
                                "content_type": att.content_type,
                                "title": title,
                                "md_path": a_md,
                            })

    async def sse_index_generator() -> AsyncIterator[dict[str, Any]]:
        """Stream batch indexing progress events over Server-Sent Events.

        Yields:
            Dictionary event payloads formatted for EventSourceResponse.
        """
        total = len(targets)
        processed = 0
        failed = 0
        failed_items: list[dict[str, Any]] = []

        logger.info("Starting batch indexing SSE loop for %d documents", total)

        yield {
            "event": "batch_start",
            "data": json.dumps({"total": total}),
        }

        if total == 0:
            yield {
                "event": "batch_complete",
                "data": json.dumps({
                    "total": 0,
                    "processed": 0,
                    "failed": 0,
                    "failed_items": [],
                }),
            }
            return

        for idx, target in enumerate(targets, 1):
            if await raw_request.is_disconnected():
                logger.info("Client disconnected during batch index. Stopping loop.")
                break

            ak = target["item_key"]
            title = target["title"]
            md_path = target["md_path"]
            parent_item = target["parent_item"]
            parent_key = target["parent_item_key"]
            content_type = target["content_type"]

            yield {
                "event": "item_start",
                "data": json.dumps({
                    "index": idx,
                    "total": total,
                    "key": ak,
                    "title": title,
                }),
            }

            try:
                markdown_text = await asyncio.to_thread(md_path.read_text, "utf-8")
                chunks = chunk_markdown(
                    markdown_text,
                    max_chars=CHUNK_MAX_CHARS,
                    overlap_chars=CHUNK_OVERLAP_CHARS,
                    document_title=title,
                )

                if chunks:
                    meta_dict = {
                        "content_type": content_type,
                    }
                    if parent_key:
                        meta_dict["parent_item_key"] = parent_key
                    if parent_item and parent_item.item_id:
                        meta_dict["parent_item_id"] = parent_item.item_id
                        item_id = parent_item.item_id
                    else:
                        meta_dict["is_standalone"] = True
                        item_id = 0

                    await asyncio.to_thread(
                        syncer.store.ingest_document,
                        ak,
                        item_id,
                        chunks,
                        meta_dict,
                    )
                    syncer.processor.update_indexed_at(ak)
                    _dispatch_edge_evaluation(ak, syncer)

                processed += 1
                logger.info(
                    "[%d/%d] Successfully indexed item %s ('%s') into %d chunks",
                    idx,
                    total,
                    ak,
                    title,
                    len(chunks),
                )
                yield {
                    "event": "item_success",
                    "data": json.dumps({
                        "index": idx,
                        "total": total,
                        "key": ak,
                        "title": title,
                        "chunks": len(chunks),
                        "processed": processed,
                    }),
                }
            except Exception as exc:
                failed += 1
                err_msg = str(exc)
                logger.error(
                    "[%d/%d] Indexing failed for item %s ('%s'): %s",
                    idx,
                    total,
                    ak,
                    title,
                    exc,
                    exc_info=True,
                )
                failed_items.append({"key": ak, "title": title, "error": err_msg})
                yield {
                    "event": "item_error",
                    "data": json.dumps({
                        "index": idx,
                        "total": total,
                        "key": ak,
                        "title": title,
                        "error": err_msg,
                    }),
                }
            finally:
                markdown_text = None
                chunks = None
                meta_dict = None
                gc.collect()

            await asyncio.sleep(0)

        yield {
            "event": "batch_complete",
            "data": json.dumps({
                "total": total,
                "processed": processed,
                "failed": failed,
                "failed_items": failed_items,
            }),
        }

    return EventSourceResponse(sse_index_generator())


@app.post("/sync", dependencies=[Depends(verify_auth)])
async def sync_library(request: SyncRequest) -> dict:
    """Manually trigger an incremental sync or full library scan.

    Raises:
        HTTPException: 400 if the backend is not yet registered; 500 on
            unexpected sync failure.

    Returns:
        Dict with keys ``status``, ``mode`` (``"incremental"`` or ``"full"``),
        and ``results``.
    """
    try:
        syncer = get_syncer()
        if request.item_ids:
            results = await syncer.ingest_items(request.item_ids)
            return {"status": "success", "mode": "incremental", "results": results}
        else:
            results = await syncer.full_sync()
            return {"status": "success", "mode": "full", "results": results}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sync failed: {e}") from e


# ---------------------------------------------------------------------------
# Phase 7 onboarding: SSE full-library sync job (plan §6 / §8)
# ---------------------------------------------------------------------------
# One active sync job per backend. A second POST /sync/stream while a job is
# running ATTACHES to the running job's event fan-out (subscriber set) instead
# of starting a parallel sync. Client disconnect and POST /sync/abort both set
# the same cooperative cancel flag honored by LibrarySyncer.full_sync.
_SYNC_PROGRESS_MIN_INTERVAL_SEC: float = 0.25
_sync_job_lock = asyncio.Lock()
_sync_job: dict[str, Any] | None = None
_SYNC_TERMINAL_EVENTS = frozenset({"done", "aborted", "auto_aborted", "error"})


def _snapshot_sync_state(job: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-serializable snapshot of the job's progress state."""
    snapshot = dict(job["state"])
    snapshot["elapsed_ms"] = int((time.perf_counter() - job["started"]) * 1000)
    return snapshot


def _broadcast_sync_event(job: dict[str, Any], event: dict[str, str]) -> None:
    """Fan an SSE event out to every subscriber queue of the job."""
    for queue in list(job["subscribers"]):
        queue.put_nowait(event)


async def _run_sync_job(job: dict[str, Any]) -> None:
    """Run the full-library sync job and broadcast throttled progress events."""
    global _sync_job
    state = job["state"]
    loop = asyncio.get_running_loop()
    last_emit = 0.0

    def progress_callback(info: dict[str, Any]) -> None:
        """Update sync state and broadcast throttled progress events.

        Args:
            info: State dictionary containing updated sync progress metrics.
        """
        nonlocal last_emit
        state.update(info)
        now = loop.time()
        # Coalesce emissions to >= 250ms intervals; each item boundary that is
        # slower than the interval still emits immediately (snapshot carries
        # the latest counts, so throttled-away boundaries lose nothing).
        if now - last_emit >= _SYNC_PROGRESS_MIN_INTERVAL_SEC:
            last_emit = now
            _broadcast_sync_event(
                job,
                {"event": "progress", "data": json.dumps(_snapshot_sync_state(job))},
            )

    def cancel_check() -> bool:
        """Check if sync job cancellation was requested.

        Returns:
            True if the cancellation event is set, False otherwise.
        """
        return job["cancel_event"].is_set()

    terminal: dict[str, str] = {
        "event": "error",
        "data": json.dumps({"error": "sync job cancelled before completion"}),
    }
    try:
        syncer = get_syncer()
        state["stage"] = "syncing"
        results = await syncer.full_sync(
            progress_callback=progress_callback, cancel_check=cancel_check
        )
        state.update({
            "processed": results.get("processed", 0),
            "skipped": results.get("skipped", 0),
            "errors": results.get("errors", 0),
            "total": results.get("total", state.get("total", 0)),
        })
        if results.get("auto_aborted"):
            state["stage"] = "auto_aborted"
            terminal = {
                "event": "auto_aborted",
                "data": json.dumps(_snapshot_sync_state(job)),
            }
        elif results.get("cancelled"):
            state["stage"] = "aborted"
            terminal = {
                "event": "aborted",
                "data": json.dumps(_snapshot_sync_state(job)),
            }
        else:
            state["stage"] = "done"
            terminal = {
                "event": "done",
                "data": json.dumps(_snapshot_sync_state(job)),
            }
    except Exception as exc:
        logger.error("Sync stream job failed: %s", exc, exc_info=True)
        state["stage"] = "error"
        terminal = {
            "event": "error",
            "data": json.dumps({**_snapshot_sync_state(job), "error": str(exc)}),
        }
    finally:
        job["active"] = False
        job["terminal"] = terminal
        # Wake every subscriber with the terminal state, then clear the
        # single-flight registry so a follow-up /sync/stream starts a new job.
        _broadcast_sync_event(job, terminal)
        async with _sync_job_lock:
            if _sync_job is job:
                _sync_job = None


def _sync_stream_event_generator(
    job: dict[str, Any],
    queue: asyncio.Queue,
    raw_request: Request,
) -> AsyncIterator[dict[str, str]]:
    """Stream one subscriber's view of the sync job; disconnect cancels cooperatively."""

    async def generator() -> AsyncIterator[dict[str, str]]:
        """Yield sync progress events and cooperative cancellation signals.

        Yields:
            SSE event dictionaries from the subscriber queue.
        """
        terminal_sent = False
        try:
            while True:
                if await raw_request.is_disconnected():
                    logger.info(
                        "Client disconnected from /sync/stream; cooperative cancel evaluated."
                    )
                    _cancel_if_last_subscriber(job, queue)
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=0.5)
                except (asyncio.TimeoutError, TimeoutError):
                    # Job may have finished between queue drain and terminal
                    # fan-out; break cleanly instead of hanging.
                    task = job.get("task")
                    if task is not None and task.done() and queue.empty():
                        # Race guard: the terminal event may have been dropped
                        # by the cancelled Queue.get above; deliver it before
                        # breaking so the stream always terminates cleanly.
                        if not terminal_sent and job.get("terminal") is not None:
                            terminal_sent = True
                            yield job["terminal"]
                        break
                    continue
                yield event
                if event is job.get("terminal") or event.get("event") in _SYNC_TERMINAL_EVENTS:
                    terminal_sent = True
                    break
        finally:
            job["subscribers"].discard(queue)
            # Transport-level safety net: when the SSE response task is
            # cancelled (client vanished), sse_starlette cancels this
            # generator directly — the body-iteration is_disconnected() check
            # above may never observe the disconnect. If this was the last
            # subscriber and the job is still running, cooperatively cancel.
            _cancel_if_last_subscriber(job, queue)

    return generator()


def _cancel_if_last_subscriber(job: dict[str, Any], queue: asyncio.Queue) -> None:
    """Set the cooperative cancel flag only when no other subscriber remains.

    Shared by all three disconnect/cancel sites (body-iteration
    ``is_disconnected()`` poll, sse_starlette ``client_close_handler``, and the
    generator's ``finally`` safety net) so a multi-window attach always keeps
    the job alive for the remaining windows, and a finished job is never
    cancelled.
    """
    remaining = job["subscribers"] - {queue}
    if not remaining and job.get("active", False) and job.get("terminal") is None:
        job["cancel_event"].set()


def _make_sync_client_close_handler(
    job: dict[str, Any], queue: asyncio.Queue
) -> Callable[[Any], Awaitable[None]]:
    """Build the sse_starlette ``client_close_handler_callable`` for one subscriber.

    Dispatched by sse_starlette when the client's ``http.disconnect`` arrives.
    Cooperatively cancel only if the disconnecting subscriber was the last one
    attached (a multi-window attach keeps the job alive for remaining windows).
    """

    async def _on_client_close(_message: Any) -> None:
        """Handle client disconnect by evaluating cooperative job cancellation.

        Args:
            _message: Disconnect event payload from the ASGI transport.
        """
        if job.get("active", False):
            logger.info(
                "Client closed /sync/stream connection; cooperative cancel evaluated."
            )
            _cancel_if_last_subscriber(job, queue)

    return _on_client_close


@app.post("/sync/stream", dependencies=[Depends(verify_auth)])
async def sync_stream(raw_request: Request) -> EventSourceResponse:
    """Start (or attach to) the SSE full-library sync job.

    Streams throttled progress events (``progress``) plus a terminal event
    (``done`` / ``aborted`` / ``auto_aborted`` / ``error``). A second request
    while a job is running attaches to the running job's fan-out. Client
    disconnect cooperatively cancels the job.
    """
    global _sync_job
    try:
        get_syncer()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with _sync_job_lock:
        job = _sync_job
        attached = job is not None and job.get("active", False)
        if not attached:
            job = {
                "task": None,
                "cancel_event": asyncio.Event(),
                "subscribers": set(),
                "state": {
                    "stage": "starting",
                    "processed": 0,
                    "skipped": 0,
                    "errors": 0,
                    "total": 0,
                    "current_title": "",
                },
                "started": time.perf_counter(),
                "terminal": None,
                "active": True,
            }
            _sync_job = job
            job["task"] = asyncio.create_task(_run_sync_job(job))

    queue: asyncio.Queue = asyncio.Queue()
    job["subscribers"].add(queue)
    queue.put_nowait({
        "event": "sync_start",
        "data": json.dumps({**_snapshot_sync_state(job), "attached": attached}),
    })
    return EventSourceResponse(
        _sync_stream_event_generator(job, queue, raw_request),
        client_close_handler_callable=_make_sync_client_close_handler(job, queue),
    )


@app.post("/sync/abort", dependencies=[Depends(verify_auth)])
async def sync_abort() -> dict:
    """Cooperatively cancel the active sync job (fail-open, deterministic abort)."""
    try:
        async with _sync_job_lock:
            job = _sync_job
        if job is None or not job.get("active", False):
            return {"status": "no_active_job"}
        job["cancel_event"].set()
        return {"status": "abort_requested"}
    except Exception as exc:
        logger.warning("Failed to request sync abort: %s", exc)
        return {"status": "no_active_job"}


@app.post("/items/delete", dependencies=[Depends(verify_auth)])
async def delete_items(request: ItemDeleteRequest) -> dict:
    """Purge items across all datastores (cache bundle, metadata DB, vector/FTS store, root index)."""
    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    valid_keys = [k for k in request.item_keys if _validate_item_key(k)]

    def _purge_worker() -> list[str]:
        """Purge requested items from disk and sync indexes synchronously.

        Returns:
            List of successfully purged item keys.
        """
        purged_keys: list[str] = []
        for key in valid_keys:
            try:
                syncer.purge_item(key, regenerate_index=False)
                purged_keys.append(key)
            except Exception as exc:
                logger.error("Failed to purge item %s: %s", key, exc)
        if purged_keys:
            syncer.processor.regenerate_root_index()
        return purged_keys

    purged = await asyncio.to_thread(_purge_worker)

    return {"status": "success", "purged": purged}


def _get_unresolved_session_interrupts(session: Any) -> set[str]:
    """Scan session events and return set of interrupt IDs waiting for FunctionResponse."""
    if not session or not getattr(session, "events", None):
        return set()
    unresolved: set[str] = set()
    for ev in session.events:
        if getattr(ev, "long_running_tool_ids", None):
            unresolved.update(ev.long_running_tool_ids)
        if ev.content and ev.content.parts:
            for p in ev.content.parts:
                fr = getattr(p, "function_response", None)
                if fr and getattr(fr, "id", None) in unresolved:
                    unresolved.discard(fr.id)
    return unresolved


async def _stream_runner_events(
    event_generator: Any,
    zotero_dir: Path | None = None,
    session_id: str | None = None,
    request_start_time: float | None = None,
    prompt_text: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    """Format ADK Runner event stream into JSON-encoded SSE events."""
    total_prompt_tokens = 0
    total_candidates_tokens = 0
    total_thoughts_tokens = 0
    total_cached_tokens = 0
    total_tokens = 0
    current_turn_cost = 0.0
    streamed_agent_models: dict[str, str] = {}
    current_invocation_id: str | None = None

    try:
        async for event in event_generator:
            if getattr(event, "invocation_id", None):
                current_invocation_id = event.invocation_id

            request_input_fc = None
            if getattr(event, "long_running_tool_ids", None) and event.content and event.content.parts:
                for part in event.content.parts:
                    fc = getattr(part, "function_call", None)
                    if fc and getattr(fc, "name", None) == "adk_request_input":
                        request_input_fc = fc
                        break

            if request_input_fc is not None:
                fc_args = getattr(request_input_fc, "args", {}) or {}
                if not isinstance(fc_args, dict):
                    fc_args = {}
                req_payload = {
                    "event_type": "request_input",
                    "author": event.author or "Search Agent",
                    "interrupt_id": request_input_fc.id,
                    "invocation_id": getattr(event, "invocation_id", None),
                    "message": fc_args.get(
                        "message",
                        "Deep research limit reached. Extend retrieval or synthesize current findings?",
                    ),
                    "payload": fc_args.get("payload", {}),
                }
                yield {"data": json.dumps(req_payload)}

            payload = {
                "id": event.id,
                "author": event.author or "system",
                "is_final": event.is_final_response(),
                "partial": bool(getattr(event, "partial", False)),
                "parts": [],
            }
            if getattr(event, "invocation_id", None):
                payload["invocation_id"] = event.invocation_id
            if getattr(event, "model_version", None):
                payload["model_version"] = event.model_version
            if getattr(event, "finish_reason", None):
                payload["finish_reason"] = _parse_finish_reason(event.finish_reason)

            if getattr(event, "usage_metadata", None):
                parsed_u = _parse_usage_metadata(event.usage_metadata)
                if parsed_u:
                    p_cnt = parsed_u.get("prompt_tokens", 0)
                    c_cnt = parsed_u.get("candidates_tokens", 0)
                    th_cnt = parsed_u.get("thoughts_tokens", 0)
                    ca_cnt = parsed_u.get("cached_tokens", 0)
                    tot_cnt = parsed_u.get("total_tokens", 0)

                    total_prompt_tokens += p_cnt
                    total_candidates_tokens += c_cnt
                    total_thoughts_tokens += th_cnt
                    total_cached_tokens += ca_cnt
                    total_tokens += tot_cnt

                    evt_model = getattr(event, "model_version", None)
                    if not evt_model:
                        try:
                            from zocrates.config import DEFAULT_LLM_MODEL, load_zocrates_config

                            cfg = load_zocrates_config(zotero_dir)
                            role_overrides = cfg.get("override_agent_models") or {}
                            auth_str = (getattr(event, "author", "") or "").lower()
                            if "online_search" in auth_str:
                                evt_model = role_overrides.get("online_search")
                            elif "online_synth" in auth_str:
                                evt_model = role_overrides.get("online_synthesizer")
                            elif "library_search" in auth_str or "search" in auth_str:
                                evt_model = role_overrides.get("library_search")
                            elif "library_synth" in auth_str or "synthes" in auth_str:
                                evt_model = role_overrides.get("library_synthesizer")
                            elif "coord" in auth_str:
                                evt_model = role_overrides.get("coordinator")
                            if not evt_model:
                                evt_model = cfg.get("llm_model") or DEFAULT_LLM_MODEL
                        except Exception:
                            evt_model = "deepseek/deepseek-chat"

                    evt_auth = getattr(event, "author", None)
                    if evt_auth and evt_model:
                        try:
                            from zocrates.utils.cost_calculator import normalize_agent_role

                            norm_role = normalize_agent_role(evt_auth)
                            if norm_role:
                                streamed_agent_models[norm_role] = evt_model
                        except Exception:
                            pass

                    evt_cost = calculate_event_cost(
                        evt_model,
                        prompt_tokens=p_cnt,
                        candidates_tokens=c_cnt,
                        cached_tokens=ca_cnt,
                        zotero_data_dir=zotero_dir,
                    )
                    current_turn_cost += evt_cost

            if total_tokens > 0:
                payload["usage_metadata"] = {
                    "prompt_tokens": total_prompt_tokens,
                    "candidates_tokens": total_candidates_tokens,
                    "thoughts_tokens": total_thoughts_tokens,
                    "cached_tokens": total_cached_tokens,
                    "total_tokens": total_tokens,
                    "cost_usd": round(current_turn_cost, 6),
                    "turn_cost_usd": round(current_turn_cost, 6),
                    "agent_models": dict(streamed_agent_models),
                }

            if event.content and event.content.parts:
                current_th_chunk: list[str] = []
                for part in event.content.parts:
                    if getattr(part, "thought", False):
                        thought_val = getattr(part, "thought", None)
                        thought_str = (
                            getattr(part, "text", "")
                            if isinstance(thought_val, bool)
                            else str(thought_val or "")
                        )
                        if thought_str:
                            current_th_chunk.append(thought_str)
                    else:
                        if current_th_chunk:
                            joined_th = "".join(current_th_chunk)
                            if not getattr(event, "partial", False):
                                joined_th = joined_th.strip()
                            if joined_th:
                                payload["parts"].append({"thought": joined_th})
                            current_th_chunk = []

                        part_data = {}
                        if part.text:
                            part_data["text"] = part.text
                        if part.function_call:
                            if getattr(part.function_call, "name", None) not in ("adk_request_input", "finish_task") and not getattr(event, "partial", False):
                                part_data["function_call"] = {
                                    "name": part.function_call.name,
                                    "args": part.function_call.args,
                                    "id": getattr(part.function_call, "id", None),
                                }
                        if part.function_response:
                            if getattr(part.function_response, "name", None) != "finish_task":
                                part_data["function_response"] = {
                                    "name": part.function_response.name,
                                    "response": part.function_response.response,
                                    "id": getattr(part.function_response, "id", None),
                                }
                        if part_data:
                            payload["parts"].append(part_data)

                if current_th_chunk:
                    joined_th = "".join(current_th_chunk)
                    if not getattr(event, "partial", False):
                        joined_th = joined_th.strip()
                    if joined_th:
                        payload["parts"].append({"thought": joined_th})

            if event.is_final_response():
                try:
                    from zocrates.agents.common.state_keys import RETRIEVED_EVIDENCE
                    from zocrates.agents.library_search.citation_resolver import resolve_citations_and_references
                    from zocrates.config import (
                        get_citations_enabled,
                        get_considered_sources_disclosure,
                        get_references_section_enabled,
                    )

                    citations_enabled = get_citations_enabled(zotero_dir)
                    references_enabled = get_references_section_enabled(zotero_dir)
                    if session_id:
                        runner = getattr(app.state, "runner", None)
                        session_service = getattr(runner, "session_service", None) if runner else services.get_session_service(zotero_dir)
                        if session_service:
                            app_name = getattr(app.state, "agent_app_name", "zocrates")
                            raw_sess = session_service.get_session(
                                app_name=app_name, user_id="default_user", session_id=session_id
                            )
                            sess = await raw_sess if inspect.isawaitable(raw_sess) else raw_sess
                            if sess and getattr(sess, "state", None):
                                if citations_enabled or references_enabled:
                                    audit_history = list(sess.state.get(RETRIEVED_EVIDENCE) or [])
                                    if audit_history:
                                        last_audit = audit_history[-1]
                                        target_inv_id = getattr(event, "invocation_id", None) or current_invocation_id
                                        audit_inv_id = last_audit.get("invocation_id")
                                        # Scope resolution strictly to the current turn's search.
                                        # Conversational turns without a search pipeline run MUST NOT append stale sources from previous turns.
                                        if audit_inv_id != target_inv_id:
                                            logger.debug(
                                                "Skipping citation resolution: search audit invocation_id (%s) does not match current turn (%s)",
                                                audit_inv_id,
                                                target_inv_id,
                                            )
                                        else:
                                            ranked_chunks = last_audit.get("evidence_chunks") or []
                                            citations = last_audit.get("citations") or []
                                            references = last_audit.get("references") or []
                                            enriched_text = last_audit.get("enriched_text")
                                            text_indices = [
                                                i for i, p in enumerate(payload["parts"]) if "text" in p
                                            ]
                                            raw_final_text = "".join(
                                                payload["parts"][i]["text"] for i in text_indices
                                            )

                                            # Fallback in-process resolution if not already enriched by search_pipeline
                                            if not enriched_text and ranked_chunks:
                                                considered_disclosure = get_considered_sources_disclosure(zotero_dir)
                                                resolved = resolve_citations_and_references(
                                                    text=raw_final_text,
                                                    ranked_chunks=ranked_chunks,
                                                    zotero_dir=zotero_dir,
                                                    references_enabled=references_enabled,
                                                    considered_disclosure=considered_disclosure,
                                                    citations_enabled=citations_enabled,
                                                    support_probabilities=last_audit.get("support_probabilities"),
                                                )
                                                enriched_text = resolved["enriched_text"]
                                                citations = resolved["citations"]
                                                references = resolved["references"]
                                                last_audit["citations"] = citations
                                                last_audit["references"] = references
                                                last_audit["cited_chunk_ids"] = resolved["cited_chunk_ids"]
                                                last_audit["enriched_text"] = enriched_text

                                            if text_indices and enriched_text:
                                                payload["parts"][text_indices[0]]["text"] = enriched_text
                                                for idx in text_indices[1:]:
                                                    payload["parts"][idx]["text"] = ""
                                                payload["parts"] = [
                                                    p for p in payload["parts"] if p.get("text") != "" or "text" not in p
                                                ]

                                            if citations or references:
                                                payload["citations"] = citations
                                                payload["references"] = references
                                                if "metadata" not in payload:
                                                    payload["metadata"] = {}
                                                payload["metadata"]["citations"] = citations
                                                payload["metadata"]["references"] = references

                                            if hasattr(event, "custom_metadata"):
                                                if event.custom_metadata is None:
                                                    event.custom_metadata = {}
                                                event.custom_metadata["citations"] = citations
                                                event.custom_metadata["references"] = references

                                            citation_verification = last_audit.get("citation_verification_telemetry")
                                            if citation_verification:
                                                payload["citation_verification"] = citation_verification
                                                if "metadata" not in payload:
                                                    payload["metadata"] = {}
                                                payload["metadata"]["citation_verification"] = citation_verification
                                                if hasattr(event, "custom_metadata"):
                                                    if event.custom_metadata is None:
                                                        event.custom_metadata = {}
                                                    event.custom_metadata["citation_verification"] = citation_verification

                                # Check for discovered candidate papers in state (scoped strictly to current invocation)
                                target_inv_id = getattr(event, "invocation_id", None) or current_invocation_id
                                cand_inv_id = sess.state.get("candidates_invocation_id")
                                if (
                                    target_inv_id
                                    and cand_inv_id
                                    and target_inv_id == cand_inv_id
                                ):
                                    candidates = (
                                        sess.state.get("candidates")
                                        or sess.state.get("selected_candidates")
                                        or (
                                            sess.state.get("metadata", {}).get("candidates")
                                            if isinstance(sess.state.get("metadata"), dict)
                                            else None
                                        )
                                    )
                                    if candidates and isinstance(candidates, list):
                                        if "metadata" not in payload:
                                            payload["metadata"] = {}
                                        candidate_list = [
                                            c.to_dict() if hasattr(c, "to_dict") else c for c in candidates
                                        ]
                                        payload["metadata"]["candidates"] = candidate_list
                                        payload["candidates"] = candidate_list

                                        if hasattr(event, "custom_metadata"):
                                            if event.custom_metadata is None:
                                                event.custom_metadata = {}
                                            event.custom_metadata["candidates"] = candidate_list
                except Exception as cit_err:
                    logger.warning("Error resolving citations on final response: %s", cit_err)

            if payload["parts"] or total_tokens > 0 or request_input_fc is None:
                yield {"data": json.dumps(payload, default=str)}

    except Exception as err:
        logger.error("Error running agent: %s", err, exc_info=True)
        if not current_invocation_id and session_id:
            try:
                runner = getattr(app.state, "runner", None)
                session_service = getattr(runner, "session_service", None) if runner else services.get_session_service(zotero_dir)
                if session_service:
                    app_name = getattr(app.state, "agent_app_name", "zocrates")
                    raw_sess = session_service.get_session(
                        app_name=app_name, user_id="default_user", session_id=session_id
                    )
                    sess = await raw_sess if inspect.isawaitable(raw_sess) else raw_sess
                    if sess and getattr(sess, "events", None):
                        for evt in reversed(sess.events):
                            if getattr(evt, "author", None) == "user" and getattr(evt, "invocation_id", None):
                                is_match = True
                                if request_start_time is not None or prompt_text is not None:
                                    is_match = False
                                    evt_ts = getattr(evt, "timestamp", None)
                                    if request_start_time is not None and isinstance(evt_ts, (int, float)):
                                        if evt_ts >= (request_start_time - 5.0):
                                            is_match = True

                                    if not is_match and prompt_text is not None:
                                        evt_text = ""
                                        evt_content = getattr(evt, "content", None)
                                        if evt_content and getattr(evt_content, "parts", None):
                                            parts_texts = []
                                            for p in evt_content.parts:
                                                if getattr(p, "thought", False):
                                                    continue
                                                if isinstance(p, str):
                                                    parts_texts.append(p)
                                                else:
                                                    txt = getattr(p, "text", None)
                                                    if txt and not isinstance(txt, MagicMock):
                                                        parts_texts.append(str(txt))
                                            evt_text = "".join(parts_texts)
                                        elif isinstance(evt_content, str):
                                            evt_text = evt_content
                                        if evt_text and (prompt_text.strip() == evt_text.strip() or prompt_text.strip() in evt_text):
                                            is_match = True

                                if is_match:
                                    current_invocation_id = evt.invocation_id
                                    break
            except Exception as sess_err:
                logger.warning("Failed to lookup fallback invocation_id for session %s: %s", session_id, sess_err)

        from zocrates.llm import format_llm_exception
        from zocrates.config import load_zocrates_config

        cfg = load_zocrates_config()
        provider = cfg.get("llm_provider", "openrouter_llm")
        base_url = cfg.get("llm_base_url")
        formatted_msg = format_llm_exception(err, provider=provider, base_url=base_url)
        err_payload: dict[str, Any] = {
            "error": formatted_msg,
            "author": "system",
            "is_final": True,
        }
        if current_invocation_id:
            err_payload["invocation_id"] = current_invocation_id
        yield {
            "data": json.dumps(err_payload)
        }


async def _process_request_snippets(
    snippets: list[SnippetItem] | None,
    coord_model: str,
    zotero_dir: Path | None,
) -> tuple[list[types.Part], str, list[dict[str, Any]]]:
    """Process incoming text and image snippets for Google ADK execution.

    Returns:
        tuple of (multimodal_parts, formatted_text_context, notices_to_yield)
    """
    if not snippets:
        return [], "", []

    from zocrates.llm import is_model_vision_capable

    multimodal_parts: list[types.Part] = []
    text_snippet_blocks: list[str] = []
    notices: list[dict[str, Any]] = []

    model_has_vision = is_model_vision_capable(coord_model, zotero_data_dir=zotero_dir)

    for snip in snippets:
        auth_attr = f' authors="{snip.author_str.strip()}"' if snip.author_str and snip.author_str.strip() else ""
        if snip.type == "text" and snip.text:
            text_snippet_blocks.append(
                f'<text_snippet paper="{snip.paper_title or "Unknown"}"{auth_attr} page="{snip.page or "?"}" key="{snip.parent_key or snip.item_key or ""}">'
                f"\n{snip.text.strip()}\n</text_snippet>"
            )

        elif snip.type == "image":
            if not snip.image_data:
                text_snippet_blocks.append(
                    f'<image_snippet paper="{snip.paper_title or "Unknown"}"{auth_attr} page="{snip.page or "?"}" key="{snip.parent_key or snip.item_key or ""}">'
                    f"\n[Attached image area payload was empty or could not be transferred.]\n</image_snippet>"
                )
                continue

            # Parse data URI safely
            raw_bytes = None
            mime_type = getattr(snip, "mime_type", None) or "image/jpeg"
            try:
                if "," in snip.image_data:
                    header, _, b64_data = snip.image_data.partition(",")
                    if match := re.search(r"data:(image/[a-zA-Z0-9.+_-]+)", header):
                        mime_type = match.group(1)
                    raw_bytes = base64.b64decode(b64_data)
                elif snip.image_data.startswith("data:image/"):
                    # Malformed URI without comma
                    raw_bytes = None
                else:
                    raw_bytes = base64.b64decode(snip.image_data)
            except Exception as b64_err:
                logger.warning("Failed to decode base64 snippet image: %s", b64_err)

            if not raw_bytes:
                text_snippet_blocks.append(
                    f'<image_snippet paper="{snip.paper_title or "Unknown"}"{auth_attr} page="{snip.page or "?"}" key="{snip.parent_key or snip.item_key or ""}">'
                    f"\n[Attached image area could not be decoded.]\n</image_snippet>"
                )
                continue

            if model_has_vision:
                # Direct multimodal injection into Google ADK
                part = types.Part.from_bytes(data=raw_bytes, mime_type=mime_type)
                multimodal_parts.append(part)
                text_snippet_blocks.append(
                    f'<image_snippet paper="{snip.paper_title or "Unknown"}"{auth_attr} page="{snip.page or "?"}" key="{snip.parent_key or snip.item_key or ""}">'
                    f"\n[Attached visual image of selected area]\n</image_snippet>"
                )
            else:
                text_snippet_blocks.append(
                    f'<image_snippet paper="{snip.paper_title or "Unknown"}"{auth_attr} page="{snip.page or "?"}" key="{snip.parent_key or snip.item_key or ""}">'
                    f"\n[Attached image area: untranscribed image on page {snip.page or '?'}. The active coordinator model ({coord_model}) does not support vision/image inputs. If needed, inspect the surrounding document text or section via retrieval tools.]\n</image_snippet>"
                )
                notices.append({
                    "notice": f"Image area attached, but active model ({coord_model}) is text-only. The agent has been notified."
                })

    formatted_context = ""
    if text_snippet_blocks:
        formatted_context = (
            "\n\n# Context: User-Selected Focal Snippets from Attachment\n\n"
            + "\n\n".join(text_snippet_blocks)
            + "\n\n"
        )

    return multimodal_parts, formatted_context, notices


@app.post("/chat", dependencies=[Depends(verify_auth)])
async def chat_endpoint(request: ChatRequest) -> EventSourceResponse:
    """Streaming chat endpoint using Server-Sent Events (SSE).

    Raises:
        HTTPException: 400 if the backend is not yet registered.

    Returns:
        An ``EventSourceResponse`` streaming JSON-encoded ``data:`` payloads,
        each containing ``id``, ``author``, ``is_final``, and ``parts`` keys.
    """
    try:
        syncer = get_syncer()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    zotero_dir = getattr(syncer, "_zotero_data_dir", None)
    _maybe_hot_swap_agent_models(zotero_dir, "before chat")

    runner: Runner = app.state.runner

    session_getter = getattr(getattr(runner, "session_service", None), "get_session", None)
    session = None
    if session_getter is not None:
        raw_session = session_getter(
            app_name="zocrates", user_id="default_user", session_id=request.session_id
        )
        if inspect.isawaitable(raw_session):
            session = await raw_session
        else:
            session = raw_session
    if session:
        pending_interrupts = _get_unresolved_session_interrupts(session)
        if pending_interrupts:
            raise HTTPException(
                status_code=409,
                detail="Session has a pending research extension request. Please respond to the checkpoint or cancel.",
            )

    active_ids = getattr(app.state, "active_chat_session_ids", None)
    if active_ids is not None:
        active_ids.add(request.session_id)

    async def sse_event_generator() -> AsyncIterator[dict[str, Any]]:
        """Resolve context items, run the ADK agent, and yield SSE payloads.

        Expands collection keys to their member item keys, builds a
        ``state_delta`` dict for the runner, then streams all ADK events as
        JSON-encoded ``data:`` payloads. Errors are surfaced as
        ``{"error": ...}`` payloads so the client can display them without
        crashing.
        """
        try:
            syncer = get_syncer()

            # Resolve active items context and keys
            has_library_context = False
            active_collections = []
            active_context_items = []
            seen_keys = set()

            for item in request.active_items or []:
                itype = (
                    item.type
                    if hasattr(item, "type") and item.type is not None
                    else (item.get("type", "item") if isinstance(item, dict) else "item")
                )
                key = (
                    item.key
                    if hasattr(item, "key") and item.key is not None
                    else (item.get("key", "") if isinstance(item, dict) else "")
                )
                title = (
                    item.title
                    if hasattr(item, "title") and item.title is not None
                    else (item.get("title", "") if isinstance(item, dict) else "")
                )

                if itype == "library":
                    has_library_context = True
                elif itype == "collection":
                    col_name = title.removeprefix("Collection: ").strip() if title else key
                    active_collections.append(col_name)
                    try:
                        keys = syncer.db.get_item_keys_in_collection(key) or syncer.db.get_item_keys_in_collection(col_name)
                    except Exception as e:
                        logger.error(
                            "Failed to fetch collection keys for %s: %s", key, e
                        )
                        keys = []
                    for k in keys:
                        if k not in seen_keys:
                            seen_keys.add(k)
                            active_context_items.append(resolve_active_item_entry(syncer, k))
                else:
                    k = key
                    if k and k not in seen_keys:
                        seen_keys.add(k)
                        doc_title = title or None
                        active_context_items.append(resolve_active_item_entry(syncer, k, doc_title))

            if has_library_context:
                active_context_items = None
                context_str = "Active selection context: Entire Zotero Library."
                active_context_items_str = context_str
            elif active_collections:
                context_str = f"Active selection context: Zotero Collections ({', '.join(active_collections)})."
                active_context_items_str = f"{context_str}\n{format_active_items(active_context_items, include_keys=False)}"
            else:
                if not active_context_items and request.snippets:
                    for snip in request.snippets:
                        p_key = snip.parent_key or snip.item_key
                        if p_key and p_key not in seen_keys:
                            seen_keys.add(p_key)
                            doc_title = snip.paper_title or None
                            entry = resolve_active_item_entry(syncer, p_key, doc_title)
                            if entry:
                                active_context_items.append(entry)
                active_context_items_str = format_active_items(active_context_items, include_keys=False)

            coord_model_str = None
            try:
                from zocrates.config import DEFAULT_LLM_MODEL, load_zocrates_config

                cfg = load_zocrates_config(zotero_dir)
                coord_model_str = (
                    (cfg.get("override_agent_models") or {}).get("coordinator")
                    or cfg.get("llm_model")
                    or DEFAULT_LLM_MODEL
                )
            except Exception:
                coord_model_str = "deepseek/deepseek-chat"

            try:
                from zocrates.agents.app import coordinator_agent

                if hasattr(coordinator_agent, "model"):
                    c_model = coordinator_agent.model
                    if hasattr(c_model, "model") and c_model.model:
                        coord_model_str = str(c_model.model)
                    elif isinstance(c_model, str) and c_model:
                        coord_model_str = c_model
            except Exception:
                pass

            multimodal_parts, focal_snippets_str, snippet_notices = await _process_request_snippets(
                request.snippets,
                coord_model=coord_model_str,
                zotero_dir=zotero_dir,
            )

            try:
                from zocrates.config import get_user_research_profile

                user_profile = get_user_research_profile()
            except Exception as e:
                logger.warning("Failed to load user research profile: %s", e)
                user_profile = ""
            user_research_profile_str = format_user_research_profile_context(user_profile)

            state_delta = {
                "active_context_items": active_context_items,
                "active_context_items_str": active_context_items_str,
                "focal_snippets_str": focal_snippets_str,
                "snippets": [s.model_dump(by_alias=True) for s in request.snippets] if request.snippets else [],
                USER_PROMPT: request.prompt,
                USER_RESEARCH_PROFILE_STR: user_research_profile_str,
            }

            user_parts: list[types.Part] = [types.Part.from_text(text=request.prompt)]
            if multimodal_parts:
                user_parts.extend(multimodal_parts)

            run_config = RunConfig(streaming_mode=StreamingMode.SSE)
            runner_gen = runner.run_async(
                user_id="default_user",
                session_id=request.session_id,
                state_delta=state_delta,
                new_message=types.Content(
                    role="user", parts=user_parts
                ),
                run_config=run_config,
            )

            for notice in snippet_notices:
                yield {"data": json.dumps(notice)}

            request_start_time = time.time()
            async for sse_item in _stream_runner_events(
                runner_gen,
                zotero_dir=zotero_dir,
                session_id=request.session_id,
                request_start_time=request_start_time,
                prompt_text=request.prompt,
            ):
                yield sse_item
        finally:
            if active_ids is not None:
                active_ids.discard(request.session_id)

    try:
        return EventSourceResponse(sse_event_generator())
    except Exception:
        if active_ids is not None:
            active_ids.discard(request.session_id)
        raise


@app.post("/chat/resume", dependencies=[Depends(verify_auth)])
async def resume_chat_endpoint(request: ResumeChatRequest) -> EventSourceResponse:
    """Resume an interrupted chat invocation awaiting Human-In-The-Loop user input."""
    try:
        syncer = get_syncer()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    zotero_dir = getattr(syncer, "_zotero_data_dir", None)
    runner: Runner = app.state.runner

    session = await runner.session_service.get_session(
        app_name="zocrates", user_id="default_user", session_id=request.session_id
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found.")

    pending_interrupts = _get_unresolved_session_interrupts(session)
    if request.interrupt_id not in pending_interrupts:
        raise HTTPException(
            status_code=409,
            detail="Invalid or stale interrupt ID. The session is not waiting for this interrupt.",
        )

    fr_part = types.Part(
        function_response=types.FunctionResponse(
            name="adk_request_input",
            id=request.interrupt_id,
            response=request.response,
        )
    )
    new_message = types.Content(role="user", parts=[fr_part])
    run_config = RunConfig(streaming_mode=StreamingMode.SSE)

    from zocrates.agents.library_search.search_pipeline import search_library_pipeline_node
    from zocrates.agents.online_search.search_pipeline import search_online_pipeline_node

    target_node = search_library_pipeline_node
    snapshot = getattr(session, "state", {}).get(HITL_SNAPSHOT) if getattr(session, "state", None) else None
    if snapshot and isinstance(snapshot, dict):
        if snapshot.get("pipeline_type") == "online_search":
            target_node = search_online_pipeline_node
        elif str(snapshot.get("isolation_scope", "")).startswith("search_online_pipeline:"):
            target_node = search_online_pipeline_node

    if target_node == search_library_pipeline_node and getattr(session, "events", None):
        for ev in reversed(session.events):
            scope = getattr(ev, "isolation_scope", None)
            if scope:
                if str(scope).startswith("search_online_pipeline:"):
                    target_node = search_online_pipeline_node
                    break
                elif str(scope).startswith(("search_library_pipeline:", "search_pipeline:")):
                    target_node = search_library_pipeline_node
                    break

    runner_gen = runner._run_node_async(
        user_id="default_user",
        session_id=request.session_id,
        invocation_id=request.invocation_id,
        new_message=new_message,
        run_config=run_config,
        node=target_node,
    )

    active_ids = getattr(app.state, "active_chat_session_ids", None)
    if active_ids is not None:
        active_ids.add(request.session_id)

    request_start_time = time.time()

    async def sse_event_generator() -> AsyncIterator[dict[str, Any]]:
        """Stream resumed runner events over Server-Sent Events with session cleanup.

        Yields:
            SSE event payloads formatted for EventSourceResponse.
        """
        try:
            async for sse_item in _stream_runner_events(
                runner_gen,
                zotero_dir=zotero_dir,
                session_id=request.session_id,
                request_start_time=request_start_time,
            ):
                yield sse_item
        finally:
            if active_ids is not None:
                active_ids.discard(request.session_id)

    try:
        return EventSourceResponse(sse_event_generator())
    except Exception:
        if active_ids is not None:
            active_ids.discard(request.session_id)
        raise


# ---------------------------------------------------------------------------
# LLM Configuration & Catalog Endpoints
# ---------------------------------------------------------------------------


@app.get("/config", dependencies=[Depends(verify_auth)])
async def get_config() -> dict:
    """Return active LLM and embedding configuration."""
    from zocrates.config import (
        DEFAULT_AUTO_RENDER_MERMAID_DIAGRAMS,
        DEFAULT_BACKEND_PORT,
        DEFAULT_CITATIONS_ENABLED,
        DEFAULT_ENABLE_MCP_SERVER,
        DEFAULT_OVERRIDE_AGENT_MODELS,
        DEFAULT_REFERENCES_SECTION_ENABLED,
        get_auto_spawn_backend,
        get_compaction_config,
        get_datalab_api_key,
        get_datalab_merge_cross_page,
        get_openalex_api_key,
        get_secure_api_key,
        get_session_maintenance_config,
        load_zocrates_config,
        resolve_zotero_data_dir,
    )

    import copy

    zot_dir = getattr(app.state, "zotero_data_dir", None) or resolve_zotero_data_dir()
    _maybe_hot_swap_agent_models(zot_dir, "GET /config")

    cfg = load_zocrates_config()
    has_key = bool(get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")))
    has_datalab = bool(get_datalab_api_key())
    has_openalex = bool(get_openalex_api_key())
    session_storage_stats = {
        "size_bytes": 0,
        "session_count": 0,
        "freelist_pages": 0,
        "total_pages": 0,
        "page_size": 4096,
    }
    if zot_dir:
        from zocrates.utils.session_maintenance import get_session_storage_stats
        session_db_path = Path(zot_dir) / "zocrates-data" / "sessions.db"
        sess_service = services.get_session_service()
        app_name = getattr(app.state, "agent_app_name", "zocrates")
        try:
            session_storage_stats = await get_session_storage_stats(
                db_path=session_db_path,
                session_service=sess_service,
                app_name=app_name,
                user_id="default_user",
            )
        except Exception as exc:
            logger.debug("Failed to get session storage stats for llm config: %s", exc)

    return {
        "status": "success",
        "llm_provider": cfg.get("llm_provider", "openrouter_llm"),
        "llm_model": cfg.get("llm_model", DEFAULT_LLM_MODEL),
        "llm_base_url": cfg.get("llm_base_url"),
        "llm_temperature": cfg.get("llm_temperature", DEFAULT_LLM_TEMPERATURE),
        "llm_max_tokens": cfg.get("llm_max_tokens", DEFAULT_LLM_MAX_TOKENS),
        "llm_num_retries": cfg.get("llm_num_retries", DEFAULT_LLM_NUM_RETRIES),
        "llm_extra_options": cfg.get("llm_extra_options", {}),
        "override_agent_models": cfg.get(
            "override_agent_models", copy.deepcopy(DEFAULT_OVERRIDE_AGENT_MODELS)
        ),
        "embedding_provider": cfg.get("embedding_provider", "local"),
        "embedding_model": cfg.get("embedding_model", "jinaai/jina-embeddings-v5-text-nano"),
        "has_openrouter_key": has_key,
        "has_datalab_key": has_datalab,
        "has_openalex_api_key": has_openalex,
        "datalab_merge_cross_page": get_datalab_merge_cross_page(zot_dir, cfg=cfg),
        "citations_enabled": cfg.get("citations_enabled", DEFAULT_CITATIONS_ENABLED),
        "references_section_enabled": cfg.get("references_section_enabled", DEFAULT_REFERENCES_SECTION_ENABLED),
        "auto_render_mermaid_diagrams": cfg.get("auto_render_mermaid_diagrams", DEFAULT_AUTO_RENDER_MERMAID_DIAGRAMS),
        "auto_spawn_backend": get_auto_spawn_backend(zot_dir, cfg=cfg),
        "backend_port": cfg.get("backend_port", DEFAULT_BACKEND_PORT),
        "enable_mcp_server": bool(cfg.get("enable_mcp_server", DEFAULT_ENABLE_MCP_SERVER)),
        "scholar_polite_email": str(cfg.get("scholar_polite_email", "")),
        "user_research_profile": str(cfg.get("user_research_profile") or ""),
        "enable_predictive_ghost_input": bool(cfg.get("enable_predictive_ghost_input", DEFAULT_ENABLE_PREDICTIVE_GHOST_INPUT)),
        "predictive_prompt_model": str(cfg.get("predictive_prompt_model") or DEFAULT_PREDICTIVE_PROMPT_MODEL),
        "citation_verification_enabled": bool(cfg.get("citation_verification_enabled", False)),
        "citation_verification_provider": str(cfg.get("citation_verification_provider", "openrouter")),
        "citation_verification_model": str(cfg.get("citation_verification_model", DEFAULT_CITATION_VERIFICATION_MODEL)),
        "citation_verification_threshold": float(cfg.get("citation_verification_threshold", 0.60)),
        "citation_verification_local_url": str(cfg.get("citation_verification_local_url", "http://127.0.0.1:8005/v1/systemone")),
        "citation_verification_local_port": int(cfg.get("citation_verification_local_port", 8005)),
        "citation_verification_auto_start_local": bool(cfg.get("citation_verification_auto_start_local", True)),
        "citation_verification_timeout_sec": float(cfg.get("citation_verification_timeout_sec", 3.0)),
        "citation_verification_turn_budget_sec": float(cfg.get("citation_verification_turn_budget_sec", 10.0)),
        **get_compaction_config(),
        **get_session_maintenance_config(zot_dir),
        "session_storage_stats": session_storage_stats,
    }


@app.post("/config", dependencies=[Depends(verify_auth)])
async def update_config(request: ConfigRequest) -> dict:
    """Update LLM and embedding settings, save API key if provided, and hot-swap active agent models."""
    from zocrates.agents.app import set_agent_models
    from zocrates.llm import get_llm_model
    from zocrates.config import (
        get_secure_api_key,
        load_zocrates_config,
        save_openalex_api_key,
        save_secure_api_key,
        save_zocrates_config,
    )

    cfg = load_zocrates_config()
    updates = {}
    if request.llm_provider is not None:
        updates["llm_provider"] = request.llm_provider
    if request.llm_model is not None:
        updates["llm_model"] = request.llm_model
    if request.llm_base_url is not None:
        updates["llm_base_url"] = request.llm_base_url
    if request.llm_temperature is not None:
        updates["llm_temperature"] = request.llm_temperature
    if request.llm_max_tokens is not None:
        updates["llm_max_tokens"] = request.llm_max_tokens
    if request.llm_num_retries is not None:
        updates["llm_num_retries"] = request.llm_num_retries
    if request.llm_extra_options is not None:
        updates["llm_extra_options"] = request.llm_extra_options
    if request.ollama_num_ctx is not None:
        updates["ollama_num_ctx"] = request.ollama_num_ctx
    if request.override_agent_models is not None:
        updates["override_agent_models"] = request.override_agent_models
    if request.reranker_model is not None:
        updates["reranker_model"] = request.reranker_model
    if request.embedding_provider is not None:
        updates["embedding_provider"] = request.embedding_provider
        if request.embedding_provider == "local" and not request.embedding_model:
            updates["embedding_model"] = "jinaai/jina-embeddings-v5-text-nano"
        elif request.embedding_model:
            updates["embedding_model"] = request.embedding_model

    if request.openrouter_api_key is not None:
        save_secure_api_key(
            request.openrouter_api_key,
            key_name="openrouter_api_key",
            env_var="OPENROUTER_API_KEY",
        )
    if request.openalex_api_key is not None:
        save_openalex_api_key(request.openalex_api_key.strip())
    if request.datalab_api_key is not None:
        from zocrates.config import save_datalab_api_key
        save_datalab_api_key(request.datalab_api_key.strip() if request.datalab_api_key else None)
    if request.datalab_merge_cross_page is not None:
        updates["datalab_merge_cross_page"] = request.datalab_merge_cross_page
    if request.citations_enabled is not None:
        updates["citations_enabled"] = request.citations_enabled
    if request.references_section_enabled is not None:
        updates["references_section_enabled"] = request.references_section_enabled
    if request.auto_render_mermaid_diagrams is not None:
        updates["auto_render_mermaid_diagrams"] = request.auto_render_mermaid_diagrams
    if request.enable_mcp_server is not None:
        updates["enable_mcp_server"] = request.enable_mcp_server
    if request.enable_predictive_ghost_input is not None:
        updates["enable_predictive_ghost_input"] = request.enable_predictive_ghost_input
    if request.predictive_prompt_model is not None:
        updates["predictive_prompt_model"] = (
            request.predictive_prompt_model.strip() or DEFAULT_PREDICTIVE_PROMPT_MODEL
        )
    if request.citation_verification_enabled is not None:
        updates["citation_verification_enabled"] = request.citation_verification_enabled
    if request.citation_verification_provider is not None:
        updates["citation_verification_provider"] = request.citation_verification_provider
    if request.citation_verification_model is not None:
        updates["citation_verification_model"] = request.citation_verification_model
    if request.citation_verification_threshold is not None:
        updates["citation_verification_threshold"] = request.citation_verification_threshold
    if request.citation_verification_local_url is not None:
        updates["citation_verification_local_url"] = request.citation_verification_local_url
    if request.citation_verification_local_port is not None:
        updates["citation_verification_local_port"] = request.citation_verification_local_port
    if request.citation_verification_auto_start_local is not None:
        updates["citation_verification_auto_start_local"] = request.citation_verification_auto_start_local
    if request.citation_verification_timeout_sec is not None:
        updates["citation_verification_timeout_sec"] = request.citation_verification_timeout_sec
    if request.citation_verification_turn_budget_sec is not None:
        updates["citation_verification_turn_budget_sec"] = request.citation_verification_turn_budget_sec
    if request.scholar_polite_email is not None:
        updates["scholar_polite_email"] = request.scholar_polite_email.strip()
    if request.user_research_profile is not None:
        max_chars = int(cfg.get("max_research_profile_chars", 1000))
        if len(request.user_research_profile) > max_chars:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"user_research_profile exceeds the maximum of {max_chars} characters "
                    f"({len(request.user_research_profile)} given)."
                ),
            )
        updates["user_research_profile"] = request.user_research_profile
    if request.enable_adaptive_compaction is not None:
        updates["enable_adaptive_compaction"] = request.enable_adaptive_compaction
    if request.adaptive_compaction_retention_turns is not None:
        updates["adaptive_compaction_retention_turns"] = request.adaptive_compaction_retention_turns
    if request.adaptive_compaction_min_turns is not None:
        updates["adaptive_compaction_min_turns"] = request.adaptive_compaction_min_turns
    if request.adaptive_compaction_token_floor is not None:
        updates["adaptive_compaction_token_floor"] = request.adaptive_compaction_token_floor
    if request.adaptive_compaction_token_ceiling is not None:
        updates["adaptive_compaction_token_ceiling"] = request.adaptive_compaction_token_ceiling
    if request.adaptive_compaction_threshold is not None:
        updates["adaptive_compaction_threshold"] = request.adaptive_compaction_threshold
    if request.adaptive_compaction_decision_model is not None:
        model_str = request.adaptive_compaction_decision_model.strip()
        from zocrates.config import DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL, is_local_decision_model
        if is_local_decision_model(model_str):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Local Laya is not supported for adaptive compaction decisions "
                    "due to capacity limits on multi-rule meta-rubrics. "
                    f"Please specify a remote decision model (e.g. '{DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL}')."
                ),
            )
        updates["adaptive_compaction_decision_model"] = model_str
    if request.compaction_summarizer_model is not None:
        updates["compaction_summarizer_model"] = request.compaction_summarizer_model.strip()
    if request.max_saved_sessions is not None:
        updates["max_saved_sessions"] = request.max_saved_sessions
    if request.session_retention_days is not None:
        updates["session_retention_days"] = request.session_retention_days
    if request.session_db_auto_vacuum_on_startup is not None:
        updates["session_db_auto_vacuum_on_startup"] = request.session_db_auto_vacuum_on_startup
    if request.prune_empty_sessions is not None:
        updates["prune_empty_sessions"] = request.prune_empty_sessions
    if "backend_port" in request.model_fields_set:
        updates["backend_port"] = request.backend_port
    if request.auto_spawn_backend is not None:
        updates["auto_spawn_backend"] = request.auto_spawn_backend

    if updates:
        save_zocrates_config(updates)

    if (
        updates.get("citation_verification_enabled", cfg.get("citation_verification_enabled"))
        and updates.get("citation_verification_provider", cfg.get("citation_verification_provider")) == "local_laya"
        and updates.get("citation_verification_auto_start_local", cfg.get("citation_verification_auto_start_local", True))
    ):
        try:
            from zocrates.rag.decision_service import is_laya_installed, start_laya_daemon
            if is_laya_installed():
                port = int(updates.get("citation_verification_local_port", cfg.get("citation_verification_local_port", 8005)))
                await asyncio.to_thread(start_laya_daemon, port=port)
        except Exception as exc:
            logger.warning("Could not auto-start Laya daemon on config update: %s", exc)

    new_model = get_llm_model()
    set_agent_models(new_model)

    if request.embedding_provider is not None or request.embedding_model is not None:
        try:
            from zocrates.config import get_collection_name
            from zocrates.rag.embeddings import get_embedder

            new_embedder = get_embedder()
            active_col = get_collection_name(new_embedder._model_name, new_embedder.dimension)
            syncer = get_syncer()
            with syncer.store._rw_lock:
                syncer.store._embedder = new_embedder
                syncer.store.active_collection_name = active_col
                syncer.store._collection = syncer.store._client.get_or_create_collection(
                    name=active_col,
                    metadata={
                        "hnsw:space": "cosine",
                        "model_name": str(new_embedder._model_name),
                        "dimension": str(new_embedder.dimension),
                    },
                )
        except Exception as exc:
            logger.warning("Could not hot-swap embedder during update_config: %s", exc)

    cfg = load_zocrates_config()
    has_key = bool(get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")))

    return {
        "status": "success",
        "config": {
            "llm_provider": cfg.get("llm_provider"),
            "llm_model": cfg.get("llm_model"),
            "llm_base_url": cfg.get("llm_base_url"),
            "llm_temperature": cfg.get("llm_temperature"),
            "llm_max_tokens": cfg.get("llm_max_tokens"),
            "llm_num_retries": cfg.get("llm_num_retries", DEFAULT_LLM_NUM_RETRIES),
            "override_agent_models": cfg.get("override_agent_models"),
            "reranker_model": cfg.get("reranker_model"),
            "embedding_provider": cfg.get("embedding_provider"),
            "embedding_model": cfg.get("embedding_model"),
            "theme": cfg.get("theme"),
            "auto_render_mermaid_diagrams": cfg.get("auto_render_mermaid_diagrams"),
            "enable_mcp_server": cfg.get("enable_mcp_server"),
            "enable_predictive_ghost_input": cfg.get("enable_predictive_ghost_input"),
            "predictive_prompt_model": cfg.get("predictive_prompt_model"),
            "datalab_merge_cross_page": cfg.get("datalab_merge_cross_page"),
            "user_research_profile": cfg.get("user_research_profile", ""),
            "citation_verification_enabled": cfg.get("citation_verification_enabled"),
            "citation_verification_provider": cfg.get("citation_verification_provider"),
            "citation_verification_model": cfg.get("citation_verification_model"),
            "citation_verification_threshold": cfg.get("citation_verification_threshold"),
            "citation_verification_local_url": cfg.get("citation_verification_local_url"),
            "citation_verification_local_port": cfg.get("citation_verification_local_port"),
            "citation_verification_auto_start_local": cfg.get("citation_verification_auto_start_local"),
            "citation_verification_timeout_sec": cfg.get("citation_verification_timeout_sec"),
            "citation_verification_turn_budget_sec": cfg.get("citation_verification_turn_budget_sec"),
            "has_openrouter_key": has_key,
        },
    }


@app.get("/llm/openrouter_models", dependencies=[Depends(verify_auth)])
async def list_openrouter_models(
        target_type: str = "llm",
        refresh: bool = False,
        free_only: bool = False,
        modality: str | None = None,
        min_context: int = 0,
) -> dict:
    """Return filtered OpenRouter models catalog (LLMs, Cloud Embeddings, or Decision Models)."""
    from zocrates.llm import get_openrouter_models
    from zocrates.config import get_secure_api_key

    has_key = bool(get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")))
    if not has_key:
        return {"status": "no_key", "models": []}

    try:
        models = await get_openrouter_models(
            free_only=free_only,
            modality=modality,
            min_context=min_context,
            target_type=target_type,
            force_refresh=refresh,
        )
        return {"status": "success", "models": models}
    except Exception as e:
        logger.error("Failed to list OpenRouter models: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to fetch OpenRouter models: {e}") from e


@app.post("/llm/test_openrouter_key", dependencies=[Depends(verify_auth)])
async def test_openrouter_key(request: OpenRouterKeyTestRequest | None = None) -> dict[str, Any]:
    """Test validity and quota/usage of an OpenRouter API key."""
    import httpx
    from zocrates.config import get_secure_api_key

    key = (request.openrouter_api_key or "").strip() if request and request.openrouter_api_key else ""
    if not key:
        key = (get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY")) or "").strip()

    if not key:
        return {
            "status": "error",
            "message": "OpenRouter API key is missing. Please enter an API key.",
        }

    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://github.com/alexplaka/Zocrates",
        "X-OpenRouter-Title": "Zocrates",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://openrouter.ai/api/v1/auth/key", headers=headers)
            if resp.status_code == 200:
                payload = resp.json()
                data = payload.get("data", {}) if isinstance(payload, dict) else {}
                details_parts: list[str] = []
                if "limit" in data and data["limit"] is not None:
                    limit_val = data["limit"]
                    details_parts.append(f"Limit: ${limit_val}" if isinstance(limit_val, (int, float)) else f"Limit: {limit_val}")
                if "usage" in data and data["usage"] is not None:
                    usage_val = data["usage"]
                    details_parts.append(f"Usage: ${usage_val}" if isinstance(usage_val, (int, float)) else f"Usage: {usage_val}")

                details_str = f" ({', '.join(details_parts)})" if details_parts else ""
                return {
                    "status": "success",
                    "message": f"Connected to OpenRouter successfully. API key is valid.{details_str}",
                    "data": data,
                }
            elif resp.status_code == 401:
                err_detail = "Invalid API key"
                try:
                    err_json = resp.json()
                    if isinstance(err_json, dict):
                        if "error" in err_json:
                            err_obj = err_json["error"]
                            if isinstance(err_obj, dict):
                                err_detail = err_obj.get("message", str(err_obj))
                            else:
                                err_detail = str(err_obj)
                        elif "message" in err_json:
                            err_detail = err_json["message"]
                except Exception:
                    err_detail = resp.text[:200] or "Invalid credentials"
                return {
                    "status": "error",
                    "message": f"OpenRouter API key rejected (401 Unauthorized): {err_detail}",
                }
            else:
                return {
                    "status": "error",
                    "message": f"OpenRouter API check failed: HTTP {resp.status_code} - {resp.text[:200]}",
                }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Failed to connect to OpenRouter: {exc}",
        }


@app.post("/llm/test", dependencies=[Depends(verify_auth)])
async def test_llm(request: LLMTestRequest | None = None) -> dict:
    """Test connectivity to the requested or currently configured LLM."""
    from zocrates.llm import test_llm_connection

    if request:
        return await test_llm_connection(
            provider=request.llm_provider,
            model=request.llm_model,
            base_url=request.llm_base_url,
            openrouter_api_key=request.openrouter_api_key,
        )
    return await test_llm_connection()


@app.post("/llm/test_embedding", dependencies=[Depends(verify_auth)])
async def test_embedding(request: EmbeddingTestRequest | None = None) -> dict:
    """Test connectivity to the requested or currently configured embedding model."""
    from zocrates.llm import test_embedding_connection

    if request:
        return await test_embedding_connection(
            provider=request.embedding_provider,
            model=request.embedding_model,
            openrouter_api_key=request.openrouter_api_key,
        )
    return await test_embedding_connection()


@app.post("/llm/test_decision_model", dependencies=[Depends(verify_auth)])
def test_decision_model(request: DecisionModelTestRequest | None = None) -> dict:
    """Test connectivity to decision model (remote OpenRouter or local Laya)."""
    from zocrates.rag.decision_service import test_decision_model_connectivity

    if request:
        return test_decision_model_connectivity(
            provider=request.provider or "openrouter",
            model=request.model,
            local_url=request.local_url,
            openrouter_api_key=request.openrouter_api_key,
        )
    return test_decision_model_connectivity()


@app.get("/llm/decision_tool/status", dependencies=[Depends(verify_auth)])
def decision_tool_status() -> dict:
    """Get status of local Laya decision tool (uv, installation, daemon running)."""
    from zocrates.rag.decision_service import get_decision_tool_status

    return get_decision_tool_status()


@app.post("/llm/decision_tool/install", dependencies=[Depends(verify_auth)])
def decision_tool_install() -> dict:
    """Install Laya tool via uv."""
    from zocrates.rag.decision_service import install_laya_tool

    return install_laya_tool()


@app.post("/llm/decision_tool/start", dependencies=[Depends(verify_auth)])
def decision_tool_start() -> dict:
    """Start local Laya daemon."""
    from zocrates.config import load_zocrates_config
    from zocrates.rag.decision_service import start_laya_daemon

    cfg = load_zocrates_config()
    port = int(cfg.get("citation_verification_local_port", 8005))
    return start_laya_daemon(port=port)


@app.post("/llm/decision_tool/stop", dependencies=[Depends(verify_auth)])
def decision_tool_stop() -> dict:
    """Stop local Laya daemon."""
    from zocrates.config import load_zocrates_config
    from zocrates.rag.decision_service import stop_laya_daemon

    cfg = load_zocrates_config()
    port = int(cfg.get("citation_verification_local_port", 8005))
    return stop_laya_daemon(port=port)


@app.get("/rag/collections", dependencies=[Depends(verify_auth)])
async def list_collections() -> dict:
    """List all versioned vector collections, their dimensions, and chunk counts.

    Raises:
        HTTPException: 400 if not registered; 500 on unexpected error.

    Returns:
        Dict with keys ``status``, ``active_collection``, and ``collections``
        (list of collection metadata dicts).
    """
    try:
        syncer = get_syncer()
        collections = syncer.store.list_collections()
        return {
            "status": "success",
            "active_collection": syncer.store.active_collection_name,
            "collections": collections,
        }
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list collections: {e}"
        ) from e


@app.post("/rag/reset", dependencies=[Depends(verify_auth)])
async def reset_collections(request: RAGResetRequest | None = None) -> dict:
    """Reset vector store collections (``"active"``, ``"all"``, or a specific name).

    Raises:
        HTTPException: 400 if not registered; 500 on unexpected error.

    Returns:
        Dict from ``VectorStore.reset_collection`` with keys ``status``,
        ``target``, ``deleted_collections``, ``fts_reset``, and
        ``active_collection``.
    """
    try:
        syncer = get_syncer()
        target = request.target if request else "active"
        result = syncer.store.reset_collection(target=target)
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to reset collection: {e}"
        ) from e


@app.post("/rag/model", dependencies=[Depends(verify_auth)])
async def update_embedding_model(request: EmbeddingModelChangeRequest) -> dict:
    """Dynamically switch the embedding mode (local vs OpenRouter-hosted cloud).

    Persists the new mode to the Zocrates config file and hot-swaps the
    embedder on the running ``LibrarySyncer`` without restarting the server.

    Raises:
        HTTPException: 400 on invalid dimension, 500 on unexpected error.

    Returns:
        Dict with keys ``status``, ``active_model``, ``dimension``,
        ``active_collection``, and ``force_cpu``.
    """
    if request.dimension is not None:
        if request.dimension > 4096 or request.dimension < 64:
            raise HTTPException(
                status_code=400,
                detail="Embedding dimension must be between 64 and 4096.",
            )
        target_mode = request.mode or os.getenv("EMBEDDING_MODE", "local")
        if target_mode == "local" and request.dimension != 768:
            raise HTTPException(
                status_code=400,
                detail="Local Jina embeddings model only supports dimension 768.",
            )

    try:
        from zocrates.config import (
            get_collection_name,
            load_zocrates_config,
            save_zocrates_config,
        )

        cfg = load_zocrates_config()
        mode = request.mode or os.getenv("EMBEDDING_MODE", cfg.get("embedding_provider", "local"))

        config_updates = {}
        if request.mode:
            config_updates["embedding_provider"] = request.mode
            os.environ["EMBEDDING_MODE"] = request.mode
        if request.model_name:
            config_updates["embedding_model"] = request.model_name

        if mode == "local" and request.dimension is not None:
            config_updates["embedding_dimension"] = 768
        elif mode != "local" and request.dimension:
            config_updates["embedding_dimension"] = request.dimension
        elif request.model_name and request.dimension is None:
            # Switch-time dimension resolution (Phase 7 Stage B): resolve the
            # new slug's dimension from the known-dimensions registry so a
            # model switch is correct from the first boot (no stale 768 from
            # the previous model, no stray wrongly-dimensioned collection).
            # Slugs absent from the registry fall back to first-embed
            # auto-detection, which persists the real dimension.
            from zocrates.rag.embeddings import KNOWN_EMBEDDING_DIMENSIONS

            norm_name = request.model_name.removeprefix("openrouter/").removeprefix("openai/")
            for known_key, known_dim in KNOWN_EMBEDDING_DIMENSIONS.items():
                if norm_name == known_key or norm_name.endswith(known_key):
                    config_updates["embedding_dimension"] = known_dim
                    break
        if request.cloud_api_key:
            config_updates["cloud_api_key"] = request.cloud_api_key
        if request.openrouter_api_key:
            config_updates["openrouter_api_key"] = request.openrouter_api_key

        if config_updates:
            save_zocrates_config(config_updates)

        cfg = load_zocrates_config()
        active_mode = os.getenv("EMBEDDING_MODE", cfg.get("embedding_provider", "local"))

        from zocrates.rag.embeddings import LocalEmbedder, OpenRouterEmbedder

        if active_mode == "cloud":
            kwargs_embed: dict[str, Any] = {
                "model_name": request.model_name or cfg.get("embedding_model")
            }
            if request.dimension is not None:
                kwargs_embed["dimension"] = request.dimension
            embedder = OpenRouterEmbedder(**kwargs_embed)
        else:
            embedder = LocalEmbedder()

        active_col = get_collection_name(embedder._model_name, embedder.dimension)

        try:
            syncer = get_syncer()
            with syncer.store._rw_lock:
                syncer.store._embedder = embedder
                syncer.store.active_collection_name = active_col
                syncer.store._collection = (
                    syncer.store._client.get_or_create_collection(
                        name=active_col,
                        metadata={
                            "hnsw:space": "cosine",
                            "model_name": str(embedder._model_name),
                            "dimension": str(embedder.dimension),
                        },
                    )
                )
        except RuntimeError as r_err:
            if "not registered" in str(r_err).lower():
                logger.info("Syncer not registered yet; embedder model config updated.")
            else:
                raise

        return {
            "status": "success",
            "requested_model": request.model_name or embedder._model_name,
            "active_model": embedder._model_name,
            "dimension": embedder.dimension,
            "is_fallback": False,
            "validation_error": None,
            "active_collection": active_col,
            "force_cpu": True,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update embedding model: {e}"
        ) from e


@app.post("/rag/converter", dependencies=[Depends(verify_auth)])
async def update_active_converter(request: ConverterChangeRequest) -> dict:
    """Dynamically switch the active document converter strategy and/or mode.

    Persists the new converter ID, mode, and fallback to the Zocrates config file and
    updates the running ``LibrarySyncer``'s processor.
    """
    try:
        from zocrates.config import (
            get_datalab_merge_cross_page,
            get_datalab_non_pdf_fallback,
            get_docling_mode,
            get_document_converter_id,
            get_document_converter_mode,
            get_enable_vlm_captioning,
            get_vlm_captioning_backup_model,
            get_vlm_captioning_local_model,
            get_vlm_captioning_model,
            get_vlm_captioning_provider,
            get_vlm_max_images_per_doc,
            save_zocrates_config,
        )

        target_id = request.document_converter_id or request.converter_id
        target_mode = request.document_converter_mode
        target_docling_mode = request.docling_mode
        target_datalab_fallback = request.datalab_non_pdf_fallback
        target_datalab_merge_cross_page = request.datalab_merge_cross_page
        target_datalab_key = request.datalab_api_key
        target_conversion_timeout = request.conversion_timeout_seconds

        target_vlm_enable = request.enable_vlm_captioning
        target_vlm_provider = request.vlm_captioning_provider
        target_vlm_model = request.vlm_captioning_model
        target_vlm_backup_model = request.vlm_captioning_backup_model
        target_vlm_local_model = request.vlm_captioning_local_model
        target_vlm_max_images = request.vlm_max_images_per_doc

        config_updates: dict[str, Any] = {}
        if target_id:
            config_updates["document_converter_id"] = target_id
        if target_mode:
            config_updates["document_converter_mode"] = target_mode
        if target_docling_mode:
            config_updates["docling_mode"] = target_docling_mode
        if target_datalab_fallback:
            config_updates["datalab_non_pdf_fallback"] = target_datalab_fallback
        if target_datalab_merge_cross_page is not None:
            config_updates["datalab_merge_cross_page"] = target_datalab_merge_cross_page
        if target_datalab_key is not None:
            config_updates["datalab_api_key"] = target_datalab_key
        if target_conversion_timeout is not None:
            config_updates["conversion_timeout_seconds"] = max(30, int(target_conversion_timeout))

        if target_vlm_enable is not None:
            config_updates["enable_vlm_captioning"] = target_vlm_enable
        if target_vlm_provider:
            config_updates["vlm_captioning_provider"] = target_vlm_provider
        if target_vlm_model:
            config_updates["vlm_captioning_model"] = target_vlm_model
        if target_vlm_backup_model:
            config_updates["vlm_captioning_backup_model"] = target_vlm_backup_model
        if target_vlm_local_model is not None:
            config_updates["vlm_captioning_local_model"] = target_vlm_local_model
        if target_vlm_max_images is not None:
            config_updates["vlm_max_images_per_doc"] = target_vlm_max_images

        if config_updates:
            save_zocrates_config(config_updates)

        if target_id:
            try:
                syncer = get_syncer()
                syncer.processor._document_converter_id = target_id
            except RuntimeError:
                pass

        if target_datalab_fallback:
            try:
                syncer = get_syncer()
                syncer.processor.datalab_non_pdf_fallback = target_datalab_fallback
            except RuntimeError:
                pass

        try:
            syncer = get_syncer()
            data_dir = syncer._zotero_data_dir
            current_id = (
                    syncer.processor._document_converter_id
                    or target_id
                    or get_document_converter_id(data_dir)
                    or "markitdown"
            )
            current_mode = target_mode or get_document_converter_mode(data_dir)
            current_docling = target_docling_mode or get_docling_mode(data_dir)
            current_datalab_fallback = (
                    getattr(syncer.processor, "datalab_non_pdf_fallback", None)
                    or target_datalab_fallback
                    or get_datalab_non_pdf_fallback(data_dir)
            )
        except RuntimeError:
            data_dir = None
            current_id = target_id or get_document_converter_id() or "markitdown"
            current_mode = target_mode or get_document_converter_mode()
            current_docling = target_docling_mode or get_docling_mode()
            current_datalab_fallback = target_datalab_fallback or get_datalab_non_pdf_fallback()

        return {
            "status": "success",
            "document_converter_id": current_id,
            "document_converter_mode": current_mode,
            "conversion_timeout_seconds": get_conversion_timeout_seconds(data_dir),
            "docling_mode": current_docling,
            "datalab_non_pdf_fallback": current_datalab_fallback,
            "datalab_merge_cross_page": get_datalab_merge_cross_page(data_dir),
            "enable_vlm_captioning": get_enable_vlm_captioning(data_dir),
            "vlm_captioning_provider": get_vlm_captioning_provider(data_dir),
            "vlm_captioning_model": get_vlm_captioning_model(data_dir),
            "vlm_captioning_backup_model": get_vlm_captioning_backup_model(data_dir),
            "vlm_captioning_local_model": get_vlm_captioning_local_model(data_dir),
            "vlm_max_images_per_doc": get_vlm_max_images_per_doc(data_dir),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update active converter: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Paper Relations Knowledge Graph Endpoints
# ---------------------------------------------------------------------------

TEMPLATES_DIR = Path(__file__).parent / "templates"
GRAPH_VIEW_TEMPLATE_PATH = TEMPLATES_DIR / "graph_view.html"


def _resolve_conversion_meta_db_path() -> Path | None:
    """Locate conversion_meta.db from active syncer, DocumentProcessor, or config."""
    # 1. Active syncer instance
    try:
        syncer = get_syncer()
        if syncer and getattr(syncer, "processor", None):
            cache_dir = getattr(syncer.processor, "_cache_dir", None)
            if cache_dir:
                p = Path(cache_dir) / "conversion_meta.db"
                if p.exists():
                    return p
    except Exception:
        pass

    # 2. DocumentProcessor default cache dir
    try:
        from zocrates.rag.document_processor import DocumentProcessor

        cache_dir = DocumentProcessor._get_default_cache_dir()
        if cache_dir:
            p = Path(cache_dir) / "conversion_meta.db"
            if p.exists():
                return p
    except Exception:
        pass

    # 3. Config auto-discovery
    try:
        from zocrates.config import get_corpus_dir, resolve_zotero_data_dir

        zotero_dir = getattr(app.state, "zotero_data_dir", None) or resolve_zotero_data_dir()
        if zotero_dir:
            p = get_corpus_dir(zotero_dir) / "conversion_meta.db"
            if p.exists():
                return p
    except Exception:
        pass

    return None


@app.get("/graph/data", dependencies=[Depends(verify_auth)])
def get_graph_data(
    center_key: str | None = None,
    collection_key: str | None = None,
    center_keys: str | None = None,
    collection_keys: str | None = None,
    depth: int = Query(default=2, ge=1, le=5),
    min_score: float = Query(default=0.50, ge=0.0, le=1.0),
    include_curated: bool = True,
    include_tags: bool = True,
) -> dict:
    """Retrieve nodes and links for paper relations network graph visualizer."""
    db_path = _resolve_conversion_meta_db_path()
    if not db_path or not db_path.exists():
        return {"nodes": [], "links": []}

    # Optional ZoteroDB for collection filtering and year resolution
    syncer = None
    zdb = None
    try:
        syncer = get_syncer()
        if syncer and getattr(syncer, "db", None):
            zdb = syncer.db
    except Exception:
        pass

    if zdb is None:
        try:
            from zocrates.config import resolve_zotero_data_dir
            from zocrates.rag.zotero_db import ZoteroDB

            z_dir = getattr(app.state, "zotero_data_dir", None) or resolve_zotero_data_dir()
            if z_dir:
                sqlite_path = z_dir / "zotero.sqlite"
                storage_dir = z_dir / "storage"
                if sqlite_path.exists():
                    zdb = ZoteroDB(sqlite_path=sqlite_path, storage_dir=storage_dir)
        except Exception:
            pass

    # Combine collection_key and collection_keys into target collection keys
    target_collections: set[str] = set()
    for col_arg in (collection_key, collection_keys):
        if col_arg:
            for part in col_arg.split(","):
                part_clean = part.strip()
                if part_clean:
                    target_collections.add(part_clean)

    collection_keys_set: set[str] | None = None
    if target_collections:
        collection_keys_set = set()
        if zdb:
            for col in target_collections:
                try:
                    collection_keys_set.update(zdb.get_item_keys_in_collection(col))
                except Exception as exc:
                    logger.warning("Failed to resolve collection keys for %s: %s", col, exc)
        else:
            logger.warning("Collections requested (%s) but ZoteroDB is not available", target_collections)

    import sqlite3

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except Exception as exc:
        logger.error("Failed to connect to %s: %s", db_path, exc)
        return {"nodes": [], "links": []}

    try:
        # 1. Read nodes from conversion_meta
        try:
            cursor = conn.execute(
                """
                SELECT item_key, title, content_type, parent_item_key, parent_item_type, keywords, abstract
                FROM conversion_meta
                """
            )
            meta_rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # Fallback if abstract column doesn't exist
            try:
                cursor = conn.execute(
                    """
                    SELECT item_key, title, content_type, parent_item_key, parent_item_type, keywords
                    FROM conversion_meta
                    """
                )
                meta_rows = cursor.fetchall()
            except sqlite3.OperationalError:
                return {"nodes": [], "links": []}

        raw_nodes_map: dict[str, dict[str, Any]] = {}
        for row in meta_rows:
            i_key = row["item_key"]
            p_key = row["parent_item_key"]

            # Filter by collection if collections provided
            if collection_keys_set is not None:
                if i_key not in collection_keys_set and (not p_key or p_key not in collection_keys_set):
                    continue

            # Parse keywords
            keywords: list[str] = []
            raw_kw = row["keywords"]
            if raw_kw:
                try:
                    parsed_kw = json.loads(raw_kw)
                    if isinstance(parsed_kw, list):
                        keywords = [str(k).strip() for k in parsed_kw if str(k).strip()]
                    elif isinstance(parsed_kw, str):
                        keywords = [parsed_kw.strip()] if parsed_kw.strip() else []
                except Exception:
                    keywords = [k.strip() for k in str(raw_kw).split(",") if k.strip()]

            # Extract bibliographic metadata from ZoteroDB and conversion_meta
            year: int | str | None = None
            authors: list[str] = []
            publication: str | None = None
            fallback_abstract: str | None = (
                row["abstract"] if "abstract" in row.keys() and row["abstract"] else None
            )
            raw_abstract: str | None = None

            if zdb:
                try:
                    item_obj = None
                    if p_key:
                        item_obj = zdb.get_item_by_key(p_key)
                    if not item_obj:
                        item_obj = zdb.get_item_by_key(i_key)
                    if item_obj:
                        if getattr(item_obj, "year", None):
                            try:
                                year = int(item_obj.year)
                            except (ValueError, TypeError):
                                year = item_obj.year
                        elif getattr(item_obj, "date", None):
                            m = re.search(r"\b(19\d\d|20\d\d)\b", str(item_obj.date))
                            if m:
                                try:
                                    year = int(m.group(1))
                                except (ValueError, TypeError):
                                    year = m.group(1)
                        if getattr(item_obj, "creators", None):
                            authors = [str(c).strip() for c in item_obj.creators if str(c).strip()]
                        if getattr(item_obj, "publication", None):
                            publication = item_obj.publication
                        if getattr(item_obj, "abstract", None):
                            raw_abstract = item_obj.abstract
                except Exception:
                    pass

            if not raw_abstract:
                raw_abstract = fallback_abstract

            abstract_full: str | None = None
            if raw_abstract and isinstance(raw_abstract, str) and raw_abstract.strip():
                abstract_full = raw_abstract.strip()

            node_type = row["parent_item_type"] or row["content_type"] or "document"
            raw_nodes_map[i_key] = {
                "id": i_key,
                "title": row["title"] or i_key,
                "year": year,
                "type": node_type,
                "keywords": keywords,
                "authors": authors,
                "publication": publication,
                "abstract": abstract_full,
                "parent_item_key": p_key,
            }

        # 2. Read edges from item_relations
        try:
            cursor = conn.execute(
                """
                SELECT source_key, target_key, score, relation_type, match_evidence
                FROM item_relations
                """
            )
            edge_rows = cursor.fetchall()
        except sqlite3.OperationalError:
            edge_rows = []

        # Filter edges by score, relation_types, and node presence
        pair_to_rows: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in edge_rows:
            score = float(row["score"])
            if score < min_score:
                continue

            rel_type = row["relation_type"]
            if not include_curated and rel_type in ("human_curated", "human_curated_and_hybrid_search"):
                continue
            if not include_tags and rel_type == "shared_tags":
                continue

            s = row["source_key"]
            t = row["target_key"]
            if s == t:
                continue
            if s not in raw_nodes_map or t not in raw_nodes_map:
                continue

            pair = (min(s, t), max(s, t))
            pair_to_rows.setdefault(pair, []).append(row)

        PRECEDENCE = {
            "human_curated_and_hybrid_search": 4,
            "human_curated": 3,
            "hybrid_search": 2,
            "shared_tags": 1,
        }

        deduplicated_links: list[dict[str, Any]] = []
        for pair, p_rows in pair_to_rows.items():
            best_row = max(
                p_rows,
                key=lambda r: (PRECEDENCE.get(r["relation_type"], 0), float(r["score"])),
            )
            best_rel_type = best_row["relation_type"]
            best_score = float(best_row["score"])

            raw_rrf = None
            evidence_summary = None

            for r in p_rows:
                ev_str = r["match_evidence"]
                if ev_str:
                    try:
                        ev_data = json.loads(ev_str)
                        if isinstance(ev_data, dict):
                            if "raw_rrf_score" in ev_data:
                                raw_rrf = float(ev_data["raw_rrf_score"])
                            elif "raw_rrf" in ev_data:
                                raw_rrf = float(ev_data["raw_rrf"])

                            if not evidence_summary:
                                if ev_data.get("source") == "zotero_itemRelations":
                                    evidence_summary = "Human-curated connection in Zotero"
                                elif ev_data.get("source") == "zotero_itemTags":
                                    tags = ev_data.get("matched_tags", [])
                                    evidence_summary = f"Shared tags: {', '.join(tags)}" if tags else "Shared tags in Zotero"
                                elif "matched_chunks" in ev_data:
                                    chunks = ev_data.get("matched_chunks", 0)
                                    methods = ev_data.get("retrieval_methods", [])
                                    methods_str = " + ".join(methods) if methods else "dense + BM25"
                                    evidence_summary = f"Hybrid search match ({methods_str}, {chunks} chunks)"
                                elif "evidence" in ev_data:
                                    evidence_summary = str(ev_data["evidence"])
                        elif isinstance(ev_data, str) and not evidence_summary:
                            evidence_summary = ev_data
                    except Exception:
                        if not evidence_summary:
                            evidence_summary = ev_str

            if not evidence_summary:
                if best_rel_type == "human_curated":
                    evidence_summary = "Human-curated connection in Zotero"
                elif best_rel_type == "shared_tags":
                    evidence_summary = "Shared tags in Zotero"
                elif best_rel_type == "human_curated_and_hybrid_search":
                    evidence_summary = "Dual-corroborated (curated and hybrid search)"
                else:
                    evidence_summary = f"{best_rel_type} match"

            deduplicated_links.append(
                {
                    "source": pair[0],
                    "target": pair[1],
                    "score": round(best_score, 4),
                    "raw_rrf": raw_rrf,
                    "relation_type": best_rel_type,
                    "evidence": evidence_summary,
                }
            )

        # 3. BFS Ego-network traversal if center_key or center_keys is provided
        target_centers: set[str] = set()
        for c_arg in (center_key, center_keys):
            if c_arg:
                for part in c_arg.split(","):
                    part_clean = part.strip()
                    if part_clean:
                        target_centers.add(part_clean)

        if target_centers:
            effective_centers: set[str] = set()
            for tk in target_centers:
                if tk in raw_nodes_map:
                    effective_centers.add(tk)
                else:
                    for k, n in raw_nodes_map.items():
                        if n.get("parent_item_key") == tk:
                            effective_centers.add(k)

            if not effective_centers:
                return {"nodes": [], "links": []}

            adj: dict[str, set[str]] = {k: set() for k in raw_nodes_map}
            for link in deduplicated_links:
                adj[link["source"]].add(link["target"])
                adj[link["target"]].add(link["source"])

            visited = set(effective_centers)
            queue = [(k, 0) for k in effective_centers]
            while queue:
                curr, d = queue.pop(0)
                if d < depth:
                    for nbr in adj.get(curr, set()):
                        if nbr not in visited:
                            visited.add(nbr)
                            queue.append((nbr, d + 1))

            active_nodes_map = {k: raw_nodes_map[k] for k in visited if k in raw_nodes_map}
            filtered_links = [
                link for link in deduplicated_links
                if link["source"] in visited and link["target"] in visited
            ]
        else:
            effective_centers = set()
            active_nodes_map = raw_nodes_map
            filtered_links = deduplicated_links

        # 4. Compute degree for each active node
        incident_counts = {node_id: 0 for node_id in active_nodes_map}
        for link in filtered_links:
            if link["source"] in incident_counts:
                incident_counts[link["source"]] += 1
            if link["target"] in incident_counts:
                incident_counts[link["target"]] += 1

        nodes_list = []
        for node_id, node_data in active_nodes_map.items():
            is_center = bool(node_id in effective_centers)
            node_obj = {
                "id": node_id,
                "title": node_data["title"],
                "year": node_data["year"],
                "type": node_data["type"],
                "keywords": node_data["keywords"],
                "authors": node_data.get("authors", []),
                "publication": node_data.get("publication"),
                "abstract": node_data.get("abstract"),
                "degree": incident_counts.get(node_id, 0),
                "is_center": is_center,
            }
            if node_data.get("parent_item_key"):
                node_obj["parent_item_key"] = node_data["parent_item_key"]
            nodes_list.append(node_obj)

        return {
            "nodes": nodes_list,
            "links": filtered_links,
        }
    finally:
        conn.close()


@app.get("/graph/view", response_class=HTMLResponse, dependencies=[Depends(verify_auth)])
async def get_graph_view() -> HTMLResponse:
    """Serve the self-contained HTML5 Canvas paper network graph visualizer."""
    if not GRAPH_VIEW_TEMPLATE_PATH.exists():
        raise HTTPException(status_code=404, detail="Graph view template not found.")
    content = GRAPH_VIEW_TEMPLATE_PATH.read_text(encoding="utf-8")
    return HTMLResponse(content=content)


@app.post("/graph/recompute", dependencies=[Depends(verify_auth)])
async def recompute_graph(req: GraphRecomputeRequest | None = None) -> dict:
    """Recompute and persist paper relation edges for specified or all items.

    Args:
        req: Optional request specifying target item keys. If not provided,
             queries all item keys from conversion_meta.db.

    Returns:
        Dict with status and recomputed_count.
    """
    try:
        syncer = get_syncer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=400,
            detail="Zocrates backend is not registered. Call /register first.",
        ) from exc

    keys: list[str] = []
    if req and req.item_keys is not None:
        keys = [k.strip() for k in req.item_keys if k and k.strip()]
    else:
        if hasattr(syncer, "processor") and hasattr(syncer.processor, "get_all_meta_keys"):
            keys = sorted(syncer.processor.get_all_meta_keys())
        else:
            db_path = _resolve_conversion_meta_db_path()
            if db_path and db_path.exists():
                import sqlite3

                with sqlite3.connect(str(db_path)) as conn:
                    rows = conn.execute("SELECT DISTINCT item_key FROM conversion_meta").fetchall()
                    keys = [r[0] for r in rows if r[0]]

    success_count = 0
    failed_keys: list[str] = []
    for k in keys:
        try:
            await asyncio.to_thread(evaluate_and_persist_edges, k, syncer, True)
            success_count += 1
        except Exception as exc:
            logger.warning("Failed to recompute edges for %s: %s", k, exc)
            failed_keys.append(k)

    return {
        "status": "success" if not failed_keys else "partial_success",
        "recomputed_count": success_count,
        "failed_keys": failed_keys,
    }


# ---------------------------------------------------------------------------
# Session Management REST Endpoints
# ---------------------------------------------------------------------------


@app.get("/sessions", dependencies=[Depends(verify_auth)])
async def list_user_sessions() -> dict:
    """List all chat session manifests for the default user.

    Returns:
        Dict with keys ``status`` and ``sessions`` (list of session summary
        dicts with ``id``, ``title``, ``created_at``, and ``updated_at``).

    Raises:
        HTTPException: 500 on unexpected session service error.
    """
    zotero_dir = getattr(app.state, "zotero_data_dir", None)
    session_service = services.get_session_service(zotero_dir)
    app_name = getattr(app.state, "agent_app_name", "zocrates")
    try:
        session_list = await session_service.list_sessions(
            app_name=app_name, user_id="default_user"
        )

        sessions_list_obj = getattr(session_list, "sessions", []) or []
        missing_ids = [
            sess.id
            for sess in sessions_list_obj
            if not sess.state or not sess.state.get("session_title") or sess.state.get("session_title") == "New Chat"
        ]

        # Fallback: Query first user message title for sessions without an explicit title
        first_msgs = {}
        if missing_ids:
            try:
                from zocrates.config import get_zocrates_data_dir
                db_path = get_zocrates_data_dir(zotero_dir) / "sessions.db"
                if db_path.exists():
                    import sqlite3
                    chunk_size = 500
                    with contextlib.closing(
                        sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
                    ) as conn:
                        cursor = conn.cursor()
                        for i in range(0, len(missing_ids), chunk_size):
                            chunk = missing_ids[i : i + chunk_size]
                            placeholders = ",".join("?" for _ in chunk)
                            cursor.execute(
                                f"""
                                SELECT session_id, event_data
                                FROM events
                                WHERE session_id IN ({placeholders}) AND app_name = ?
                                ORDER BY timestamp ASC
                                """,
                                (*chunk, app_name),
                            )
                            for sid, edata in cursor.fetchall():
                                if sid in first_msgs:
                                    continue
                                try:
                                    d = json.loads(edata)
                                    if d.get("author") != "user" and d.get("role") != "user":
                                        continue
                                    parts = d.get("content", {}).get("parts", [])
                                    txt = ""
                                    for p in parts:
                                        if p.get("text"):
                                            txt = p["text"].strip()
                                            break
                                    if txt:
                                        first_msgs[sid] = txt[:30] + ("..." if len(txt) > 30 else "")
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug("Could not fetch first messages for session titles: %s", e)

        sessions_data = []
        for sess in sessions_list_obj:
            title = sess.state.get("session_title") if sess.state else None
            if not title or title == "New Chat":
                title = first_msgs.get(sess.id) or "New Chat"
            sessions_data.append({
                "id": sess.id,
                "title": title,
                "created_at": getattr(sess, "created_at", None),
                "updated_at": getattr(sess, "last_update_time", None) or getattr(sess, "updated_at", None),
            })
        return {"status": "success", "sessions": sessions_data}
    except Exception as exc:
        logger.error("Failed to list sessions: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to list sessions: {exc}") from exc


@app.post("/sessions", dependencies=[Depends(verify_auth)])
async def create_user_session(req: CreateSessionRequest | None = None) -> dict:
    """Create a new chat session for the default user.

    Args:
        req: Optional request body. If omitted, a UUID-based session ID and
            ``"New Chat"`` title are used.

    Returns:
        Dict with keys ``status`` and ``session`` (containing ``id``,
        ``title``, ``created_at``, and an empty ``messages`` list).

    Raises:
        HTTPException: 500 on unexpected session service error.
    """
    session_service = services.get_session_service()
    app_name = getattr(app.state, "agent_app_name", "zocrates")
    import uuid

    sid = req.session_id if (req and req.session_id) else f"session-{uuid.uuid4().hex[:12]}"
    title = req.title if (req and req.title) else "New Chat"
    try:
        sess = await session_service.create_session(
            app_name=app_name,
            user_id="default_user",
            session_id=sid,
            state={"session_title": title},
        )
        return {
            "status": "success",
            "session": {
                "id": sess.id,
                "title": title,
                "created_at": getattr(sess, "created_at", None),
                "messages": [],
            },
        }
    except Exception as exc:
        logger.error("Failed to create session %s: %s", sid, exc)
        raise HTTPException(status_code=500, detail=f"Failed to create session: {exc}") from exc


@app.get("/sessions/storage_stats", dependencies=[Depends(verify_auth)])
async def get_sessions_storage_stats_endpoint() -> dict:
    """Return physical database size and session count for sessions.db."""
    zotero_dir = getattr(app.state, "zotero_data_dir", None)
    if not zotero_dir:
        from zocrates.config import resolve_zotero_data_dir
        zotero_dir = resolve_zotero_data_dir()
    session_service = services.get_session_service(zotero_dir)
    app_name = getattr(app.state, "agent_app_name", "zocrates")
    db_path = None
    if zotero_dir:
        db_path = Path(zotero_dir).expanduser().resolve() / "zocrates-data" / "sessions.db"
    elif hasattr(session_service, "db_url"):
        url = str(session_service.db_url)
        prefix = "sqlite+aiosqlite:///"
        if url.startswith(prefix):
            db_path = Path(url[len(prefix):])

    if not db_path:
        return {
            "status": "success",
            "size_bytes": 0,
            "session_count": 0,
            "freelist_pages": 0,
            "total_pages": 0,
            "page_size": 4096,
        }

    from zocrates.utils.session_maintenance import get_session_storage_stats
    stats = await get_session_storage_stats(
        db_path=db_path,
        session_service=session_service,
        app_name=app_name,
        user_id="default_user",
    )
    return {
        "status": "success",
        "size_bytes": stats.get("size_bytes", 0),
        "session_count": stats.get("session_count", 0),
        **stats,
    }


@app.post("/sessions/maintenance", dependencies=[Depends(verify_auth)])
async def run_sessions_maintenance_endpoint() -> dict:
    """Run on-demand session database retention pruning and space reclamation."""
    zotero_dir = getattr(app.state, "zotero_data_dir", None)
    if not zotero_dir:
        from zocrates.config import resolve_zotero_data_dir
        zotero_dir = resolve_zotero_data_dir()
    session_service = services.get_session_service(zotero_dir)
    app_name = getattr(app.state, "agent_app_name", "zocrates")

    if not zotero_dir and hasattr(session_service, "db_url"):
        url = str(session_service.db_url)
        prefix = "sqlite+aiosqlite:///"
        if url.startswith(prefix):
            p = Path(url[len(prefix):])
            if p.parent.name == "zocrates-data":
                zotero_dir = p.parent.parent

    if not zotero_dir:
        raise HTTPException(
            status_code=400,
            detail="Backend is not registered with a Zotero data directory.",
        )

    from zocrates.utils.session_maintenance import run_session_database_maintenance
    from zocrates.config import get_session_maintenance_config
    active_ids = getattr(app.state, "active_chat_session_ids", None)
    cfg = get_session_maintenance_config(zotero_dir)
    results = await run_session_database_maintenance(
        zotero_data_dir=zotero_dir,
        session_service=session_service,
        config=cfg,
        active_session_ids=active_ids,
        app_name=app_name,
        user_id="default_user",
    )
    return {
        "status": "success",
        "results": results,
        **results,
    }


@app.get("/sessions/{session_id}", dependencies=[Depends(verify_auth)])
async def get_user_session(session_id: str) -> dict:
    """Retrieve message history for a specific chat session.

    Args:
        session_id: The session ID to retrieve.

    Returns:
        Dict with keys ``status`` and ``session`` (formatted by
        ``format_session_for_ui``).

    Raises:
        HTTPException: 404 if session_id does not exist; 500 on error.
    """
    session_service = services.get_session_service()
    app_name = getattr(app.state, "agent_app_name", "zocrates")
    try:
        sess = await session_service.get_session(
            app_name=app_name, user_id="default_user", session_id=session_id
        )
        if not sess:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found.")
        return {"status": "success", "session": format_session_for_ui(sess)}
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to get session %s: %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to get session: {exc}") from exc


@app.delete("/sessions/{session_id}", dependencies=[Depends(verify_auth)])
async def delete_user_session(session_id: str) -> dict:
    """Delete a chat session from the session database.

    Args:
        session_id: The session ID to delete.

    Returns:
        Dict with keys ``status`` and ``message`` confirming deletion.

    Raises:
        HTTPException: 500 on unexpected session service error.
    """
    session_service = services.get_session_service()
    app_name = getattr(app.state, "agent_app_name", "zocrates")
    try:
        await session_service.delete_session(
            app_name=app_name, user_id="default_user", session_id=session_id
        )
        return {"status": "success", "message": f"Session {session_id} deleted."}
    except Exception as exc:
        logger.error("Failed to delete session %s: %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to delete session: {exc}") from exc


@app.post("/sessions/{session_id}/suggest_next_prompt", dependencies=[Depends(verify_auth)])
async def suggest_next_prompt(session_id: str, request: SuggestPromptRequest) -> dict[str, Any]:
    """Predict the next logical user prompt based on recent research turns.

    Runs after turn finalization so it never stalls the main SSE stream.

    Args:
        session_id: The session whose research turn history is inspected.
        request: Request containing the ``invocation_id`` of the turn that just ended.

    Returns:
        Dict with keys ``status`` (``success``, ``no_suggestion``, ``disabled`` or
        ``error``) and ``suggested_prompt`` (``str`` or ``None``). Responses from a
        predictor call also carry ``telemetry`` (model, latency, usage, cost) or ``None``.
    """
    from zocrates.agents.coordinator.predictor import predict_next_user_prompt
    from zocrates.config import get_secure_api_key, load_zocrates_config

    try:
        syncer = get_syncer()
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    zot_dir = getattr(syncer, "_zotero_data_dir", None)
    cfg = load_zocrates_config(zot_dir)
    if not cfg.get("enable_predictive_ghost_input", DEFAULT_ENABLE_PREDICTIVE_GHOST_INPUT):
        return {"status": "disabled", "suggested_prompt": None}

    runner: Runner = getattr(app.state, "runner", None)
    session_service = (
        getattr(runner, "session_service", None) if runner else services.get_session_service()
    )
    if not session_service:
        return {"status": "error", "suggested_prompt": None}

    app_name = getattr(app.state, "agent_app_name", "zocrates")
    try:
        raw_sess = session_service.get_session(
            app_name=app_name, user_id="default_user", session_id=session_id
        )
        sess = await raw_sess if inspect.isawaitable(raw_sess) else raw_sess
    except Exception as exc:
        logger.warning("suggest_next_prompt could not load session %s: %s", session_id, exc)
        return {"status": "error", "suggested_prompt": None}
    if not sess or not getattr(sess, "state", None):
        return {"status": "no_suggestion", "suggested_prompt": None}

    # Exact invocation gate: the turn that just ended must have executed a research pipeline.
    history = list(sess.state.get(RESEARCH_TURN_HISTORY) or [])
    if not history or history[-1].get("invocation_id") != request.invocation_id:
        return {"status": "no_suggestion", "suggested_prompt": None}

    pred_model = cfg.get("predictive_prompt_model") or DEFAULT_PREDICTIVE_PROMPT_MODEL
    api_key = get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))
    is_local = pred_model.startswith(("ollama/", "ollama_chat/", "vllm/", "local/"))
    if not is_local and not api_key:
        return {"status": "disabled", "suggested_prompt": None}

    telemetry: dict[str, Any] = {}
    suggestion = await predict_next_user_prompt(
        research_history=history[-2:],
        model=pred_model,
        api_key=api_key,
        telemetry_out=telemetry,
    )
    logger.info(
        "Predictive next-prompt: %s in %.1fms (tokens=%d, cost=$%.6f)",
        suggestion,
        telemetry.get("latency_ms", 0),
        (telemetry.get("usage") or {}).get("total_tokens", 0),
        telemetry.get("cost_usd", 0.0),
    )
    if not suggestion:
        return {"status": "no_suggestion", "suggested_prompt": None, "telemetry": telemetry or None}

    return {"status": "success", "suggested_prompt": suggestion, "telemetry": telemetry or None}


@app.post("/sessions/{session_id}/rewind", dependencies=[Depends(verify_auth)])
async def rewind_user_session(session_id: str, request: RewindSessionRequest) -> dict:
    """Rewind a chat session to before the specified invocation.

    Args:
        session_id: The session ID to rewind.
        request: Request containing the invocation_id to rewind before.

    Returns:
        Dict with keys ``status``, ``message``, and ``session`` (formatted by
        ``format_session_for_ui``).

    Raises:
        HTTPException: 404 if session_id or invocation_id not found; 500 on error.
    """
    runner: Runner = getattr(app.state, "runner", None)
    session_service = services.get_session_service()
    app_name = getattr(app.state, "agent_app_name", "zocrates")
    if not runner:
        raise HTTPException(status_code=500, detail="Agent runner not initialized.")
    try:
        await runner.rewind_async(
            user_id="default_user",
            session_id=session_id,
            rewind_before_invocation_id=request.invocation_id,
        )
        sess = await session_service.get_session(
            app_name=app_name, user_id="default_user", session_id=session_id
        )
        if not sess:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found after rewind.")
        return {
            "status": "success",
            "message": f"Session {session_id} rewound before invocation {request.invocation_id}.",
            "session": format_session_for_ui(sess),
        }
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Failed to rewind session %s: %s", session_id, exc)
        raise HTTPException(status_code=500, detail=f"Failed to rewind session: {exc}") from exc


# ---------------------------------------------------------------------------
# Local MCP Server Endpoints
# ---------------------------------------------------------------------------


def _verify_mcp_access() -> Any:
    """Validate that MCP access is enabled and backend services are initialized.

    Returns the active LibrarySyncer instance if registered.
    Raises:
        HTTPException 403: if MCP server access is disabled in configuration.
        HTTPException 503: if the Zotero library is not yet registered or synced.
    """
    syncer = None
    try:
        syncer = get_syncer()
    except RuntimeError:
        syncer = None

    data_dir = getattr(syncer, "_zotero_data_dir", None) if syncer else None
    if not is_mcp_server_enabled(data_dir):
        raise HTTPException(
            status_code=403,
            detail=(
                "MCP server access is disabled in Zocrates preferences. "
                "Please enable it in Zotero Preferences -> Zocrates to allow "
                "external AI tools to query your library."
            ),
        )

    if syncer is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Zotero library is not yet registered or synced. "
                "Please open Zotero with Zocrates enabled."
            ),
        )

    return syncer


@app.post("/mcp/search", dependencies=[Depends(verify_auth)])
def mcp_search(request: MCPSearchRequest) -> dict[str, Any]:
    """MCP Search endpoint: hybrid semantic + keyword search over indexed library chunks.

    Clamps limit between 1 and 20. Synchronous def dispatches to worker thread pool.
    """
    _verify_mcp_access()

    if isinstance(request.item_key, str):
        if (
            request.item_key.strip().lower() not in ("all", "*", "library", "entire_library")
            and not _validate_item_key(request.item_key)
        ):
            raise HTTPException(status_code=400, detail=f"Invalid item key format: '{request.item_key}'")
    elif isinstance(request.item_key, list):
        for k in request.item_key:
            if (
                isinstance(k, str)
                and k.strip().lower() not in ("all", "*", "library", "entire_library")
                and not _validate_item_key(k)
            ):
                raise HTTPException(status_code=400, detail=f"Invalid item key format: '{k}'")

    limit = min(max(1, request.limit), 20)
    res = search_corpus_core(
        query=request.query,
        item_key=request.item_key,
        chunk_type=request.chunk_type,
        limit=limit,
        exclude_chunk_ids=request.exclude_chunk_ids,
    )
    if res.get("status") == "error":
        msg = res.get("message", "Search failed.")
        if "invalid" in msg.lower():
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=500, detail=msg)

    return res


@app.get("/mcp/documents/{item_key}/toc", dependencies=[Depends(verify_auth)])
def mcp_get_toc(item_key: str) -> dict[str, Any]:
    """MCP Table of Contents endpoint: retrieves structured sections, figures, tables, and abstract.

    Synchronous def dispatches to worker thread pool.
    """
    _verify_mcp_access()

    if not _validate_item_key(item_key):
        raise HTTPException(status_code=400, detail=f"Invalid item key format: '{item_key}'")

    res = get_document_toc_core(item_key=item_key)
    if res.get("status") == "error":
        msg = res.get("message", "Failed to retrieve table of contents.")
        lower_msg = msg.lower()
        if "invalid" in lower_msg:
            raise HTTPException(status_code=400, detail=msg)
        if "not found" in lower_msg or "not yet converted" in lower_msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=500, detail=msg)

    return res


@app.post("/mcp/documents/{item_key}/read", dependencies=[Depends(verify_auth)])
def mcp_read_document(
    item_key: str,
    request: Request,
    req: MCPReadRequest | None = None,
) -> dict[str, Any]:
    """MCP Read Document endpoint: reads targeted sections, figures, tables, or abstract.

    Augments asset entries with an absolute `url` pointing to `/assets/{item_key}/{path}`.
    Synchronous def dispatches to worker thread pool.
    """
    _verify_mcp_access()

    if not _validate_item_key(item_key):
        raise HTTPException(status_code=400, detail=f"Invalid item key format: '{item_key}'")

    if req is None:
        req = MCPReadRequest()

    res = read_document_core(item_key=item_key, asset_id=req.asset_id, page=req.page)
    if res.get("status") == "error":
        msg = res.get("message", "")
        if not msg and res.get("unresolved_asset_ids"):
            msg = f"Asset IDs {res.get('unresolved_asset_ids')} not found in document {item_key}."
            raise HTTPException(status_code=404, detail=msg)
        if not msg:
            msg = res.get("notice") or "Failed to read document."
        lower_msg = msg.lower()
        if "invalid" in lower_msg:
            raise HTTPException(status_code=400, detail=msg)
        if "not found" in lower_msg or "not yet converted" in lower_msg or "no abstract" in lower_msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=500, detail=msg)

    base_url = str(request.base_url).rstrip("/")
    resolved_key = res.get("item_key", item_key)
    for asset in res.get("assets", []):
        if isinstance(asset, dict) and asset.get("path"):
            asset_path = str(asset["path"]).lstrip("/")
            asset["url"] = f"{base_url}/assets/{resolved_key}/{asset_path}"

    return res


@app.get("/mcp/documents/{item_key}/relations", dependencies=[Depends(verify_auth)])
def mcp_get_relations(
    item_key: str,
    ceiling: int = 25,
) -> dict[str, Any]:
    """MCP Relations endpoint: retrieves verified related papers from the knowledge graph.

    Clamps ceiling between 1 and 50. Synchronous def dispatches to worker thread pool.
    """
    _verify_mcp_access()

    if not _validate_item_key(item_key):
        raise HTTPException(status_code=400, detail=f"Invalid item key format: '{item_key}'")

    clamped_ceiling = min(max(1, ceiling), 50)
    res = get_related_items_core(item_key=item_key, ceiling=clamped_ceiling)
    if res.get("status") == "error":
        msg = res.get("message", "Failed to retrieve related items.")
        lower_msg = msg.lower()
        if "invalid" in lower_msg:
            raise HTTPException(status_code=400, detail=msg)
        if "not found" in lower_msg or "not yet converted" in lower_msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=500, detail=msg)

    return res


@app.get("/mcp/catalog", dependencies=[Depends(verify_auth)])
def mcp_get_catalog() -> dict[str, Any]:
    """MCP Catalog endpoint: returns root-level knowledge base catalog index.md.

    Synchronous def dispatches to worker thread pool.
    """
    syncer = _verify_mcp_access()
    cache_dir = Path(syncer.processor._cache_dir)
    index_file = cache_dir / "index.md"
    if not index_file.exists():
        generate_root_index_md(cache_dir)
    if not index_file.exists():
        index_file.write_text("# Zocrates Knowledge Base Catalog\n\nTotal Converted Documents: 0\n", encoding="utf-8")

    return {
        "status": "success",
        "content": index_file.read_text(encoding="utf-8"),
    }


def _get_live_zotero_db() -> Any | None:
    """Retrieve live ZoteroDB from active LibrarySyncer or fallback to disk resolution."""
    try:
        from zocrates.rag.service_manager import get_syncer
        syncer = get_syncer()
        if hasattr(syncer, "db") and syncer.db is not None:
            return syncer.db
    except Exception:
        pass

    try:
        from zocrates.config import resolve_zotero_data_dir
        from zocrates.rag.zotero_db import ZoteroDB
        z_dir = resolve_zotero_data_dir()
        if z_dir:
            sql_path = Path(z_dir) / "zotero.sqlite"
            st_dir = Path(z_dir) / "storage"
            if sql_path.exists():
                return ZoteroDB(sqlite_path=sql_path, storage_dir=st_dir)
    except Exception:
        pass

    return None


# ---------------------------------------------------------------------------
# Online Literature Discovery & Import Staging Endpoints
# ---------------------------------------------------------------------------


@app.post("/discover/search", dependencies=[Depends(verify_auth)])
async def discover_search(req: DiscoverSearchRequest) -> dict[str, Any]:
    """Search for scholarly literature and return novel candidates."""
    try:
        engine = await asyncio.to_thread(get_default_discovery_engine)
        results = await engine.search(
            query=req.query,
            limit=req.limit,
            year_from=req.year_from,
            year_to=req.year_to,
            is_oa_only=req.is_oa_only,
        )
        return {"candidates": [c.to_dict() for c in results]}
    except Exception as exc:
        logger.error("Failed executing discovery search: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Discovery search failed: {exc}")


@app.post("/discover/resolve", dependencies=[Depends(verify_auth)])
async def discover_resolve(req: DiscoverResolveRequest) -> dict[str, Any]:
    """Resolve a single identifier (DOI, arXiv ID, or OpenAlex ID) and verify library presence."""
    try:
        engine = await asyncio.to_thread(get_default_discovery_engine)
        candidate, in_library = await engine.resolve_identifier(req.identifier)
        return {
            "candidate": candidate.to_dict() if candidate else None,
            "in_library": in_library,
        }
    except Exception as exc:
        logger.error("Failed resolving identifier %r: %s", req.identifier, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Identifier resolution failed: {exc}")


@app.post("/discover/prepare_batch", dependencies=[Depends(verify_auth)])
async def discover_prepare_batch(req: DiscoverPrepareBatchRequest) -> dict[str, Any]:
    """Re-check deduplication against live library and stage candidate PDFs for batch import."""
    import secrets
    import httpx
    from zocrates.discovery.dedup import Deduplicator, normalize_arxiv_id, normalize_doi, normalize_title
    from zocrates.discovery.models import CandidatePaper, StagedItem

    if len(req.candidates) > 50:
        raise HTTPException(status_code=400, detail="Batch size exceeds maximum of 50 candidates.")

    # 1. Instantiate deduplicator against live library in threadpool
    zotero_db = await asyncio.to_thread(_get_live_zotero_db)
    if zotero_db is None:
        logger.warning(
            "Live ZoteroDB not available for import dedup re-check; proceeding with intra-batch dedup only"
        )
    deduplicator = await asyncio.to_thread(Deduplicator, zotero_db=zotero_db)

    # 2. Re-check each candidate against live library
    skipped_duplicates: list[str] = []
    novel_candidates: list[CandidatePaper] = []

    for raw in req.candidates:
        candidate = raw if isinstance(raw, CandidatePaper) else CandidatePaper.from_dict(raw)
        if not candidate.candidate_id:
            candidate.candidate_id = f"cand_{secrets.token_hex(6)}"

        if deduplicator.is_in_library(candidate):
            skipped_duplicates.append(candidate.candidate_id or candidate.title)
        else:
            novel_candidates.append(candidate)
            # Track within-batch so subsequent duplicates in the same payload are also caught
            if candidate.doi:
                norm_d = normalize_doi(candidate.doi)
                if norm_d:
                    deduplicator.existing_dois.add(norm_d)
            if candidate.arxiv_id:
                norm_a = normalize_arxiv_id(candidate.arxiv_id)
                if norm_a:
                    deduplicator.existing_arxiv_ids.add(norm_a)
            if candidate.title:
                norm_t = normalize_title(candidate.title)
                if norm_t:
                    deduplicator.existing_records.append({
                        "title": candidate.title,
                        "normalized_title": norm_t,
                        "year": candidate.year,
                        "creators": candidate.authors,
                    })

    # 3. Create batch staging directory
    batch_token = secrets.token_hex(16)
    staging_root = get_staging_root()
    staging_dir = staging_root / batch_token
    staging_dir.mkdir(parents=True, exist_ok=True)

    # 4. Download and stage PDFs for novel candidates
    staged_items: list[StagedItem] = []
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15.0, read=30.0, write=15.0, pool=30.0),
        follow_redirects=True,
    ) as client:
        for candidate in novel_candidates:
            staged_path = ""
            if candidate.oa_pdf_url:
                try:
                    res_path = await download_and_stage_pdf(candidate, staging_dir, client=client)
                    if res_path:
                        staged_path = res_path
                except Exception as exc:
                    logger.warning("Failed downloading PDF for candidate %s: %s", candidate.candidate_id, exc)
                    staged_path = ""

            staged_item = StagedItem(
                candidate_id=candidate.candidate_id,
                staged_path=staged_path,
                metadata=candidate.to_dict(),
            )
            staged_items.append(staged_item)

    return {
        "batch_token": batch_token,
        "staged_items": [item.to_dict() for item in staged_items],
        "skipped_duplicates": skipped_duplicates,
    }


@app.post("/discover/cleanup_batch", dependencies=[Depends(verify_auth)])
def discover_cleanup_batch(req: DiscoverCleanupBatchRequest) -> dict[str, str]:
    """Clean up temporary staging directory for a completed or aborted discovery import batch."""
    token = req.batch_token.strip() if req.batch_token else ""
    if not token or not re.match(r"^[a-zA-Z0-9_\-]+$", token):
        raise HTTPException(status_code=400, detail="Invalid batch token format")

    staging_root = get_staging_root().resolve()
    target_dir = (staging_root / token).resolve()

    if not target_dir.is_relative_to(staging_root) or target_dir == staging_root:
        raise HTTPException(status_code=400, detail="Invalid batch token path traversal")

    if target_dir.exists() and target_dir.is_dir():
        shutil.rmtree(target_dir, ignore_errors=True)

    return {"status": "cleaned"}


@app.post("/discovery/test_openalex", dependencies=[Depends(verify_auth)])
async def test_openalex(request: OpenAlexTestRequest | None = None) -> dict[str, Any]:
    """Test connection and credentials for OpenAlex API checking rate-limit."""
    import httpx
    from zocrates.config import get_openalex_api_key, get_scholar_polite_email
    from zocrates.discovery.providers.base import make_user_agent

    key = (request.openalex_api_key or "").strip() if request and request.openalex_api_key else ""
    if not key:
        key = get_openalex_api_key() or ""

    if not key or not key.strip():
        return {
            "status": "error",
            "message": "OpenAlex API key is missing. Please provide an API key.",
        }

    clean_key = key.strip()
    headers = {
        "Authorization": f"Bearer {clean_key}",
        "User-Agent": make_user_agent(get_scholar_polite_email()),
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get("https://api.openalex.org/rate-limit", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "status": "success",
                    "message": "Connected to OpenAlex API successfully. API key is valid.",
                    "rate_limit": data,
                }
            elif resp.status_code in (401, 403):
                return {
                    "status": "error",
                    "message": f"OpenAlex API key rejected (HTTP {resp.status_code}).",
                }
            else:
                return {
                    "status": "error",
                    "message": f"OpenAlex API check failed: HTTP {resp.status_code} - {resp.text[:200]}",
                }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Connection error: {exc}",
        }


@app.post("/diagnostics/report", dependencies=[Depends(verify_auth)])
async def generate_diagnostic_report(request: DiagnosticReportRequest) -> dict[str, Any]:
    """Generate a multi-layer sanitized, GitHub-ready diagnostic report on user request."""
    from zocrates.utils.diagnostics import build_diagnostic_report_bundle

    try:
        syncer = get_syncer()
        zot_dir: Path | None = Path(syncer._zotero_data_dir)
    except RuntimeError:
        hint = _status_zotero_data_dir_hint()
        zot_dir = Path(hint) if hint else None

    sess = None
    if request.session_id:
        runner: Runner | None = getattr(app.state, "runner", None)
        session_service = (
            getattr(runner, "session_service", None)
            if runner
            else services.get_session_service()
        )
        if session_service:
            app_name = getattr(app.state, "agent_app_name", "zocrates")
            try:
                raw_sess = session_service.get_session(
                    app_name=app_name,
                    user_id="default_user",
                    session_id=request.session_id,
                )
                sess = await raw_sess if inspect.isawaitable(raw_sess) else raw_sess
            except Exception as exc:
                logger.debug(
                    "diagnostics report could not load session %s: %s",
                    request.session_id,
                    exc,
                )

    def _build_sync() -> dict[str, Any]:
        return build_diagnostic_report_bundle(
            zotero_data_dir=zot_dir,
            session=sess,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            error_message=request.error_message,
            include_prompts=request.include_prompts,
            include_item_titles=request.include_item_titles,
            include_logs=request.include_logs,
            log_tail_lines=request.log_tail_lines,
            frontend_metadata=request.frontend_metadata,
        )

    return await anyio.to_thread.run_sync(_build_sync)


@app.post("/shutdown", dependencies=[Depends(verify_auth)])
async def shutdown_endpoint() -> dict:
    """Gracefully terminate the Zocrates backend server."""
    logger.info("Received POST /shutdown request. Scheduling process termination.")
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, _trigger_shutdown)
    return {"status": "ok", "message": "Backend shutting down"}


# Main execution
if __name__ == "__main__":
    import uvicorn

    from zocrates.config import BACKEND_HOST, BACKEND_PORT

    # uvicorn.run("fast_api_app:app", host=BACKEND_HOST, port=BACKEND_PORT, reload=True)
    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT, reload=False)
