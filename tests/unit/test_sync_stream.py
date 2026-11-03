"""Phase 7 Stage A tests: SSE full-library sync job (POST /sync/stream, /sync/abort)
and LibrarySyncer.full_sync progress/cancel/auto-abort support.

Plan references: onboarding_configuration_step_plan.md §6, §8, §10.
"""

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from zocrates import fast_api_app as fa
from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.rag.library_syncer import LibrarySyncer
from tests.unit.test_api_endpoints import parse_sse_events


def _auth_client() -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_auth_token()}"}


@pytest.fixture(autouse=True)
def reset_sync_registry():
    """Ensure a clean single-flight sync job registry around each test."""
    fa._sync_job = None
    yield
    fa._sync_job = None


@pytest.fixture(autouse=True)
def reset_sse_app_status():
    """Reset sse_starlette AppStatus.should_exit_event to prevent event loop mismatch."""
    import sse_starlette.sse

    sse_starlette.sse.AppStatus.should_exit_event = None
    yield
    sse_starlette.sse.AppStatus.should_exit_event = None


def _make_syncer(full_sync_impl) -> MagicMock:
    syncer = MagicMock()
    syncer.full_sync = AsyncMock(side_effect=full_sync_impl)
    return syncer


def _summary(**overrides) -> dict:
    base = {
        "total": 2,
        "processed": 2,
        "skipped": 0,
        "errors": 0,
        "orphans_cleaned": 0,
        "metadata_refreshed": 0,
        "cancelled": False,
        "auto_aborted": False,
    }
    base.update(overrides)
    return base


class StubRequest:
    """Minimal Request stand-in for direct generator-level tests."""

    def __init__(self, disconnected: bool = False) -> None:
        self._disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self._disconnected


# ---------------------------------------------------------------------------
# Endpoint-level lifecycle (TestClient)
# ---------------------------------------------------------------------------
def test_sync_stream_unregistered_returns_400():
    with patch("zocrates.fast_api_app.get_syncer", side_effect=RuntimeError("not registered")):
        client = _auth_client()
        res = client.post("/sync/stream")
    assert res.status_code == 400


def test_sync_stream_full_lifecycle_done_events():
    """Start → throttled progress events → terminal done with counts; registry cleared."""

    async def fake_full_sync(progress_callback=None, cancel_check=None):
        assert callable(progress_callback) and callable(cancel_check)
        assert cancel_check() is False
        progress_callback({
            "stage": "items", "processed": 1, "skipped": 0, "errors": 0,
            "total": 2, "current_title": "Paper A", "elapsed_ms": 5,
        })
        await asyncio.sleep(0.3)  # exceed the 250ms coalescing window
        progress_callback({
            "stage": "items", "processed": 2, "skipped": 0, "errors": 0,
            "total": 2, "current_title": "Paper B", "elapsed_ms": 310,
        })
        return _summary()

    syncer = _make_syncer(fake_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        client = _auth_client()
        res = client.post("/sync/stream")

    assert res.status_code == 200
    events = parse_sse_events(res.text)
    names = [e["event"] for e in events]
    assert names[0] == "sync_start"
    assert events[0]["data"]["attached"] is False
    assert "progress" in names
    assert names[-1] == "done"
    done_data = events[-1]["data"]
    assert done_data["processed"] == 2
    assert done_data["total"] == 2
    assert done_data["stage"] == "done"
    # Coalescing: the two callbacks fired within/around the 250ms window, so at
    # most one intermediate progress event may be emitted per 250ms bucket.
    assert names.count("progress") <= 2
    # Single-flight registry cleared after terminal event
    assert fa._sync_job is None


def test_sync_stream_error_terminal_event():
    async def failing_full_sync(progress_callback=None, cancel_check=None):
        raise RuntimeError("boom during sync")

    syncer = _make_syncer(failing_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        client = _auth_client()
        res = client.post("/sync/stream")

    events = parse_sse_events(res.text)
    assert events[-1]["event"] == "error"
    assert "boom during sync" in events[-1]["data"]["error"]
    assert fa._sync_job is None


def test_sync_stream_auto_aborted_terminal_event():
    async def fake_full_sync(progress_callback=None, cancel_check=None):
        return _summary(auto_aborted=True, processed=1, errors=10)

    syncer = _make_syncer(fake_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        client = _auth_client()
        res = client.post("/sync/stream")

    events = parse_sse_events(res.text)
    assert events[-1]["event"] == "auto_aborted"
    assert events[-1]["data"]["processed"] == 1
    assert events[-1]["data"]["errors"] == 10
    assert events[-1]["data"]["stage"] == "auto_aborted"


def test_sync_stream_aborted_terminal_event_on_cooperative_cancel():
    async def fake_full_sync(progress_callback=None, cancel_check=None):
        return _summary(cancelled=True, processed=1)

    syncer = _make_syncer(fake_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        client = _auth_client()
        res = client.post("/sync/stream")

    events = parse_sse_events(res.text)
    assert events[-1]["event"] == "aborted"
    assert events[-1]["data"]["processed"] == 1
    assert events[-1]["data"]["stage"] == "aborted"


def test_sync_abort_endpoint_states():
    # No active job
    client = _auth_client()
    res = client.post("/sync/abort")
    assert res.status_code == 200
    assert res.json() == {"status": "no_active_job"}

    # Active (fake) job → abort_requested + cancel flag set
    fake_job = {
        "task": None,
        "cancel_event": threading.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing", "processed": 1},
        "started": 0.0,
        "terminal": None,
        "active": True,
    }
    fa._sync_job = fake_job
    res = client.post("/sync/abort")
    assert res.json() == {"status": "abort_requested"}
    assert fake_job["cancel_event"].is_set()


def test_sync_endpoint_backward_compatible():
    """POST /sync still invokes full_sync without new kwargs (code preservation)."""
    syncer = _make_syncer(lambda **kwargs: _summary())
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        client = _auth_client()
        res = client.post("/sync", json={})
    assert res.status_code == 200
    assert res.json()["mode"] == "full"
    syncer.full_sync.assert_awaited_once_with()


# ---------------------------------------------------------------------------
# Concurrent-window attach (real concurrent streams over ASGITransport)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_second_stream_attaches_to_running_job():
    """Two concurrent /sync/stream requests share ONE sync job's fan-out."""

    started = asyncio.Event()

    async def slow_full_sync(progress_callback=None, cancel_check=None):
        started.set()
        for i in range(1, 4):
            progress_callback({
                "stage": "items", "processed": i, "skipped": 0, "errors": 0,
                "total": 3, "current_title": f"Paper {i}", "elapsed_ms": i * 10,
            })
            await asyncio.sleep(0.1)
        return _summary(total=3, processed=3)

    syncer = _make_syncer(slow_full_sync)

    async def consume() -> list[dict]:
        raw = ""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=_auth_headers()
        ) as aclient:
            # The server closes the stream after the terminal event, so the
            # client simply drains until EOF and parses the collected frames.
            async with aclient.stream("POST", "/sync/stream") as resp:
                async for line in resp.aiter_lines():
                    raw += line + "\n"
        return parse_sse_events(raw)

    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        results = await asyncio.gather(consume(), consume())

    syncer.full_sync.assert_awaited_once()
    first_names = [e["event"] for e in results[0]]
    second_names = [e["event"] for e in results[1]]
    # Exactly one job was created; both streams saw the same terminal event
    assert first_names[-1] == "done"
    assert second_names[-1] == "done"
    assert results[0][-1]["data"] == results[1][-1]["data"]
    # The attach marker appears on whichever stream attached to the running job
    assert fa._sync_job is None


@pytest.mark.asyncio
async def test_sync_start_event_reports_attach_state():
    async def instant_full_sync(progress_callback=None, cancel_check=None):
        return _summary(total=0)

    syncer = _make_syncer(instant_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", headers=_auth_headers()
        ) as aclient:
            res = await aclient.post("/sync/stream")
    events = parse_sse_events(res.text)
    assert events[0]["event"] == "sync_start"
    assert events[0]["data"]["attached"] is False


# ---------------------------------------------------------------------------
# Generator-level disconnect / terminal handling
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_generator_disconnect_sets_cooperative_cancel():
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing"},
        "started": 0.0,
        "terminal": None,
        "active": True,
    }
    queue: asyncio.Queue = asyncio.Queue()
    job["subscribers"].add(queue)
    gen = fa._sync_stream_event_generator(job, queue, StubRequest(disconnected=True))
    events = [ev async for ev in gen]
    assert events == []
    assert job["cancel_event"].is_set()
    assert queue not in job["subscribers"]


@pytest.mark.asyncio
async def test_generator_yields_until_terminal_then_stops():
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing"},
        "started": 0.0,
        "terminal": None,
        "active": True,
    }
    queue: asyncio.Queue = asyncio.Queue()
    job["subscribers"].add(queue)

    async def producer() -> None:
        await asyncio.sleep(0.05)
        queue.put_nowait({"event": "progress", "data": json.dumps({"processed": 1})})
        terminal = {"event": "done", "data": json.dumps({"processed": 2})}
        job["terminal"] = terminal
        queue.put_nowait(terminal)

    task = asyncio.create_task(producer())
    gen = fa._sync_stream_event_generator(job, queue, StubRequest(disconnected=False))
    events = [ev async for ev in gen]
    await task
    assert [e["event"] for e in events] == ["progress", "done"]
    assert not job["subscribers"]
    # Finally-guard negative case: the job already finished (terminal set), so
    # the guard must NOT have requested a cancel on clean stream exit.
    assert not job["cancel_event"].is_set()


@pytest.mark.asyncio
async def test_generator_multi_window_disconnect_does_not_cancel_shared_job():
    """Finding-1 regression: a body-iteration is_disconnected() poll on window A
    must NOT cooperatively cancel a job that window B is still attached to."""
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing"},
        "started": 0.0,
        "terminal": None,
        "active": True,
    }
    queue_a: asyncio.Queue = asyncio.Queue()
    queue_b: asyncio.Queue = asyncio.Queue()
    job["subscribers"].update({queue_a, queue_b})
    gen = fa._sync_stream_event_generator(job, queue_a, StubRequest(disconnected=True))
    events = [ev async for ev in gen]
    assert events == []
    assert queue_a not in job["subscribers"]  # A detached
    assert queue_b in job["subscribers"]  # B still attached
    assert not job["cancel_event"].is_set()  # shared job keeps running

    # Last-subscriber disconnect DOES cancel
    gen_b = fa._sync_stream_event_generator(job, queue_b, StubRequest(disconnected=True))
    [ev async for ev in gen_b]
    assert job["cancel_event"].is_set()


# ---------------------------------------------------------------------------
# LibrarySyncer.full_sync additive parameters
# ---------------------------------------------------------------------------
def _make_min_syncer(tmp_path, items, notes=None, attachments=None) -> LibrarySyncer:
    zotero_dir = tmp_path / "zotero"
    zotero_dir.mkdir(parents=True, exist_ok=True)
    sqlite_file = zotero_dir / "zotero.sqlite"
    sqlite_file.touch()

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    syncer = LibrarySyncer.__new__(LibrarySyncer)
    syncer._zotero_data_dir = zotero_dir
    syncer.processor = MagicMock()
    syncer.processor._cache_dir = cache_dir
    syncer.processor.list_cached_keys = MagicMock(return_value=[])
    syncer.processor.get_all_meta_keys = MagicMock(return_value=set())
    syncer.processor.ensure_parent_item_key = MagicMock()
    syncer.processor.regenerate_root_index = MagicMock()
    syncer.store = MagicMock()
    syncer.store.get_all_item_keys = MagicMock(return_value=set())
    syncer.sync_metadata_staleness = MagicMock(return_value={"updated": 0})

    mock_db = MagicMock()
    mock_db.get_all_items.return_value = items
    mock_db.get_standalone_notes.return_value = notes or []
    mock_db.get_standalone_attachments.return_value = attachments or []
    mock_db.get_all_attachments.return_value = []
    mock_db.get_attachments.return_value = []
    mock_db.get_notes_for_item.return_value = []
    syncer.db = mock_db
    return syncer


def _make_item(i: int) -> MagicMock:
    item = MagicMock()
    item.item_id = i
    item.item_key = f"ITEM{i}"
    item.title = f"Paper {i}"
    return item


@pytest.mark.asyncio
async def test_full_sync_progress_callback_receives_item_boundaries(tmp_path):
    syncer = _make_min_syncer(tmp_path, [_make_item(1), _make_item(2)])
    syncer._ingest_item = AsyncMock(side_effect=lambda *a, **k: "processed")

    events: list[dict] = []
    summary = await syncer.full_sync(progress_callback=events.append)

    assert summary["processed"] == 2
    assert len(events) == 3
    assert events[0]["stage"] == "starting"
    assert events[0]["total"] == 2  # determinate total before the first conversion
    assert events[1]["current_title"] == "Paper 1"
    assert events[1]["stage"] == "items"
    assert events[1]["total"] == 2
    assert events[2]["processed"] == 2
    assert events[-1]["elapsed_ms"] >= 0


@pytest.mark.asyncio
async def test_full_sync_progress_callback_failure_is_fail_open(tmp_path):
    syncer = _make_min_syncer(tmp_path, [_make_item(1)])
    syncer._ingest_item = AsyncMock(return_value="processed")

    def bad_callback(_info: dict) -> None:
        raise ValueError("callback exploded")

    summary = await syncer.full_sync(progress_callback=bad_callback)
    assert summary["processed"] == 1
    assert summary["cancelled"] is False


@pytest.mark.asyncio
async def test_full_sync_cooperative_cancel_stops_and_skips_tail(tmp_path):
    syncer = _make_min_syncer(tmp_path, [_make_item(1), _make_item(2), _make_item(3)])
    calls: list[int] = []

    async def ingest(item_id, item_key, title, *a, **k):
        calls.append(item_id)
        return "processed"

    syncer._ingest_item = AsyncMock(side_effect=ingest)
    # Cancel after the first item completes
    state = {"count": 0}

    def cancel_check() -> bool:
        state["count"] += 1
        return True  # first poll happens after item 1 completes

    with patch.object(LibrarySyncer, "purge_item") as mock_purge:
        summary = await syncer.full_sync(cancel_check=cancel_check)

    assert summary["cancelled"] is True
    assert summary["auto_aborted"] is False
    assert len(calls) == 1  # current item finished, then stopped
    mock_purge.assert_not_called()  # tail orphan purge skipped


@pytest.mark.asyncio
async def test_full_sync_auto_abort_on_consecutive_failures(tmp_path):
    syncer = _make_min_syncer(tmp_path, [_make_item(i) for i in range(1, 6)])
    calls: list[int] = []

    async def ingest(item_id, item_key, title, *a, **k):
        calls.append(item_id)
        raise RuntimeError("poison item")

    syncer._ingest_item = AsyncMock(side_effect=ingest)

    with patch(
        "zocrates.rag.library_syncer.get_sync_auto_abort_failures", return_value=3
    ), patch.object(LibrarySyncer, "purge_item") as mock_purge:
        summary = await syncer.full_sync()

    assert summary["auto_aborted"] is True
    assert summary["cancelled"] is False
    assert summary["errors"] == 3  # stopped right at the threshold
    assert len(calls) == 3
    mock_purge.assert_not_called()


@pytest.mark.asyncio
async def test_full_sync_auto_abort_counter_resets_on_success(tmp_path):
    syncer = _make_min_syncer(tmp_path, [_make_item(i) for i in range(1, 6)])
    calls: list[str] = []

    async def ingest(item_id, item_key, title, *a, **k):
        calls.append(item_key)
        if item_key in ("ITEM2", "ITEM3"):
            raise RuntimeError("transient failure")
        return "processed"

    syncer._ingest_item = AsyncMock(side_effect=ingest)

    with patch("zocrates.rag.library_syncer.get_sync_auto_abort_failures", return_value=3):
        summary = await syncer.full_sync()

    assert summary["auto_aborted"] is False
    assert summary["processed"] == 3
    assert summary["errors"] == 2
    assert len(calls) == 5  # all items attempted; failures never consecutively hit 3


@pytest.mark.asyncio
async def test_full_sync_default_parameters_backward_compatible(tmp_path):
    """full_sync() with no args behaves exactly as before (plan §8 code preservation)."""
    syncer = _make_min_syncer(tmp_path, [_make_item(1)])
    syncer._ingest_item = AsyncMock(return_value="processed")
    summary = await syncer.full_sync()
    assert summary == {
        "total": 1,
        "processed": 1,
        "skipped": 0,
        "errors": 0,
        "orphans_cleaned": 0,
        "metadata_refreshed": 0,
        "cancelled": False,
        "auto_aborted": False,
    }

# ---------------------------------------------------------------------------
# Review-fix coverage: disconnect mechanisms, throttle bursts, terminal race
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_client_close_handler_cancels_only_for_sole_subscriber():
    """sse_starlette dispatches the close handler on http.disconnect; cancel
    only fires when the disconnecting subscriber was the last one attached."""
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing"},
        "started": 0.0,
        "terminal": None,
        "active": True,
    }
    queue: asyncio.Queue = asyncio.Queue()
    other: asyncio.Queue = asyncio.Queue()
    job["subscribers"].update({queue, other})
    handler = fa._make_sync_client_close_handler(job, queue)

    # Another window is still attached → no cancel
    await handler({"type": "http.disconnect"})
    assert not job["cancel_event"].is_set()

    # Sole subscriber → cooperative cancel
    job["subscribers"].discard(other)
    await handler({"type": "http.disconnect"})
    assert job["cancel_event"].is_set()


@pytest.mark.asyncio
async def test_client_close_handler_ignores_finished_job():
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "done"},
        "started": 0.0,
        "terminal": {"event": "done", "data": "{}"},
        "active": False,
    }
    queue: asyncio.Queue = asyncio.Queue()
    job["subscribers"].add(queue)
    handler = fa._make_sync_client_close_handler(job, queue)
    await handler({"type": "http.disconnect"})
    assert not job["cancel_event"].is_set()


@pytest.mark.asyncio
async def test_generator_task_cancellation_sets_cooperative_cancel():
    """Safety net: cancelling the SSE response task (how sse_starlette reacts
    to a vanished client) triggers the generator's finally-guard cancel."""
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing"},
        "started": 0.0,
        "terminal": None,
        "active": True,
    }
    queue: asyncio.Queue = asyncio.Queue()
    job["subscribers"].add(queue)
    gen = fa._sync_stream_event_generator(job, queue, StubRequest(disconnected=False))
    task = asyncio.create_task(gen.__anext__())  # blocks in queue.get timeout wait
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert job["cancel_event"].is_set()
    assert queue not in job["subscribers"]


def test_progress_throttling_bursts_within_one_window():
    """A burst of callbacks inside a single 250ms window emits exactly ONE
    progress event (regression guard for the coalescing constant)."""

    async def burst_full_sync(progress_callback=None, cancel_check=None):
        # Pure synchronous burst: all five callbacks land within one
        # coalescing window deterministically (no sleeps to race against).
        for i in range(1, 6):
            progress_callback({
                "stage": "items", "processed": i, "skipped": 0, "errors": 0,
                "total": 5, "current_title": f"Paper {i}", "elapsed_ms": i,
            })
        return _summary(total=5, processed=5)

    syncer = _make_syncer(burst_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        client = _auth_client()
        res = client.post("/sync/stream")

    events = parse_sse_events(res.text)
    names = [e["event"] for e in events]
    assert names[0] == "sync_start"
    assert names.count("progress") == 1
    assert names[-1] == "done"


@pytest.mark.asyncio
async def test_terminal_delivered_after_queue_timeout_race():
    """P2 race guard: if the terminal fan-out item is lost to a cancelled
    Queue.get, the generator still yields the terminal event before EOF."""
    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "done"},
        "started": 0.0,
        "terminal": {"event": "done", "data": json.dumps({"processed": 2})},
        "active": False,
    }
    queue: asyncio.Queue = asyncio.Queue()  # empty: simulates the dropped item

    async def already_done() -> None:
        return None

    job["task"] = asyncio.create_task(already_done())
    await job["task"]  # mark the task done, as _run_sync_job's finally would
    gen = fa._sync_stream_event_generator(job, queue, StubRequest(disconnected=False))
    events = [ev async for ev in gen]
    assert [e["event"] for e in events] == ["done"]
    assert json.loads(events[-1]["data"])["processed"] == 2
    assert queue not in job["subscribers"]


@pytest.mark.asyncio
async def test_event_source_response_wires_client_close_handler():
    """Finding-3 regression: /sync/stream must pass client_close_handler_callable
    into EventSourceResponse (dropping the kwarg would silently regress P1-1)."""
    captured: dict = {}

    real_response = fa.EventSourceResponse

    def capturing_response(generator, **kwargs):
        captured.update(kwargs)
        return real_response(generator, **kwargs)

    async def instant_full_sync(progress_callback=None, cancel_check=None):
        return _summary(total=0)

    syncer = _make_syncer(instant_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer), \
         patch("zocrates.fast_api_app.EventSourceResponse", side_effect=capturing_response):
        client = _auth_client()
        res = client.post("/sync/stream")

    assert res.status_code == 200
    assert captured.get("client_close_handler_callable") is not None
    assert callable(captured["client_close_handler_callable"])


@pytest.mark.asyncio
async def test_runner_finally_uses_fallback_terminal_on_cancellation():
    """Finding-2 regression: if the runner task is cancelled (BaseException path),
    the finally block must still broadcast a terminal event and clear the
    registry instead of raising UnboundLocalError."""
    started = asyncio.Event()

    async def hang_full_sync(progress_callback=None, cancel_check=None):
        started.set()
        await asyncio.sleep(30)  # simulate long-running sync; task cancelled below

    job = {
        "task": None,
        "cancel_event": asyncio.Event(),
        "subscribers": set(),
        "state": {"stage": "syncing", "processed": 0, "skipped": 0, "errors": 0, "total": 0, "current_title": ""},
        "started": time.perf_counter(),
        "terminal": None,
        "active": True,
    }
    syncer = _make_syncer(hang_full_sync)
    with patch("zocrates.fast_api_app.get_syncer", return_value=syncer):
        # Register the job so the identity-checked registry clear in the
        # runner's finally is actually exercised (not vacuously true).
        fa._sync_job = job
        terminal_queue: asyncio.Queue = asyncio.Queue()
        job["subscribers"].add(terminal_queue)
        job["task"] = asyncio.create_task(fa._run_sync_job(job))
        # Let the runner actually enter the awaited full_sync before cancelling
        # (create_task only schedules; an immediate cancel would be a no-op).
        await started.wait()
        await asyncio.sleep(0)
        job["task"].cancel()
        await asyncio.gather(job["task"], return_exceptions=True)

    assert job["terminal"] is not None
    assert job["terminal"]["event"] == "error"
    assert "cancelled" in job["terminal"]["data"]
    assert job["active"] is False
    assert fa._sync_job is None  # registry cleared despite cancellation
    # The fallback terminal was fanned out to attached subscribers
    fanned = terminal_queue.get_nowait()
    assert fanned["event"] == "error"
