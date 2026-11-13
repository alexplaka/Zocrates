"""Unit tests for preference endpoints (/rag/model)."""

from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from zocrates.config import get_auth_token
from zocrates.fast_api_app import app

client = TestClient(app, headers={"Authorization": f"Bearer {get_auth_token()}"})


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.utils.services.get_session_service")
@patch("zocrates.utils.services.get_artifact_service")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_embedding_model_endpoint_local(mock_get_syncer, mock_art_svc, mock_sess_svc, mock_save):
    """Verify POST /rag/model switches to local mode and persists the config.

    Mocks the syncer, services, and embedder to avoid real I/O. Asserts that
    the endpoint returns a 200 with the expected model name and dimension, and
    that ``save_zocrates_config`` is called exactly once to persist the change.
    """
    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer

    with patch("zocrates.rag.embeddings.LocalEmbedder") as mock_embedder_cls:
        mock_embedder_instance = MagicMock()
        mock_embedder_instance._model_name = "jinaai/jina-embeddings-v5-text-nano"
        mock_embedder_instance.dimension = 768
        mock_embedder_cls.return_value = mock_embedder_instance

        response = client.post(
            "/rag/model",
            json={"mode": "local"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["active_model"] == "jinaai/jina-embeddings-v5-text-nano"
        assert data["requested_model"] == "jinaai/jina-embeddings-v5-text-nano"
        assert data["dimension"] == 768
        assert data["force_cpu"] is True
        mock_save.assert_called_once_with({"embedding_provider": "local"})


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.utils.services.get_session_service")
@patch("zocrates.utils.services.get_artifact_service")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_embedding_model_endpoint_cloud_with_model_name(
    mock_get_syncer, mock_art_svc, mock_sess_svc, mock_save
):
    """Verify POST /rag/model accepts mode='cloud' and model_name property."""
    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer

    with patch("zocrates.rag.embeddings.OpenRouterEmbedder") as mock_embedder_cls:
        mock_embedder_instance = MagicMock()
        mock_embedder_instance._model_name = "openrouter/baai/bge-m3"
        mock_embedder_instance.dimension = 1024
        mock_embedder_cls.return_value = mock_embedder_instance

        response = client.post(
            "/rag/model",
            json={"mode": "cloud", "model_name": "baai/bge-m3"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["requested_model"] == "baai/bge-m3"
        assert data["active_model"] == "openrouter/baai/bge-m3"
        assert data["dimension"] == 1024
        mock_save.assert_called_once_with({
            "embedding_provider": "cloud",
            "embedding_model": "baai/bge-m3",
            # Switch-time dimension resolution (Phase 7 Stage B): registry-known
            # slug persists its known dimension alongside the model change.
            "embedding_dimension": 1024,
        })
        mock_embedder_cls.assert_called_once_with(model_name="baai/bge-m3")


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_active_converter_endpoint(mock_get_syncer, mock_save):
    """Verify POST /rag/converter updates active converter ID, mode, and persists config."""
    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer
    response = client.post(
        "/rag/converter",
        json={"converter_id": "markitdown", "document_converter_mode": "local"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["document_converter_id"] == "markitdown"
    assert data["document_converter_mode"] == "local"
    mock_save.assert_called_once_with({
        "document_converter_id": "markitdown",
        "document_converter_mode": "local",
    })
    assert mock_syncer.processor._document_converter_id == "markitdown"


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_active_converter_vlm_preferences(mock_get_syncer, mock_save):
    """Verify POST /rag/converter updates VLM settings and persists config."""
    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer
    response = client.post(
        "/rag/converter",
        json={
            "enable_vlm_captioning": True,
            "vlm_captioning_provider": "openrouter",
            "vlm_captioning_model": "openrouter/google/gemma-4-26b-a4b-it:free",
            "vlm_captioning_backup_model": "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
            "vlm_max_images_per_doc": 12,
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    mock_save.assert_called_once_with({
        "enable_vlm_captioning": True,
        "vlm_captioning_provider": "openrouter",
        "vlm_captioning_model": "openrouter/google/gemma-4-26b-a4b-it:free",
        "vlm_captioning_backup_model": "openrouter/nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "vlm_max_images_per_doc": 12,
    })


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_active_converter_sets_datalab_api_key(mock_get_syncer, mock_save):
    """Verify POST /rag/converter with non-empty datalab_api_key sets get_datalab_api_key()."""
    from zocrates.config import get_datalab_api_key, save_datalab_api_key

    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer

    try:
        response = client.post(
            "/rag/converter",
            json={
                "converter_id": "datalab",
                "document_converter_mode": "cloud",
                "datalab_api_key": "test-datalab-valid-key-12345",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert get_datalab_api_key() == "test-datalab-valid-key-12345"
        mock_save.assert_called_once_with({
            "document_converter_id": "datalab",
            "document_converter_mode": "cloud",
            "datalab_api_key": "test-datalab-valid-key-12345",
        })
    finally:
        save_datalab_api_key(None)


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_active_converter_empty_datalab_api_key_does_not_clobber(mock_get_syncer, mock_save):
    """Verify POST /rag/converter with datalab_api_key: "" does NOT clobber an existing key."""
    from zocrates.config import get_datalab_api_key, save_datalab_api_key

    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer

    save_datalab_api_key("preexisting-active-datalab-key")
    try:
        response = client.post(
            "/rag/converter",
            json={
                "converter_id": "datalab",
                "document_converter_mode": "cloud",
                "datalab_api_key": "",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        # Must preserve existing active key without clobbering or removing from environment
        assert get_datalab_api_key() == "preexisting-active-datalab-key"
        # Must NOT include datalab_api_key in config updates
        mock_save.assert_called_once_with({
            "document_converter_id": "datalab",
            "document_converter_mode": "cloud",
        })
    finally:
        save_datalab_api_key(None)


def test_is_mcp_server_enabled_getter(tmp_path):
    """Verify is_mcp_server_enabled getter returns True by default and respects configured boolean."""
    import json
    from zocrates.config import DEFAULT_ENABLE_MCP_SERVER, is_mcp_server_enabled

    # When no config exists on disk, default is True
    assert DEFAULT_ENABLE_MCP_SERVER is True
    assert is_mcp_server_enabled(tmp_path) is True

    # When explicitly disabled on disk
    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"
    config_path.write_text(json.dumps({"enable_mcp_server": False}))

    assert is_mcp_server_enabled(tmp_path) is False

    # When explicitly enabled on disk
    config_path.write_text(json.dumps({"enable_mcp_server": True}))
    assert is_mcp_server_enabled(tmp_path) is True


def test_load_config_backfills_enable_mcp_server(tmp_path):
    """Verify load_zocrates_config backfills enable_mcp_server=True if missing on disk."""
    import json
    from zocrates.config import load_zocrates_config

    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"
    # Seed config without enable_mcp_server
    config_path.write_text(json.dumps({"llm_model": "google/gemini-3.8-flash"}))

    loaded = load_zocrates_config(tmp_path)
    assert loaded["enable_mcp_server"] is True

    # Verify backfill was written to disk
    persisted = json.loads(config_path.read_text())
    assert "enable_mcp_server" in persisted
    assert persisted["enable_mcp_server"] is True


def test_get_llm_config_includes_enable_mcp_server(tmp_path):
    """Verify GET /config returns enable_mcp_server default True."""
    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        res = client.get("/config")
        assert res.status_code == 200
        data = res.json()
        assert "enable_mcp_server" in data
        assert data["enable_mcp_server"] is True


def test_post_llm_config_roundtrip_enable_mcp_server(tmp_path):
    """Verify POST /config persists enable_mcp_server and GET reflects it."""
    import json

    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"

    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
         patch("zocrates.agents.app.set_agent_models"):
        # Disable MCP server
        res = client.post(
            "/config",
            json={"enable_mcp_server": False},
        )
        assert res.status_code == 200
        saved = json.loads(config_path.read_text())
        assert saved["enable_mcp_server"] is False

        # Confirm GET reflects new value
        res2 = client.get("/config")
        assert res2.status_code == 200
        assert res2.json()["enable_mcp_server"] is False

        # Re-enable MCP server
        res3 = client.post(
            "/config",
            json={"enable_mcp_server": True},
        )
        assert res3.status_code == 200
        saved2 = json.loads(config_path.read_text())
        assert saved2["enable_mcp_server"] is True

        res4 = client.get("/config")
        assert res4.status_code == 200
        assert res4.json()["enable_mcp_server"] is True


def test_storage_concurrency_sqlite_hardening(tmp_path):
    """Verify FTS and Meta DBs open with WAL mode, 5000ms busy_timeout, and thread-safe init."""
    from zocrates.rag.vector_store import _open_fts_db, _INITIALIZED_FTS_DBS
    from zocrates.rag.document_processor import _open_meta_db

    fts_db_path = tmp_path / "test_fts.db"
    assert fts_db_path not in _INITIALIZED_FTS_DBS

    conn_fts = _open_fts_db(fts_db_path)
    try:
        # Check WAL mode
        journal_mode = conn_fts.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        # Check busy_timeout is set to 5000ms
        busy_timeout = conn_fts.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert busy_timeout == 5000

        # Check path is recorded in initialized set
        assert fts_db_path in _INITIALIZED_FTS_DBS
    finally:
        conn_fts.close()

    # Second open should reuse initialized path without re-running schema
    conn_fts2 = _open_fts_db(fts_db_path)
    try:
        assert conn_fts2.execute("PRAGMA busy_timeout;").fetchone()[0] == 5000
    finally:
        conn_fts2.close()

    # Meta DB check
    cache_dir = tmp_path / "cache"
    conn_meta = _open_meta_db(cache_dir)
    try:
        meta_journal_mode = conn_meta.execute("PRAGMA journal_mode;").fetchone()[0]
        assert meta_journal_mode.lower() == "wal"

        meta_busy_timeout = conn_meta.execute("PRAGMA busy_timeout;").fetchone()[0]
        assert meta_busy_timeout == 5000
    finally:
        conn_meta.close()


def test_get_scholar_polite_email_getter(tmp_path):
    """Verify get_scholar_polite_email getter returns empty string by default and strips configured values."""
    import json
    from zocrates.config import DEFAULT_SCHOLAR_POLITE_EMAIL, get_scholar_polite_email

    assert DEFAULT_SCHOLAR_POLITE_EMAIL == ""
    assert get_scholar_polite_email(tmp_path) == ""

    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"
    config_path.write_text(json.dumps({"scholar_polite_email": "  researcher@uni.edu  "}))

    assert get_scholar_polite_email(tmp_path) == "researcher@uni.edu"


def test_load_config_backfills_scholar_polite_email(tmp_path):
    """Verify load_zocrates_config backfills scholar_polite_email="" if missing on disk."""
    import json
    from zocrates.config import load_zocrates_config

    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"
    config_path.write_text(json.dumps({"llm_model": "google/gemini-3.8-flash"}))

    loaded = load_zocrates_config(tmp_path)
    assert "scholar_polite_email" in loaded
    assert loaded["scholar_polite_email"] == ""

    persisted = json.loads(config_path.read_text())
    assert "scholar_polite_email" in persisted
    assert persisted["scholar_polite_email"] == ""


def test_get_llm_config_includes_scholar_polite_email(tmp_path):
    """Verify GET /config returns scholar_polite_email default empty string."""
    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        res = client.get("/config")
        assert res.status_code == 200
        data = res.json()
        assert "scholar_polite_email" in data
        assert data["scholar_polite_email"] == ""


def test_post_llm_config_roundtrip_scholar_polite_email(tmp_path):
    """Verify POST /config persists scholar_polite_email and GET reflects it."""
    import json

    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"

    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path), \
         patch("zocrates.agents.app.set_agent_models"):
        res = client.post(
            "/config",
            json={"scholar_polite_email": "author@lab.org"},
        )
        assert res.status_code == 200

        saved = json.loads(config_path.read_text())
        assert saved["scholar_polite_email"] == "author@lab.org"

        res2 = client.get("/config")
        assert res2.status_code == 200
        assert res2.json()["scholar_polite_email"] == "author@lab.org"


def test_status_endpoint_includes_scholar_polite_email(tmp_path):
    """Verify GET /status includes scholar_polite_email in unregistered state."""
    with patch("zocrates.config.resolve_zotero_data_dir", return_value=tmp_path):
        res = client.get("/status")
        assert res.status_code == 200
        data = res.json()
        assert "scholar_polite_email" in data
        assert data["scholar_polite_email"] == ""


@patch("zocrates.fast_api_app.initialize_services")
@patch("zocrates.fast_api_app.setup_file_logging")
@patch("zocrates.agents.app.app")
def test_register_persists_scholar_polite_email(mock_adk_app, mock_logging, mock_init, tmp_path):
    """Verify POST /register persists scholar_polite_email to zocrates_config.json."""
    import json
    from zocrates.config import get_scholar_polite_email

    (tmp_path / "zotero.sqlite").touch()
    (tmp_path / "storage").mkdir(parents=True, exist_ok=True)

    mock_syncer = MagicMock()
    mock_syncer.sync_metadata_staleness.return_value = {"updated": 0}
    mock_init.return_value = mock_syncer
    mock_adk_app.name = "zocrates"

    res = client.post(
        "/register",
        json={
            "zotero_data_dir": str(tmp_path),
            "scholar_polite_email": "scholar@university.edu",
        },
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"

    # Verify email persisted to disk
    assert get_scholar_polite_email(tmp_path) == "scholar@university.edu"
    config_path = tmp_path / "zocrates-data" / "zocrates_config.json"
    assert config_path.exists()
    saved = json.loads(config_path.read_text())
    assert saved["scholar_polite_email"] == "scholar@university.edu"


def test_discovery_engine_defaults_to_configured_polite_email(tmp_path):
    """Verify get_default_discovery_engine uses scholar_polite_email from config."""
    import json
    from zocrates.discovery.engine import get_default_discovery_engine

    config_dir = tmp_path / "zocrates-data"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "zocrates_config.json"
    config_path.write_text(json.dumps({"scholar_polite_email": "polite_bot@research.org"}))

    engine = get_default_discovery_engine(zotero_data_dir=tmp_path)
    assert engine.email == "polite_bot@research.org"
    assert engine.provider.email == "polite_bot@research.org"
    assert engine.abstract_fallback.email == "polite_bot@research.org"
    assert engine.unpaywall.email == "polite_bot@research.org"






@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.utils.services.get_session_service")
@patch("zocrates.utils.services.get_artifact_service")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_embedding_model_resolves_dimension_from_registry(
    mock_get_syncer, mock_art_svc, mock_sess_svc, mock_save
):
    """Phase 7 Stage B: a registry-known cloud slug persists its known dimension
    at switch time, so the next boot is correct from the start (no stale 768
    inherited from the previous model)."""
    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer

    with patch("zocrates.rag.embeddings.OpenRouterEmbedder") as mock_cloud:
        mock_embedder_instance = MagicMock()
        mock_embedder_instance._model_name = "openrouter/nvidia/nemotron-3-embed-1b:free"
        mock_embedder_instance.dimension = 1024
        mock_cloud.return_value = mock_embedder_instance

        response = client.post(
            "/rag/model",
            json={"mode": "cloud", "model_name": "openrouter/nvidia/nemotron-3-embed-1b:free"},
        )

        assert response.status_code == 200
        saved = mock_save.call_args[0][0]
        assert saved["embedding_dimension"] == 1024


@patch("zocrates.config.save_zocrates_config")
@patch("zocrates.utils.services.get_session_service")
@patch("zocrates.utils.services.get_artifact_service")
@patch("zocrates.fast_api_app.get_syncer")
def test_update_embedding_model_explicit_dimension_wins_over_registry(
    mock_get_syncer, mock_art_svc, mock_sess_svc, mock_save
):
    """Phase 7 Stage B: an explicit request dimension is respected verbatim and
    is never overridden by the known-dimensions registry."""
    mock_syncer = MagicMock()
    mock_get_syncer.return_value = mock_syncer

    with patch("zocrates.rag.embeddings.OpenRouterEmbedder") as mock_cloud:
        mock_embedder_instance = MagicMock()
        mock_embedder_instance._model_name = "openrouter/nvidia/nemotron-3-embed-1b:free"
        mock_embedder_instance.dimension = 2048
        mock_cloud.return_value = mock_embedder_instance

        response = client.post(
            "/rag/model",
            json={
                "mode": "cloud",
                "model_name": "openrouter/nvidia/nemotron-3-embed-1b:free",
                "dimension": 2048,
            },
        )

        assert response.status_code == 200
        saved = mock_save.call_args[0][0]
        assert saved["embedding_dimension"] == 2048
