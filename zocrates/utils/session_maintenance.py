"""Session database maintenance, retention pruning, and space reclamation."""

import asyncio
import contextlib
import logging
import shutil
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from google.adk.sessions.database_session_service import DatabaseSessionService

logger = logging.getLogger(__name__)

EMPTY_SESSION_MIN_AGE_HOURS = 24.0
VACUUM_FREELIST_RATIO_THRESHOLD = 0.25
VACUUM_MIN_DB_SIZE_BYTES = 30 * 1024 * 1024  # 30 MB floor


def get_sqlite_freelist_stats(db_path: Path) -> tuple[int, int, int]:
    """Return (freelist_count, page_count, page_size) from SQLite header.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Tuple of (freelist_count, page_count, page_size).
    """
    if not db_path.exists():
        return 0, 0, 4096
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        ) as conn:
            cursor = conn.cursor()
            freelist = cursor.execute("PRAGMA freelist_count;").fetchone()[0]
            pages = cursor.execute("PRAGMA page_count;").fetchone()[0]
            page_size = cursor.execute("PRAGMA page_size;").fetchone()[0]
            return int(freelist), int(pages), int(page_size)
    except Exception as exc:
        logger.debug("Failed to read SQLite freelist stats from %s: %s", db_path.name, exc)
        return 0, 0, 4096


def _get_sort_key(s: Any) -> datetime:
    """Extract a timezone-aware UTC datetime for session sorting and age checks.

    Handles float POSIX timestamp, create_time, datetime instances, and ISO/SQLite strings.
    """
    dt = None
    if isinstance(s, dict):
        dt = (
            s.get("last_update_time")
            or s.get("updated_at")
            or s.get("create_time")
            or s.get("created_at")
        )
    else:
        dt = getattr(s, "last_update_time", None)
        if dt is None:
            dt = getattr(s, "updated_at", None)
        if dt is None:
            dt = getattr(s, "create_time", None)
        if dt is None:
            dt = getattr(s, "created_at", None)

    if isinstance(dt, (int, float)):
        try:
            return datetime.fromtimestamp(float(dt), tz=timezone.utc)
        except Exception:
            pass
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if isinstance(dt, str):
        try:
            clean_dt = dt.replace(" ", "T")
            parsed = datetime.fromisoformat(clean_dt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


async def prune_abandoned_empty_sessions(
    session_service: DatabaseSessionService,
    db_path: Path,
    app_name: str = "zocrates",
    user_id: str = "default_user",
    min_age_hours: float = EMPTY_SESSION_MIN_AGE_HOURS,
    active_session_ids: set[str] | None = None,
) -> int:
    """Prune sessions with zero events that are older than min_age_hours.

    Args:
        session_service: ADK DatabaseSessionService instance.
        db_path: Path to sessions.db SQLite database.
        app_name: ADK app name identifier.
        user_id: ADK user identifier.
        min_age_hours: Minimum age in hours before an empty session is eligible for deletion.
        active_session_ids: Set of session IDs currently streaming or active (protected from deletion).

    Returns:
        Count of successfully deleted empty sessions.
    """
    if not db_path.exists():
        return 0

    active_ids = active_session_ids or set()
    cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=min_age_hours)

    session_list = await session_service.list_sessions(
        app_name=app_name, user_id=user_id
    )
    sessions = getattr(session_list, "sessions", []) or []
    if not sessions:
        return 0

    sessions_with_events: set[str] = set()
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        ) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT DISTINCT session_id FROM events WHERE app_name = ? AND user_id = ?",
                (app_name, user_id),
            )
            sessions_with_events = {row[0] for row in cursor.fetchall()}
    except Exception as exc:
        logger.debug("Failed to query events table for empty session census: %s", exc)
        return 0

    deleted = 0
    for sess in sessions:
        if sess.id in active_ids:
            continue
        if sess.id in sessions_with_events:
            continue
        sess_dt = _get_sort_key(sess)
        if sess_dt <= cutoff_dt:
            try:
                await session_service.delete_session(
                    app_name=app_name, user_id=user_id, session_id=sess.id
                )
                deleted += 1
            except Exception as exc:
                logger.warning("Failed to delete abandoned session %s: %s", sess.id, exc)

    if deleted > 0:
        logger.info(
            "Pruned %d abandoned empty session(s) older than %gh",
            deleted,
            min_age_hours,
        )
    return deleted


async def prune_retention_sessions(
    session_service: DatabaseSessionService,
    app_name: str = "zocrates",
    user_id: str = "default_user",
    retention_days: int | None = 180,
    max_saved_sessions: int | None = 100,
    active_session_ids: set[str] | None = None,
) -> int:
    """Prune sessions exceeding retention age or maximum session cap.

    Args:
        session_service: ADK DatabaseSessionService instance.
        app_name: ADK app name identifier.
        user_id: ADK user identifier.
        retention_days: Age in days past which sessions are deleted (0 or None disables).
        max_saved_sessions: Max count of sessions to retain (0 or None disables).
        active_session_ids: Set of session IDs currently streaming or active (protected from deletion).

    Returns:
        Count of successfully deleted expired/excess sessions.
    """
    active_ids = active_session_ids or set()
    session_list = await session_service.list_sessions(
        app_name=app_name, user_id=user_id
    )
    sessions = getattr(session_list, "sessions", []) or []
    if not sessions:
        return 0

    sorted_sessions = sorted(sessions, key=_get_sort_key)
    now = datetime.now(timezone.utc)
    to_delete_ids: set[str] = set()

    # 1. Age-based expiration (skip actively streaming sessions)
    if retention_days and retention_days > 0:
        cutoff = now - timedelta(days=retention_days)
        for s in sorted_sessions:
            if s.id not in active_ids and _get_sort_key(s) < cutoff:
                to_delete_ids.add(s.id)

    # 2. Max session count cap (evaluated against remaining live sessions)
    remaining_sessions = [s for s in sorted_sessions if s.id not in to_delete_ids]
    if max_saved_sessions and max_saved_sessions > 0:
        if len(remaining_sessions) > max_saved_sessions:
            excess_needed = len(remaining_sessions) - max_saved_sessions
            for s in remaining_sessions:
                if excess_needed <= 0:
                    break
                if s.id not in active_ids:
                    to_delete_ids.add(s.id)
                    excess_needed -= 1

    deleted = 0
    for sid in to_delete_ids:
        try:
            await session_service.delete_session(
                app_name=app_name, user_id=user_id, session_id=sid
            )
            deleted += 1
        except Exception as exc:
            logger.warning("Failed to delete expired session %s: %s", sid, exc)

    if deleted > 0:
        logger.info(
            "Pruned %d session(s) matching retention policy (retention_days=%s, max_saved=%s)",
            deleted,
            retention_days,
            max_saved_sessions,
        )
    return deleted


def vacuum_session_database(db_path: Path) -> tuple[bool, int, int]:
    """Execute VACUUM on sessions.db using a dedicated fail-open raw connection.

    Checks disk usage beforehand requiring at least 1.5x DB size free.

    Args:
        db_path: Path to sessions.db.

    Returns:
        Tuple of (success: bool, before_bytes: int, after_bytes: int).
    """
    if not db_path.exists():
        return False, 0, 0

    file_size_before = db_path.stat().st_size
    try:
        total, used, free = shutil.disk_usage(db_path.parent)
        if free < int(file_size_before * 1.5):
            logger.warning(
                "Skipping VACUUM on %s: insufficient disk space (need ~%d MB, have %d MB free)",
                db_path.name,
                int(file_size_before * 1.5 / 1024 / 1024),
                int(free / 1024 / 1024),
            )
            return False, file_size_before, file_size_before

        start_t = time.perf_counter()
        with contextlib.closing(
            sqlite3.connect(str(db_path), timeout=5.0)
        ) as conn:
            conn.execute("VACUUM;")
        elapsed_ms = (time.perf_counter() - start_t) * 1000.0
        file_size_after = db_path.stat().st_size
        logger.info(
            "Successfully vacuumed %s in %.1fms (reclaimed %.2f MB, new size: %.2f MB)",
            db_path.name,
            elapsed_ms,
            (file_size_before - file_size_after) / 1024 / 1024,
            file_size_after / 1024 / 1024,
        )
        return True, file_size_before, file_size_after
    except Exception as exc:
        logger.warning(
            "Failed to vacuum session database %s (fail-open): %s", db_path.name, exc
        )
        return False, file_size_before, file_size_before


async def get_session_storage_stats(
    db_path: Path,
    session_service: DatabaseSessionService | None = None,
    app_name: str = "zocrates",
    user_id: str = "default_user",
) -> dict[str, Any]:
    """Return physical database size and session count metrics for sessions.db.

    Args:
        db_path: Path to sessions.db.
        session_service: Optional ADK DatabaseSessionService to count sessions.
        app_name: ADK app name identifier.
        user_id: ADK user identifier.

    Returns:
        Dict with keys size_bytes, session_count, freelist_pages, total_pages, page_size.
    """
    size_bytes = db_path.stat().st_size if db_path.exists() else 0
    session_count = 0
    if session_service is not None:
        try:
            session_list = await session_service.list_sessions(
                app_name=app_name, user_id=user_id
            )
            sessions = getattr(session_list, "sessions", []) or []
            session_count = len(sessions)
        except Exception as exc:
            logger.debug("Failed to list sessions via session_service for stats: %s", exc)

    if session_count == 0 and db_path.exists():
        try:
            with contextlib.closing(
                sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
            ) as conn:
                cursor = conn.cursor()
                row = cursor.execute(
                    "SELECT COUNT(*) FROM sessions WHERE app_name = ? AND user_id = ?",
                    (app_name, user_id),
                ).fetchone()
                if row:
                    session_count = int(row[0])
        except Exception:
            pass

    freelist, pages, page_size = get_sqlite_freelist_stats(db_path)
    return {
        "size_bytes": size_bytes,
        "session_count": session_count,
        "freelist_pages": freelist,
        "total_pages": pages,
        "page_size": page_size,
    }


async def run_session_database_maintenance(
    zotero_data_dir: Path | str,
    session_service: DatabaseSessionService,
    config: dict[str, Any] | None = None,
    active_session_ids: set[str] | None = None,
    app_name: str = "zocrates",
    user_id: str = "default_user",
) -> dict[str, Any]:
    """Execute session database maintenance cycle with concurrency and fail-open guards.

    Args:
        zotero_data_dir: Root Zotero data directory.
        session_service: ADK DatabaseSessionService instance.
        config: Maintenance configuration dict from zocrates_config.json.
        active_session_ids: Set of session IDs actively streaming turns.
        app_name: ADK app name identifier.
        user_id: ADK user identifier.

    Returns:
        Summary dict containing pruned counts, vacuum execution status, and reclaimed bytes.
    """
    cfg = config or {}
    zotero_path = Path(zotero_data_dir).expanduser().resolve()
    db_path = zotero_path / "zocrates-data" / "sessions.db"
    active_ids = active_session_ids or set()

    initial_bytes = db_path.stat().st_size if db_path.exists() else 0
    results: dict[str, Any] = {
        "empty_sessions_pruned": 0,
        "retention_sessions_pruned": 0,
        "vacuum_executed": False,
        "initial_size_bytes": initial_bytes,
        "final_size_bytes": initial_bytes,
        "reclaimed_bytes": 0,
        "skipped_reason": None,
    }

    if not db_path.exists():
        return results

    try:
        # 1. Prune empty abandoned sessions (skipping active streams)
        if cfg.get("prune_empty_sessions", True):
            results["empty_sessions_pruned"] = await prune_abandoned_empty_sessions(
                session_service=session_service,
                db_path=db_path,
                app_name=app_name,
                user_id=user_id,
                active_session_ids=active_ids,
            )

        # 2. Prune old / excess sessions (skipping active streams)
        max_saved = cfg.get("max_saved_sessions", 100)
        retention_days = cfg.get("session_retention_days", 180)
        if (max_saved and max_saved > 0) or (retention_days and retention_days > 0):
            results["retention_sessions_pruned"] = await prune_retention_sessions(
                session_service=session_service,
                app_name=app_name,
                user_id=user_id,
                retention_days=retention_days,
                max_saved_sessions=max_saved,
                active_session_ids=active_ids,
            )

        # 3. Check freelist and conditionally vacuum (in worker thread to avoid blocking event loop)
        if cfg.get("session_db_auto_vacuum_on_startup", True):
            if active_ids:
                results["skipped_reason"] = "chat_in_progress"
                results["final_size_bytes"] = db_path.stat().st_size
                logger.info(
                    "Deferred VACUUM on %s because active chat is in progress",
                    db_path.name,
                )
            else:
                total_deleted = results["empty_sessions_pruned"] + results["retention_sessions_pruned"]
                freelist, pages, page_size = get_sqlite_freelist_stats(db_path)
                file_size = db_path.stat().st_size

                should_vacuum = (
                    (total_deleted > 0 and pages > 0 and (freelist / pages) >= VACUUM_FREELIST_RATIO_THRESHOLD)
                    or
                    (file_size >= VACUUM_MIN_DB_SIZE_BYTES and pages > 0 and (freelist / pages) >= VACUUM_FREELIST_RATIO_THRESHOLD)
                )

                if should_vacuum:
                    vac_ok, b_bytes, a_bytes = await asyncio.to_thread(
                        vacuum_session_database, db_path
                    )
                    results["vacuum_executed"] = vac_ok
                    results["final_size_bytes"] = a_bytes
                    results["reclaimed_bytes"] = max(0, b_bytes - a_bytes)
                else:
                    results["final_size_bytes"] = file_size
        else:
            results["final_size_bytes"] = db_path.stat().st_size
    except Exception as exc:
        logger.warning("Error during session database maintenance (fail-open): %s", exc)
        results["skipped_reason"] = f"error: {exc}"

    return results
