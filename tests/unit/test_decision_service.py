"""Unit tests for decision service and citation verification configuration endpoints."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app
from zocrates.rag.decision_service import (
    _is_laya_process,
    _process_is_on_port,
    check_laya_running,
    find_uv_binary,
    get_decision_tool_status,
    install_laya_tool,
    is_laya_installed,
    is_uv_available,
    start_laya_daemon,
    stop_laya_daemon,
    test_decision_model_connectivity as service_test_connectivity,
)

client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


@pytest.fixture(autouse=True)
def reset_daemon_state():
    """Reset global managed daemon state before and after each test."""
    import zocrates.rag.decision_service as ds

    ds._managed_process = None
    ds._managed_port = None
    yield
    ds._managed_process = None
    ds._managed_port = None


# ---------------------------------------------------------------------------
# Unit tests: decision_service functions
# ---------------------------------------------------------------------------


def test_is_uv_available():
    """Verify is_uv_available returns True when uv binary is found."""
    with patch("shutil.which", return_value="/usr/local/bin/uv"):
        assert is_uv_available() is True

    with patch("shutil.which", return_value=None), patch("pathlib.Path.is_file", return_value=False):
        assert is_uv_available() is False


def test_find_uv_binary_zocrates_data_and_homebrew(tmp_path):
    """Verify find_uv_binary finds uv in zocrates-data/bin/uv and /opt/homebrew/bin/uv."""
    import sys

    zotero_dir = tmp_path / "zotero"
    zocrates_data = zotero_dir / "zocrates-data"
    uv_name = "uv.exe" if sys.platform == "win32" else "uv"
    uv_bin_zocrates = zocrates_data / "bin" / uv_name
    uv_bin_homebrew = Path("/opt/homebrew/bin/uv")

    # 1. Finds in zocrates-data/bin/<uv_name> via resolve_zotero_data_dir
    def fake_is_file_zocrates(path_obj):
        return path_obj == uv_bin_zocrates

    with (
        patch("shutil.which", return_value=None),
        patch("zocrates.rag.decision_service.resolve_zotero_data_dir", return_value=zotero_dir),
        patch("pathlib.Path.is_file", fake_is_file_zocrates),
        patch("os.access", return_value=True),
    ):
        assert find_uv_binary() == str(uv_bin_zocrates)

    # 2. Finds in /opt/homebrew/bin/uv
    def fake_is_file_homebrew(path_obj):
        return path_obj == uv_bin_homebrew

    with (
        patch("shutil.which", return_value=None),
        patch("zocrates.rag.decision_service.resolve_zotero_data_dir", return_value=None),
        patch("pathlib.Path.is_file", fake_is_file_homebrew),
        patch("os.access", return_value=True),
    ):
        assert find_uv_binary() == str(uv_bin_homebrew)

    # 3. Finds in ~/.cargo/bin/uv.exe and ~/.local/bin/uv.exe on Windows
    cargo_uv = Path.home() / ".cargo" / "bin" / "uv.exe"
    with (
        patch("shutil.which", return_value=None),
        patch("sys.platform", "win32"),
        patch("zocrates.rag.decision_service.resolve_zotero_data_dir", return_value=None),
        patch("pathlib.Path.is_file", lambda p: p == cargo_uv),
        patch("os.access", return_value=True),
    ):
        assert find_uv_binary() == str(cargo_uv)

    local_uv = Path.home() / ".local" / "bin" / "uv.exe"
    with (
        patch("shutil.which", return_value=None),
        patch("sys.platform", "win32"),
        patch("zocrates.rag.decision_service.resolve_zotero_data_dir", return_value=None),
        patch("pathlib.Path.is_file", lambda p: p == local_uv),
        patch("os.access", return_value=True),
    ):
        assert find_uv_binary() == str(local_uv)


def test_is_laya_installed():
    """Verify is_laya_installed checks binary, uv tool list, and tool directory."""
    with patch("zocrates.rag.decision_service.find_laya_serve_binary", return_value="/home/user/.local/bin/laya-serve"):
        assert is_laya_installed() is True

    with (
        patch("zocrates.rag.decision_service.find_laya_serve_binary", return_value=None),
        patch("zocrates.rag.decision_service.find_uv_binary", return_value="/bin/uv"),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="laya v0.1.0\n"),
        ),
    ):
        assert is_laya_installed() is True

    with (
        patch("zocrates.rag.decision_service.find_laya_serve_binary", return_value=None),
        patch("zocrates.rag.decision_service.find_uv_binary", return_value=None),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.is_file", return_value=False),
    ):
        assert is_laya_installed() is False


def test_check_laya_running():
    """Verify check_laya_running returns True if /health returns 200 with Laya signature."""
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"status": "ok", "device": "cpu"}

    with patch("requests.get", return_value=mock_resp):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is True

    # Fallback probe to /openapi.json when /health fails
    mock_health_fail = MagicMock(status_code=404)
    mock_openapi_ok = MagicMock(status_code=200)
    mock_openapi_ok.json.return_value = {"info": {"title": "Laya API", "version": "0.1.0"}}

    def mock_get(url, *args, **kwargs):
        if url.endswith("/health"):
            return mock_health_fail
        if url.endswith("/openapi.json"):
            return mock_openapi_ok
        return MagicMock(status_code=404)

    with patch("requests.get", side_effect=mock_get):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is True

    with patch("requests.get", side_effect=requests.ConnectionError("Connection refused")):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is False


def test_check_laya_running_rejects_unrelated_server():
    """Verify unrelated responses (404, HTML, non-Laya JSON) return False."""
    # 1. 404 on /health and /openapi.json
    mock_404 = MagicMock(status_code=404)
    with patch("requests.get", return_value=mock_404):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is False

    # 2. HTML response on /health (json() raises ValueError)
    mock_html = MagicMock(status_code=200)
    mock_html.json.side_effect = ValueError("Invalid JSON: <html>...</html>")
    with patch("requests.get", return_value=mock_html):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is False

    # 3. Non-Laya JSON on /health and non-Laya JSON on /openapi.json
    mock_non_laya = MagicMock(status_code=200)
    mock_non_laya.json.return_value = {"app": "other_service", "status": "running"}
    with patch("requests.get", return_value=mock_non_laya):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is False

    # 4. Old loose check: 200/404/405 on GET / is rejected
    def mock_server(url, *args, **kwargs):
        if url.endswith("/health") or url.endswith("/openapi.json"):
            return MagicMock(status_code=404)
        if url.endswith("/"):
            return MagicMock(status_code=200)
        return MagicMock(status_code=404)

    with patch("requests.get", side_effect=mock_server):
        assert check_laya_running("http://127.0.0.1:8005/v1/systemone") is False


def test_get_decision_tool_status():
    """Verify get_decision_tool_status reports runtime environment correctly."""
    with (
        patch("zocrates.rag.decision_service.is_uv_available", return_value=True),
        patch("zocrates.rag.decision_service.is_laya_installed", return_value=True),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=False),
    ):
        status = get_decision_tool_status()
        assert status["status"] == "success"
        assert status["uv_available"] is True
        assert status["tool_installed"] is True
        assert status["server_running"] is False
        assert status["daemon_starting"] is False
        assert status["active_device"] == "installed (offline)"


def test_get_decision_tool_status_daemon_starting():
    """Verify daemon_starting is True when daemon_managed is True but server is not yet running."""
    mock_proc = MagicMock()
    mock_proc.poll.return_value = None  # Process running
    with (
        patch("zocrates.rag.decision_service.is_uv_available", return_value=True),
        patch("zocrates.rag.decision_service.is_laya_installed", return_value=True),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=False),
        patch("zocrates.rag.decision_service._managed_process", mock_proc),
    ):
        status = get_decision_tool_status()
        assert status["server_running"] is False
        assert status["daemon_managed"] is True
        assert status["daemon_starting"] is True


def test_install_laya_tool():
    """Verify install_laya_tool runs uv tool install command."""
    with patch("zocrates.rag.decision_service.find_uv_binary", return_value=None):
        res = install_laya_tool()
        assert res["status"] == "error"
        assert "not found" in res["message"]

    with (
        patch("zocrates.rag.decision_service.find_uv_binary", return_value="/bin/uv"),
        patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ),
    ):
        res = install_laya_tool()
        assert res["status"] == "success"
        assert "successfully" in res["message"]


def test_start_laya_daemon_already_running():
    """Verify start_laya_daemon does not respawn if server already running."""
    with patch("zocrates.rag.decision_service.check_laya_running", return_value=True):
        res = start_laya_daemon(port=8005)
        assert res["status"] == "success"
        assert "already running" in res["message"]


def test_start_laya_daemon_port_conflict_with_backend():
    """Verify error when port equals get_backend_port()."""
    with patch("zocrates.rag.decision_service.get_backend_port", return_value=8005):
        res = start_laya_daemon(port=8005)
        assert res["status"] == "error"
        assert "Port 8005 conflicts with the active Zocrates backend server." in res["message"]


def test_start_laya_daemon_writes_pid_file(tmp_path):
    """Verify laya.pid and laya.port are written upon daemon launch."""
    mock_proc = MagicMock()
    mock_proc.poll.side_effect = [None, None]
    mock_proc.pid = 42424

    with (
        patch("zocrates.rag.decision_service.get_backend_port", return_value=8765),
        patch("zocrates.rag.decision_service.check_laya_running", side_effect=[False, True]),
        patch("zocrates.rag.decision_service.find_laya_serve_binary", return_value="/bin/laya-serve"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
    ):
        res = start_laya_daemon(port=8005)
        assert res["status"] == "success"
        pid_file = tmp_path / "laya.pid"
        port_file = tmp_path / "laya.port"
        assert pid_file.exists()
        assert pid_file.read_text(encoding="utf-8").strip() == "42424"
        assert port_file.exists()
        assert port_file.read_text(encoding="utf-8").strip() == "8005"


def test_stop_laya_daemon_cleans_pid_file(tmp_path):
    """Verify laya.pid and laya.port are cleaned up when stopping daemon."""
    pid_file = tmp_path / "laya.pid"
    port_file = tmp_path / "laya.port"
    pid_file.write_text("42424", encoding="utf-8")
    port_file.write_text("8005", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.pid = 42424
    mock_proc.name.return_value = "laya-serve"
    mock_proc.cmdline.return_value = ["laya-serve", "--port", "8005"]

    with (
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process", return_value=mock_proc),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=False),
    ):
        res = stop_laya_daemon(port=8005)
        assert res["status"] == "success"
        mock_proc.terminate.assert_called_once()
        assert not pid_file.exists()
        assert not port_file.exists()


def test_stop_laya_daemon_never_kills_unrelated_process(tmp_path):
    """Verify an unrelated process on port 8005 is not terminated."""
    pid_file = tmp_path / "laya.pid"
    port_file = tmp_path / "laya.port"
    pid_file.write_text("12345", encoding="utf-8")
    port_file.write_text("8005", encoding="utf-8")

    unrelated_proc = MagicMock()
    unrelated_proc.pid = 12345
    unrelated_proc.name.return_value = "nginx"
    unrelated_proc.cmdline.return_value = ["/usr/sbin/nginx", "-g", "daemon off;"]

    with (
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process", return_value=unrelated_proc),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=False),
        patch("psutil.process_iter", return_value=[unrelated_proc]),
    ):
        res = stop_laya_daemon(port=8005)
        assert res["status"] == "success"
        unrelated_proc.terminate.assert_not_called()
        unrelated_proc.kill.assert_not_called()
        assert not pid_file.exists()
        assert not port_file.exists()


def test_start_and_stop_laya_daemon(tmp_path):
    """Verify start_laya_daemon spawns process and stop_laya_daemon terminates it."""
    mock_proc = MagicMock()
    mock_proc.poll.side_effect = [None, None, 0]
    mock_proc.pid = 99999

    with (
        patch("zocrates.rag.decision_service.get_backend_port", return_value=8765),
        patch("zocrates.rag.decision_service.check_laya_running", side_effect=[False, True, False]),
        patch("zocrates.rag.decision_service.find_laya_serve_binary", return_value="/bin/laya-serve"),
        patch("subprocess.Popen", return_value=mock_proc),
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
    ):
        start_res = start_laya_daemon(port=8005)
        assert start_res["status"] == "success"

        stop_res = stop_laya_daemon(port=8005)
        assert stop_res["status"] == "success"
        mock_proc.terminate.assert_called_once()


def test_decision_model_connectivity_openrouter():
    """Verify connectivity test against OpenRouter decisions."""
    with (
        patch(
            "zocrates.rag.decision_service.load_zocrates_config",
            return_value={"citation_verification_provider": "openrouter"},
        ),
        patch("zocrates.agents.library_search.citation_verifier._check_support_openrouter", return_value=0.98),
    ):
        res = service_test_connectivity(
            provider="openrouter",
            model="typesafe/jev-1.13",
            openrouter_api_key="sk-or-test-key",
        )
        assert res["status"] == "ok"
        assert res["ok"] is True
        assert res["provider"] == "openrouter"
        assert res["support_probability"] == 0.98
        assert "Successfully verified" in res["message"]

    # When key is missing
    with (
        patch("zocrates.rag.decision_service.load_zocrates_config", return_value={}),
        patch("zocrates.rag.decision_service.get_secure_api_key", return_value=None),
        patch.dict("os.environ", {}, clear=True),
    ):
        res = service_test_connectivity(provider="openrouter", openrouter_api_key=None)
        assert res["status"] == "error"
        assert res["ok"] is False
        assert "missing" in res["message"]


def test_decision_model_connectivity_local_laya():
    """Verify connectivity test against Local Laya endpoint."""
    with (
        patch("zocrates.rag.decision_service.check_laya_running", return_value=False),
    ):
        res = service_test_connectivity(
            provider="local_laya",
            local_url="http://127.0.0.1:8005/v1/systemone",
        )
        assert res["status"] == "error"
        assert res["ok"] is False
        assert "not reachable" in res["message"]

    with (
        patch("zocrates.rag.decision_service.check_laya_running", return_value=True),
        patch("zocrates.agents.library_search.citation_verifier._check_support_local_laya", return_value=0.92) as mock_check,
    ):
        res = service_test_connectivity(
            provider="local_laya",
            model="inception/mercury-decide:free",
            local_url="http://127.0.0.1:8005/v1/systemone",
        )
        assert res["status"] == "ok"
        assert res["ok"] is True
        assert res["provider"] == "local_laya"
        assert res["model"] == "laya"
        assert res["support_probability"] == 0.92
        assert mock_check.call_args.kwargs["model"] == "laya"


# ---------------------------------------------------------------------------
# Unit tests: FastAPI Endpoints
# ---------------------------------------------------------------------------


def test_status_endpoint_includes_citation_verification_settings():
    """Verify /status exposes citation_verification_* configuration."""
    response = client.get("/status")
    assert response.status_code == 200
    data = response.json()
    assert "citation_verification_enabled" in data
    assert "citation_verification_provider" in data
    assert "citation_verification_model" in data
    assert "citation_verification_threshold" in data
    assert "citation_verification_local_url" in data
    assert "citation_verification_local_port" in data
    assert "citation_verification_auto_start_local" in data
    assert "citation_verification_timeout_sec" in data
    assert "citation_verification_turn_budget_sec" in data


def test_llm_config_get_and_post_verification_settings():
    """Verify /config GET returns and POST updates citation verification options."""
    get_res = client.get("/config")
    assert get_res.status_code == 200
    get_data = get_res.json()
    assert "citation_verification_enabled" in get_data
    assert "citation_verification_provider" in get_data
    assert "citation_verification_model" in get_data
    assert "citation_verification_threshold" in get_data

    with (
        patch("zocrates.config.save_zocrates_config") as mock_save,
        patch("zocrates.llm.get_llm_model"),
        patch("zocrates.agents.app.set_agent_models"),
    ):
        post_res = client.post(
            "/config",
            json={
                "citation_verification_enabled": True,
                "citation_verification_provider": "local_laya",
                "citation_verification_threshold": 0.75,
                "citation_verification_auto_start_local": False,
            },
        )
        assert post_res.status_code == 200
        mock_save.assert_called_once()
        saved_dict = mock_save.call_args[0][0]
        assert saved_dict["citation_verification_enabled"] is True
        assert saved_dict["citation_verification_provider"] == "local_laya"
        assert saved_dict["citation_verification_threshold"] == 0.75
        assert saved_dict["citation_verification_auto_start_local"] is False


def test_test_decision_model_endpoint():
    """Verify POST /llm/test_decision_model calls service and returns result."""
    with patch(
        "zocrates.rag.decision_service.test_decision_model_connectivity",
        return_value={"status": "ok", "message": "Connected successfully", "support_probability": 0.99},
    ) as mock_test:
        res = client.post(
            "/llm/test_decision_model",
            json={
                "provider": "openrouter",
                "model": "typesafe/jev-1.13",
                "openrouter_api_key": "sk-or-test",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "ok"
        mock_test.assert_called_once_with(
            provider="openrouter",
            model="typesafe/jev-1.13",
            local_url=None,
            openrouter_api_key="sk-or-test",
        )


def test_decision_tool_management_endpoints():
    """Verify /llm/decision_tool/status, install, start, and stop endpoints."""
    with patch(
        "zocrates.rag.decision_service.get_decision_tool_status",
        return_value={"status": "success", "server_running": True},
    ):
        res = client.get("/llm/decision_tool/status")
        assert res.status_code == 200
        assert res.json()["server_running"] is True

    with patch(
        "zocrates.rag.decision_service.install_laya_tool",
        return_value={"status": "success", "message": "Installed"},
    ):
        res = client.post("/llm/decision_tool/install")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

    with patch(
        "zocrates.rag.decision_service.start_laya_daemon",
        return_value={"status": "success", "message": "Started", "port": 8005},
    ):
        res = client.post("/llm/decision_tool/start")
        assert res.status_code == 200
        assert res.json()["status"] == "success"

    with patch(
        "zocrates.rag.decision_service.stop_laya_daemon",
        return_value={"status": "success", "message": "Stopped", "port": 8005},
    ):
        res = client.post("/llm/decision_tool/stop")
        assert res.status_code == 200
        assert res.json()["status"] == "success"


def test_cleanup_managed_daemon(tmp_path):
    """Verify _cleanup_managed_daemon terminates managed process and removes matching laya.pid."""
    pid_file = tmp_path / "laya.pid"
    port_file = tmp_path / "laya.port"
    pid_file.write_text("55555", encoding="utf-8")
    port_file.write_text("8005", encoding="utf-8")

    mock_proc = MagicMock()
    mock_proc.pid = 55555
    mock_proc.poll.return_value = None

    import zocrates.rag.decision_service as ds
    ds._managed_process = mock_proc
    ds._managed_port = 8005

    with patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path):
        ds._cleanup_managed_daemon()
        mock_proc.terminate.assert_called_once()
        assert not pid_file.exists()
        assert not port_file.exists()
        assert ds._managed_process is None
        assert ds._managed_port is None


def test_is_laya_process_exact_matching():
    """Verify _is_laya_process rejects substring false positives and matches valid Laya processes."""
    # 1. False positives that contain "laya" as substring
    p_himalaya = MagicMock()
    p_himalaya.name.return_value = "himalaya"
    p_himalaya.cmdline.return_value = ["/usr/bin/himalaya", "list"]
    p_himalaya.exe.return_value = "/usr/bin/himalaya"
    assert _is_laya_process(p_himalaya) is False

    p_pytest = MagicMock()
    p_pytest.name.return_value = "python3"
    p_pytest.cmdline.return_value = ["python3", "-m", "pytest", "tests/unit/test_citation_verifier_local_laya.py"]
    p_pytest.exe.return_value = "/usr/bin/python3"
    assert _is_laya_process(p_pytest) is False

    p_proj = MagicMock()
    p_proj.name.return_value = "python"
    p_proj.cmdline.return_value = ["python", "/home/alex/laya-project/server.py"]
    p_proj.exe.return_value = "/usr/bin/python"
    assert _is_laya_process(p_proj) is False

    # 2. Genuine Laya processes
    p_laya_serve = MagicMock()
    p_laya_serve.name.return_value = "laya-serve"
    p_laya_serve.cmdline.return_value = ["/home/alex/.local/bin/laya-serve", "--port", "8005"]
    p_laya_serve.exe.return_value = "/home/alex/.local/bin/laya-serve"
    assert _is_laya_process(p_laya_serve) is True

    p_uv_tool = MagicMock()
    p_uv_tool.name.return_value = "python3"
    p_uv_tool.cmdline.return_value = ["/home/alex/.local/share/uv/tools/laya/bin/python3", "-m", "laya.serve"]
    p_uv_tool.exe.return_value = "/home/alex/.local/share/uv/tools/laya/bin/python3"
    assert _is_laya_process(p_uv_tool) is True


def test_process_is_on_port_listen_only():
    """Verify _process_is_on_port requires connection to be in LISTEN status."""
    import psutil

    # 1. Active LISTEN socket on port 8005 -> True
    conn_listen = MagicMock()
    conn_listen.status = psutil.CONN_LISTEN
    conn_listen.laddr.port = 8005

    p_listening = MagicMock()
    p_listening.net_connections.return_value = [conn_listen]
    assert _process_is_on_port(p_listening, 8005) is True

    # 2. Outbound ESTABLISHED client connection to 8005 -> False
    conn_client = MagicMock()
    conn_client.status = psutil.CONN_ESTABLISHED
    conn_client.laddr.port = 45678  # client ephemeral port
    conn_client.raddr.port = 8005

    p_client = MagicMock()
    p_client.net_connections.return_value = [conn_client]
    assert _process_is_on_port(p_client, 8005) is False


def test_stop_laya_daemon_fallback_scan_terminates_genuine_laya(tmp_path):
    """Fallback scan identifies and terminates a genuine Laya daemon listening on port."""
    import psutil

    laya_proc = MagicMock()
    laya_proc.pid = 77777
    laya_proc.name.return_value = "laya-serve"
    laya_proc.cmdline.return_value = ["laya-serve", "--port", "8005"]

    conn = MagicMock()
    conn.status = psutil.CONN_LISTEN
    conn.laddr.port = 8005
    laya_proc.net_connections.return_value = [conn]

    with (
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=True),
        patch("psutil.process_iter", return_value=[laya_proc]),
    ):
        res = stop_laya_daemon(port=8005)
        assert res["status"] == "success"
        laya_proc.terminate.assert_called_once()


def test_stop_laya_daemon_fallback_scan_skips_unrelated_on_port(tmp_path):
    """Fallback scan ignores unrelated processes (e.g. nginx) listening on the port."""
    import psutil

    nginx_proc = MagicMock()
    nginx_proc.pid = 88888
    nginx_proc.name.return_value = "nginx"
    nginx_proc.cmdline.return_value = ["/usr/sbin/nginx", "-g", "daemon off;"]

    conn = MagicMock()
    conn.status = psutil.CONN_LISTEN
    conn.laddr.port = 8005
    nginx_proc.net_connections.return_value = [conn]

    with (
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=True),
        patch("psutil.process_iter", return_value=[nginx_proc]),
    ):
        res = stop_laya_daemon(port=8005)
        assert res["status"] == "success"
        nginx_proc.terminate.assert_not_called()
        nginx_proc.kill.assert_not_called()


def test_stop_laya_daemon_pid_port_mismatch_ignored(tmp_path):
    """If laya.port records a different port, stop_laya_daemon(port) leaves it alone."""
    pid_file = tmp_path / "laya.pid"
    port_file = tmp_path / "laya.port"
    pid_file.write_text("66666", encoding="utf-8")
    port_file.write_text("8006", encoding="utf-8")  # Different port!

    other_proc = MagicMock()
    other_proc.pid = 66666
    other_proc.name.return_value = "laya-serve"
    other_proc.cmdline.return_value = ["laya-serve", "--port", "8006"]

    with (
        patch("zocrates.rag.decision_service.get_zocrates_data_dir", return_value=tmp_path),
        patch("psutil.pid_exists", return_value=True),
        patch("psutil.Process", return_value=other_proc),
        patch("zocrates.rag.decision_service.check_laya_running", return_value=False),
    ):
        # We request stopping port 8005, but recorded port is 8006
        res = stop_laya_daemon(port=8005)
        assert res["status"] == "success"
        other_proc.terminate.assert_not_called()
        # The pid/port files for port 8006 are preserved
        assert pid_file.exists()
        assert port_file.exists()
