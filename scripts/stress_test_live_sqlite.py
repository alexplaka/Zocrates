"""
Live Concurrency & Lock-Contention Stress Test for Zocrates SQLite Access

This script stress-tests ZoteroDB and live backend endpoints under intense
concurrency and simulated live write-lock contention.

Requirements:
    1. A Zotero data directory with `zotero.sqlite` (e.g. ~/Zotero).
       - If Zotero is running, it tests against live Zotero process locks.
       - Can be specified via --data-dir or ZOTERO_DATA_DIR env var (auto-detected by default).
    2. (Optional) A running Zocrates backend:
       - If the backend is running, it tests live HTTP endpoints (/items/<key>/status).
       - If the backend is offline, it skips HTTP tests and completes the direct DB stress test.

To run the live stress-test with default settings (50 concurrent threads, 1,000 queries):

 ```bash
   uv run python scripts/stress_test_live_sqlite.py
 ```

 Optional flags:
 - Custom data directory:
   ```bash
     uv run python scripts/stress_test_live_sqlite.py --data-dir ~/Zotero
   ```
 - Customize concurrency / iterations:
   ```bash
     uv run python scripts/stress_test_live_sqlite.py --workers 30 --iterations 10
   ```
 - Skip HTTP endpoint testing (DB-only):
   ```bash
     uv run python scripts/stress_test_live_sqlite.py --skip-http
   ```
"""

import argparse
import concurrent.futures
import sqlite3
import sys
import time
from pathlib import Path

import requests

from zocrates.config import resolve_zotero_data_dir
from zocrates.rag.zotero_db import (
    ZoteroDB,
    _cleanup_sqlite_snapshots,
    _sqlite_snapshots,
    _sqlite_snapshot_lock,
)


def discover_paths(explicit_dir: str | None = None) -> tuple[Path, Path, Path, Path]:
    """Resolve Zotero and Zocrates data paths."""
    if explicit_dir:
        zotero_dir = Path(explicit_dir).expanduser().resolve()
    else:
        resolved = resolve_zotero_data_dir()
        if resolved and (resolved / "zotero.sqlite").exists():
            zotero_dir = resolved
        else:
            candidates = [
                Path.home() / "Zotero",
                Path.home() / "Library" / "Application Support" / "Zotero" / "Profiles",
            ]
            found = None
            for cand in candidates:
                if (cand / "zotero.sqlite").exists():
                    found = cand
                    break
            zotero_dir = found or (Path.home() / "Zotero")

    sqlite_path = zotero_dir / "zotero.sqlite"
    storage_dir = zotero_dir / "storage"
    data_dir = zotero_dir / "zocrates-data"
    return zotero_dir, sqlite_path, storage_dir, data_dir


def read_backend_credentials(data_dir: Path) -> tuple[str | None, str | None]:
    """Read backend port and auth token from sidecars if available."""
    port_file = data_dir / "backend.port"
    auth_file = data_dir / "backend.auth"
    port = port_file.read_text().strip() if port_file.exists() else None
    auth = auth_file.read_text().strip() if auth_file.exists() else None
    return port, auth


def is_backend_online(base_url: str) -> bool:
    """Check if the backend is reachable."""
    try:
        resp = requests.get(f"{base_url}/status", timeout=1.0)
        return resp.status_code == 200
    except Exception:
        return False


def lock_injector(sqlite_path: Path, stop_event, stats):
    """Periodically takes exclusive locks on zotero.sqlite to simulate heavy writes."""
    lock_count = 0
    while not stop_event.is_set():
        try:
            conn = sqlite3.connect(str(sqlite_path), timeout=0.5)
            conn.execute("BEGIN IMMEDIATE")
            lock_count += 1
            # Hold lock for 100ms - 200ms
            time.sleep(0.12)
            conn.rollback()
            conn.close()
        except sqlite3.OperationalError:
            pass
        except Exception:
            pass
        time.sleep(0.05)
    stats["locks_injected"] = lock_count


def db_worker(worker_id, sqlite_path, storage_dir, num_iterations, item_keys, results):
    """Simulates concurrent reader queries through ZoteroDB."""
    db = ZoteroDB(sqlite_path, storage_dir)
    success = 0
    failures = 0
    errors = []

    for i in range(num_iterations):
        op_type = i % 4
        try:
            if op_type == 0:
                items = db.get_all_items()
                assert len(items) > 0
            elif op_type == 1:
                atts = db.get_all_attachments()
                assert len(atts) >= 0
            elif op_type == 2:
                key = item_keys[i % len(item_keys)]
                item = db.get_item_by_key(key)
            elif op_type == 3:
                records = db.get_all_dedup_records()
                assert len(records) > 0
            success += 1
        except Exception as exc:
            failures += 1
            errors.append(f"Worker {worker_id} op {op_type} failed: {exc}")

    results[worker_id] = {"success": success, "failures": failures, "errors": errors}


def http_worker(worker_id, base_url, headers, item_keys, num_requests, http_results):
    """Fires concurrent HTTP requests to live backend."""
    success = 0
    failures = 0
    errors = []

    for i in range(num_requests):
        key = item_keys[i % len(item_keys)]
        url = f"{base_url}/items/{key}/status"
        try:
            resp = requests.get(url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                success += 1
            else:
                failures += 1
                errors.append(f"HTTP {resp.status_code}: {resp.text[:100]}")
        except Exception as e:
            failures += 1
            errors.append(str(e))

    http_results[worker_id] = {"success": success, "failures": failures, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="Live Concurrency & Lock Stress Test for Zocrates SQLite Access")
    parser.add_argument("--data-dir", "-d", help="Path to Zotero data directory (contains zotero.sqlite)")
    parser.add_argument("--workers", "-w", type=int, default=50, help="Number of concurrent DB workers (default: 50)")
    parser.add_argument("--iterations", "-i", type=int, default=20, help="Iterations per DB worker (default: 20)")
    parser.add_argument("--skip-http", action="store_true", help="Skip HTTP endpoint tests")
    args = parser.parse_args()

    zotero_dir, sqlite_path, storage_dir, data_dir = discover_paths(args.data_dir)

    print("=" * 70)
    print("ZOCRATES LIVE SQLITE CONCURRENCY & LOCK STRESS TEST")
    print("=" * 70)
    print(f"Zotero Data Dir: {zotero_dir}")
    print(f"Database Path:   {sqlite_path}")

    if not sqlite_path.exists():
        print(f"\n❌ Error: Database file not found at {sqlite_path}")
        print("Please specify your Zotero data directory with: --data-dir /path/to/Zotero")
        sys.exit(1)

    # Initial check
    initial_db = ZoteroDB(sqlite_path, storage_dir)
    items = initial_db.get_all_items()
    item_keys = [it.item_key for it in items]
    print(f"Library Items:   {len(item_keys)} items available for testing.")

    # Check backend status
    port, auth_token = read_backend_credentials(data_dir)
    base_url = f"http://127.0.0.1:{port}" if port else None
    backend_active = bool(base_url and is_backend_online(base_url)) and not args.skip_http

    if backend_active:
        print(f"Backend Status:  ACTIVE on {base_url} (Auth token: {'present' if auth_token else 'missing'})")
    else:
        print("Backend Status:  OFFLINE (skipping HTTP endpoints; testing direct DB concurrency)")

    # Start lock injector thread
    import threading
    stop_event = threading.Event()
    stats = {}
    injector_thread = threading.Thread(target=lock_injector, args=(sqlite_path, stop_event, stats))
    injector_thread.start()
    print("\n[Phase 1] Starting background write-lock injector...")

    # Phase 1: Concurrent DB workers
    total_db_ops = args.workers * args.iterations
    print(f"[Phase 2] Spawning {args.workers} concurrent DB threads ({total_db_ops} total queries)...")

    db_results = {}
    t0 = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(db_worker, wid, sqlite_path, storage_dir, args.iterations, item_keys, db_results)
            for wid in range(args.workers)
        ]
        concurrent.futures.wait(futures)
    t_db = time.time() - t0

    # Phase 2: HTTP Workers (if backend is active)
    http_results = {}
    t_http = 0.0
    total_http_ops = 0
    if backend_active:
        num_http_workers = min(args.workers, 30)
        http_per_worker = 10
        total_http_ops = num_http_workers * http_per_worker
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}
        print(f"[Phase 3] Spawning {num_http_workers} concurrent HTTP workers ({total_http_ops} requests)...")

        t1 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_http_workers) as executor:
            futures = [
                executor.submit(http_worker, wid, base_url, headers, item_keys, http_per_worker, http_results)
                for wid in range(num_http_workers)
            ]
            concurrent.futures.wait(futures)
        t_http = time.time() - t1

    # Stop injector
    stop_event.set()
    injector_thread.join()

    # Aggregate Results
    total_db_success = sum(r["success"] for r in db_results.values())
    total_db_failures = sum(r["failures"] for r in db_results.values())
    all_db_errors = [e for r in db_results.values() for e in r["errors"]]

    total_http_success = sum(r["success"] for r in http_results.values())
    total_http_failures = sum(r["failures"] for r in http_results.values())
    all_http_errors = [e for r in http_results.values() for e in r["errors"]]

    print("\n" + "=" * 70)
    print("STRESS TEST RESULTS")
    print("=" * 70)
    print(f"Direct DB Queries:     {total_db_success} / {total_db_ops} passed ({t_db:.2f}s, {total_db_ops / t_db:.1f} ops/sec)")
    print(f"Direct DB Failures:    {total_db_failures}")
    if backend_active:
        print(f"HTTP Status Requests:  {total_http_success} / {total_http_ops} passed ({t_http:.2f}s, {total_http_ops / t_http:.1f} req/sec)")
        print(f"HTTP Status Failures:  {total_http_failures}")

    with _sqlite_snapshot_lock:
        print(f"Active Cached Snapshots:{len(_sqlite_snapshots)}")
        for path, (tmp_path, sig) in _sqlite_snapshots.items():
            print(f"  - Active snapshot for {Path(path).name} -> {tmp_path}")

    if all_db_errors:
        print("\n❌ Sample DB Errors:")
        for err in all_db_errors[:5]:
            print(f"   {err}")

    if all_http_errors:
        print("\n❌ Sample HTTP Errors:")
        for err in all_http_errors[:5]:
            print(f"   {err}")

    # Test snapshot cleanup
    print("\nVerifying snapshot cleanup...")
    _cleanup_sqlite_snapshots()
    with _sqlite_snapshot_lock:
        assert len(_sqlite_snapshots) == 0
    print("✓ Snapshot cache and temp directories successfully cleaned up.")

    if total_db_failures == 0 and total_http_failures == 0:
        print("\n🎉 ALL TESTS PASSED! 100% SUCCESS RATE UNDER INTENSE CONCURRENCY & LOCK CONTENTION.")
    else:
        print("\n⚠️ FAILURES DETECTED. Review errors above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
