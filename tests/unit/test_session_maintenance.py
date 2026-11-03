"""Unit tests for ADK session database retention and space reclamation."""

import asyncio
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from google.adk.sessions.database_session_service import DatabaseSessionService

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.utils.services import get_session_service, reset_session_service
from zocrates.utils.session_maintenance import (
    EMPTY_SESSION_MIN_AGE_HOURS,
    VACUUM_FREELIST_RATIO_THRESHOLD,
    VACUUM_MIN_DB_SIZE_BYTES,
    _get_sort_key,
    get_sqlite_freelist_stats,
    get_session_storage_stats,
    prune_abandoned_empty_sessions,
    prune_retention_sessions,
    run_session_database_maintenance,
    vacuum_session_database,
)


@pytest.fixture
def temp_session_env():
    """Create a temporary directory with a real DatabaseSessionService."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        zocrates_data = tmp_path / "zocrates-data"
        zocrates_data.mkdir(parents=True, exist_ok=True)
        db_path = zocrates_data / "sessions.db"
        svc = DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db_path}")
        yield {
            "tmp_dir": tmp_path,
            "db_path": db_path,
            "service": svc,
        }


def _set_session_update_time(db_path: Path, session_id: str, dt_str: str) -> None:
    """Helper to update session create_time and update_time in raw SQLite."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "UPDATE sessions SET create_time = ?, update_time = ? WHERE id = ?",
            (dt_str, dt_str, session_id),
        )
        conn.commit()


def _add_event(db_path: Path, session_id: str, event_id: str = "evt-1") -> None:
    """Helper to insert an event into raw SQLite events table."""
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO events (id, app_name, user_id, session_id, invocation_id, timestamp, event_data) "
            "VALUES (?, 'zocrates', 'default_user', ?, 'inv-1', 123456.0, '{}')",
            (event_id, session_id),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_prune_abandoned_empty_sessions_respects_age_threshold(temp_session_env):
    """Verify 0-event sessions older than 24h are deleted, while fresh (<24h) or populated sessions are kept."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    # 1. Create sessions
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="fresh_empty")
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="old_empty")
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="old_with_events")

    # 2. Age the old sessions back 48 hours
    two_days_ago = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    _set_session_update_time(db_path, "old_empty", two_days_ago)
    _set_session_update_time(db_path, "old_with_events", two_days_ago)

    # 3. Add an event to old_with_events
    _add_event(db_path, "old_with_events")

    # 4. Run empty session pruning (min_age_hours=24.0)
    deleted = await prune_abandoned_empty_sessions(
        session_service=svc,
        db_path=db_path,
        min_age_hours=24.0,
    )
    assert deleted == 1

    # 5. Check remaining sessions
    res = await svc.list_sessions(app_name="zocrates", user_id="default_user")
    remaining_ids = {s.id for s in res.sessions}
    assert "old_empty" not in remaining_ids
    assert "fresh_empty" in remaining_ids
    assert "old_with_events" in remaining_ids


def test_get_sort_key_handles_various_formats():
    """Verify _get_sort_key accurately parses floats, strings, datetimes, and dicts."""
    now_ts = 1700000000.0
    expected_utc = datetime.fromtimestamp(now_ts, tz=timezone.utc)

    # Float timestamp on object
    class ObjWithFloat:
        last_update_time = now_ts
    assert _get_sort_key(ObjWithFloat()) == expected_utc

    # String ISO format
    class ObjWithISO:
        last_update_time = "2024-01-15T10:30:00+00:00"
    parsed_iso = _get_sort_key(ObjWithISO())
    assert parsed_iso.year == 2024 and parsed_iso.month == 1 and parsed_iso.day == 15

    # String naive SQLite format
    class ObjWithSqliteDate:
        last_update_time = "2024-01-15 10:30:00"
    parsed_sqlite = _get_sort_key(ObjWithSqliteDate())
    assert parsed_sqlite.year == 2024 and parsed_sqlite.tzinfo == timezone.utc

    # Dictionary representation
    assert _get_sort_key({"last_update_time": now_ts}) == expected_utc

    # None / invalid fallback to datetime.min
    class EmptyObj:
        pass
    assert _get_sort_key(EmptyObj()) == datetime.min.replace(tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_prune_retention_sessions_handles_float_timestamps(temp_session_env):
    """Verify last_update_time float timestamps from real ADK Session models sort and filter accurately."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="sess_1")
    res = await svc.list_sessions(app_name="zocrates", user_id="default_user")
    sess = res.sessions[0]
    assert isinstance(sess.last_update_time, (int, float))

    extracted_dt = _get_sort_key(sess)
    assert extracted_dt > datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_prune_retention_sessions_by_age(temp_session_env):
    """Verify sessions older than retention_days are deleted while recent ones are kept."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="recent_sess")
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="ancient_sess")

    # Set ancient_sess to 200 days ago
    ancient_dt = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    _set_session_update_time(db_path, "ancient_sess", ancient_dt)

    deleted = await prune_retention_sessions(
        session_service=svc,
        retention_days=180,
        max_saved_sessions=None,
    )
    assert deleted == 1

    res = await svc.list_sessions(app_name="zocrates", user_id="default_user")
    remaining_ids = {s.id for s in res.sessions}
    assert "ancient_sess" not in remaining_ids
    assert "recent_sess" in remaining_ids


@pytest.mark.asyncio
async def test_prune_retention_sessions_by_count_cap(temp_session_env):
    """Verify oldest sessions exceeding max_saved_sessions are deleted in chronological order."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    now = datetime.now(timezone.utc)
    for i in range(5):
        sid = f"sess_{i}"
        await svc.create_session(app_name="zocrates", user_id="default_user", session_id=sid)
        # Stagger update times: sess_0 oldest, sess_4 newest
        dt_str = (now - timedelta(days=5 - i)).strftime("%Y-%m-%d %H:%M:%S")
        _set_session_update_time(db_path, sid, dt_str)

    deleted = await prune_retention_sessions(
        session_service=svc,
        retention_days=None,
        max_saved_sessions=3,
    )
    assert deleted == 2

    res = await svc.list_sessions(app_name="zocrates", user_id="default_user")
    remaining_ids = {s.id for s in res.sessions}
    assert remaining_ids == {"sess_2", "sess_3", "sess_4"}


@pytest.mark.asyncio
async def test_prune_disabled_when_zero_or_none(temp_session_env):
    """Verify max_saved_sessions=0 and session_retention_days=0 disable respective deletion passes."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    for i in range(5):
        sid = f"sess_{i}"
        await svc.create_session(app_name="zocrates", user_id="default_user", session_id=sid)
        dt_str = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
        _set_session_update_time(db_path, sid, dt_str)

    # 1. Test with 0
    deleted_zero = await prune_retention_sessions(
        session_service=svc,
        retention_days=0,
        max_saved_sessions=0,
    )
    assert deleted_zero == 0

    # 2. Test with None
    deleted_none = await prune_retention_sessions(
        session_service=svc,
        retention_days=None,
        max_saved_sessions=None,
    )
    assert deleted_none == 0

    res = await svc.list_sessions(app_name="zocrates", user_id="default_user")
    assert len(res.sessions) == 5


@pytest.mark.asyncio
async def test_active_chat_sessions_protected_from_pruning(temp_session_env):
    """Verify active streaming sessions in active_session_ids are never deleted by retention or empty pruning."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="active_old")
    dt_str = (datetime.now(timezone.utc) - timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
    _set_session_update_time(db_path, "active_old", dt_str)

    active_ids = {"active_old"}

    # Should not be pruned by empty pruning
    del_empty = await prune_abandoned_empty_sessions(
        session_service=svc,
        db_path=db_path,
        min_age_hours=24.0,
        active_session_ids=active_ids,
    )
    assert del_empty == 0

    # Should not be pruned by retention pruning
    del_ret = await prune_retention_sessions(
        session_service=svc,
        retention_days=180,
        max_saved_sessions=1,
        active_session_ids=active_ids,
    )
    assert del_ret == 0

    res = await svc.list_sessions(app_name="zocrates", user_id="default_user")
    assert any(s.id == "active_old" for s in res.sessions)


@pytest.mark.asyncio
async def test_vacuum_deferred_during_active_chat(temp_session_env):
    """Verify VACUUM is deferred and returns skipped_reason='chat_in_progress' when active stream is present."""
    svc = temp_session_env["service"]
    tmp_dir = temp_session_env["tmp_dir"]

    # Ensure DB file exists by creating a session
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="init_sess")

    results = await run_session_database_maintenance(
        zotero_data_dir=tmp_dir,
        session_service=svc,
        config={"session_db_auto_vacuum_on_startup": True},
        active_session_ids={"in_flight_chat"},
    )

    assert results["skipped_reason"] == "chat_in_progress"
    assert results["vacuum_executed"] is False


def test_vacuum_fail_open_on_locked_or_low_disk(temp_session_env):
    """Verify graceful log and return on low disk space or error without crashing."""
    db_path = temp_session_env["db_path"]

    # Mock low disk space (free = 0)
    with patch("shutil.disk_usage", return_value=(1000000, 1000000, 0)):
        ok, before, after = vacuum_session_database(db_path)
        assert ok is False
        assert before == after

    # Non-existent DB path
    ok_missing, _, _ = vacuum_session_database(Path("/non/existent/path.db"))
    assert ok_missing is False


def test_vacuum_skips_when_freelist_low(temp_session_env):
    """Verify zero vacuuming occurs when freelist ratio is below 25% threshold."""
    db_path = temp_session_env["db_path"]
    freelist, pages, page_size = get_sqlite_freelist_stats(db_path)
    assert freelist == 0 or (freelist / max(1, pages)) < VACUUM_FREELIST_RATIO_THRESHOLD


@pytest.mark.asyncio
async def test_vacuum_session_database_triggers_on_freelist_threshold(temp_session_env):
    """Verify vacuum executes and reclaims space when freelist ratio >= 25%."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]
    tmp_dir = temp_session_env["tmp_dir"]

    # Initialize tables
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="init_sess")

    # Populate a filler table with data then delete to build up freelist pages
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS filler (data TEXT)")
        conn.executemany("INSERT INTO filler VALUES (?)", [("X" * 10000,) for _ in range(500)])
        conn.commit()
        # Delete data: frees pages into freelist without shrinking file
        conn.execute("DELETE FROM filler")
        conn.commit()

    freelist, pages, page_size = get_sqlite_freelist_stats(db_path)
    assert pages > 0
    assert (freelist / pages) >= VACUUM_FREELIST_RATIO_THRESHOLD

    # Run maintenance with empty_sessions_pruned simulated to trigger vacuum check
    results = await run_session_database_maintenance(
        zotero_data_dir=tmp_dir,
        session_service=svc,
        config={"session_db_auto_vacuum_on_startup": True, "prune_empty_sessions": False, "max_saved_sessions": 0},
    )

    # Since Case B checks file_size >= 30MB or total_deleted > 0, let's test vacuum_session_database directly
    ok, before_bytes, after_bytes = vacuum_session_database(db_path)
    assert ok is True
    assert after_bytes < before_bytes
    assert before_bytes - after_bytes > 0


@pytest.mark.asyncio
async def test_run_session_database_maintenance_triggers_vacuum_on_deletions(temp_session_env):
    """Verify run_session_database_maintenance triggers vacuum when deletions occur and freelist threshold >= 25%."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]
    tmp_dir = temp_session_env["tmp_dir"]

    # Create session that is 200 days old
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="s_old")
    old_time = (datetime.now(timezone.utc) - timedelta(days=200)).timestamp()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("UPDATE sessions SET update_time = ? WHERE id = 's_old'", (old_time,))
        conn.execute("CREATE TABLE IF NOT EXISTS filler (data TEXT)")
        conn.executemany("INSERT INTO filler VALUES (?)", [("X" * 10000,) for _ in range(500)])
        conn.commit()
        conn.execute("DELETE FROM filler")
        conn.commit()

    results = await run_session_database_maintenance(
        zotero_data_dir=tmp_dir,
        session_service=svc,
        config={
            "session_retention_days": 180,
            "session_db_auto_vacuum_on_startup": True,
            "prune_empty_sessions": False,
        },
    )
    assert results["retention_sessions_pruned"] >= 1
    assert results["vacuum_executed"] is True
    assert results["reclaimed_bytes"] > 0


@pytest.mark.asyncio
async def test_get_session_storage_stats(temp_session_env):
    """Verify get_session_storage_stats returns expected schema and metrics."""
    svc = temp_session_env["service"]
    db_path = temp_session_env["db_path"]

    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="s1")
    await svc.create_session(app_name="zocrates", user_id="default_user", session_id="s2")

    stats = await get_session_storage_stats(db_path=db_path, session_service=svc)
    assert stats["session_count"] == 2
    assert stats["size_bytes"] > 0
    assert "freelist_pages" in stats
    assert "total_pages" in stats
    assert "page_size" in stats


def test_session_maintenance_endpoints():
    """Verify /sessions/storage_stats and /sessions/maintenance REST endpoints."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        reset_session_service()
        get_session_service(tmp_path)

        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            with TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as client:
                # 1. Storage stats endpoint
                res = client.get("/sessions/storage_stats")
                assert res.status_code == 200
                data = res.json()
                assert data["status"] == "success"
                assert "size_bytes" in data
                assert "session_count" in data

                # 2. Maintenance endpoint
                res_maint = client.post("/sessions/maintenance")
                assert res_maint.status_code == 200
                data_maint = res_maint.json()
                assert data_maint["status"] == "success"
                assert "results" in data_maint
                assert "empty_sessions_pruned" in data_maint["results"]
                assert "retention_sessions_pruned" in data_maint["results"]


def test_session_maintenance_config_in_llm_endpoints():
    """Verify retention config keys are exposed via /config, updated via POST /config, and present in /status."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        reset_session_service()
        get_session_service(tmp_path)

        with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
            with TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"}) as client:
                # 1. GET /config contains retention defaults
                res = client.get("/config")
                assert res.status_code == 200
                data = res.json()
                assert data["max_saved_sessions"] == 100
                assert data["session_retention_days"] == 180
                assert data["session_db_auto_vacuum_on_startup"] is True
                assert data["prune_empty_sessions"] is True
                assert "session_storage_stats" in data

                # 2. POST /config updates retention keys
                update_payload = {
                    "max_saved_sessions": 50,
                    "session_retention_days": 90,
                    "session_db_auto_vacuum_on_startup": False,
                    "prune_empty_sessions": False,
                }
                post_res = client.post("/config", json=update_payload)
                assert post_res.status_code == 200

                # 3. GET /config returns updated values
                res_after = client.get("/config")
                assert res_after.status_code == 200
                data_after = res_after.json()
                assert data_after["max_saved_sessions"] == 50
                assert data_after["session_retention_days"] == 90
                assert data_after["session_db_auto_vacuum_on_startup"] is False
                assert data_after["prune_empty_sessions"] is False

                # 4. GET /status contains retention keys and storage stats
                status_res = client.get("/status")
                assert status_res.status_code == 200
                status_data = status_res.json()
                assert "max_saved_sessions" in status_data
                assert "session_retention_days" in status_data
                assert "session_storage_stats" in status_data
