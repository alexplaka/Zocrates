"""
Central configuration for the Zocrates RAG engine.
All settings are read from environment variables with safe defaults.
"""

import contextlib
import copy
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from filelock import FileLock
from pydantic import SecretStr

load_dotenv()

# Disable noisy ChromaDB PostHog telemetry and HuggingFace Tokenizers parallelism warnings
os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend server
# ---------------------------------------------------------------------------
DEFAULT_BACKEND_PORT: int | None = None

# Deterministic port offset parameters. These are shared — byte-for-byte — with
# the JavaScript mirror in ``zotero-plugin/addon/lib/portOffset.js``. The port
# for a library is ``BASE_BACKEND_PORT + (first 8 bytes of the big-endian
# SHA-256 digest of the absolute zocrates-data dir path % 100)``, i.e. always
# within the 8765..8864 range.
BASE_BACKEND_PORT: int = 8765
DETERMINISTIC_PORT_SPAN: int = 100


def compute_deterministic_port(data_dir: Path) -> int:
    """Return the deterministic backend port for the given data directory.

    Pinned formula — MUST stay byte-for-byte identical to the JavaScript
    implementation in ``zotero-plugin/addon/lib/portOffset.js`` so the Zotero
    frontend and the Python backend derive the same port independently::

        8765 + (int.from_bytes(hashlib.sha256(str(data_dir).encode("utf-8")).digest()[:8], "big") % 100)

    Yields a stable port in the 8765..8864 range per library path, preventing
    ``EADDRINUSE`` collisions across concurrent Zotero profiles.
    """
    digest = hashlib.sha256(str(data_dir).encode("utf-8")).digest()
    return BASE_BACKEND_PORT + (int.from_bytes(digest[:8], "big") % DETERMINISTIC_PORT_SPAN)


def find_free_port(start: int, span: int = 100) -> int:
    """Return the first free loopback TCP port from ``start`` (inclusive).

    Probes ``127.0.0.1`` with an outbound TCP connect (250ms timeout). If the
    candidate is occupied, steps forward up to ``span`` candidates (last
    probed candidate is ``start + span - 1``). If every candidate is busy,
    returns ``start`` itself — the backend will surface the bind error.
    """
    for candidate in range(start, start + span):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.25)
                if probe.connect_ex(("127.0.0.1", candidate)) != 0:
                    return candidate
        except OSError:
            # A failed probe (e.g. timeout) cannot confirm availability;
            # treat the candidate as occupied and keep stepping.
            continue
    return start


def _parse_env_backend_port() -> int | None:
    """Parse the ``BACKEND_PORT`` environment override, or return ``None``."""
    val = os.getenv("BACKEND_PORT")
    if val:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return None


def _resolve_backend_port() -> int:
    """Resolve the backend port.

    Precedence:
      1. ``BACKEND_PORT`` environment variable (explicit override).
      2. ``backend_port`` (int > 0) in ``zocrates_config.json``.
      3. Deterministic offset: ``compute_deterministic_port`` of the resolved
         zocrates-data directory, stepped forward via ``find_free_port`` to
         the first free loopback port.

    When no Zotero data directory can be resolved, the plain 8765 base port is
    returned and NO port probing is performed, so module imports stay
    deterministic in test and scripting environments.
    """
    env_port = _parse_env_backend_port()
    if env_port is not None:
        return env_port
    cfg_port = _parse_config_backend_port()
    if cfg_port is not None:
        return cfg_port
    resolved = _resolve_import_time_zotero_data_dir()
    if resolved is None:
        return BASE_BACKEND_PORT
    return find_free_port(compute_deterministic_port(resolved / ZOCRATES_DATA_SUBDIR))


BACKEND_HOST: str = os.getenv("BACKEND_HOST", "127.0.0.1")

DEFAULT_ALLOW_ORIGINS: list[str] = [
    "chrome://zocrates",
    "moz-extension://*",
]

# Restrict CORS origins to explicit local/chrome extension origins by default
ALLOW_ORIGINS: list[str] = (
    [o.strip() for o in os.getenv("ALLOW_ORIGINS", "").split(",") if o.strip()]
    if os.getenv("ALLOW_ORIGINS")
    else list(DEFAULT_ALLOW_ORIGINS)
)

# ---------------------------------------------------------------------------
# Zotero data directory (set by the extension on /register)
# ---------------------------------------------------------------------------
ZOTERO_DATA_DIR: Path | None = Path(v) if (v := os.getenv("ZOTERO_DATA_DIR")) else None

# ---------------------------------------------------------------------------
# Zocrates data directory (inside ZOTERO_DATA_DIR once registered)
# ---------------------------------------------------------------------------
ZOCRATES_DATA_SUBDIR: str = "zocrates-data"


def _resolve_import_time_zotero_data_dir() -> Path | None:
    """Best-effort Zotero data-dir resolution that is safe at module import time.

    Deliberately avoids ``resolve_zotero_data_dir``'s ``LibrarySyncer`` branch:
    importing ``app.rag.service_manager`` while ``app.config`` itself is still
    initializing would be a circular import, and no syncer exists at import
    time anyway. Mirrors the ``ZOTERO_DATA_DIR`` env + home-profile discovery
    glue used by ``resolve_zotero_data_dir`` without any side effects.
    """
    if env_dir := os.getenv("ZOTERO_DATA_DIR"):
        return Path(env_dir)
    candidates = (
        list(Path.home().glob("Zotero*/zocrates-data"))
        + list(Path.home().glob("Zotero*/*/zocrates-data"))
        + list(Path.home().glob(".var/app/org.zotero.Zotero/data/zotero/zocrates-data"))
        + list(Path.home().glob(".zotero/zotero/*/zocrates-data"))
    )
    if candidates:
        return candidates[0].parent
    return None


def _parse_config_backend_port(zotero_data_dir: Path | None = None) -> int | None:
    """Read-only peek at ``backend_port`` in ``zocrates_config.json``.

    Unlike ``load_zocrates_config`` this performs no file-lock side effects,
    no default backfilling, and no disk writes — it is safe to call during
    module import. Returns the port only when it parses to an int > 0.
    """
    if zotero_data_dir is not None:
        resolved = Path(zotero_data_dir)
    else:
        resolved = _resolve_import_time_zotero_data_dir()
    if resolved is None:
        return None
    config_path = resolved / ZOCRATES_DATA_SUBDIR / "zocrates_config.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path) as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    port = raw.get("backend_port", DEFAULT_BACKEND_PORT)
    if port is None:
        return None
    try:
        port_int = int(port)
    except (ValueError, TypeError):
        return None
    return port_int if port_int > 0 else None


# Resolved once at import time. Precedence: env BACKEND_PORT >
# zocrates_config.json "backend_port" > deterministic offset (see
# _resolve_backend_port). The Zotero launcher always passes BACKEND_PORT
# explicitly, so spawned children inherit the frontend-resolved port.
BACKEND_PORT: int = _resolve_backend_port()


# ---------------------------------------------------------------------------
# VLM Figure Enrichment Defaults
# ---------------------------------------------------------------------------
DEFAULT_ENABLE_VLM_CAPTIONING: bool = True
DEFAULT_VLM_CAPTIONING_PROVIDER: str = "openrouter"
DEFAULT_VLM_CAPTIONING_MODEL: str = "openrouter/deepseek/deepseek-v4.1-flash"
DEFAULT_VLM_CAPTIONING_BACKUP_MODEL: str = "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"
DEFAULT_VLM_CAPTIONING_LOCAL_MODEL: str = ""
DEFAULT_VLM_MAX_IMAGES_PER_DOC: int = 100
DEFAULT_VLM_MIN_IMAGE_DIMENSION: int = 120
DEFAULT_VLM_MIN_FILE_SIZE_BYTES: int = 4096
DEFAULT_VLM_TIMEOUT_SECONDS: int = 30
DEFAULT_SEMANTIC_SIMILARITY_THRESHOLD: float = 0.50
DEFAULT_RERANKER_MODEL: str = "qwen/qwen3-reranker-8b"
# Phase 7 onboarding: consecutive-failure auto-abort threshold for the
# full-library sync job (poison-item protection). Materialized per SSOT rule;
# no Preferences UI (onboarding_configuration_step_plan.md §8).
DEFAULT_SYNC_AUTO_ABORT_FAILURES: int = 10

# ---------------------------------------------------------------------------
# Search Harness & HITL Extension Defaults
# ---------------------------------------------------------------------------
DEFAULT_SEARCH_HARNESS: dict[str, Any] = {
    "max_retrieval_tools": 10,
    "budget_extension_increment": 5,
    "max_absolute_retrieval_limit": 20,
    "max_extension_count": 2,
    "hard_word_ceiling": 7500,
    "word_ceiling_extension_increment": 3750,
    "max_absolute_word_ceiling": 15000,
    "word_ceiling_hitl_trigger_percent": 80,
    "max_rerank_chunks": 15,
    "rerank_chunks_extension_increment": 5,
    "max_absolute_rerank_chunks": 25,
    "hitl_enabled": True,
    "max_injected_toc_items": 3,
    "max_unique_cached_tocs": 10,
    "online_initial_retrieval_tools": 8,
    "max_online_extensions": 2,
    "online_budget_increment": 5,
    "max_absolute_online_tools": 18,
}

# ---------------------------------------------------------------------------
# Graph Paper Relations Defaults
# ---------------------------------------------------------------------------
DEFAULT_GRAPH_RELATIONS: dict[str, Any] = {
    "enabled": True,
    "strategy": "hybrid_corroborated",
    "candidate_pool_size": 50,
    "min_hybrid_score": 0.015,
    "score_tau": 0.10,
    "shared_tags_score": 0.70,
    "include_human_curated": True,
    "include_shared_tags": True,
    "min_shared_tags": 2,
    "sync_bundle_files": True,
}

# ---------------------------------------------------------------------------
# Citation & References Defaults
# ---------------------------------------------------------------------------
DEFAULT_CITATIONS_ENABLED: bool = True
DEFAULT_REFERENCES_SECTION_ENABLED: bool = True
DEFAULT_CONSIDERED_SOURCES_DISCLOSURE: bool = True
DEFAULT_AUTO_RENDER_MERMAID_DIAGRAMS: bool = True
DEFAULT_ENABLE_MCP_SERVER: bool = True
# Whether the Zotero bootstrap may auto-provision/spawn a managed backend.
# Developers running the backend from a terminal set this to false so Zotero
# only ever attaches warm (never downloads Python / provisions a .venv).
DEFAULT_AUTO_SPAWN_BACKEND: bool = True
DEFAULT_SCHOLAR_POLITE_EMAIL: str = ""
DEFAULT_USER_RESEARCH_PROFILE: str = ""
DEFAULT_MAX_RESEARCH_PROFILE_CHARS: int = 1000
DEFAULT_CONVERSION_TIMEOUT_SECONDS: int = 360
CONVERSION_TIMEOUT_SECONDS: int = int(
    os.getenv("CONVERSION_TIMEOUT_SECONDS", str(DEFAULT_CONVERSION_TIMEOUT_SECONDS))
)
DEFAULT_DATALAB_MERGE_CROSS_PAGE: bool = False

# ---------------------------------------------------------------------------
# Citation Verification Defaults (Decision Models)
# ---------------------------------------------------------------------------
DEFAULT_CITATION_VERIFICATION_ENABLED: bool = False
DEFAULT_CITATION_VERIFICATION_PROVIDER: str = "openrouter"
DEFAULT_CITATION_VERIFICATION_MODEL: str = "~typesafe/jev-latest"
DEFAULT_CITATION_VERIFICATION_THRESHOLD: float = 0.60
DEFAULT_CITATION_VERIFICATION_LOCAL_URL: str = "http://127.0.0.1:8005/v1/systemone"
DEFAULT_CITATION_VERIFICATION_LOCAL_PORT: int = 8005
DEFAULT_CITATION_VERIFICATION_AUTO_START_LOCAL: bool = True
DEFAULT_CITATION_VERIFICATION_TIMEOUT_SEC: float = 10.0
DEFAULT_CITATION_VERIFICATION_TURN_BUDGET_SEC: float = 20.0

# ---------------------------------------------------------------------------
# Predictive Next-Prompt (Ghost Input) Defaults
# ---------------------------------------------------------------------------
DEFAULT_ENABLE_PREDICTIVE_GHOST_INPUT: bool = True
DEFAULT_PREDICTIVE_PROMPT_MODEL: str = "openrouter/~deepseek/deepseek-flash-latest"

# ---------------------------------------------------------------------------
# Adaptive Context Compaction Defaults (Post-Turn Decision Model)
# ---------------------------------------------------------------------------
DEFAULT_ENABLE_ADAPTIVE_COMPACTION: bool = True
DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS: int = 2
DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS: int = 4
DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR: int = 30000
DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING: int = 100000
DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD: float = 0.75
DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL: str = "inception/mercury-decide"
DEFAULT_ADAPTIVE_DECISION_MODEL: str = DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL
DEFAULT_COMPACTION_SUMMARIZER_MODEL: str = "openrouter/~deepseek/deepseek-flash-latest"

# ---------------------------------------------------------------------------
# Session Database Retention & Maintenance Defaults
# ---------------------------------------------------------------------------
DEFAULT_MAX_SAVED_SESSIONS: int = 100
DEFAULT_SESSION_RETENTION_DAYS: int = 180
DEFAULT_SESSION_DB_AUTO_VACUUM_ON_STARTUP: bool = True
DEFAULT_PRUNE_EMPTY_SESSIONS: bool = True

# ---------------------------------------------------------------------------
# LLM Defaults & Provider Mappings
# ---------------------------------------------------------------------------
DEFAULT_LLM_MODEL: str = "openrouter/~z-ai/glm-flash-latest"
DEFAULT_LLM_BASE_URL: str = ""
DEFAULT_LLM_TEMPERATURE: float | None = None
DEFAULT_LLM_MAX_TOKENS: int = 16384
DEFAULT_LLM_NUM_RETRIES: int = 4
DEFAULT_OLLAMA_NUM_CTX: int = 16384

DEFAULT_OVERRIDE_AGENT_MODELS: dict[str, dict[str, Any]] = {
    "coordinator": {
        "model": None,
        "thinking_level": "medium",
    },
    "library_search": {
        "model": None,
        "thinking_level": "low",
    },
    "library_synthesizer": {
        "model": None,
        "thinking_level": "high",
    },
    "online_search": {
        "model": None,
        "thinking_level": "low",
    },
    "online_synthesizer": {
        "model": None,
        "thinking_level": "high",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "llm_provider": "openrouter_llm",
    "llm_model": DEFAULT_LLM_MODEL,
    # Optional server base URL override for local providers (ollama / vllm /
    # openai_compatible). Empty string = use each provider's built-in default.
    "llm_base_url": DEFAULT_LLM_BASE_URL,
    "llm_temperature": DEFAULT_LLM_TEMPERATURE,
    "llm_max_tokens": DEFAULT_LLM_MAX_TOKENS,
    "llm_num_retries": DEFAULT_LLM_NUM_RETRIES,
    "ollama_num_ctx": DEFAULT_OLLAMA_NUM_CTX,
    "override_agent_models": copy.deepcopy(DEFAULT_OVERRIDE_AGENT_MODELS),
    "embedding_provider": "local",
    "embedding_model": "jinaai/jina-embeddings-v5-text-nano",
    # SSOT materialization (Phase 7 onboarding, plan §8): dimension persisted by
    # POST /rag/model but previously absent from the defaults.
    "embedding_dimension": 768,
    "document_converter_mode": "local",
    "document_converter_id": "markitdown",
    "docling_mode": "auto",
    "datalab_non_pdf_fallback": "markitdown",
    "datalab_merge_cross_page": DEFAULT_DATALAB_MERGE_CROSS_PAGE,
    "conversion_timeout_seconds": DEFAULT_CONVERSION_TIMEOUT_SECONDS,
    "enable_vlm_captioning": DEFAULT_ENABLE_VLM_CAPTIONING,
    "vlm_captioning_provider": DEFAULT_VLM_CAPTIONING_PROVIDER,
    "vlm_captioning_model": DEFAULT_VLM_CAPTIONING_MODEL,
    "vlm_captioning_backup_model": DEFAULT_VLM_CAPTIONING_BACKUP_MODEL,
    "vlm_captioning_local_model": DEFAULT_VLM_CAPTIONING_LOCAL_MODEL,
    "vlm_max_images_per_doc": DEFAULT_VLM_MAX_IMAGES_PER_DOC,
    "vlm_timeout_seconds": DEFAULT_VLM_TIMEOUT_SECONDS,
    "semantic_similarity_threshold": DEFAULT_SEMANTIC_SIMILARITY_THRESHOLD,
    "reranker_model": DEFAULT_RERANKER_MODEL,
    "search_harness": copy.deepcopy(DEFAULT_SEARCH_HARNESS),
    "graph_relations": copy.deepcopy(DEFAULT_GRAPH_RELATIONS),
    "citations_enabled": DEFAULT_CITATIONS_ENABLED,
    "references_section_enabled": DEFAULT_REFERENCES_SECTION_ENABLED,
    "considered_sources_disclosure": DEFAULT_CONSIDERED_SOURCES_DISCLOSURE,
    "auto_render_mermaid_diagrams": DEFAULT_AUTO_RENDER_MERMAID_DIAGRAMS,
    "enable_mcp_server": DEFAULT_ENABLE_MCP_SERVER,
    "auto_spawn_backend": DEFAULT_AUTO_SPAWN_BACKEND,
    "enable_predictive_ghost_input": DEFAULT_ENABLE_PREDICTIVE_GHOST_INPUT,
    "predictive_prompt_model": DEFAULT_PREDICTIVE_PROMPT_MODEL,
    "scholar_polite_email": DEFAULT_SCHOLAR_POLITE_EMAIL,
    "user_research_profile": DEFAULT_USER_RESEARCH_PROFILE,
    "max_research_profile_chars": DEFAULT_MAX_RESEARCH_PROFILE_CHARS,
    "citation_verification_enabled": False,
    "citation_verification_provider": "openrouter",
    "citation_verification_model": DEFAULT_CITATION_VERIFICATION_MODEL,
    "citation_verification_threshold": 0.60,
    "citation_verification_local_url": "http://127.0.0.1:8005/v1/systemone",
    "citation_verification_local_port": 8005,
    "citation_verification_auto_start_local": True,
    "citation_verification_timeout_sec": 10.0,
    "citation_verification_turn_budget_sec": 20.0,
    "enable_adaptive_compaction": DEFAULT_ENABLE_ADAPTIVE_COMPACTION,
    "adaptive_compaction_retention_turns": DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
    "adaptive_compaction_min_turns": DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS,
    "adaptive_compaction_token_floor": DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR,
    "adaptive_compaction_token_ceiling": DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING,
    "adaptive_compaction_threshold": DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD,
    "adaptive_compaction_decision_model": DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL,
    "compaction_summarizer_model": DEFAULT_COMPACTION_SUMMARIZER_MODEL,
    "max_saved_sessions": DEFAULT_MAX_SAVED_SESSIONS,
    "session_retention_days": DEFAULT_SESSION_RETENTION_DAYS,
    "session_db_auto_vacuum_on_startup": DEFAULT_SESSION_DB_AUTO_VACUUM_ON_STARTUP,
    "prune_empty_sessions": DEFAULT_PRUNE_EMPTY_SESSIONS,
    "backend_port": DEFAULT_BACKEND_PORT,
    # Frontend bootstrap handshake budget (ms): how long the Zotero launcher
    # polls GET /status before giving up on a still-starting spawned backend.
    # The FIRST boot compiles .pyc for the entire ML stack and can exceed 120s
    # on slow disks / CPU-only machines; default is 5 minutes.
    "bootstrap_handshake_timeout_ms": 300000,
    # Phase 7 onboarding (plan §8): consecutive-failure auto-abort threshold for
    # the full-library sync job.
    "sync_auto_abort_failures": DEFAULT_SYNC_AUTO_ABORT_FAILURES,
}

PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "openrouter_llm": "openrouter/~z-ai/glm-flash-latest",
    "openrouter": "openrouter/~z-ai/glm-flash-latest",
    "ollama": "llama3.1:8b",
    "vllm": "default",
    "openai_compatible": "default",
}

_last_config_mtime: float = 0.0

_CONFIG_CACHE: dict[Path, dict[str, Any]] = {}
_CONFIG_CACHE_FINGERPRINT: dict[Path, tuple[int, int]] = {}
_CONFIG_CACHE_MTIME_NS = _CONFIG_CACHE_FINGERPRINT  # Backwards-compatibility alias
_CONFIG_CACHE_LOCK = threading.Lock()


def stamp_config_mtime(zotero_data_dir: Path | str | None = None) -> None:
    """Stamp the in-memory config mtime sentinel with the current mtime of zocrates_config.json."""
    global _last_config_mtime
    resolved_dir = resolve_zotero_data_dir(zotero_data_dir)
    if resolved_dir is None:
        return
    config_path = get_config_path(resolved_dir)
    try:
        if config_path.exists():
            _last_config_mtime = config_path.stat().st_mtime
    except OSError:
        pass


def check_config_freshness(zotero_data_dir: Path | str | None = None) -> bool:
    """Check if zocrates_config.json on disk has been modified since last check.

    If _last_config_mtime is uninitialized (0.0), initializes it and returns False.
    If current mtime > _last_config_mtime, updates _last_config_mtime and returns True.
    Otherwise returns False. Handles OSError safely.
    """
    global _last_config_mtime
    resolved_dir = resolve_zotero_data_dir(zotero_data_dir)
    if resolved_dir is None:
        return False
    config_path = get_config_path(resolved_dir)
    try:
        if not config_path.exists():
            return False
        current_mtime = config_path.stat().st_mtime
        if _last_config_mtime == 0.0:
            _last_config_mtime = current_mtime
            return False
        if current_mtime > _last_config_mtime:
            _last_config_mtime = current_mtime
            return True
        return False
    except OSError:
        return False


def rearm_config_freshness() -> None:
    """Re-arm the config freshness sentinel so a failed hot-swap can be retried immediately.

    Sets _last_config_mtime to 1.0 (epoch + 1s). Since any actual file mtime is greater
    than 1.0, the next check_config_freshness call will re-trigger True.
    """
    global _last_config_mtime
    _last_config_mtime = 1.0



def resolve_zotero_data_dir(zotero_data_dir: Path | str | None = None) -> Path | None:
    """Resolve the active Zotero data directory with auto-discovery fallback.

    Args:
        zotero_data_dir: Explicit data directory path or None.

    Returns:
        Resolved Path to the Zotero data directory, or None if not located.
    """
    if zotero_data_dir is not None:
        return Path(zotero_data_dir)
    try:
        from zocrates.rag.service_manager import get_syncer
        syncer = get_syncer()
        if syncer and getattr(syncer, "_zotero_data_dir", None):
            return Path(syncer._zotero_data_dir)
    except Exception:
        pass
    if ZOTERO_DATA_DIR:
        return Path(ZOTERO_DATA_DIR)
    if env_dir := os.getenv("ZOTERO_DATA_DIR"):
        return Path(env_dir)
    # Auto-discover active Zotero profile in user home directory (fallback for offline scripts)
    candidates = (
        list(Path.home().glob("Zotero*/zocrates-data"))
        + list(Path.home().glob("Zotero*/*/zocrates-data"))
        + list(Path.home().glob(".var/app/org.zotero.Zotero/data/zotero/zocrates-data"))
        + list(Path.home().glob(".zotero/zotero/*/zocrates-data"))
    )
    if candidates:
        selected = candidates[0].parent
        if len(candidates) > 1:
            logger.warning(
                "Multiple Zotero profiles found with zocrates-data (%s). "
                "Defaulting to %s. Set ZOTERO_DATA_DIR to select a specific profile.",
                [str(c.parent) for c in candidates],
                selected,
            )
        else:
            logger.debug("Auto-discovered Zotero data directory: %s", selected)
        return selected
    return None


def get_zocrates_data_dir(zotero_data_dir: Path | str | None = None) -> Path:
    """Return (and create if needed) the Zocrates data directory.

    Args:
        zotero_data_dir: Root Zotero data directory (Path, string, or None for auto-discovery).

    Returns:
        Path to ``<zotero_data_dir>/zocrates-data/``, created if absent.
    """
    resolved = resolve_zotero_data_dir(zotero_data_dir)
    if resolved is None:
        raise RuntimeError(
            "Cannot determine Zotero data directory. "
            "Please ensure Zotero is running and registered, or set ZOTERO_DATA_DIR."
        )
    zocrates_dir = resolved / ZOCRATES_DATA_SUBDIR
    zocrates_dir.mkdir(parents=True, exist_ok=True)
    return zocrates_dir


def get_corpus_dir(zotero_data_dir: Path) -> Path:
    """Return (and create if needed) the document corpus directory.

    Args:
        zotero_data_dir: Root Zotero data directory.

    Returns:
        Path to ``<zocrates_data>/corpus/``, created if absent.
    """
    zocrates_dir = get_zocrates_data_dir(zotero_data_dir)
    legacy_dir = zocrates_dir / "markdown_cache"
    corpus_dir = zocrates_dir / "corpus"
    if legacy_dir.is_dir() and not corpus_dir.exists():
        try:
            legacy_dir.rename(corpus_dir)
            logger.info("Migrated legacy markdown_cache to %s", corpus_dir)
        except OSError as exc:
            logger.warning(
                "Failed to automatically migrate legacy markdown_cache to corpus: %s. "
                "Please rename '%s' to '%s' manually.",
                exc,
                legacy_dir,
                corpus_dir,
            )
    elif legacy_dir.is_dir() and corpus_dir.is_dir():
        logger.debug("Both corpus/ and legacy markdown_cache/ exist. Using %s.", corpus_dir)
    corpus_dir.mkdir(parents=True, exist_ok=True)
    return corpus_dir


get_markdown_cache_dir = get_corpus_dir


def get_chroma_dir(zotero_data_dir: Path) -> Path:
    """Return (and create if needed) the ChromaDB storage directory.

    Args:
        zotero_data_dir: Root Zotero data directory.

    Returns:
        Path to ``<zocrates_data>/chroma/``, created if absent.
    """
    zocrates_dir = get_zocrates_data_dir(zotero_data_dir)
    chroma_dir = zocrates_dir / "chroma"
    chroma_dir.mkdir(parents=True, exist_ok=True)
    return chroma_dir


def get_fts_db_path(zotero_data_dir: Path) -> Path:
    """Return the path to the FTS5 SQLite database.

    Args:
        zotero_data_dir: Root Zotero data directory.

    Returns:
        Path to ``<zocrates_data>/fts_index.db``.
    """
    zocrates_dir = get_zocrates_data_dir(zotero_data_dir)
    return zocrates_dir / "fts_index.db"


def get_log_dir(zotero_data_dir: Path) -> Path:
    """Return (and create if needed) the Zocrates log directory.

    Args:
        zotero_data_dir: Root Zotero data directory.

    Returns:
        Path to ``<zocrates_data>/logs/``, created if absent.
    """
    log_dir = get_zocrates_data_dir(zotero_data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def get_config_path(zotero_data_dir: Path) -> Path:
    """Return the path to the Zocrates config JSON file.

    Args:
        zotero_data_dir: Root Zotero data directory.

    Returns:
        Path to ``<zocrates_data>/zocrates_config.json``.
    """
    return get_zocrates_data_dir(zotero_data_dir) / "zocrates_config.json"


_CACHED_AUTH_TOKEN: str | None = None


def _write_auth_sidecar(auth_file: Path, token: str) -> None:
    """Write auth token to sidecar file with 0o600 permissions (best-effort)."""
    try:
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(auth_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token + "\n")
        except Exception:
            with contextlib.suppress(OSError):
                os.close(fd)
            raise
        with contextlib.suppress(OSError):
            os.chmod(auth_file, 0o600)
    except OSError as exc:
        logger.debug("Could not write backend.auth sidecar at %s: %s", auth_file, exc)


def _resolve_explicit_zotero_data_dir(zotero_data_dir: Path | str | None = None) -> Path | None:
    """Resolve Zotero data directory from explicit arg, active syncer, or ZOTERO_DATA_DIR env."""
    if zotero_data_dir is not None:
        return Path(zotero_data_dir)
    try:
        from zocrates.rag.service_manager import get_syncer

        syncer = get_syncer()
        syncer_dir = getattr(syncer, "_zotero_data_dir", None)
        if isinstance(syncer_dir, (str, Path)):
            return Path(syncer_dir)
    except Exception:
        pass
    if ZOTERO_DATA_DIR:
        return Path(ZOTERO_DATA_DIR)
    if env_dir := os.getenv("ZOTERO_DATA_DIR"):
        return Path(env_dir)
    return None


def get_auth_token(zotero_data_dir: Path | str | None = None) -> str:
    """Resolve or generate the local backend authentication token.

    Resolution order:
      1. ``ZOCRATES_AUTH_TOKEN`` environment variable.
      2. Sidecar file ``<zocrates-data>/backend.auth``.
      3. Cached in-memory token, or newly generated 256-bit hex token
         (``secrets.token_hex(32)``) persisted to ``backend.auth`` with mode ``0o600``.
    """
    global _CACHED_AUTH_TOKEN

    env_token = os.getenv("ZOCRATES_AUTH_TOKEN", "").strip()
    if env_token:
        if zotero_data_dir is not None:
            auth_file = Path(zotero_data_dir) / ZOCRATES_DATA_SUBDIR / "backend.auth"
            if not auth_file.exists():
                _write_auth_sidecar(auth_file, env_token)
        return env_token

    if zotero_data_dir is not None:
        resolved_dir = Path(zotero_data_dir)
        auth_file = resolved_dir / ZOCRATES_DATA_SUBDIR / "backend.auth"
        try:
            if auth_file.is_file():
                file_token = auth_file.read_text(encoding="utf-8").strip()
                if file_token:
                    _CACHED_AUTH_TOKEN = file_token
                    return file_token
        except OSError:
            pass
        token = _CACHED_AUTH_TOKEN or secrets.token_hex(32)
        _write_auth_sidecar(auth_file, token)
        _CACHED_AUTH_TOKEN = token
        return token

    if _CACHED_AUTH_TOKEN:
        return _CACHED_AUTH_TOKEN

    resolved_dir = _resolve_explicit_zotero_data_dir(None)
    if resolved_dir is not None:
        auth_file = resolved_dir / ZOCRATES_DATA_SUBDIR / "backend.auth"
        try:
            if auth_file.is_file():
                file_token = auth_file.read_text(encoding="utf-8").strip()
                if file_token:
                    _CACHED_AUTH_TOKEN = file_token
                    return file_token
        except OSError:
            pass
        token = secrets.token_hex(32)
        _write_auth_sidecar(auth_file, token)
        _CACHED_AUTH_TOKEN = token
        return token

    _CACHED_AUTH_TOKEN = secrets.token_hex(32)
    return _CACHED_AUTH_TOKEN


def load_zocrates_config(zotero_data_dir: Path | str | None = None) -> dict:
    """Load the Zocrates config from the data directory.

    If ``zotero_data_dir`` is ``None``, attempts to obtain it from the active
    ``LibrarySyncer`` service or the ``ZOTERO_DATA_DIR`` environment variable.

    Returns config merged with standard defaults.

    Args:
        zotero_data_dir: Root Zotero data directory.

    Returns:
        Parsed JSON config dict merged with defaults.
    """
    defaults = copy.deepcopy(DEFAULT_CONFIG)

    resolved_dir = resolve_zotero_data_dir(zotero_data_dir)
    if resolved_dir is None:
        return defaults

    config_path = get_config_path(resolved_dir)
    resolved_path = config_path.resolve()
    if not config_path.exists():
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE.pop(resolved_path, None)
            _CONFIG_CACHE_FINGERPRINT.pop(resolved_path, None)
        return defaults

    try:
        stat_res = config_path.stat()
        current_fingerprint = (stat_res.st_mtime_ns, stat_res.st_size)
    except OSError:
        return defaults

    with _CONFIG_CACHE_LOCK:
        if resolved_path in _CONFIG_CACHE and _CONFIG_CACHE_FINGERPRINT.get(resolved_path) == current_fingerprint:
            return copy.deepcopy(_CONFIG_CACHE[resolved_path])

    if config_path.exists():
        try:
            lock_path = str(config_path) + ".lock"
            with FileLock(lock_path):
                try:
                    stat_res = config_path.stat()
                    current_fingerprint = (stat_res.st_mtime_ns, stat_res.st_size)
                except OSError:
                    return defaults

                with _CONFIG_CACHE_LOCK:
                    if resolved_path in _CONFIG_CACHE and _CONFIG_CACHE_FINGERPRINT.get(resolved_path) == current_fingerprint:
                        return copy.deepcopy(_CONFIG_CACHE[resolved_path])

                with open(config_path) as f:
                    loaded = json.load(f)

                # Backfill override_agent_models if missing or incomplete
                needs_save = False

                if "override_agent_models" not in loaded or not isinstance(loaded["override_agent_models"], dict):
                    loaded["override_agent_models"] = copy.deepcopy(DEFAULT_OVERRIDE_AGENT_MODELS)
                    needs_save = True
                else:
                    for role, default_role_cfg in DEFAULT_OVERRIDE_AGENT_MODELS.items():
                        if role not in loaded["override_agent_models"]:
                            loaded["override_agent_models"][role] = copy.deepcopy(default_role_cfg)
                            needs_save = True

                # Backfill search_harness if missing or incomplete in existing config file on disk
                if "search_harness" not in loaded or not isinstance(loaded["search_harness"], dict):
                    loaded["search_harness"] = copy.deepcopy(DEFAULT_SEARCH_HARNESS)
                    needs_save = True
                else:
                    for k, default_val in DEFAULT_SEARCH_HARNESS.items():
                        if k not in loaded["search_harness"]:
                            loaded["search_harness"][k] = copy.deepcopy(default_val)
                            needs_save = True

                # Backfill graph_relations if missing or incomplete in existing config file on disk
                if "graph_relations" not in loaded or not isinstance(loaded["graph_relations"], dict):
                    loaded["graph_relations"] = copy.deepcopy(DEFAULT_GRAPH_RELATIONS)
                    needs_save = True
                else:
                    for k, default_val in DEFAULT_GRAPH_RELATIONS.items():
                        if k not in loaded["graph_relations"]:
                            loaded["graph_relations"][k] = copy.deepcopy(default_val)
                            needs_save = True

                # Backfill llm_num_retries if missing from existing config file on disk
                if "llm_num_retries" not in loaded:
                    loaded["llm_num_retries"] = DEFAULT_LLM_NUM_RETRIES
                    needs_save = True

                # Backfill llm_base_url if missing from existing config file on disk
                if "llm_base_url" not in loaded:
                    loaded["llm_base_url"] = DEFAULT_LLM_BASE_URL
                    needs_save = True

                # Backfill citations options if missing from existing config file on disk
                if "citations_enabled" not in loaded:
                    loaded["citations_enabled"] = DEFAULT_CITATIONS_ENABLED
                    needs_save = True
                if "references_section_enabled" not in loaded:
                    loaded["references_section_enabled"] = DEFAULT_REFERENCES_SECTION_ENABLED
                    needs_save = True
                if "considered_sources_disclosure" not in loaded:
                    loaded["considered_sources_disclosure"] = DEFAULT_CONSIDERED_SOURCES_DISCLOSURE
                    needs_save = True
                if "auto_render_mermaid_diagrams" not in loaded:
                    loaded["auto_render_mermaid_diagrams"] = DEFAULT_AUTO_RENDER_MERMAID_DIAGRAMS
                    needs_save = True
                if "enable_mcp_server" not in loaded:
                    loaded["enable_mcp_server"] = DEFAULT_ENABLE_MCP_SERVER
                    needs_save = True
                if "auto_spawn_backend" not in loaded:
                    loaded["auto_spawn_backend"] = DEFAULT_AUTO_SPAWN_BACKEND
                    needs_save = True
                if "enable_predictive_ghost_input" not in loaded:
                    loaded["enable_predictive_ghost_input"] = DEFAULT_ENABLE_PREDICTIVE_GHOST_INPUT
                    needs_save = True
                if "predictive_prompt_model" not in loaded:
                    loaded["predictive_prompt_model"] = DEFAULT_PREDICTIVE_PROMPT_MODEL
                    needs_save = True
                if "scholar_polite_email" not in loaded:
                    loaded["scholar_polite_email"] = DEFAULT_SCHOLAR_POLITE_EMAIL
                    needs_save = True
                if "conversion_timeout_seconds" not in loaded:
                    loaded["conversion_timeout_seconds"] = DEFAULT_CONVERSION_TIMEOUT_SECONDS
                    needs_save = True
                if "datalab_merge_cross_page" not in loaded:
                    loaded["datalab_merge_cross_page"] = DEFAULT_DATALAB_MERGE_CROSS_PAGE
                    needs_save = True
                if "user_research_profile" not in loaded:
                    loaded["user_research_profile"] = DEFAULT_USER_RESEARCH_PROFILE
                    needs_save = True
                if "max_research_profile_chars" not in loaded:
                    loaded["max_research_profile_chars"] = DEFAULT_MAX_RESEARCH_PROFILE_CHARS
                    needs_save = True
                if "max_saved_sessions" not in loaded:
                    loaded["max_saved_sessions"] = DEFAULT_MAX_SAVED_SESSIONS
                    needs_save = True
                if "session_retention_days" not in loaded:
                    loaded["session_retention_days"] = DEFAULT_SESSION_RETENTION_DAYS
                    needs_save = True
                if "session_db_auto_vacuum_on_startup" not in loaded:
                    loaded["session_db_auto_vacuum_on_startup"] = DEFAULT_SESSION_DB_AUTO_VACUUM_ON_STARTUP
                    needs_save = True
                if "prune_empty_sessions" not in loaded:
                    loaded["prune_empty_sessions"] = DEFAULT_PRUNE_EMPTY_SESSIONS
                    needs_save = True
                if "backend_port" not in loaded:
                    loaded["backend_port"] = DEFAULT_BACKEND_PORT
                    needs_save = True

                # Backfill Phase 7 onboarding SSOT keys if missing from existing
                # config files on disk (embedding_dimension / sync_auto_abort_failures)
                if "embedding_dimension" not in loaded:
                    loaded["embedding_dimension"] = DEFAULT_CONFIG["embedding_dimension"]
                    needs_save = True
                if "sync_auto_abort_failures" not in loaded:
                    loaded["sync_auto_abort_failures"] = DEFAULT_SYNC_AUTO_ABORT_FAILURES
                    needs_save = True

                if "vlm_captioning_backup_model" not in loaded:
                    loaded["vlm_captioning_backup_model"] = DEFAULT_CONFIG["vlm_captioning_backup_model"]
                    needs_save = True
                if "vlm_captioning_model" not in loaded:
                    loaded["vlm_captioning_model"] = DEFAULT_CONFIG["vlm_captioning_model"]
                    needs_save = True
                if "vlm_captioning_provider" not in loaded:
                    loaded["vlm_captioning_provider"] = DEFAULT_CONFIG["vlm_captioning_provider"]
                    needs_save = True
                if "enable_vlm_captioning" not in loaded:
                    loaded["enable_vlm_captioning"] = DEFAULT_CONFIG["enable_vlm_captioning"]
                    needs_save = True
                if "vlm_max_images_per_doc" not in loaded:
                    loaded["vlm_max_images_per_doc"] = DEFAULT_CONFIG["vlm_max_images_per_doc"]
                    needs_save = True

                # Detect and migrate deprecated compaction keys from disk
                if "compaction_token_threshold" in loaded and "adaptive_compaction_token_ceiling" not in loaded:
                    loaded["adaptive_compaction_token_ceiling"] = loaded["compaction_token_threshold"]
                    needs_save = True
                if "enable_context_compaction" in loaded and "enable_adaptive_compaction" not in loaded:
                    loaded["enable_adaptive_compaction"] = loaded["enable_context_compaction"]
                    needs_save = True
                if "adaptive_decision_model" in loaded and "adaptive_compaction_decision_model" not in loaded:
                    loaded["adaptive_compaction_decision_model"] = loaded.pop("adaptive_decision_model")
                    needs_save = True
                elif "adaptive_decision_model" in loaded:
                    loaded.pop("adaptive_decision_model", None)
                    needs_save = True

                # Purge all 5 deprecated compaction keys
                deprecated_compaction_keys = (
                    "compaction_interval",
                    "compaction_overlap_size",
                    "compaction_token_threshold",
                    "compaction_event_retention_size",
                    "enable_context_compaction",
                )
                for dep_key in deprecated_compaction_keys:
                    if dep_key in loaded:
                        loaded.pop(dep_key, None)
                        needs_save = True

                # Backfill missing adaptive compaction options
                adaptive_compaction_defaults = {
                    "enable_adaptive_compaction": DEFAULT_ENABLE_ADAPTIVE_COMPACTION,
                    "adaptive_compaction_retention_turns": DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS,
                    "adaptive_compaction_min_turns": DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS,
                    "adaptive_compaction_token_floor": DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR,
                    "adaptive_compaction_token_ceiling": DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING,
                    "adaptive_compaction_threshold": DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD,
                    "adaptive_compaction_decision_model": DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL,
                    "compaction_summarizer_model": DEFAULT_COMPACTION_SUMMARIZER_MODEL,
                }
                for k, default_val in adaptive_compaction_defaults.items():
                    if k not in loaded:
                        loaded[k] = default_val
                        needs_save = True

                # Disallow local Laya for compaction decisions on disk; reset to default remote model
                if is_local_decision_model(loaded.get("adaptive_compaction_decision_model")):
                    loaded["adaptive_compaction_decision_model"] = DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL
                    needs_save = True

                # Backfill citation verification options if missing from existing config file on disk
                verification_defaults = {
                    "citation_verification_enabled": DEFAULT_CITATION_VERIFICATION_ENABLED,
                    "citation_verification_provider": DEFAULT_CITATION_VERIFICATION_PROVIDER,
                    "citation_verification_model": DEFAULT_CITATION_VERIFICATION_MODEL,
                    "citation_verification_threshold": DEFAULT_CITATION_VERIFICATION_THRESHOLD,
                    "citation_verification_local_url": DEFAULT_CITATION_VERIFICATION_LOCAL_URL,
                    "citation_verification_local_port": DEFAULT_CITATION_VERIFICATION_LOCAL_PORT,
                    "citation_verification_auto_start_local": DEFAULT_CITATION_VERIFICATION_AUTO_START_LOCAL,
                    "citation_verification_timeout_sec": DEFAULT_CITATION_VERIFICATION_TIMEOUT_SEC,
                    "citation_verification_turn_budget_sec": DEFAULT_CITATION_VERIFICATION_TURN_BUDGET_SEC,
                }
                for k, default_val in verification_defaults.items():
                    if k not in loaded:
                        loaded[k] = default_val
                        needs_save = True
                # Migrate legacy default 3.0s/10.0s timeout and budget to 10.0s/20.0s for CPU inference headroom
                try:
                    if float(loaded.get("citation_verification_timeout_sec", 0)) == 3.0:
                        loaded["citation_verification_timeout_sec"] = DEFAULT_CITATION_VERIFICATION_TIMEOUT_SEC
                        needs_save = True
                except (ValueError, TypeError):
                    pass
                try:
                    if float(loaded.get("citation_verification_turn_budget_sec", 0)) == 10.0:
                        loaded["citation_verification_turn_budget_sec"] = DEFAULT_CITATION_VERIFICATION_TURN_BUDGET_SEC
                        needs_save = True
                except (ValueError, TypeError):
                    pass

                if needs_save:
                    try:
                        with open(config_path, "w") as f_out:
                            json.dump(loaded, f_out, indent=2)
                        stamp_config_mtime(resolved_dir)
                    except Exception as save_err:
                        logger.warning(
                            "Failed to backfill default dialogue agent models to %s: %s",
                            config_path,
                            save_err,
                        )
                    try:
                        stat_res = config_path.stat()
                        current_fingerprint = (stat_res.st_mtime_ns, stat_res.st_size)
                    except OSError:
                        pass

                res = dict(defaults)
                res.update(loaded)
                with _CONFIG_CACHE_LOCK:
                    _CONFIG_CACHE[resolved_path] = copy.deepcopy(res)
                    _CONFIG_CACHE_FINGERPRINT[resolved_path] = current_fingerprint
                return copy.deepcopy(res)
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
    return defaults


def save_zocrates_config(config: dict, zotero_data_dir: Path | None = None) -> None:
    """Persist ``config`` (merged into any existing values) to the data directory.

    If ``zotero_data_dir`` is ``None``, attempts to obtain it from the active
    ``LibrarySyncer`` service or the ``ZOTERO_DATA_DIR`` environment variable.
    Uses a ``FileLock`` to prevent concurrent write corruption.

    API keys are automatically intercepted and routed to the in-memory environment via
    ``save_secure_api_key``, ensuring they are never stored in plain text on disk.

    Args:
        config: Key/value pairs to merge into the existing config file.
        zotero_data_dir: Root Zotero data directory.
    """
    config_copy = dict(config)

    profile_val = config_copy.get("user_research_profile")
    if profile_val is not None:
        if not isinstance(profile_val, str):
            raise ValueError("user_research_profile must be a string.")
        max_chars = int(config_copy.get("max_research_profile_chars") or DEFAULT_MAX_RESEARCH_PROFILE_CHARS)
        if len(profile_val) > max_chars:
            raise ValueError(
                f"user_research_profile exceeds the maximum of "
                f"{max_chars} characters ({len(profile_val)} given)."
            )

    port_val = config_copy.get("backend_port")
    if port_val is not None:
        try:
            port_int = int(port_val)
            if not (1 <= port_int <= 65535):
                raise ValueError(
                    f"backend_port must be between 1 and 65535, got {port_int}."
                )
            config_copy["backend_port"] = port_int
        except (ValueError, TypeError) as port_err:
            raise ValueError(f"Invalid backend_port: {port_err}") from port_err

    for k in list(config_copy.keys()):
        if k.endswith("_api_key") or k == "cloud_api_key":
            val = config_copy.pop(k)
            if "openrouter" in k:
                save_secure_api_key(val, key_name="openrouter_api_key", env_var="OPENROUTER_API_KEY")
            else:
                env_var = k.upper()
                save_secure_api_key(val, key_name=k, env_var=env_var)

    zotero_data_dir = resolve_zotero_data_dir(zotero_data_dir)
    if zotero_data_dir is None:
        logger.warning("Cannot save config: Zotero data directory is unresolved.")
        return

    config_path = get_config_path(zotero_data_dir)
    lock_path = str(config_path) + ".lock"
    try:
        with FileLock(lock_path):
            existing: dict = {}
            if config_path.exists():
                try:
                    with open(config_path) as f:
                        existing = json.load(f)
                except Exception as e:
                    logger.warning(
                        "Failed to read existing config at %s: %s", config_path, e
                    )
            # Ensure plain text API keys are migrated to environment and purged from JSON file
            for k in list(existing.keys()):
                if k.endswith("_api_key") or k == "cloud_api_key":
                    legacy_val = existing.pop(k, None)
                    if legacy_val:
                        try:
                            if "openrouter" in k:
                                save_secure_api_key(legacy_val, key_name="openrouter_api_key", env_var="OPENROUTER_API_KEY")
                            else:
                                env_var = k.upper()
                                save_secure_api_key(legacy_val, key_name=k, env_var=env_var)
                        except Exception:
                            pass
            if "override_agent_models" not in existing and "override_agent_models" not in config_copy:
                existing["override_agent_models"] = copy.deepcopy(DEFAULT_OVERRIDE_AGENT_MODELS)
            elif "override_agent_models" in config_copy and isinstance(config_copy["override_agent_models"], dict):
                base_overrides = copy.deepcopy(existing.get("override_agent_models") or DEFAULT_OVERRIDE_AGENT_MODELS)
                for role, role_cfg in config_copy["override_agent_models"].items():
                    if role in base_overrides and isinstance(role_cfg, dict) and isinstance(base_overrides[role], dict):
                        base_overrides[role].update(role_cfg)
                    else:
                        base_overrides[role] = role_cfg
                config_copy["override_agent_models"] = base_overrides
            existing.update(config_copy)
            with open(config_path, "w") as f:
                json.dump(existing, f, indent=2)
            stamp_config_mtime(zotero_data_dir)
            logger.info("Saved Zocrates config to %s", config_path)
            resolved_path = config_path.resolve()
            with _CONFIG_CACHE_LOCK:
                _CONFIG_CACHE.pop(resolved_path, None)
                _CONFIG_CACHE_FINGERPRINT.pop(resolved_path, None)
    except Exception as e:
        logger.warning("Failed to save config to %s: %s", config_path, e)


def clear_config_cache(zotero_data_dir: Path | str | None = None) -> None:
    """Clear in-memory config cache for a specific data dir or globally."""
    resolved_path = None
    if zotero_data_dir is not None:
        resolved = resolve_zotero_data_dir(zotero_data_dir)
        if resolved is not None:
            resolved_path = get_config_path(resolved).resolve()
    with _CONFIG_CACHE_LOCK:
        if resolved_path is not None:
            _CONFIG_CACHE.pop(resolved_path, None)
            _CONFIG_CACHE_FINGERPRINT.pop(resolved_path, None)
        else:
            _CONFIG_CACHE.clear()
            _CONFIG_CACHE_FINGERPRINT.clear()


def save_secure_api_key(
    api_key: str | SecretStr | None,
    key_name: str = "openrouter_api_key",
    env_var: str | None = "OPENROUTER_API_KEY",
) -> None:
    """Store a named API key in the session environment.

    Zotero Preferences / Login Manager (via the front-end extension) serves as
    the authoritative secure persistent vault for user credentials across restarts.
    The backend server stores keys in-memory as session variables (os.environ) synced
    from Zotero (via environment variables on startup and POST /config during runtime).

    Does NOT persist plain-text keys to disk configuration files.

    Args:
        api_key: SecretStr, raw string key, or None/empty to delete.
        key_name: Entry identifier (e.g., 'openrouter_api_key', 'datalab_api_key').
        env_var: Associated environment variable name to sync in memory.
    """
    if isinstance(api_key, SecretStr):
        secret_val = api_key.get_secret_value()
    elif api_key is not None:
        secret_val = str(api_key)
    else:
        secret_val = ""

    if secret_val and secret_val.strip():
        val = secret_val.strip()
        if env_var:
            os.environ[env_var] = val
    else:
        if env_var:
            os.environ.pop(env_var, None)


def delete_secure_api_key(
    key_name: str = "openrouter_api_key",
    env_var: str | None = "OPENROUTER_API_KEY",
) -> None:
    """Delete a named API key from the session environment (os.environ).

    Zotero Preferences / Login Manager is the secure persistent vault.
    Backend keys are in-memory session variables.
    """
    save_secure_api_key(None, key_name=key_name, env_var=env_var)


def get_secure_api_key(
    key_name: str = "openrouter_api_key",
    env_vars: list[str] | tuple[str, ...] | None = ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"),
) -> str | None:
    """Load a named API key from in-memory session environment variables.

    Zotero Preferences / Login Manager serves as the secure persistent vault.
    Backend keys are in-memory session variables (os.environ) synced from Zotero.

    Args:
        key_name: Entry identifier (e.g., 'openrouter_api_key', 'openai_api_key').
        env_vars: Environment variable names to check.

    Returns:
        The API key string or None if not found.
    """
    if env_vars:
        for var in env_vars:
            val = os.getenv(var)
            if val:
                return val
    return None


# ---------------------------------------------------------------------------
# Embedding model
# ---------------------------------------------------------------------------


# Module-level constants: env-vars take precedence; no data dir available yet
# so config file is not consulted at import time.
EMBEDDING_MODE: str = os.getenv("EMBEDDING_MODE", "local")
LOCAL_EMBEDDING_MODEL: str = os.getenv(
    "LOCAL_EMBEDDING_MODEL", "jinaai/jina-embeddings-v5-text-nano"
)
EMBEDDING_DIMENSION: int = int(os.getenv("EMBEDDING_DIMENSION", "768"))


def get_force_cpu_pref(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether CPU-only execution mode is forced for local embeddings.

    Checks the ``FORCE_CPU`` environment variable first; if unset or falsy,
    falls back to the ``force_cpu`` value in the persisted Zocrates config file.

    Args:
        zotero_data_dir: Path to the Zotero data directory. If ``None``,
            only the environment variable is consulted and the config file
            is not read.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        ``True`` if CPU-only mode should be forced, ``False`` otherwise.
    """
    val = os.getenv("FORCE_CPU", "")
    if val.lower() in ("true", "1", "yes"):
        return True
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("force_cpu", False))


def get_datalab_api_key(zotero_data_dir: Path | None = None) -> str | None:
    """Return the Datalab API key from environment variables.

    Zotero Preferences / Login Manager is the authoritative persistent vault for
    the Datalab API key. The backend stores the key in-memory (synced from Zotero
    via environment variable DATALAB_API_KEY on startup or POST /config at runtime).

    Args:
        zotero_data_dir: Optional path to the Zotero data directory.

    Returns:
        The API key string or None if not configured.
    """
    return get_secure_api_key(
        "datalab_api_key",
        env_vars=["DATALAB_API_KEY", "CHANDRA_API_KEY"],
    )


def save_datalab_api_key(api_key: str | SecretStr | None) -> None:
    """Save the Datalab API key in the session environment.

    Args:
        api_key: SecretStr, raw string key, or None/empty to delete.
    """
    save_secure_api_key(api_key, key_name="datalab_api_key", env_var="DATALAB_API_KEY")


def get_openalex_api_key(zotero_data_dir: Path | None = None) -> str | None:
    """Retrieve the configured OpenAlex API key from the environment."""
    return get_secure_api_key(
        "openalex_api_key",
        env_vars=["OPENALEX_API_KEY"],
    )


def save_openalex_api_key(api_key: str | SecretStr | None) -> None:
    """Save or clear the OpenAlex API key in the session environment."""
    save_secure_api_key(api_key, key_name="openalex_api_key", env_var="OPENALEX_API_KEY")


def get_document_converter_mode(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the document converter mode ('local' or 'cloud').

    Checks the ``DOCUMENT_CONVERTER_MODE`` environment variable first;
    if unset, falls back to the ``document_converter_mode`` in ``zocrates_config.json``
    (defaulting to ``"local"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Mode string (``"local"`` or ``"cloud"``).
    """
    env_val = os.getenv("DOCUMENT_CONVERTER_MODE", "")
    if env_val:
        return env_val.lower()
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("document_converter_mode", "local")


def get_document_converter_id(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the document converter ID from the Zocrates config.

    Checks ``DOCUMENT_CONVERTER_ID`` environment variable first;
    if unset, falls back to ``document_converter_id`` in ``zocrates_config.json``
    (defaulting to ``"markitdown"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Converter ID string (e.g., ``"markitdown"``, ``"docling_local"``).
    """
    env_val = os.getenv("DOCUMENT_CONVERTER_ID", "")
    if env_val:
        return env_val
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("document_converter_id", "markitdown")


def get_docling_mode(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the Docling operating mode ('auto', 'fast', or 'full').

    Checks ``DOCUMENT_CONVERTER_DOCLING_MODE`` or ``DOCLING_MODE`` environment
    variable first; if unset, falls back to ``docling_mode`` in ``zocrates_config.json``
    (defaulting to ``"auto"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Docling mode string (``"auto"``, ``"fast"``, or ``"full"``).
    """
    env_val = os.getenv("DOCUMENT_CONVERTER_DOCLING_MODE") or os.getenv("DOCLING_MODE")
    if env_val:
        return env_val.lower()
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("docling_mode", "auto").lower()


def get_datalab_non_pdf_fallback(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the fallback converter ID for non-PDF items when Datalab is active ('markitdown' or 'docling_local').

    Checks ``DATALAB_NON_PDF_FALLBACK`` environment variable first;
    if unset, falls back to ``datalab_non_pdf_fallback`` in ``zocrates_config.json``
    (defaulting to ``"markitdown"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Fallback converter ID string (``"markitdown"`` or ``"docling_local"``).
    """
    env_val = os.getenv("DATALAB_NON_PDF_FALLBACK", "")
    if env_val:
        return env_val.lower()
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("datalab_non_pdf_fallback", "markitdown").lower()


def get_datalab_merge_cross_page(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether Datalab cross-page table and paragraph merging is enabled.

    Checks ``DATALAB_MERGE_CROSS_PAGE`` environment variable first (evaluating
    truthiness: "1", "true", "yes", "on"); if unset, falls back to in-memory
    cfg if provided, otherwise loads from ``zocrates_config.json``
    (defaulting to ``False``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        ``True`` if enabled, ``False`` otherwise.
    """
    env_val = os.getenv("DATALAB_MERGE_CROSS_PAGE")
    if env_val is not None:
        return env_val.strip().lower() in ("1", "true", "yes", "on")
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("datalab_merge_cross_page", DEFAULT_DATALAB_MERGE_CROSS_PAGE))


def get_enable_vlm_captioning(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether VLM image captioning and figure enrichment is enabled.

    Checks ``ENABLE_VLM_CAPTIONING`` environment variable first;
    if unset, falls back to ``enable_vlm_captioning`` in ``zocrates_config.json``
    (defaulting to ``True``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        ``True`` if enabled, ``False`` otherwise.
    """
    env_val = os.getenv("ENABLE_VLM_CAPTIONING")
    if env_val is not None:
        return env_val.lower() in ("true", "1", "yes")
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("enable_vlm_captioning", DEFAULT_ENABLE_VLM_CAPTIONING))


def get_vlm_captioning_provider(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the configured VLM provider ('openrouter' or 'ollama').

    Checks ``VLM_CAPTIONING_PROVIDER`` environment variable first;
    if unset, falls back to ``vlm_captioning_provider`` in ``zocrates_config.json``
    (defaulting to ``"openrouter"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Provider string ('openrouter' or 'ollama').
    """
    env_val = os.getenv("VLM_CAPTIONING_PROVIDER", "")
    if env_val:
        return env_val.lower()
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("vlm_captioning_provider", DEFAULT_VLM_CAPTIONING_PROVIDER).lower()


def get_vlm_captioning_model(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the primary VLM model identifier for cloud captioning.

    Checks ``VLM_CAPTIONING_MODEL`` environment variable first;
    if unset, falls back to ``vlm_captioning_model`` in ``zocrates_config.json``
    (defaulting to ``"openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Model identifier string.
    """
    env_val = os.getenv("VLM_CAPTIONING_MODEL", "")
    if env_val:
        return env_val
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("vlm_captioning_model", DEFAULT_VLM_CAPTIONING_MODEL)


def get_vlm_captioning_backup_model(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the backup VLM model identifier for cloud captioning fallback.

    Checks ``VLM_CAPTIONING_BACKUP_MODEL`` environment variable first;
    if unset, falls back to ``vlm_captioning_backup_model`` in ``zocrates_config.json``
    (defaulting to ``"openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Backup model identifier string.
    """
    env_val = os.getenv("VLM_CAPTIONING_BACKUP_MODEL", "")
    if env_val:
        return env_val
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get(
        "vlm_captioning_backup_model", DEFAULT_VLM_CAPTIONING_BACKUP_MODEL
    )


def get_vlm_captioning_local_model(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the local Ollama model name for figure captioning.

    Checks ``VLM_CAPTIONING_LOCAL_MODEL`` environment variable first;
    if unset, falls back to ``vlm_captioning_local_model`` in ``zocrates_config.json``
    (defaulting to ``""``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Local model name string.
    """
    env_val = os.getenv("VLM_CAPTIONING_LOCAL_MODEL", "")
    if env_val:
        return env_val
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return cfg.get("vlm_captioning_local_model", DEFAULT_VLM_CAPTIONING_LOCAL_MODEL)


def get_vlm_max_images_per_doc(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Return the maximum number of images to caption per document.

    Checks ``VLM_MAX_IMAGES_PER_DOC`` environment variable first;
    if unset, falls back to ``vlm_max_images_per_doc`` in ``zocrates_config.json``
    (defaulting to ``20``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Max images integer ceiling.
    """
    env_val = os.getenv("VLM_MAX_IMAGES_PER_DOC")
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            pass
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    try:
        return int(cfg.get("vlm_max_images_per_doc", DEFAULT_VLM_MAX_IMAGES_PER_DOC))
    except (ValueError, TypeError):
        return DEFAULT_VLM_MAX_IMAGES_PER_DOC


def get_vlm_timeout_seconds(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Return the timeout in seconds for individual VLM captioning requests.

    Checks ``VLM_TIMEOUT_SECONDS`` environment variable first;
    if unset, falls back to ``vlm_timeout_seconds`` in ``zocrates_config.json``
    (defaulting to ``30``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Timeout integer in seconds.
    """
    env_val = os.getenv("VLM_TIMEOUT_SECONDS")
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            pass
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    try:
        return int(cfg.get("vlm_timeout_seconds", DEFAULT_VLM_TIMEOUT_SECONDS))
    except (ValueError, TypeError):
        return DEFAULT_VLM_TIMEOUT_SECONDS


def get_semantic_similarity_threshold(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> float:
    """Return the minimum cosine similarity threshold for semantic vector retrieval.

    Reads strictly from ``semantic_similarity_threshold`` in ``zocrates_config.json``
    (clamped between 0.0 and 1.0, defaulting to ``0.50``).

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Float similarity threshold between 0.0 and 1.0.
    """
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    try:
        val = float(cfg.get("semantic_similarity_threshold", DEFAULT_SEMANTIC_SIMILARITY_THRESHOLD))
        return max(0.0, min(1.0, val))
    except (ValueError, TypeError):
        return DEFAULT_SEMANTIC_SIMILARITY_THRESHOLD


def get_sync_auto_abort_failures(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Return the consecutive-failure auto-abort threshold for full-library sync jobs.

    Reads ``sync_auto_abort_failures`` from ``zocrates_config.json`` (defaulting
    to ``DEFAULT_SYNC_AUTO_ABORT_FAILURES``). Values <= 0 disable auto-abort.

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.
    """
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    try:
        return int(cfg.get("sync_auto_abort_failures", DEFAULT_SYNC_AUTO_ABORT_FAILURES))
    except (ValueError, TypeError):
        return DEFAULT_SYNC_AUTO_ABORT_FAILURES


def get_search_harness_config(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the search harness configuration dict from zocrates_config.json merged with defaults.

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Dict containing search harness parameters (budget, limits, ceilings, HITL flags).
    """
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    res = copy.deepcopy(DEFAULT_SEARCH_HARNESS)
    harness_cfg = cfg.get("search_harness")
    if isinstance(harness_cfg, dict):
        res.update(harness_cfg)
    return res


def get_graph_relations_config(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the graph relations configuration dict from zocrates_config.json merged with defaults.

    Args:
        zotero_data_dir: Path to the Zotero data directory.
        cfg: Optional in-memory config dict. If provided, bypasses disk read.

    Returns:
        Dict containing graph relations parameters.
    """
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    res = copy.deepcopy(DEFAULT_GRAPH_RELATIONS)
    graph_cfg = cfg.get("graph_relations")
    if isinstance(graph_cfg, dict):
        res.update(graph_cfg)
    return res


def get_citations_enabled(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether in-text citations are enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("citations_enabled", DEFAULT_CITATIONS_ENABLED))


def get_references_section_enabled(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether references section is enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("references_section_enabled", DEFAULT_REFERENCES_SECTION_ENABLED))


def get_considered_sources_disclosure(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether considered sources disclosure is enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("considered_sources_disclosure", DEFAULT_CONSIDERED_SOURCES_DISCLOSURE))


def get_auto_render_mermaid_diagrams(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether auto-rendering Mermaid diagrams is enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("auto_render_mermaid_diagrams", DEFAULT_AUTO_RENDER_MERMAID_DIAGRAMS))


def get_auto_spawn_backend(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether the Zotero bootstrap may auto-provision/spawn a managed backend."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("auto_spawn_backend")
    return bool(val if val is not None else DEFAULT_AUTO_SPAWN_BACKEND)


def is_mcp_server_enabled(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether local MCP server access is enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("enable_mcp_server", DEFAULT_ENABLE_MCP_SERVER))


def get_citation_verification_enabled(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether citation verification via decision models is enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("citation_verification_enabled", DEFAULT_CITATION_VERIFICATION_ENABLED))


def get_scholar_polite_email(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return configured email for scholarly discovery APIs polite pool."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("scholar_polite_email", DEFAULT_SCHOLAR_POLITE_EMAIL)
    return str(val).strip() if val else ""


def get_user_research_profile(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Return the configured user research profile (stripped), or ``""`` if unset."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("user_research_profile", DEFAULT_USER_RESEARCH_PROFILE)
    return str(val).strip() if val else ""


def is_adaptive_compaction_enabled(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether adaptive context compaction is enabled from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("enable_adaptive_compaction")
    return bool(val if val is not None else DEFAULT_ENABLE_ADAPTIVE_COMPACTION)


def is_context_compaction_enabled(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Deprecated alias for is_adaptive_compaction_enabled."""
    return is_adaptive_compaction_enabled(zotero_data_dir, cfg=cfg)


def is_local_decision_model(model_name: str | None) -> bool:
    """Check if model name indicates local Laya decision model."""
    cleaned = (model_name or "").strip().lower()
    return (
        cleaned in ("laya", "local", "local_laya", "local/laya")
        or cleaned.startswith("local:")
        or cleaned.startswith("local/")
    )


def get_compaction_config(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return adaptive context compaction settings from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return {
        "enable_adaptive_compaction": bool(
            cfg.get("enable_adaptive_compaction")
            if cfg.get("enable_adaptive_compaction") is not None
            else DEFAULT_ENABLE_ADAPTIVE_COMPACTION
        ),
        "adaptive_compaction_retention_turns": int(
            cfg.get("adaptive_compaction_retention_turns")
            if cfg.get("adaptive_compaction_retention_turns") is not None
            else DEFAULT_ADAPTIVE_COMPACTION_RETENTION_TURNS
        ),
        "adaptive_compaction_min_turns": int(
            cfg.get("adaptive_compaction_min_turns")
            if cfg.get("adaptive_compaction_min_turns") is not None
            else DEFAULT_ADAPTIVE_COMPACTION_MIN_TURNS
        ),
        "adaptive_compaction_token_floor": int(
            cfg.get("adaptive_compaction_token_floor")
            if cfg.get("adaptive_compaction_token_floor") is not None
            else DEFAULT_ADAPTIVE_COMPACTION_TOKEN_FLOOR
        ),
        "adaptive_compaction_token_ceiling": int(
            cfg.get("adaptive_compaction_token_ceiling")
            if cfg.get("adaptive_compaction_token_ceiling") is not None
            else DEFAULT_ADAPTIVE_COMPACTION_TOKEN_CEILING
        ),
        "adaptive_compaction_threshold": float(
            cfg.get("adaptive_compaction_threshold")
            if cfg.get("adaptive_compaction_threshold") is not None
            else DEFAULT_ADAPTIVE_COMPACTION_THRESHOLD
        ),
        "adaptive_compaction_decision_model": str(
            cfg.get("adaptive_compaction_decision_model")
            if cfg.get("adaptive_compaction_decision_model") is not None
            else (cfg.get("adaptive_decision_model") or DEFAULT_ADAPTIVE_COMPACTION_DECISION_MODEL)
        ),
        "compaction_summarizer_model": str(
            cfg.get("compaction_summarizer_model")
            if cfg.get("compaction_summarizer_model") is not None
            else DEFAULT_COMPACTION_SUMMARIZER_MODEL
        ).strip(),
    }


def get_max_saved_sessions(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> int | None:
    """Return maximum number of saved sessions to retain, or None if disabled/unlimited."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("max_saved_sessions", DEFAULT_MAX_SAVED_SESSIONS)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return DEFAULT_MAX_SAVED_SESSIONS


def get_session_retention_days(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> int | None:
    """Return maximum age in days for session retention, or None if disabled/unlimited."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("session_retention_days", DEFAULT_SESSION_RETENTION_DAYS)
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return DEFAULT_SESSION_RETENTION_DAYS


def get_session_auto_vacuum_on_startup(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether session database auto-vacuum on startup is enabled."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("session_db_auto_vacuum_on_startup", DEFAULT_SESSION_DB_AUTO_VACUUM_ON_STARTUP))


def get_prune_empty_sessions(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> bool:
    """Return whether automatic pruning of abandoned empty sessions is enabled."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return bool(cfg.get("prune_empty_sessions", DEFAULT_PRUNE_EMPTY_SESSIONS))


def get_session_maintenance_config(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return session database retention and maintenance settings dictionary from zocrates_config.json."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    return {
        "max_saved_sessions": cfg.get("max_saved_sessions", DEFAULT_MAX_SAVED_SESSIONS),
        "session_retention_days": cfg.get("session_retention_days", DEFAULT_SESSION_RETENTION_DAYS),
        "session_db_auto_vacuum_on_startup": bool(
            cfg.get("session_db_auto_vacuum_on_startup", DEFAULT_SESSION_DB_AUTO_VACUUM_ON_STARTUP)
        ),
        "prune_empty_sessions": bool(
            cfg.get("prune_empty_sessions", DEFAULT_PRUNE_EMPTY_SESSIONS)
        ),
    }


# Functional aliases for config access and updates
get_config = load_zocrates_config
load_or_create_config = load_zocrates_config
update_config = save_zocrates_config


def get_backend_port(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> int | None:
    """Return configured backend port from environment or zocrates_config.json, or None if dynamic.

    Delegates to the shared env/config parse helpers used by
    ``_resolve_backend_port`` (env BACKEND_PORT > config "backend_port")
    instead of duplicating the parsing logic. ``None`` means the dynamic
    deterministic offset tier applies (see ``compute_deterministic_port``).
    """
    env_port = _parse_env_backend_port()
    if env_port is not None:
        return env_port
    if cfg is not None:
        port = cfg.get("backend_port")
        if port is None:
            return None
        try:
            port_int = int(port)
            return port_int if port_int > 0 else None
        except (ValueError, TypeError):
            return None
    return _parse_config_backend_port(zotero_data_dir)


def get_conversion_timeout_seconds(
    zotero_data_dir: Path | None = None,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Return the timeout in seconds for document conversion operations.

    Checks ``CONVERSION_TIMEOUT_SECONDS`` environment variable first;
    if unset, falls back to ``conversion_timeout_seconds`` in ``zocrates_config.json``
    (defaulting to 360s to accommodate CPU-based layout inference in Docling).
    """
    env_val = os.getenv("CONVERSION_TIMEOUT_SECONDS")
    if env_val is not None:
        try:
            return max(30, int(env_val))
        except ValueError:
            pass
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    try:
        return max(
            30,
            int(
                cfg.get(
                    "conversion_timeout_seconds",
                    DEFAULT_CONVERSION_TIMEOUT_SECONDS,
                )
            ),
        )
    except (ValueError, TypeError):
        return DEFAULT_CONVERSION_TIMEOUT_SECONDS


def get_ollama_num_ctx(
    zotero_data_dir: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> int:
    """Return configured Ollama context window size (num_ctx), defaulting to 16384."""
    if cfg is None:
        cfg = load_zocrates_config(zotero_data_dir)
    val = cfg.get("ollama_num_ctx")
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return DEFAULT_OLLAMA_NUM_CTX


def get_collection_name(model_name: str, dimension: int) -> str:
    """
    Generate a sanitized, versioned ChromaDB collection name from model and dimension.

    Replaces any non-alphanumeric characters with underscores, collapses
    consecutive underscores, and appends the dimension suffix.

    Example::

        get_collection_name("jinaai/jina-embeddings-v5-text-nano", 768)
        # -> 'zotero_library_jinaai_jina_embeddings_v5_text_nano_768d'

    Args:
        model_name: Hugging Face or Gemini model identifier.
        dimension: Embedding vector dimension.

    Returns:
        A lowercase, underscore-delimited collection name string.
    """
    sanitized_model = re.sub(r"[^a-zA-Z0-9_]", "_", model_name.strip())
    sanitized_model = re.sub(r"_+", "_", sanitized_model).strip("_")
    return f"zotero_library_{sanitized_model}_{dimension}d"


# ---------------------------------------------------------------------------
# LLM & Embedding Defaults
# ---------------------------------------------------------------------------

GOOGLE_CLOUD_LOCATION: str = os.getenv("GOOGLE_CLOUD_LOCATION", "global")

# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------
CHUNK_MAX_CHARS: int = int(os.getenv("CHUNK_MAX_CHARS", "4000"))
CHUNK_OVERLAP_CHARS: int = int(os.getenv("CHUNK_OVERLAP_CHARS", "200"))

# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------
RETRIEVAL_TOP_K: int = int(os.getenv("RETRIEVAL_TOP_K", "20"))
RETRIEVAL_FINAL_K: int = int(os.getenv("RETRIEVAL_FINAL_K", "8"))
