"""Unit tests for app.config backend_port settings, loading, backfilling, and resolution."""

import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zocrates.config import (
    DEFAULT_BACKEND_PORT,
    DEFAULT_CONFIG,
    DEFAULT_DATALAB_MERGE_CROSS_PAGE,
    _CONFIG_CACHE,
    _CONFIG_CACHE_FINGERPRINT,
    _CONFIG_CACHE_MTIME_NS,
    _resolve_backend_port,
    check_config_freshness,
    clear_config_cache,
    get_auto_render_mermaid_diagrams,
    get_backend_port,
    get_citation_verification_enabled,
    get_citations_enabled,
    get_compaction_config,
    get_considered_sources_disclosure,
    get_conversion_timeout_seconds,
    get_datalab_merge_cross_page,
    get_datalab_non_pdf_fallback,
    get_docling_mode,
    get_document_converter_id,
    get_document_converter_mode,
    get_enable_vlm_captioning,
    get_force_cpu_pref,
    get_graph_relations_config,
    get_max_saved_sessions,
    get_prune_empty_sessions,
    get_references_section_enabled,
    get_scholar_polite_email,
    get_search_harness_config,
    get_semantic_similarity_threshold,
    get_session_auto_vacuum_on_startup,
    get_session_maintenance_config,
    get_session_retention_days,
    get_sync_auto_abort_failures,
    get_user_research_profile,
    get_vlm_captioning_backup_model,
    get_vlm_captioning_local_model,
    get_vlm_captioning_model,
    get_vlm_captioning_provider,
    get_vlm_max_images_per_doc,
    get_vlm_timeout_seconds,
    is_adaptive_compaction_enabled,
    is_context_compaction_enabled,
    is_mcp_server_enabled,
    get_config_path,
    load_zocrates_config,
    save_zocrates_config,
)


class TestBackendPortConfig:
    """Tests for backend_port defaults, environment handling, and disk backfill."""

    def test_default_backend_port_constant_and_config(self) -> None:
        """Verify DEFAULT_BACKEND_PORT is None and DEFAULT_CONFIG has 'backend_port': None."""
        assert DEFAULT_BACKEND_PORT is None
        assert "backend_port" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["backend_port"] is None

    def test_resolve_backend_port_default_and_env(self) -> None:
        """Verify _resolve_backend_port returns the base port when unset, or parsed int when env is valid.

        Data-dir resolution and the config peek are patched so the base-port
        fallback (no offset tier) stays deterministic regardless of whether a
        Zotero profile is discoverable on the host machine.
        """
        with patch("zocrates.config._resolve_import_time_zotero_data_dir", return_value=None), \
                patch("zocrates.config._parse_config_backend_port", return_value=None), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BACKEND_PORT", None)
            assert _resolve_backend_port() == 8765

            os.environ["BACKEND_PORT"] = "9090"
            assert _resolve_backend_port() == 9090

            os.environ["BACKEND_PORT"] = "invalid"
            assert _resolve_backend_port() == 8765

    def test_load_config_backfills_backend_port_if_missing(self, tmp_path: Path) -> None:
        """Verify load_zocrates_config backfills backend_port: None to an existing config file."""
        cfg_path = get_config_path(tmp_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        # Write existing config omitting backend_port
        initial_data = {
            "llm_provider": "openrouter_llm",
            "llm_model": "test-model",
        }
        with open(cfg_path, "w") as f:
            json.dump(initial_data, f)

        cfg = load_zocrates_config(tmp_path)
        assert "backend_port" in cfg
        assert cfg["backend_port"] is None

        # Verify disk file was updated with backfilled key
        with open(cfg_path) as f:
            persisted = json.load(f)
        assert "backend_port" in persisted
        assert persisted["backend_port"] is None

    def test_load_config_preserves_explicit_backend_port(self, tmp_path: Path) -> None:
        """Verify load_zocrates_config preserves existing backend_port value."""
        cfg_path = get_config_path(tmp_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)

        initial_data = {
            "llm_provider": "openrouter_llm",
            "backend_port": 8888,
        }
        with open(cfg_path, "w") as f:
            json.dump(initial_data, f)

        cfg = load_zocrates_config(tmp_path)
        assert cfg["backend_port"] == 8888

    def test_save_config_persists_and_validates_backend_port(self, tmp_path: Path) -> None:
        """Verify save_zocrates_config persists valid port and rejects invalid ports."""
        save_zocrates_config({"backend_port": 9123}, zotero_data_dir=tmp_path)
        cfg = load_zocrates_config(tmp_path)
        assert cfg["backend_port"] == 9123

        # Reset to None
        save_zocrates_config({"backend_port": None}, zotero_data_dir=tmp_path)
        cfg = load_zocrates_config(tmp_path)
        assert cfg["backend_port"] is None

        # Invalid ports
        with pytest.raises(ValueError, match="backend_port"):
            save_zocrates_config({"backend_port": 0}, zotero_data_dir=tmp_path)

        with pytest.raises(ValueError, match="backend_port"):
            save_zocrates_config({"backend_port": 70000}, zotero_data_dir=tmp_path)

        with pytest.raises(ValueError, match="backend_port"):
            save_zocrates_config({"backend_port": "not-a-port"}, zotero_data_dir=tmp_path)

    def test_get_backend_port_resolution_order(self, tmp_path: Path) -> None:
        """Verify get_backend_port checks BACKEND_PORT env var first, then config, then None."""
        # 1. Neither env nor config -> None
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BACKEND_PORT", None)
            assert get_backend_port(tmp_path) is None

            # 2. Config has explicit port -> returns config port
            save_zocrates_config({"backend_port": 8769}, zotero_data_dir=tmp_path)
            assert get_backend_port(tmp_path) == 8769

            # 3. Environment variable set -> overrides / takes priority
            os.environ["BACKEND_PORT"] = "9999"
            assert get_backend_port(tmp_path) == 9999


def test_load_config_backfills_llm_base_url_if_missing(tmp_path: Path) -> None:
    """SSOT guarantee: llm_base_url (server base URL for local providers) must be
    materialized in DEFAULT_CONFIG and backfilled to pre-existing config files
    on disk, since the Preferences UI reads/writes it via /config."""
    from zocrates.config import DEFAULT_LLM_BASE_URL

    assert "llm_base_url" in DEFAULT_CONFIG
    assert DEFAULT_CONFIG["llm_base_url"] == DEFAULT_LLM_BASE_URL

    cfg_path = get_config_path(tmp_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    # Write existing config omitting llm_base_url
    initial_data = {"llm_provider": "openrouter_llm", "llm_model": "test-model"}
    with open(cfg_path, "w") as f:
        json.dump(initial_data, f)

    cfg = load_zocrates_config(tmp_path)
    assert cfg["llm_base_url"] == DEFAULT_LLM_BASE_URL

    # Verify disk file was updated with the backfilled key
    with open(cfg_path) as f:
        persisted = json.load(f)
    assert "llm_base_url" in persisted
    assert persisted["llm_base_url"] == DEFAULT_LLM_BASE_URL

    # Explicit user value must be preserved on subsequent loads
    save_zocrates_config({"llm_base_url": "http://localhost:11434"}, zotero_data_dir=tmp_path)
    cfg = load_zocrates_config(tmp_path)
    assert cfg["llm_base_url"] == "http://localhost:11434"


def test_auto_spawn_backend_default_backfill_and_getter(tmp_path: Path) -> None:
    """SSOT guarantee: auto_spawn_backend defaults to True, is materialized in
    DEFAULT_CONFIG, is backfilled to pre-existing config files on disk, and is
    readable via get_auto_spawn_backend. Developer profiles set it to false so
    the Zotero bootstrap attaches warm (or stays offline) without provisioning."""
    from zocrates.config import DEFAULT_AUTO_SPAWN_BACKEND, get_auto_spawn_backend

    assert DEFAULT_AUTO_SPAWN_BACKEND is True
    assert DEFAULT_CONFIG["auto_spawn_backend"] is True

    cfg_path = get_config_path(tmp_path)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    # Write existing config omitting auto_spawn_backend
    initial_data = {"llm_provider": "openrouter_llm", "llm_model": "test-model"}
    with open(cfg_path, "w") as f:
        json.dump(initial_data, f)

    cfg = load_zocrates_config(tmp_path)
    assert cfg["auto_spawn_backend"] is True

    # Verify disk file was updated with the backfilled key
    with open(cfg_path) as f:
        persisted = json.load(f)
    assert persisted["auto_spawn_backend"] is True

    # Explicit false (dev profile opt-out) must be preserved on reload and
    # honored by the getter, including via the cfg= fast path.
    save_zocrates_config({"auto_spawn_backend": False}, zotero_data_dir=tmp_path)
    assert load_zocrates_config(tmp_path)["auto_spawn_backend"] is False
    assert get_auto_spawn_backend(tmp_path) is False
    assert get_auto_spawn_backend(cfg={"auto_spawn_backend": False}) is False

    # Missing key with cfg fast path falls back to the default (True)
    assert get_auto_spawn_backend(cfg={}) is True


def test_datalab_merge_cross_page_default_override_and_backfill(tmp_path: Path) -> None:
    """Verify DEFAULT_DATALAB_MERGE_CROSS_PAGE is False, env var overrides,
    cfg parameter fast-paths, and FileLock backfills missing keys on disk."""
    clear_config_cache()
    assert DEFAULT_DATALAB_MERGE_CROSS_PAGE is False
    assert DEFAULT_CONFIG["datalab_merge_cross_page"] is False

    # Default when no file exists and no env var
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATALAB_MERGE_CROSS_PAGE", None)
        assert get_datalab_merge_cross_page(tmp_path) is False
        assert get_datalab_merge_cross_page(cfg={}) is False
        assert get_datalab_merge_cross_page(cfg={"datalab_merge_cross_page": True}) is True

    # Environment variable override truthiness
    for truthy in ("1", "true", "True", "yes", "YES", "on", "On", " 1 "):
        with patch.dict(os.environ, {"DATALAB_MERGE_CROSS_PAGE": truthy}):
            assert get_datalab_merge_cross_page(tmp_path) is True
            assert get_datalab_merge_cross_page(cfg={"datalab_merge_cross_page": False}) is True

    # Environment variable override falsiness
    for falsy in ("0", "false", "no", "off", "anything_else"):
        with patch.dict(os.environ, {"DATALAB_MERGE_CROSS_PAGE": falsy}):
            assert get_datalab_merge_cross_page(tmp_path) is False
            assert get_datalab_merge_cross_page(cfg={"datalab_merge_cross_page": True}) is False

    # Disk backfill under FileLock
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("DATALAB_MERGE_CROSS_PAGE", None)
        clear_config_cache()
        cfg_path = get_config_path(tmp_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump({"llm_provider": "openrouter_llm"}, f)

        loaded = load_zocrates_config(tmp_path)
        assert loaded["datalab_merge_cross_page"] is False

        with open(cfg_path) as f:
            persisted = json.load(f)
        assert persisted["datalab_merge_cross_page"] is False

        # User update is persisted and honored
        save_zocrates_config({"datalab_merge_cross_page": True}, zotero_data_dir=tmp_path)
        assert load_zocrates_config(tmp_path)["datalab_merge_cross_page"] is True
        assert get_datalab_merge_cross_page(tmp_path) is True


def test_sqlalchemy_declares_asyncio_extra_and_tested_cap() -> None:
    """Guard the greenlet regression: wheel-provisioned venvs resolve dependency
    RANGES (not the dev lockfile). SQLAlchemy 2.1 removed the automatic greenlet
    dependency, which broke ADK's DatabaseSessionService (async engine creation)
    in prod venvs. The pyproject entry must declare the [asyncio] extra and stay
    capped to the test-verified minor."""
    import tomllib
    from pathlib import Path

    pyproject = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text())
    deps = pyproject["project"]["dependencies"]
    sqlalchemy_entries = [d for d in deps if d.startswith("sqlalchemy")]
    assert len(sqlalchemy_entries) == 1, f"expected exactly one sqlalchemy entry, got {sqlalchemy_entries}"
    entry = sqlalchemy_entries[0]
    assert "[asyncio]" in entry, f"sqlalchemy must declare the [asyncio] extra (greenlet): {entry}"
    assert "<2.1" in entry, f"sqlalchemy must stay capped to the test-verified minor: {entry}"


class TestConfigCacheAndGetters:
    """Tests for in-memory config caching, mtime invalidation, and getter parameterization."""

    def test_config_cache_hit_bypasses_file_read_and_lock(self, tmp_path: Path) -> None:
        """Verify that on cache hit, load_zocrates_config returns cached dict without
        re-reading disk or acquiring file locks."""
        clear_config_cache()
        cfg_path = get_config_path(tmp_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump({"llm_model": "initial-model"}, f)

        # First load populates cache
        first = load_zocrates_config(tmp_path)
        assert first["llm_model"] == "initial-model"

        # Second load: patch FileLock and builtins.open
        with patch("zocrates.config.FileLock") as mock_lock, \
                patch("builtins.open") as mock_open:
            second = load_zocrates_config(tmp_path)
            assert second["llm_model"] == "initial-model"
            mock_lock.assert_not_called()
            mock_open.assert_not_called()

    def test_config_cache_invalidation_on_disk_mtime_change(self, tmp_path: Path) -> None:
        """Verify that modifying zocrates_config.json on disk (modifying mtime)
        immediately invalidates cache and returns fresh data."""
        clear_config_cache()
        cfg_path = get_config_path(tmp_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump({"llm_model": "model-v1"}, f)

        first = load_zocrates_config(tmp_path)
        assert first["llm_model"] == "model-v1"

        # Update disk file with new content and advance mtime
        time.sleep(0.01)
        with open(cfg_path, "w") as f:
            json.dump({"llm_model": "model-v2"}, f)
        stat_before = cfg_path.stat()
        os.utime(cfg_path, (stat_before.st_atime, stat_before.st_mtime + 5.0))

        second = load_zocrates_config(tmp_path)
        assert second["llm_model"] == "model-v2"

    def test_save_config_invalidates_cache_and_backfills(self, tmp_path: Path) -> None:
        """Verify save_zocrates_config invalidates in-memory cache so subsequent
        load_zocrates_config runs backfills and persists them to disk."""
        clear_config_cache()
        save_zocrates_config({"scholar_polite_email": "researcher@example.com"}, zotero_data_dir=tmp_path)

        resolved_path = get_config_path(tmp_path).resolve()
        assert resolved_path not in _CONFIG_CACHE

        cfg = load_zocrates_config(tmp_path)
        assert cfg["scholar_polite_email"] == "researcher@example.com"
        assert "override_agent_models" in cfg
        assert "search_harness" in cfg
        assert resolved_path in _CONFIG_CACHE

        # Verify disk file has backfilled keys
        cfg_path = get_config_path(tmp_path)
        with open(cfg_path) as f:
            persisted = json.load(f)
        assert persisted["scholar_polite_email"] == "researcher@example.com"
        assert "override_agent_models" in persisted
        assert "search_harness" in persisted

    def test_clear_config_cache(self, tmp_path: Path) -> None:
        """Verify clear_config_cache empties the cache for a specific dir or globally."""
        save_zocrates_config({"llm_model": "cached-model"}, zotero_data_dir=tmp_path)
        load_zocrates_config(tmp_path)
        resolved_path = get_config_path(tmp_path).resolve()
        assert resolved_path in _CONFIG_CACHE

        # Clear specific dir
        clear_config_cache(tmp_path)
        assert resolved_path not in _CONFIG_CACHE
        assert resolved_path not in _CONFIG_CACHE_FINGERPRINT

        # Populate again, then test global clear
        load_zocrates_config(tmp_path)
        assert resolved_path in _CONFIG_CACHE
        clear_config_cache()
        assert len(_CONFIG_CACHE) == 0
        assert len(_CONFIG_CACHE_FINGERPRINT) == 0

    def test_config_cache_isolation_on_mutation(self, tmp_path: Path) -> None:
        """Verify mutating returned dict from load_zocrates_config does NOT mutate
        the in-memory cache or subsequent loads."""
        clear_config_cache()
        cfg_path = get_config_path(tmp_path)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "w") as f:
            json.dump({"llm_model": "original-model"}, f)

        first = load_zocrates_config(tmp_path)
        assert first["llm_model"] == "original-model"

        # Mutate top-level and nested structure in returned dict
        first["llm_model"] = "mutated-model"
        first["search_harness"]["max_retrieval_tools"] = 999

        # Subsequent load must return unmutated data from cache
        second = load_zocrates_config(tmp_path)
        assert second["llm_model"] == "original-model"
        assert second["search_harness"]["max_retrieval_tools"] != 999

        # Direct cache inspection
        resolved_path = cfg_path.resolve()
        assert _CONFIG_CACHE[resolved_path]["llm_model"] == "original-model"
        assert _CONFIG_CACHE[resolved_path]["search_harness"]["max_retrieval_tools"] != 999

    def test_external_edit_load_does_not_stamp_mtime_or_blind_freshness(self, tmp_path: Path) -> None:
        """Verify read-only load_zocrates_config calls do NOT stamp _last_config_mtime,
        so check_config_freshness() correctly detects external edits."""
        clear_config_cache()
        import zocrates.config as _cfg

        old_mtime = _cfg._last_config_mtime
        try:
            _cfg._last_config_mtime = 0.0

            # Create fully backfilled file so load does not set needs_save
            cfg = load_zocrates_config(tmp_path)
            save_zocrates_config(cfg, zotero_data_dir=tmp_path)

            # Baseline check_config_freshness initializes sentinel and returns False
            assert check_config_freshness(tmp_path) is False

            # Simulate external edit and advance mtime
            cfg_path = get_config_path(tmp_path)
            time.sleep(0.02)
            with open(cfg_path) as f:
                data = json.load(f)
            data["llm_model"] = "externally-edited-model"
            with open(cfg_path, "w") as f:
                json.dump(data, f)
            stat = cfg_path.stat()
            os.utime(cfg_path, (stat.st_atime, stat.st_mtime + 5.0))

            # A read-only load_zocrates_config (such as /status polling)
            loaded = load_zocrates_config(tmp_path)
            assert loaded["llm_model"] == "externally-edited-model"

            # check_config_freshness must still return True because load_zocrates_config
            # did NOT stamp _last_config_mtime!
            assert check_config_freshness(tmp_path) is True
        finally:
            _cfg._last_config_mtime = old_mtime

    def test_env_var_overrides_beat_cfg_parameter(self, tmp_path: Path) -> None:
        """Verify environment variables take precedence over explicitly passed cfg dict."""
        mock_cfg: dict[str, Any] = {
            "force_cpu": False,
            "backend_port": 8765,
            "document_converter_mode": "cloud",
            "document_converter_id": "docling_local",
            "docling_mode": "full",
            "datalab_non_pdf_fallback": "docling_local",
            "datalab_merge_cross_page": False,
            "enable_vlm_captioning": False,
            "vlm_captioning_provider": "ollama",
            "vlm_captioning_model": "test-vlm-model",
            "vlm_captioning_backup_model": "test-vlm-backup",
            "vlm_captioning_local_model": "llama-vision",
            "vlm_max_images_per_doc": 10,
            "vlm_timeout_seconds": 30,
            "conversion_timeout_seconds": 60,
        }

        with patch.dict(
            os.environ,
            {
                "FORCE_CPU": "true",
                "BACKEND_PORT": "9999",
                "DOCUMENT_CONVERTER_MODE": "local",
                "DOCUMENT_CONVERTER_ID": "datalab",
                "DOCUMENT_CONVERTER_DOCLING_MODE": "fast",
                "DATALAB_NON_PDF_FALLBACK": "markitdown",
                "DATALAB_MERGE_CROSS_PAGE": "1",
                "ENABLE_VLM_CAPTIONING": "true",
                "VLM_CAPTIONING_PROVIDER": "openrouter",
                "VLM_CAPTIONING_MODEL": "override-model",
                "VLM_CAPTIONING_BACKUP_MODEL": "override-backup",
                "VLM_CAPTIONING_LOCAL_MODEL": "override-local",
                "VLM_MAX_IMAGES_PER_DOC": "99",
                "VLM_TIMEOUT_SECONDS": "120",
                "CONVERSION_TIMEOUT_SECONDS": "300",
            },
            clear=False,
        ):
            assert get_force_cpu_pref(tmp_path, cfg=mock_cfg) is True
            assert get_backend_port(tmp_path, cfg=mock_cfg) == 9999
            assert get_document_converter_mode(tmp_path, cfg=mock_cfg) == "local"
            assert get_document_converter_id(tmp_path, cfg=mock_cfg) == "datalab"
            assert get_docling_mode(tmp_path, cfg=mock_cfg) == "fast"
            assert get_datalab_non_pdf_fallback(tmp_path, cfg=mock_cfg) == "markitdown"
            assert get_datalab_merge_cross_page(tmp_path, cfg=mock_cfg) is True
            assert get_enable_vlm_captioning(tmp_path, cfg=mock_cfg) is True
            assert get_vlm_captioning_provider(tmp_path, cfg=mock_cfg) == "openrouter"
            assert get_vlm_captioning_model(tmp_path, cfg=mock_cfg) == "override-model"
            assert get_vlm_captioning_backup_model(tmp_path, cfg=mock_cfg) == "override-backup"
            assert get_vlm_captioning_local_model(tmp_path, cfg=mock_cfg) == "override-local"
            assert get_vlm_max_images_per_doc(tmp_path, cfg=mock_cfg) == 99
            assert get_vlm_timeout_seconds(tmp_path, cfg=mock_cfg) == 120
            assert get_conversion_timeout_seconds(tmp_path, cfg=mock_cfg) == 300

    def test_helper_getters_with_cfg_bypass_disk(self, tmp_path: Path) -> None:
        """Verify passing cfg to helper getters bypasses disk read entirely and returns values matching cfg."""
        mock_cfg: dict[str, Any] = {
            "force_cpu": True,
            "document_converter_mode": "cloud",
            "document_converter_id": "docling_local",
            "docling_mode": "full",
            "datalab_non_pdf_fallback": "docling_local",
            "datalab_merge_cross_page": True,
            "enable_vlm_captioning": False,
            "vlm_captioning_provider": "ollama",
            "vlm_captioning_model": "test-vlm-model",
            "vlm_captioning_backup_model": "test-vlm-backup",
            "vlm_captioning_local_model": "llama-vision",
            "vlm_max_images_per_doc": 42,
            "vlm_timeout_seconds": 99,
            "semantic_similarity_threshold": 0.88,
            "sync_auto_abort_failures": 7,
            "search_harness": {"max_retrieval_tools_per_run": 15},
            "graph_relations": {"lpa_max_iterations": 25},
            "citations_enabled": False,
            "references_section_enabled": False,
            "considered_sources_disclosure": False,
            "auto_render_mermaid_diagrams": True,
            "enable_mcp_server": False,
            "citation_verification_enabled": True,
            "scholar_polite_email": "test@domain.org",
            "user_research_profile": "Bioinformatics specialist",
            "enable_adaptive_compaction": False,
            "max_saved_sessions": 50,
            "session_retention_days": 30,
            "session_db_auto_vacuum_on_startup": True,
            "prune_empty_sessions": False,
            "backend_port": 8901,
            "conversion_timeout_seconds": 180,
        }

        # Ensure env vars that would override cfg are unset
        with patch.dict(os.environ, {}, clear=False), \
                patch("zocrates.config.load_zocrates_config") as mock_load:
            for k in [
                "FORCE_CPU",
                "DOCUMENT_CONVERTER_MODE",
                "DOCUMENT_CONVERTER_ID",
                "DOCUMENT_CONVERTER_DOCLING_MODE",
                "DOCLING_MODE",
                "DATALAB_NON_PDF_FALLBACK",
                "DATALAB_MERGE_CROSS_PAGE",
                "ENABLE_VLM_CAPTIONING",
                "VLM_CAPTIONING_PROVIDER",
                "VLM_CAPTIONING_MODEL",
                "VLM_CAPTIONING_BACKUP_MODEL",
                "VLM_CAPTIONING_LOCAL_MODEL",
                "VLM_MAX_IMAGES_PER_DOC",
                "VLM_TIMEOUT_SECONDS",
                "BACKEND_PORT",
                "CONVERSION_TIMEOUT_SECONDS",
            ]:
                os.environ.pop(k, None)

            assert get_force_cpu_pref(tmp_path, cfg=mock_cfg) is True
            assert get_document_converter_mode(tmp_path, cfg=mock_cfg) == "cloud"
            assert get_document_converter_id(tmp_path, cfg=mock_cfg) == "docling_local"
            assert get_docling_mode(tmp_path, cfg=mock_cfg) == "full"
            assert get_datalab_non_pdf_fallback(tmp_path, cfg=mock_cfg) == "docling_local"
            assert get_datalab_merge_cross_page(tmp_path, cfg=mock_cfg) is True
            assert get_enable_vlm_captioning(tmp_path, cfg=mock_cfg) is False
            assert get_vlm_captioning_provider(tmp_path, cfg=mock_cfg) == "ollama"
            assert get_vlm_captioning_model(tmp_path, cfg=mock_cfg) == "test-vlm-model"
            assert get_vlm_captioning_backup_model(tmp_path, cfg=mock_cfg) == "test-vlm-backup"
            assert get_vlm_captioning_local_model(tmp_path, cfg=mock_cfg) == "llama-vision"
            assert get_vlm_max_images_per_doc(tmp_path, cfg=mock_cfg) == 42
            assert get_vlm_timeout_seconds(tmp_path, cfg=mock_cfg) == 99
            assert get_semantic_similarity_threshold(tmp_path, cfg=mock_cfg) == 0.88
            assert get_sync_auto_abort_failures(tmp_path, cfg=mock_cfg) == 7
            assert get_search_harness_config(tmp_path, cfg=mock_cfg)["max_retrieval_tools_per_run"] == 15
            assert get_graph_relations_config(tmp_path, cfg=mock_cfg)["lpa_max_iterations"] == 25
            assert get_citations_enabled(tmp_path, cfg=mock_cfg) is False
            assert get_references_section_enabled(tmp_path, cfg=mock_cfg) is False
            assert get_considered_sources_disclosure(tmp_path, cfg=mock_cfg) is False
            assert get_auto_render_mermaid_diagrams(tmp_path, cfg=mock_cfg) is True
            assert is_mcp_server_enabled(tmp_path, cfg=mock_cfg) is False
            assert get_citation_verification_enabled(tmp_path, cfg=mock_cfg) is True
            assert get_scholar_polite_email(tmp_path, cfg=mock_cfg) == "test@domain.org"
            assert get_user_research_profile(tmp_path, cfg=mock_cfg) == "Bioinformatics specialist"
            assert is_adaptive_compaction_enabled(tmp_path, cfg=mock_cfg) is False
            assert is_context_compaction_enabled(tmp_path, cfg=mock_cfg) is False
            comp_cfg = get_compaction_config(tmp_path, cfg=mock_cfg)
            assert comp_cfg["enable_adaptive_compaction"] is False
            assert get_max_saved_sessions(tmp_path, cfg=mock_cfg) == 50
            assert get_session_retention_days(tmp_path, cfg=mock_cfg) == 30
            assert get_session_auto_vacuum_on_startup(tmp_path, cfg=mock_cfg) is True
            assert get_prune_empty_sessions(tmp_path, cfg=mock_cfg) is False
            maint_cfg = get_session_maintenance_config(tmp_path, cfg=mock_cfg)
            assert maint_cfg["max_saved_sessions"] == 50
            assert maint_cfg["prune_empty_sessions"] is False
            assert get_backend_port(tmp_path, cfg=mock_cfg) == 8901
            assert get_conversion_timeout_seconds(tmp_path, cfg=mock_cfg) == 180

            mock_load.assert_not_called()


class TestSecureApiKeyStorage:
    """Tests for in-memory session API key management via os.environ."""

    def test_save_get_and_delete_secure_api_key(self, monkeypatch) -> None:
        from pydantic import SecretStr
        from zocrates.config import (
            delete_secure_api_key,
            get_secure_api_key,
            save_secure_api_key,
        )

        monkeypatch.delenv("TEST_STORAGE_API_KEY", raising=False)

        # Initially key is not present
        assert get_secure_api_key("test_storage_key", ("TEST_STORAGE_API_KEY",)) is None

        # Save string key
        save_secure_api_key("sk-test-secret-123", key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        assert os.environ.get("TEST_STORAGE_API_KEY") == "sk-test-secret-123"
        assert get_secure_api_key("test_storage_key", ("TEST_STORAGE_API_KEY",)) == "sk-test-secret-123"

        # Save SecretStr key
        save_secure_api_key(SecretStr("sk-test-secret-456"), key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        assert os.environ.get("TEST_STORAGE_API_KEY") == "sk-test-secret-456"
        assert get_secure_api_key("test_storage_key", ("TEST_STORAGE_API_KEY",)) == "sk-test-secret-456"

        # Delete key
        delete_secure_api_key(key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        assert os.environ.get("TEST_STORAGE_API_KEY") is None
        assert get_secure_api_key("test_storage_key", ("TEST_STORAGE_API_KEY",)) is None

        # Save None or whitespace clears key
        save_secure_api_key("sk-test-temp", key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        assert os.environ.get("TEST_STORAGE_API_KEY") == "sk-test-temp"
        save_secure_api_key("", key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        assert os.environ.get("TEST_STORAGE_API_KEY") is None

        save_secure_api_key("sk-test-temp", key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        save_secure_api_key("   ", key_name="test_storage_key", env_var="TEST_STORAGE_API_KEY")
        assert os.environ.get("TEST_STORAGE_API_KEY") is None

    def test_get_secure_api_key_checks_candidate_env_vars_in_order(self, monkeypatch) -> None:
        from zocrates.config import get_secure_api_key

        monkeypatch.delenv("PRIMARY_TEST_KEY", raising=False)
        monkeypatch.delenv("FALLBACK_TEST_KEY", raising=False)

        assert get_secure_api_key("test_key", ("PRIMARY_TEST_KEY", "FALLBACK_TEST_KEY")) is None

        monkeypatch.setenv("FALLBACK_TEST_KEY", "fallback-secret")
        assert get_secure_api_key("test_key", ("PRIMARY_TEST_KEY", "FALLBACK_TEST_KEY")) == "fallback-secret"

        monkeypatch.setenv("PRIMARY_TEST_KEY", "primary-secret")
        assert get_secure_api_key("test_key", ("PRIMARY_TEST_KEY", "FALLBACK_TEST_KEY")) == "primary-secret"

    def test_no_keyring_dependency_or_cache_objects(self) -> None:
        import zocrates.config as zc_config

        assert not hasattr(zc_config, "keyring")
        assert not hasattr(zc_config, "_NEGATIVE_KEYRING_CACHE")
        assert not hasattr(zc_config, "_NEGATIVE_KEYRING_LOCK")
        assert not hasattr(zc_config, "clear_negative_keyring_cache")


class TestCorpusDirectory:
    """Tests for get_corpus_dir, get_markdown_cache_dir alias, and legacy migration."""

    def test_corpus_dir_creation_and_alias(self, tmp_path: Path) -> None:
        from zocrates.config import get_corpus_dir, get_markdown_cache_dir

        corpus_dir = get_corpus_dir(tmp_path)
        assert corpus_dir.is_dir()
        assert corpus_dir.name == "corpus"
        assert corpus_dir.parent.name == "zocrates-data"

        # Verify backward compatible alias
        assert get_markdown_cache_dir is get_corpus_dir
        alias_dir = get_markdown_cache_dir(tmp_path)
        assert alias_dir == corpus_dir

    def test_legacy_markdown_cache_migration(self, tmp_path: Path) -> None:
        from zocrates.config import get_corpus_dir

        zocrates_data = tmp_path / "zocrates-data"
        zocrates_data.mkdir(parents=True, exist_ok=True)
        legacy_dir = zocrates_data / "markdown_cache"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        dummy_file = legacy_dir / "test.txt"
        dummy_file.write_text("migrated content", encoding="utf-8")

        corpus_dir = get_corpus_dir(tmp_path)
        assert corpus_dir.name == "corpus"
        assert corpus_dir.is_dir()
        assert not legacy_dir.exists()
        assert (corpus_dir / "test.txt").read_text(encoding="utf-8") == "migrated content"

    def test_legacy_migration_skipped_if_corpus_already_exists(self, tmp_path: Path) -> None:
        from zocrates.config import get_corpus_dir

        zocrates_data = tmp_path / "zocrates-data"
        zocrates_data.mkdir(parents=True, exist_ok=True)
        legacy_dir = zocrates_data / "markdown_cache"
        legacy_dir.mkdir(parents=True, exist_ok=True)
        (legacy_dir / "old.txt").write_text("old", encoding="utf-8")

        existing_corpus = zocrates_data / "corpus"
        existing_corpus.mkdir(parents=True, exist_ok=True)
        (existing_corpus / "new.txt").write_text("new", encoding="utf-8")

        corpus_dir = get_corpus_dir(tmp_path)
        assert corpus_dir == existing_corpus
        assert (corpus_dir / "new.txt").read_text(encoding="utf-8") == "new"
        assert legacy_dir.exists()



