"""Decision model service manager for citation verification and local Laya tooling.

Provides lifecycle management for the local Laya decision model server (running
as an isolated `uv tool`), probing tools and health status, and testing
decision model connectivity against OpenRouter or local endpoints.
"""

import atexit
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any

import psutil
import requests

from zocrates.config import (
    DEFAULT_CITATION_VERIFICATION_LOCAL_URL,
    DEFAULT_CITATION_VERIFICATION_MODEL,
    get_backend_port,
    get_secure_api_key,
    get_zocrates_data_dir,
    load_zocrates_config,
    resolve_zotero_data_dir,
)

logger = logging.getLogger(__name__)

__all__ = [
    "check_laya_running",
    "find_uv_binary",
    "get_decision_tool_status",
    "install_laya_tool",
    "is_laya_installed",
    "is_uv_available",
    "start_laya_daemon",
    "stop_laya_daemon",
    "test_decision_model_connectivity",
]

_daemon_lock = threading.Lock()
_managed_process: subprocess.Popen | None = None
_managed_port: int | None = None


def find_uv_binary() -> str | None:
    """Locate the `uv` executable on PATH or standard install locations."""
    found = shutil.which("uv")
    if found:
        return found
    uv_name = "uv.exe" if sys.platform == "win32" else "uv"
    candidates = [
        Path.home() / ".cargo" / "bin" / uv_name,
        Path.home() / ".local" / "bin" / uv_name,
        Path("/usr/local/bin/uv"),
        Path("/opt/homebrew/bin/uv"),
    ]
    try:
        resolved_zotero = resolve_zotero_data_dir()
        if resolved_zotero is not None:
            candidates.append(resolved_zotero / "zocrates-data" / "bin" / uv_name)
    except Exception:
        pass

    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def is_uv_available() -> bool:
    """Return whether the `uv` package manager is installed and executable."""
    return find_uv_binary() is not None


def find_laya_serve_binary() -> str | None:
    """Locate the `laya-serve` binary on PATH or in standard user tool directories."""
    found = shutil.which("laya-serve")
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / "laya-serve",
        Path.home() / ".cargo" / "bin" / "laya-serve",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def is_laya_installed() -> bool:
    """Return whether Laya is installed either as a standalone binary or uv tool."""
    if find_laya_serve_binary() is not None:
        return True

    uv_bin = find_uv_binary()
    if uv_bin:
        try:
            res = subprocess.run(
                [uv_bin, "tool", "list"],
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            if res.returncode == 0 and "laya" in res.stdout:
                return True
        except Exception:
            pass

    tool_dir = Path.home() / ".local" / "share" / "uv" / "tools" / "laya"
    if tool_dir.exists() and tool_dir.is_dir():
        return True

    return False


def find_laya_python() -> str | None:
    """Locate the Python executable used by the isolated Laya uv tool environment."""
    tool_py = Path.home() / ".local" / "share" / "uv" / "tools" / "laya" / "bin" / "python3"
    if tool_py.is_file() and os.access(tool_py, os.X_OK):
        return str(tool_py)

    laya_bin = find_laya_serve_binary()
    if laya_bin:
        try:
            with open(laya_bin, "r", encoding="utf-8") as f:
                first_line = f.readline()
                if first_line.startswith("#!"):
                    shebang = first_line[2:].strip()
                    if os.path.isfile(shebang) and os.access(shebang, os.X_OK):
                        return shebang
        except Exception:
            pass

    return shutil.which("python3")


def resolve_laya_device() -> str:
    """Determine the optimal and functional execution device for Laya (cuda, mps, xpu, cpu).

    Probes actual execution inside the Laya Python environment to guard against
    CUDA capability mismatches (e.g. sm_61 on PyTorch CUDA >= 7.5 builds) or OOMs.
    """
    env_override = os.environ.get("LAYA_DEVICE")
    if env_override and env_override.strip():
        return env_override.strip().lower()

    py_bin = find_laya_python()
    if not py_bin:
        return "cpu"

    # 1. Probe CUDA execution with an actual tensor operation in an isolated subprocess
    try:
        res = subprocess.run(
            [py_bin, "-c", "import torch; assert torch.cuda.is_available(); (torch.zeros(1).cuda() + 1).item()"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if res.returncode == 0:
            return "cuda"
        logger.info(
            "Laya CUDA probe failed (returncode %d); falling back to alternate devices. Output: %s",
            res.returncode,
            res.stderr.strip() or res.stdout.strip(),
        )
    except Exception as exc:
        logger.info("Laya CUDA probe timed out or errored: %s", exc)

    # 2. Probe MPS (Apple Silicon)
    try:
        res = subprocess.run(
            [py_bin, "-c", "import torch; assert hasattr(torch.backends, 'mps') and torch.backends.mps.is_available(); (torch.zeros(1).to('mps') + 1).item()"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if res.returncode == 0:
            return "mps"
    except Exception:
        pass

    # 3. Probe XPU (Intel GPU)
    try:
        res = subprocess.run(
            [py_bin, "-c", "import torch; assert hasattr(torch, 'xpu') and torch.xpu.is_available(); (torch.zeros(1).to('xpu') + 1).item()"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        if res.returncode == 0:
            return "xpu"
    except Exception:
        pass

    return "cpu"


def _is_laya_process(proc: Any) -> bool:
    """Check whether a process is definitively a Laya daemon.

    Requires that the executable, process name, or a command-line token
    specifically represents the `laya-serve` or `laya` executable/module,
    rather than a casual substring match in file paths or arguments.
    """
    try:
        # Check process name
        name_val = proc.name() if callable(getattr(proc, "name", None)) else getattr(proc, "name", "")
        name = str(name_val or "").lower()
        if name in ("laya-serve", "laya"):
            return True

        # Check command line tokens
        cmdline_val = proc.cmdline() if callable(getattr(proc, "cmdline", None)) else getattr(proc, "cmdline", [])
        if isinstance(cmdline_val, (list, tuple)) and cmdline_val:
            # Check binary / script name in argv[0]
            first_arg = Path(str(cmdline_val[0])).name.lower()
            if first_arg in ("laya-serve", "laya"):
                return True

            # Check if running python inside isolated uv laya tool
            if "/uv/tools/laya/" in str(cmdline_val[0]).lower().replace("\\", "/"):
                return True

            # Check arguments for exact laya-serve command or laya module
            for i, arg in enumerate(cmdline_val):
                arg_str = str(arg).strip()
                arg_base = Path(arg_str).name.lower()
                if arg_base in ("laya-serve", "laya"):
                    return True
                if arg_str in ("laya.serve", "laya:serve"):
                    return True
                if arg_str == "-m" and i + 1 < len(cmdline_val) and cmdline_val[i + 1] in ("laya", "laya.serve"):
                    return True
                # uv tool run laya-serve
                if arg_str == "run" and i + 1 < len(cmdline_val) and Path(cmdline_val[i + 1]).name.lower() == "laya-serve":
                    return True

        # Check exe path if available
        exe_val = proc.exe() if callable(getattr(proc, "exe", None)) else getattr(proc, "exe", "")
        if exe_val:
            exe_base = Path(str(exe_val)).name.lower()
            if exe_base in ("laya-serve", "laya"):
                return True
            if "/uv/tools/laya/" in str(exe_val).lower().replace("\\", "/"):
                return True
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        return False
    except Exception:
        return False
    return False


def _process_is_on_port(proc: Any, port: int) -> bool:
    """Check if process is actively listening on the target port."""
    try:
        conn_method = getattr(proc, "net_connections", getattr(proc, "connections", None))
        if callable(conn_method):
            for conn in conn_method(kind="inet"):
                status = getattr(conn, "status", None)
                if status in (psutil.CONN_LISTEN, "LISTEN"):
                    laddr = getattr(conn, "laddr", None)
                    if laddr and getattr(laddr, "port", None) == port:
                        return True
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess, Exception):
        pass

    return False


def _terminate_psutil_proc(proc: Any, timeout: float = 3.0) -> bool:
    """Safely terminate a process (SIGTERM, then SIGKILL if needed)."""
    try:
        if callable(getattr(proc, "terminate", None)):
            proc.terminate()
        try:
            if callable(getattr(proc, "wait", None)):
                proc.wait(timeout=timeout)
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            if callable(getattr(proc, "kill", None)):
                proc.kill()
            try:
                if callable(getattr(proc, "wait", None)):
                    proc.wait(timeout=1.0)
            except Exception:
                pass
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except Exception as exc:
        logger.warning("Error terminating process: %s", exc)
        return False


def check_laya_running(
    url: str = DEFAULT_CITATION_VERIFICATION_LOCAL_URL,
    timeout: float = 0.5,
) -> bool:
    """Check if the local Laya server is running and reachable."""
    parsed = urllib.parse.urlparse(url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"
    try:
        resp = requests.get(health_url, timeout=timeout)
        if resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    if data.get("status") == "ok" and any(
                        k in data for k in ("loaded", "device", "checkpoint_devices", "cpu_fallbacks")
                    ):
                        return True
            except Exception:
                pass
    except Exception:
        pass

    openapi_url = f"{parsed.scheme}://{parsed.netloc}/openapi.json"
    try:
        resp = requests.get(openapi_url, timeout=timeout)
        if resp.status_code == 200:
            try:
                data = resp.json()
                if isinstance(data, dict):
                    info = data.get("info", {})
                    if isinstance(info, dict):
                        title = str(info.get("title", "")).lower()
                        desc = str(info.get("description", "")).lower()
                        if "laya" in title or "laya" in desc:
                            return True
            except Exception:
                pass
    except Exception:
        pass

    return False


def get_decision_tool_status(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Inspect local decision model runtime environment and return state metrics."""
    cfg = dict(config) if config is not None else load_zocrates_config()
    local_url = cfg.get("citation_verification_local_url", DEFAULT_CITATION_VERIFICATION_LOCAL_URL)
    port = int(cfg.get("citation_verification_local_port", 8005))

    uv_avail = is_uv_available()
    laya_inst = is_laya_installed()
    running = check_laya_running(local_url, timeout=0.5)

    active_device = "unknown"
    if running:
        parsed = urllib.parse.urlparse(local_url)
        health_url = f"{parsed.scheme}://{parsed.netloc}/health"
        try:
            h_resp = requests.get(health_url, timeout=0.5)
            if h_resp.status_code == 200:
                h_data = h_resp.json()
                active_device = h_data.get("device", "running")
        except Exception:
            active_device = "running"
    elif laya_inst:
        active_device = "installed (offline)"
    else:
        active_device = "not installed"

    global _managed_process
    daemon_managed = _managed_process is not None and _managed_process.poll() is None

    return {
        "status": "success",
        "uv_available": uv_avail,
        "tool_installed": laya_inst,
        "server_running": running,
        "daemon_managed": daemon_managed,
        "daemon_starting": daemon_managed and not running,
        "port": port,
        "local_url": local_url,
        "active_device": active_device,
    }


def install_laya_tool() -> dict[str, Any]:
    """Install Laya as an isolated uv tool with FastAPI and Uvicorn."""
    uv_bin = find_uv_binary()
    if not uv_bin:
        return {
            "status": "error",
            "message": "The `uv` package manager was not found. Please install `uv` (https://docs.astral.sh/uv/) first.",
        }

    cmd = [
        uv_bin,
        "tool",
        "install",
        "--with",
        "uvicorn",
        "--with",
        "fastapi",
        "laya",
    ]
    logger.info("Installing Laya tool via: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300.0,
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip() or f"Process exited with code {proc.returncode}"
            logger.error("Failed to install Laya uv tool: %s", err_msg)
            return {
                "status": "error",
                "message": f"Installation failed: {err_msg}",
            }
        logger.info("Successfully installed Laya uv tool.")
        return {
            "status": "success",
            "message": "Laya tool installed successfully.",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "message": "Installation timed out after 300 seconds.",
        }
    except Exception as exc:
        logger.error("Exception installing Laya: %s", exc)
        return {
            "status": "error",
            "message": f"Installation error: {exc}",
        }


def start_laya_daemon(port: int = 8005) -> dict[str, Any]:
    """Spawn laya-serve in the background."""
    global _managed_process, _managed_port

    backend_port = get_backend_port()
    if backend_port is not None and port == backend_port:
        return {
            "status": "error",
            "message": f"Port {port} conflicts with the active Zocrates backend server.",
        }

    target_url = f"http://127.0.0.1:{port}/v1/systemone"
    if check_laya_running(target_url, timeout=0.5):
        return {
            "status": "success",
            "message": f"Laya server is already running on port {port}.",
            "port": port,
        }

    laya_bin = find_laya_serve_binary()
    uv_bin = find_uv_binary()

    if not laya_bin and not uv_bin:
        return {
            "status": "error",
            "message": "Neither `laya-serve` binary nor `uv` tool runner found. Please install Laya first.",
        }

    if laya_bin:
        cmd = [laya_bin]
    else:
        cmd = [uv_bin, "tool", "run", "laya-serve"]

    env = os.environ.copy()
    env["LAYA_HOST"] = "127.0.0.1"
    env["LAYA_PORT"] = str(port)
    env["LAYA_MODELS"] = "english"
    device = resolve_laya_device()
    env["LAYA_DEVICE"] = device
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}{os.pathsep}{env.get('PATH', '')}"

    with _daemon_lock:
        if _managed_process is not None and _managed_process.poll() is None:
            return {
                "status": "success",
                "message": f"Laya server is already running on port {_managed_port}.",
                "port": _managed_port,
            }
        _managed_process = None
        _managed_port = None

        logger.info("Spawning Laya daemon: %s on port %s with LAYA_DEVICE=%s", " ".join(cmd), port, device)
        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            _managed_process = proc
            _managed_port = port
            try:
                data_dir = get_zocrates_data_dir()
                (data_dir / "laya.pid").write_text(str(proc.pid), encoding="utf-8")
                (data_dir / "laya.port").write_text(str(port), encoding="utf-8")
            except Exception as write_err:
                logger.warning("Could not write laya.pid/laya.port: %s", write_err)
        except Exception as exc:
            logger.error("Failed to spawn Laya daemon: %s", exc)
            return {
                "status": "error",
                "message": f"Failed to spawn Laya daemon: {exc}",
            }

    deadline = time.time() + 30.0
    while time.time() < deadline:
        if proc.poll() is not None:
            try:
                data_dir = get_zocrates_data_dir()
                (data_dir / "laya.pid").unlink(missing_ok=True)
                (data_dir / "laya.port").unlink(missing_ok=True)
            except Exception:
                pass
            return {
                "status": "error",
                "message": f"Laya daemon process terminated unexpectedly with code {proc.poll()}.",
            }
        if check_laya_running(target_url, timeout=0.3):
            return {
                "status": "success",
                "message": f"Laya daemon started successfully on port {port}.",
                "port": port,
            }
        time.sleep(0.3)

    return {
        "status": "success",
        "message": f"Laya daemon process launched (PID: {proc.pid}), awaiting model initialization.",
        "port": port,
    }


def stop_laya_daemon(port: int = 8005) -> dict[str, Any]:
    """Terminate the managed daemon or verified Laya process listening on port."""
    global _managed_process, _managed_port

    stopped = False
    with _daemon_lock:
        if _managed_process is not None:
            if _managed_port is None or _managed_port == port:
                try:
                    if _managed_process.poll() is None:
                        _managed_process.terminate()
                        try:
                            _managed_process.wait(timeout=3.0)
                        except subprocess.TimeoutExpired:
                            _managed_process.kill()
                            _managed_process.wait(timeout=1.0)
                        stopped = True
                except Exception as exc:
                    logger.warning("Error stopping managed Laya process: %s", exc)
                finally:
                    _managed_process = None
                    _managed_port = None

    try:
        data_dir = get_zocrates_data_dir()
        pid_file = data_dir / "laya.pid"
        port_file = data_dir / "laya.port"
        if pid_file.exists():
            recorded_port = None
            if port_file.exists():
                try:
                    recorded_port = int(port_file.read_text(encoding="utf-8").strip())
                except Exception:
                    pass

            # Only terminate if port matches recorded port or no specific recorded port
            if recorded_port is None or recorded_port == port:
                try:
                    pid_str = pid_file.read_text(encoding="utf-8").strip()
                    if pid_str.isdigit():
                        pid = int(pid_str)
                        if psutil.pid_exists(pid):
                            try:
                                proc = psutil.Process(pid)
                                if _is_laya_process(proc):
                                    if _terminate_psutil_proc(proc, timeout=3.0):
                                        stopped = True
                                        pid_file.unlink(missing_ok=True)
                                        port_file.unlink(missing_ok=True)
                                else:
                                    logger.warning(
                                        "Process PID %d recorded in laya.pid is not a Laya process; refusing to terminate.",
                                        pid,
                                    )
                                    pid_file.unlink(missing_ok=True)
                                    port_file.unlink(missing_ok=True)
                            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                                pid_file.unlink(missing_ok=True)
                                port_file.unlink(missing_ok=True)
                            except Exception as exc:
                                logger.warning("Error terminating process %d from laya.pid: %s", pid, exc)
                        else:
                            pid_file.unlink(missing_ok=True)
                            port_file.unlink(missing_ok=True)
                except Exception as read_err:
                    logger.warning("Error reading laya.pid: %s", read_err)
        elif port_file.exists():
            port_file.unlink(missing_ok=True)
    except Exception as exc:
        logger.warning("Error checking or cleaning laya.pid: %s", exc)

    target_url = f"http://127.0.0.1:{port}/v1/systemone"
    if check_laya_running(target_url, timeout=0.3):
        try:
            for p in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if _is_laya_process(p) and _process_is_on_port(p, port):
                        logger.info("Terminating Laya process %d found listening on port %d", p.pid, port)
                        if _terminate_psutil_proc(p, timeout=3.0):
                            stopped = True
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
        except Exception as exc:
            logger.warning("Error inspecting processes listening on port %d: %s", port, exc)

    return {
        "status": "success",
        "message": "Laya daemon stopped." if stopped else "Laya daemon was not running.",
        "port": port,
    }


def _cleanup_managed_daemon() -> None:
    """Atexit hook to terminate any background Laya process on interpreter exit."""
    global _managed_process, _managed_port
    managed_pid = None
    if _managed_process is not None:
        managed_pid = _managed_process.pid
        if _managed_process.poll() is None:
            try:
                _managed_process.terminate()
                _managed_process.wait(timeout=2.0)
            except Exception:
                try:
                    _managed_process.kill()
                except Exception:
                    pass
        _managed_process = None
        _managed_port = None

    try:
        data_dir = get_zocrates_data_dir()
        pid_file = data_dir / "laya.pid"
        port_file = data_dir / "laya.port"
        if pid_file.exists():
            try:
                content = pid_file.read_text(encoding="utf-8").strip()
                if managed_pid is not None and content == str(managed_pid):
                    pid_file.unlink(missing_ok=True)
                    port_file.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


atexit.register(_cleanup_managed_daemon)


def test_decision_model_connectivity(
    provider: str = "openrouter",
    model: str | None = None,
    local_url: str | None = None,
    openrouter_api_key: str | None = None,
) -> dict[str, Any]:
    """Test connectivity to decision model (remote OpenRouter or local Laya)."""
    cfg = load_zocrates_config()
    provider = (provider or cfg.get("citation_verification_provider", "openrouter")).lower()

    source_text = "Transformers rely entirely on self-attention mechanisms without recurrence or convolution."
    claim = "Transformers eliminate recurrence in favor of self-attention mechanisms."

    t0 = time.perf_counter()

    if provider == "local_laya":
        effective_url = local_url or cfg.get("citation_verification_local_url", DEFAULT_CITATION_VERIFICATION_LOCAL_URL)
        effective_model = "laya"
        parsed = urllib.parse.urlparse(effective_url)
        health_url = f"{parsed.scheme}://{parsed.netloc}/health"
        if not check_laya_running(effective_url, timeout=1.0):
            return {
                "ok": False,
                "status": "error",
                "provider": "local_laya",
                "message": f"Local Laya server is not reachable at {effective_url} (probe failed on {health_url}). Make sure the daemon is running.",
            }

        from zocrates.agents.library_search.citation_verifier import _check_support_local_laya

        prob = _check_support_local_laya(
            claim=claim,
            source_text=source_text,
            model=effective_model,
            local_url=effective_url,
            timeout=8.0,
            skip_health_probe=True,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if prob is not None:
            return {
                "ok": True,
                "status": "ok",
                "provider": "local_laya",
                "model": effective_model,
                "local_url": effective_url,
                "support_probability": prob,
                "latency_ms": round(latency_ms, 1),
                "message": f"Successfully verified test claim with local Laya (support: {prob:.1%}, latency: {latency_ms:.0f}ms)",
            }
        else:
            return {
                "ok": False,
                "status": "error",
                "provider": "local_laya",
                "message": (
                    f"Laya server at {effective_url} failed to evaluate test decision (HTTP 500 inference failed). "
                    "This typically occurs if the GPU compute capability is unsupported (e.g. sm_61) or VRAM is exhausted. "
                    "Restart Laya with LAYA_DEVICE=cpu (or click 'Start Local Server' in Preferences)."
                ),
            }

    else:
        effective_model = model or cfg.get("citation_verification_model", DEFAULT_CITATION_VERIFICATION_MODEL)
        effective_key = (
            openrouter_api_key
            or get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY", "OPEN_ROUTER_API_KEY"))
            or os.environ.get("OPENROUTER_API_KEY")
        )
        if not effective_key:
            return {
                "ok": False,
                "status": "error",
                "provider": "openrouter",
                "message": "OpenRouter API key is missing. Please provide a key in OpenRouter settings.",
            }

        from zocrates.agents.library_search.citation_verifier import _check_support_openrouter

        prob = _check_support_openrouter(
            claim=claim,
            source_text=source_text,
            model=effective_model,
            api_key=effective_key,
            timeout=6.0,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0

        if prob is not None:
            return {
                "ok": True,
                "status": "ok",
                "provider": "openrouter",
                "model": effective_model,
                "support_probability": prob,
                "latency_ms": round(latency_ms, 1),
                "message": f"Successfully verified test claim with OpenRouter ({effective_model}, support: {prob:.1%}, latency: {latency_ms:.0f}ms)",
            }
        else:
            return {
                "ok": False,
                "status": "error",
                "provider": "openrouter",
                "message": f"OpenRouter decision model ({effective_model}) failed to respond or returned an error.",
            }
