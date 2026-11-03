"""Process-wide ADK session/artifact services shared by every serving surface."""

import functools
import os
from pathlib import Path

from google.adk.artifacts import BaseArtifactService, GcsArtifactService, InMemoryArtifactService
from google.adk.sessions.base_session_service import BaseSessionService

_session_service = None


def get_session_service(zotero_data_dir: Path | str | None = None) -> BaseSessionService:
    """Return the process-wide ADK session service, creating it if necessary.

    Resolution priority:
      1. If ``zotero_data_dir`` is provided, create a
         ``DatabaseSessionService`` backed by SQLite at
         ``<zotero_data_dir>/zocrates-data/sessions.db`` and store it as
         the module-level singleton.
      2. If the singleton is already set, return it unchanged.
      3. If ``GOOGLE_CLOUD_AGENT_ENGINE_ID`` is set, return a
         ``VertexAiSessionService``.
      4. Otherwise, return an ``InMemorySessionService``.

    Args:
        zotero_data_dir: Path to the Zotero data directory. When provided,
            a new persistent session service is created and replaces any
            existing singleton.

    Returns:
        The active ADK session service instance.
    """
    global _session_service

    if zotero_data_dir is not None:
        path = Path(zotero_data_dir).expanduser().resolve()
        db_path = path / "zocrates-data" / "sessions.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        from google.adk.sessions.database_session_service import DatabaseSessionService

        _session_service = DatabaseSessionService(
            db_url=f"sqlite+aiosqlite:///{db_path}"
        )
        return _session_service

    if _session_service is not None:
        return _session_service

    if agent_engine_id := os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_ID"):
        from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService

        _session_service = VertexAiSessionService(
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            # Runtime-injected agent-engine region, not GOOGLE_CLOUD_LOCATION
            # (which agent.py pins to "global").
            location=os.environ.get("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION")
            or os.environ.get("GOOGLE_CLOUD_LOCATION"),
            agent_engine_id=agent_engine_id,
        )
        return _session_service

    from google.adk.sessions.in_memory_session_service import InMemorySessionService

    _session_service = InMemorySessionService()
    return _session_service


def reset_session_service() -> None:
    """Reset the cached session service instance (useful for testing or re-registration)."""
    global _session_service
    _session_service = None


@functools.cache
def get_artifact_service() -> BaseArtifactService:
    """Return the process-wide ADK artifact service (cached via ``functools.cache``).

    Uses ``GcsArtifactService`` when ``LOGS_BUCKET_NAME`` is set in the
    environment, otherwise falls back to ``InMemoryArtifactService``.

    Returns:
        The active ADK artifact service instance.
    """
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


