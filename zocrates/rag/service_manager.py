"""Service manager: holds the global/singleton LibrarySyncer instance."""

import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zocrates.rag.library_syncer import LibrarySyncer

logger = logging.getLogger(__name__)

_init_lock = threading.Lock()
_syncer: "LibrarySyncer | None" = None


def initialize_services(zotero_data_dir: Path) -> "LibrarySyncer":
    """Initialize the LibrarySyncer and database connections.
    
    Thread-safe: uses a lock to prevent concurrent initialization.
    Tears down the previous instance before creating a new one.
    """
    global _syncer
    with _init_lock:
        if _syncer is not None:
            logger.info("Tearing down previous LibrarySyncer instance.")
            try:
                _syncer.close()
            except Exception as exc:
                logger.warning("Error closing previous syncer: %s", exc)

        sqlite_path = zotero_data_dir / "zotero.sqlite"
        if not sqlite_path.exists():
            raise FileNotFoundError(f"zotero.sqlite not found at {sqlite_path}")

        logger.info(
            "Initializing Zocrates services with data directory: %s",
            zotero_data_dir,
        )
        from zocrates.utils.services import get_session_service
        get_session_service(zotero_data_dir)

        from zocrates.rag.library_syncer import LibrarySyncer

        _syncer = LibrarySyncer(zotero_data_dir=zotero_data_dir, sqlite_path=sqlite_path)

        # Ensure default zocrates_config.json is persisted if not present
        import copy
        from zocrates.config import (
            DEFAULT_OVERRIDE_AGENT_MODELS,
            DEFAULT_SEARCH_HARNESS,
            get_config_path,
            save_zocrates_config,
        )
        config_path = get_config_path(zotero_data_dir)
        if not config_path.exists():
            save_zocrates_config(
                {
                    "document_converter_id": "markitdown",
                    "document_converter_mode": "local",
                    "embedding_provider": "local",
                    "embedding_model": "jinaai/jina-embeddings-v5-text-nano",
                    "force_cpu": False,
                    "override_agent_models": copy.deepcopy(
                        DEFAULT_OVERRIDE_AGENT_MODELS
                    ),
                    "search_harness": copy.deepcopy(DEFAULT_SEARCH_HARNESS),
                },
                zotero_data_dir,
            )

        return _syncer


def get_syncer() -> "LibrarySyncer":
    """Get the active LibrarySyncer instance. Raises RuntimeError if not registered."""
    if _syncer is None:
        raise RuntimeError(
            "Zocrates backend services have not been registered/initialized yet. "
            "Please register your Zotero data directory first."
        )
    return _syncer
