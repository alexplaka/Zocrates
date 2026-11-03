import asyncio
import json
import os
import tempfile
from pathlib import Path
import pytest

from zocrates.config import (
    DEFAULT_LLM_MAX_TOKENS,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TEMPERATURE,
    PROVIDER_DEFAULT_MODELS,
    check_config_freshness,
    get_config_path,
    get_secure_api_key,
    load_zocrates_config,
    save_secure_api_key,
    save_zocrates_config,
    stamp_config_mtime,
)
from zocrates.llm import (
    get_llm_model,
    fetch_and_cache_openrouter_models,
    get_openrouter_models,
    format_llm_exception,
    test_llm_connection as run_test_llm_connection,
)
from zocrates.rag.embeddings import OpenRouterEmbedder, get_embedder
from zocrates.agents.app import root_agent, set_agent_models


def test_config_defaults_and_secure_key(tmp_path):
    cfg = load_zocrates_config(tmp_path)
    assert cfg["llm_provider"] == "openrouter_llm"
    assert cfg["llm_model"] == "openrouter/~z-ai/glm-flash-latest"
    assert cfg["llm_temperature"] is None
    assert cfg["llm_max_tokens"] == 16384
    assert cfg["embedding_provider"] == "local"

    save_secure_api_key("test_key_123", key_name="openrouter_api_key", env_var="OPENROUTER_API_KEY")
    assert get_secure_api_key("openrouter_api_key", ("OPENROUTER_API_KEY",)) == "test_key_123"


def test_get_llm_model(tmp_path):
    model = get_llm_model(tmp_path)
    assert model.model == "openrouter/~z-ai/glm-flash-latest"


@pytest.fixture(autouse=False)
def reset_mtime_sentinel():
    import zocrates.config  as _cfg
    old = _cfg._last_config_mtime
    _cfg._last_config_mtime = 0.0
    yield
    _cfg._last_config_mtime = old


def test_config_freshness_and_stamp(tmp_path, reset_mtime_sentinel):
    # Non-existent config returns False
    assert check_config_freshness(tmp_path) is False

    # Programmatic save stamps the mtime, so freshness returns False
    save_zocrates_config({"llm_provider": "openrouter_llm"}, tmp_path)
    assert check_config_freshness(tmp_path) is False

    # Simulate manual edit with later timestamp
    config_file = get_config_path(tmp_path)
    current_mtime = config_file.stat().st_mtime
    future_mtime = current_mtime + 5.0
    os.utime(config_file, (future_mtime, future_mtime))

    # Next check detects freshness and returns True
    assert check_config_freshness(tmp_path) is True

    # Immediate subsequent check without modification returns False
    assert check_config_freshness(tmp_path) is False


def test_get_llm_model_ollama_mismatch_fallback(tmp_path):
    save_zocrates_config({
        "llm_provider": "ollama",
        "llm_model": "google/gemini-3.8-flash",
    }, tmp_path)
    model = get_llm_model(tmp_path)
    assert model.model == "ollama_chat/llama3.1:8b"


def test_get_llm_model_temperature_none_omitted(tmp_path):
    save_zocrates_config({
        "llm_provider": "openrouter_llm",
        "llm_temperature": None,
    }, tmp_path)
    model = get_llm_model(tmp_path)
    assert "temperature" not in getattr(model, "_additional_args", {})
    assert getattr(model, "_additional_args", {}).get("max_tokens") == 16384

    save_zocrates_config({
        "llm_temperature": 0.7,
        "llm_max_tokens": 4096,
    }, tmp_path)
    model2 = get_llm_model(tmp_path)
    assert getattr(model2, "_additional_args", {}).get("temperature") == 0.7
    assert getattr(model2, "_additional_args", {}).get("max_tokens") == 4096


import litellm
import requests
from unittest.mock import MagicMock, patch


def test_openrouter_model_fetching(tmp_path):
    async def run_test():
        models = await get_openrouter_models(zotero_data_dir=tmp_path, force_refresh=True)
        assert isinstance(models, list)
        assert len(models) > 0

        embed_models = await get_openrouter_models(target_type="embedding", zotero_data_dir=tmp_path,
                                                   force_refresh=True)
        assert isinstance(embed_models, list)
        assert len(embed_models) > 0

        decision_models = await get_openrouter_models(target_type="decision", zotero_data_dir=tmp_path,
                                                      force_refresh=True)
        assert isinstance(decision_models, list)
        assert len(decision_models) > 0
        assert decision_models[0]["id"] == "inception/mercury-decide"
        assert decision_models[0]["name"] == "Inception: Mercury Decide"
        assert "description" in decision_models[0]
        assert "pricing" in decision_models[0]
        assert "architecture" in decision_models[0]
        assert decision_models[0]["architecture"]["output_modalities"] == ["decisions"]
        assert decision_models[0]["is_free"] is False
        assert (tmp_path / "zocrates-data" / "openrouter_models_cache" / "decision_models.json").exists()

    mock_llm_response = MagicMock()
    mock_llm_response.raise_for_status = MagicMock()
    mock_llm_response.json.return_value = {
        "data": [
            {
                "id": "anthropic/claude-3.5-sonnet",
                "name": "Anthropic: Claude 3.5 Sonnet",
                "context_length": 200000,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            }
        ]
    }

    mock_embed_response = MagicMock()
    mock_embed_response.raise_for_status = MagicMock()
    mock_embed_response.json.return_value = {
        "data": [
            {
                "id": "nvidia/nemotron-3-embed-1b:free",
                "name": "NVIDIA: Nemotron 3 Embed 1B (free)",
                "context_length": 32768,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["embeddings"]},
                "pricing": {"prompt": "0", "completion": "0"},
            }
        ]
    }

    mock_decision_response = MagicMock()
    mock_decision_response.raise_for_status = MagicMock()
    mock_decision_response.json.return_value = {
        "data": [
            {
                "id": "inception/mercury-decide",
                "name": "Inception: Mercury Decide",
                "context_length": 32768,
                "description": "Decision model for calibrated claim evaluation",
                "architecture": {"input_modalities": ["text"], "output_modalities": ["decisions"]},
                "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            }
        ]
    }

    def mock_get(url, **kwargs):
        if "embeddings" in url:
            return mock_embed_response
        if "output_modalities=decisions" in url:
            return mock_decision_response
        return mock_llm_response

    with patch("zocrates.llm.get_secure_api_key", return_value="dummy_key"), patch("requests.get", side_effect=mock_get):
        asyncio.run(run_test())


def test_openrouter_embedder_factory(tmp_path, monkeypatch):
    """Hermetic factory test.

    The OpenRouterEmbedder's tier-2 dimension resolution probes the REAL
    machine's Chroma library (config discovery finds provisioned profiles) and
    resolves the live collection dimension (e.g. 768). Correct at runtime, but
    it must not leak into this factory test — patch discovery and the syncer
    hook to a tmp dir so resolution falls through to the default.
    """
    import zocrates.config as _cfg_mod
    import zocrates.rag.service_manager as _sm_mod

    monkeypatch.setattr(_cfg_mod, "resolve_zotero_data_dir", lambda *a, **k: tmp_path)
    # Also patch the config source: load_zocrates_config() reads whichever
    # profile discovery picks, and different profiles carry different
    # embedding_model values (registry-resolved dimensions differ, e.g.
    # bge-m3=1024 vs jina-v5-nano=768). Hermetic = no profile config at all.
    monkeypatch.setattr(_cfg_mod, "load_zocrates_config", lambda *a, **k: {})

    def _no_syncer(*args, **kwargs):
        raise RuntimeError("hermetic test: no syncer")

    monkeypatch.setattr(_sm_mod, "get_syncer", _no_syncer)
    embedder = get_embedder("cloud")
    assert isinstance(embedder, OpenRouterEmbedder)
    assert embedder.dimension == 1024  # Default fallback dimension


def test_llm_connection_helper(tmp_path):
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = "pong"
    with patch("zocrates.llm.get_secure_api_key", return_value="dummy_key"), patch("litellm.completion",
                                                                              return_value=mock_resp):
        res = asyncio.run(run_test_llm_connection(zotero_data_dir=tmp_path))
        assert "status" in res
        assert "model" in res
        assert res.get("ok") is True

    with patch("zocrates.llm.get_secure_api_key", return_value=""):
        res_nokey = asyncio.run(run_test_llm_connection(provider="openrouter_llm", openrouter_api_key="", zotero_data_dir=tmp_path))
        assert res_nokey["status"] == "error"
        assert res_nokey["ok"] is False


def test_set_agent_models(tmp_path):
    from zocrates.agents.app import coordinator_agent

    save_zocrates_config({}, tmp_path)
    new_model = get_llm_model(tmp_path)
    set_agent_models(new_model, zotero_data_dir=tmp_path)
    assert coordinator_agent is not None
    assert coordinator_agent.model == new_model
    assert coordinator_agent._subagents["library_search"].model == new_model
    # Pass 1 local LLM hardening: synthesizer roles default to model: None and inherit new_model
    assert coordinator_agent._subagents["library_synthesizer"].model == new_model
    assert coordinator_agent.generate_content_config is not None


def test_format_llm_exception():
    # 1. Connection error for Ollama
    err = ConnectionRefusedError("[Errno 111] Connection refused")
    msg_ollama = format_llm_exception(err, provider="ollama", base_url="http://localhost:11434")
    assert "Unable to connect to Ollama server at http://localhost:11434" in msg_ollama
    assert "ollama serve" in msg_ollama

    # 2. Connection error for vLLM
    msg_vllm = format_llm_exception(err, provider="vllm", base_url="http://localhost:8000/v1")
    assert "Unable to connect to local server at http://localhost:8000/v1" in msg_vllm
    assert "--enable-auto-tool-choice" in msg_vllm

    # 3. Connection error for OpenRouter
    msg_or = format_llm_exception(err, provider="openrouter_llm")
    assert "Unable to connect to OpenRouter API" in msg_or

    # 4. Authentication error
    auth_err = Exception("AuthenticationError: 401 Unauthorized - Invalid API key")
    msg_auth = format_llm_exception(auth_err, provider="openrouter_llm")
    assert "Authentication failed (401)" in msg_auth


def test_embedding_connection_helper(tmp_path):
    from zocrates.llm import test_embedding_connection

    # Test local provider
    res_local = asyncio.run(test_embedding_connection(provider="local", zotero_data_dir=tmp_path))
    assert res_local["status"] == "ok"
    assert res_local["ok"] is True
    assert res_local["dimension"] == 768

    # Test cloud provider without key
    with patch("zocrates.llm.get_secure_api_key", return_value=""):
        res_nokey = asyncio.run(test_embedding_connection(provider="cloud", openrouter_api_key="",
                                                          zotero_data_dir=tmp_path))
        assert res_nokey["status"] == "error"
        assert res_nokey["ok"] is False
        assert "OPENROUTER_API_KEY is not configured" in res_nokey["message"]

    # Test cloud provider with mock embedder
    with patch("zocrates.llm.get_secure_api_key", return_value="dummy_key"), patch("litellm.embedding",
                                                                              return_value=MagicMock(data=[
                                                                                  {"embedding": [0.1] * 1024}])):
        res_cloud = asyncio.run(
            test_embedding_connection(provider="cloud", model="nvidia/nemotron-3-embed-1b:free",
                                      openrouter_api_key="dummy_key", zotero_data_dir=tmp_path))
        assert res_cloud["status"] == "ok"
        assert res_cloud["ok"] is True
        assert res_cloud["dimension"] == 1024


def test_build_agent_llm_precedence(tmp_path):
    from zocrates.llm import build_agent_llm

    # Global config has default openrouter
    global_cfg = {
        "llm_provider": "openrouter_llm",
        "llm_model": "google/gemini-3.8-flash",
        "llm_temperature": 0.5,
        "llm_max_tokens": 8192,
    }

    # Agent config overrides provider, model, temperature, max_tokens
    agent_cfg = {
        "provider": "ollama",
        "model": "llama3.1:8b",
        "temperature": 0.1,
        "max_tokens": 2048,
    }

    agent_llm = build_agent_llm(agent_cfg, global_cfg=global_cfg)
    assert agent_llm.model == "ollama_chat/llama3.1:8b"
    assert getattr(agent_llm, "_additional_args", {}).get("temperature") == 0.1
    assert getattr(agent_llm, "_additional_args", {}).get("max_tokens") == 2048

    # Agent config with null model inherits global model with provider prefix
    agent_cfg_inherit = {
        "model": None,
        "temperature": 0.2,
    }
    agent_llm_inherit = build_agent_llm(agent_cfg_inherit, global_cfg=global_cfg)
    assert agent_llm_inherit.model == "openrouter/google/gemini-3.8-flash"
    assert getattr(agent_llm_inherit, "_additional_args", {}).get("temperature") == 0.2
    assert getattr(agent_llm_inherit, "_additional_args", {}).get("max_tokens") == 8192


def test_override_agent_models_pinning_and_inheritance(tmp_path):
    from zocrates.agents.app import coordinator_agent

    config_data = {
        "llm_provider": "openrouter_llm",
        "llm_model": "google/gemini-3.8-flash",
        "override_agent_models": {
            "coordinator": {
                "model": None,
                "thinking_level": "medium",
            },
            "library_search": {
                "model": "ollama/llama3.1:8b",
                "thinking_level": "minimal",
            },
            "library_synthesizer": {
                "model": None,
                "thinking_level": "high",
            },
        },
    }
    save_zocrates_config(config_data, tmp_path)

    set_agent_models(zotero_data_dir=tmp_path)
    assert coordinator_agent is not None

    # Coordinator inherits global model, thinking_level='medium'
    assert coordinator_agent.model.model == "openrouter/google/gemini-3.8-flash"
    coord_conf = coordinator_agent.generate_content_config
    assert coord_conf is not None
    assert coord_conf.thinking_config.thinking_level.name.lower() == "medium"

    # Search subagent is pinned to ollama/llama3.1:8b, thinking_level='minimal'
    search_subagent = coordinator_agent._subagents["library_search"]
    assert search_subagent.model.model == "ollama_chat/llama3.1:8b"
    search_conf = search_subagent.generate_content_config
    assert search_conf is not None
    assert search_conf.thinking_config.thinking_level.name.lower() == "minimal"

    # Synthesizer inherits global model, thinking_level='high'
    synth_subagent = coordinator_agent._subagents["library_synthesizer"]
    assert synth_subagent.model.model == "openrouter/google/gemini-3.8-flash"
    synth_conf = synth_subagent.generate_content_config
    assert synth_conf is not None
    assert synth_conf.thinking_config.thinking_level.name.lower() == "high"


def test_dynamic_model_inheritance_hot_swap(tmp_path):
    from zocrates.agents.app import coordinator_agent

    # Initial state with pinned search model
    config_data = {
        "llm_provider": "openrouter_llm",
        "llm_model": "google/gemini-3.8-flash",
        "override_agent_models": {
            "library_search": {
                "model": "ollama/llama3.1:8b",
            }
        },
    }
    save_zocrates_config(config_data, tmp_path)
    set_agent_models(zotero_data_dir=tmp_path)

    assert coordinator_agent.model.model == "openrouter/google/gemini-3.8-flash"
    assert coordinator_agent._subagents["library_search"].model.model == "ollama_chat/llama3.1:8b"
    assert coordinator_agent._subagents["library_synthesizer"].model.model == "openrouter/google/gemini-3.8-flash"

    # Update global model in config
    save_zocrates_config({"llm_model": "qwen/qwen-2.5-72b-instruct"}, tmp_path)
    set_agent_models(zotero_data_dir=tmp_path)

    # Coordinator updates to the new global model; search stays pinned to its
    # override and the synthesizer inherits the new global model (Pass 1 hardening)
    assert coordinator_agent.model.model == "openrouter/qwen/qwen-2.5-72b-instruct"
    assert coordinator_agent._subagents["library_search"].model.model == "ollama_chat/llama3.1:8b"
    assert (
        coordinator_agent._subagents["library_synthesizer"].model.model
        == "openrouter/qwen/qwen-2.5-72b-instruct"
    )


def test_thinking_level_disabled_variants():
    from zocrates.agents.common.config import get_agent_generate_content_config

    assert get_agent_generate_content_config(None) is None
    assert get_agent_generate_content_config("none") is None
    assert get_agent_generate_content_config("off") is None
    assert get_agent_generate_content_config("disabled") is None
    assert get_agent_generate_content_config("false") is None
    assert get_agent_generate_content_config("") is None

    cfg = get_agent_generate_content_config("low")
    assert cfg is not None
    assert cfg.thinking_config.thinking_level.name.lower() == "low"


def test_default_override_agent_models(tmp_path):
    from zocrates.config import DEFAULT_OVERRIDE_AGENT_MODELS, get_config_path

    cfg = load_zocrates_config(tmp_path)
    assert cfg.get("override_agent_models") == DEFAULT_OVERRIDE_AGENT_MODELS

    # Write a legacy config file to disk that lacks override_agent_models
    config_path = get_config_path(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"llm_provider": "openrouter_llm", "llm_model": "google/gemini-3.8-flash"}, f)

    # Verify that file on disk does not have override_agent_models yet
    with open(config_path) as f:
        disk_raw = json.load(f)
    assert "override_agent_models" not in disk_raw

    # Load config via load_zocrates_config - this should backfill disk file
    loaded_cfg = load_zocrates_config(tmp_path)
    assert loaded_cfg.get("override_agent_models") == DEFAULT_OVERRIDE_AGENT_MODELS

    # Verify that file on disk was automatically backfilled with DEFAULT_OVERRIDE_AGENT_MODELS
    with open(config_path) as f:
        disk_updated = json.load(f)
    assert "override_agent_models" in disk_updated
    assert disk_updated["override_agent_models"] == DEFAULT_OVERRIDE_AGENT_MODELS


def test_override_agent_models_backfill(tmp_path):
    from zocrates.config import (
        DEFAULT_OVERRIDE_AGENT_MODELS,
        get_config_path,
    )

    # 1. Backfill when completely missing
    cfg = load_zocrates_config(tmp_path)
    assert cfg.get("override_agent_models") == DEFAULT_OVERRIDE_AGENT_MODELS

    # 2. Backfill missing roles when partially present
    config_path = get_config_path(tmp_path)
    partial_cfg = {
        "llm_provider": "openrouter_llm",
        "override_agent_models": {
            "coordinator": {"model": "openrouter/custom-coord", "thinking_level": "medium"},
        },
    }
    with open(config_path, "w") as f:
        json.dump(partial_cfg, f)

    loaded = load_zocrates_config(tmp_path)
    overrides = loaded.get("override_agent_models")
    assert overrides["coordinator"]["model"] == "openrouter/custom-coord"
    assert overrides["library_search"]["model"] is None
    # Backfilled synthesizer role uses default override (model: None)
    assert overrides["library_synthesizer"]["model"] is None
    assert overrides["online_search"]["model"] is None


def test_llm_num_retries_config_default_and_backfill(tmp_path):
    from zocrates.config import DEFAULT_LLM_NUM_RETRIES, get_config_path

    cfg = load_zocrates_config(tmp_path)
    assert cfg.get("llm_num_retries") == DEFAULT_LLM_NUM_RETRIES
    assert cfg.get("llm_num_retries") == 4

    # Write a legacy config file to disk that lacks llm_num_retries
    config_path = get_config_path(tmp_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w") as f:
        json.dump({"llm_provider": "openrouter_llm", "llm_model": "google/gemini-3.8-flash"}, f)

    with open(config_path) as f:
        disk_raw = json.load(f)
    assert "llm_num_retries" not in disk_raw

    # Loading backfills llm_num_retries
    loaded_cfg = load_zocrates_config(tmp_path)
    assert loaded_cfg.get("llm_num_retries") == 4

    with open(config_path) as f:
        disk_updated = json.load(f)
    assert "llm_num_retries" in disk_updated
    assert disk_updated["llm_num_retries"] == 4


def test_build_agent_llm_retries_precedence():
    from zocrates.llm import build_agent_llm
    from zocrates.config import DEFAULT_LLM_NUM_RETRIES

    # Default fallback
    llm_default = build_agent_llm({}, global_cfg={})
    assert getattr(llm_default, "_additional_args", {}).get("num_retries") == DEFAULT_LLM_NUM_RETRIES

    # Global config override
    llm_global = build_agent_llm({}, global_cfg={"llm_num_retries": 6})
    assert getattr(llm_global, "_additional_args", {}).get("num_retries") == 6

    # Agent config override takes precedence over global
    llm_agent = build_agent_llm({"num_retries": 2}, global_cfg={"llm_num_retries": 8})
    assert getattr(llm_agent, "_additional_args", {}).get("num_retries") == 2


def test_get_agent_generate_content_config_retries():
    from zocrates.agents.common.config import get_agent_generate_content_config
    from zocrates.config import DEFAULT_LLM_NUM_RETRIES

    # Default attempts
    cfg_default = get_agent_generate_content_config("low")
    assert cfg_default is not None
    assert cfg_default.http_options.retry_options.attempts == DEFAULT_LLM_NUM_RETRIES
    assert cfg_default.http_options.retry_options.attempts == 4

    # Explicit attempts
    cfg_custom = get_agent_generate_content_config("high", num_retries=7)
    assert cfg_custom is not None
    assert cfg_custom.http_options.retry_options.attempts == 7


def test_get_agent_generate_content_config_config_driven_retries(monkeypatch):
    from zocrates.agents.common.config import get_agent_generate_content_config

    monkeypatch.setattr(
        "zocrates.agents.common.config.load_zocrates_config",
        lambda zotero_data_dir=None: {"llm_num_retries": 9},
    )

    cfg = get_agent_generate_content_config("medium")
    assert cfg is not None
    assert cfg.http_options.retry_options.attempts == 9

    # Explicit num_retries should override config value
    cfg_override = get_agent_generate_content_config("medium", num_retries=2)
    assert cfg_override is not None
    assert cfg_override.http_options.retry_options.attempts == 2


def test_resolve_model_name_ollama(caplog):
    import logging
    from zocrates.llm import _resolve_model_name

    # Strips ollama/ prefix and wraps with ollama_chat/
    assert _resolve_model_name("ollama/llama3.1:8b", "ollama") == "ollama_chat/llama3.1:8b"
    # Preserves ollama_chat/ prefix
    assert (
        _resolve_model_name("ollama_chat/lfm2.5-thinking:1.2b", "ollama")
        == "ollama_chat/lfm2.5-thinking:1.2b"
    )
    # Bare model name wrapped with ollama_chat/
    assert _resolve_model_name("lfm2.5-thinking:1.2b", "ollama") == "ollama_chat/lfm2.5-thinking:1.2b"

    # Known remote provider prefixes fall back to Ollama default with warning
    with caplog.at_level(logging.WARNING):
        res = _resolve_model_name("google/gemini-2.5-flash", "ollama")
        assert res == "ollama_chat/llama3.1:8b"
        assert "remote provider model" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        res2 = _resolve_model_name("openrouter/deepseek/deepseek-chat", "ollama")
        assert res2 == "ollama_chat/llama3.1:8b"
        assert "remote provider model" in caplog.text

    # Community and HuggingFace models with slashes are preserved
    assert (
        _resolve_model_name("hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF", "ollama")
        == "ollama_chat/hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF"
    )
    assert (
        _resolve_model_name("huggingface.co/author/model", "ollama")
        == "ollama_chat/huggingface.co/author/model"
    )
    assert (
        _resolve_model_name("huihui_ai/Llama-3.2-3B-Instruct-abliterated", "ollama")
        == "ollama_chat/huihui_ai/Llama-3.2-3B-Instruct-abliterated"
    )


def test_resolve_model_name_vllm():
    from zocrates.llm import _resolve_model_name

    assert (
        _resolve_model_name("openai/meta-llama/Llama-3-8b", "vllm")
        == "openai/meta-llama/Llama-3-8b"
    )
    assert (
        _resolve_model_name("meta-llama/Llama-3-8b", "vllm")
        == "openai/meta-llama/Llama-3-8b"
    )
    assert _resolve_model_name("default", "vllm") == "openai/default"
    assert (
        _resolve_model_name("custom-model", "openai_compatible")
        == "openai/custom-model"
    )


def test_format_base_url():
    from zocrates.llm import _format_base_url

    # Scheme auto-prepend
    assert _format_base_url("localhost:11434", "ollama") == "http://localhost:11434"
    assert _format_base_url("localhost:8000", "vllm") == "http://localhost:8000/v1"

    # Case-insensitive scheme check
    assert _format_base_url("HTTP://localhost:8000", "vllm") == "http://localhost:8000/v1"
    assert _format_base_url("HTTPS://localhost:8443", "vllm") == "https://localhost:8443/v1"

    # Ollama strips /v1 and trailing slash
    assert _format_base_url("http://localhost:11434/v1", "ollama") == "http://localhost:11434"
    assert _format_base_url("http://localhost:11434/v1/", "ollama") == "http://localhost:11434"
    assert _format_base_url("http://localhost:11434/", "ollama") == "http://localhost:11434"

    # vLLM appends /v1 only when path is empty
    assert _format_base_url("http://localhost:8000", "vllm") == "http://localhost:8000/v1"
    assert _format_base_url("http://localhost:8000/", "vllm") == "http://localhost:8000/v1"
    assert _format_base_url("http://localhost:8000/v1", "vllm") == "http://localhost:8000/v1"
    assert _format_base_url("http://localhost:8000/custom/api", "vllm") == "http://localhost:8000/custom/api"


def test_build_agent_llm_ollama_num_ctx_and_drop_params():
    from zocrates.llm import build_agent_llm

    # Default ollama_num_ctx (16384) passed as top-level kwarg
    llm = build_agent_llm({"provider": "ollama", "model": "llama3.1:8b"}, global_cfg={})
    assert getattr(llm, "_additional_args", {}).get("num_ctx") == 16384
    assert getattr(llm, "_additional_args", {}).get("drop_params") is True

    # Overridden via global config ollama_num_ctx
    llm2 = build_agent_llm(
        {"provider": "ollama", "model": "llama3.1:8b"},
        global_cfg={"ollama_num_ctx": 8192},
    )
    assert getattr(llm2, "_additional_args", {}).get("num_ctx") == 8192

    # Overridden via extra_options num_ctx
    llm3 = build_agent_llm(
        {"provider": "ollama", "model": "llama3.1:8b", "extra_options": {"num_ctx": 32768}},
        global_cfg={},
    )
    assert getattr(llm3, "_additional_args", {}).get("num_ctx") == 32768


def test_build_agent_llm_vllm_context_clamping():
    from zocrates.llm import build_agent_llm

    # Legacy default max_tokens (16384) clamped to 4096
    llm_clamped = build_agent_llm(
        {"provider": "vllm", "model": "default"},
        global_cfg={"llm_max_tokens": 16384},
    )
    assert getattr(llm_clamped, "_additional_args", {}).get("max_tokens") == 4096
    assert getattr(llm_clamped, "_additional_args", {}).get("drop_params") is True

    # Custom max_tokens untouched
    llm_custom = build_agent_llm(
        {"provider": "vllm", "model": "default", "max_tokens": 2048},
        global_cfg={},
    )
    assert getattr(llm_custom, "_additional_args", {}).get("max_tokens") == 2048

    # Explicit agent_cfg max_tokens of 16384 is NOT clamped (user specifically asked for 16384)
    llm_explicit_16k = build_agent_llm(
        {"provider": "vllm", "model": "default", "max_tokens": 16384},
        global_cfg={},
    )
    assert getattr(llm_explicit_16k, "_additional_args", {}).get("max_tokens") == 16384


def test_get_agent_generate_content_config_minimal_reasoning_effort():
    """Verify that 'minimal' reasoning effort is mapped to 'low' in OpenAI/vLLM reasoning_effort schema."""
    from zocrates.agents.common.config import get_agent_generate_content_config

    cfg = get_agent_generate_content_config("minimal")
    assert cfg is not None
    extra = cfg.http_options.extra_body
    # OpenAI/vLLM parameter mapped to "low" to avoid HTTP 422
    assert extra["reasoning_effort"] == "low"
    # OpenRouter reasoning effort preserved
    assert extra["reasoning"]["effort"] == "minimal"


def test_get_agent_generate_content_config_ollama_thinking_gating():
    """Verify that think: true is sent only to thinking-capable Ollama models."""
    from unittest.mock import patch
    from zocrates.agents.common.config import (
        clear_ollama_capabilities_cache,
        get_agent_generate_content_config,
        is_ollama_thinking_supported,
    )

    clear_ollama_capabilities_cache()

    # 1. Thinking models by name
    assert is_ollama_thinking_supported("ollama_chat/lfm2.5-thinking:1.2b") is True
    assert is_ollama_thinking_supported("ollama/deepseek-r1:8b") is True
    assert is_ollama_thinking_supported("qwq:32b") is True

    cfg_thinking = get_agent_generate_content_config("low", model="ollama_chat/lfm2.5-thinking:1.2b")
    assert cfg_thinking is not None
    assert cfg_thinking.http_options.extra_body.get("think") is True

    # 2. Non-thinking models by name (when offline/fallback)
    with patch("requests.get", side_effect=Exception("offline")), patch("requests.post", side_effect=Exception("offline")):
        assert is_ollama_thinking_supported("ollama/llama3.1:8b") is False
        assert is_ollama_thinking_supported("mistral:7b") is False

        cfg_non_thinking = get_agent_generate_content_config("low", model="ollama/llama3.1:8b")
        assert cfg_non_thinking is not None
        assert "think" not in cfg_non_thinking.http_options.extra_body

    # 3. Model declared thinking via Ollama /api/tags
    clear_ollama_capabilities_cache()
    mock_resp = type("MockResp", (), {
        "status_code": 200,
        "json": lambda self: {
            "models": [
                {"name": "custom-thinking-model:latest", "capabilities": ["completion", "tools", "thinking"]},
                {"name": "standard-model:latest", "capabilities": ["completion", "tools"]},
            ]
        },
    })()
    with patch("requests.get", return_value=mock_resp):
        assert is_ollama_thinking_supported("custom-thinking-model:latest") is True
        assert is_ollama_thinking_supported("standard-model:latest") is False

    # 4. Model declared thinking via Ollama /api/show when /api/tags lacks capabilities (older Ollama)
    clear_ollama_capabilities_cache()
    mock_tags_no_caps = type("MockResp", (), {
        "status_code": 200,
        "json": lambda self: {
            "models": [
                {"name": "legacy-model:latest"},
            ]
        },
    })()
    mock_show_caps = type("MockResp", (), {
        "status_code": 200,
        "json": lambda self: {
            "capabilities": ["thinking"],
        },
    })()
    with patch("requests.get", return_value=mock_tags_no_caps), patch("requests.post", return_value=mock_show_caps):
        assert is_ollama_thinking_supported("legacy-model:latest") is True


@pytest.mark.asyncio
async def test_llm_connection_tool_capability_probe():
    """Verify tool capability probe for local endpoints and vLLM actionable diagnostic hint."""
    from unittest.mock import patch, MagicMock
    from zocrates.llm import test_llm_connection

    # Successful basic chat but failed tool probe on vLLM
    call_count = 0

    def mock_completion(**kwargs):
        nonlocal call_count
        call_count += 1
        if "tools" in kwargs:
            raise RuntimeError("vLLM does not support tool choice without --enable-auto-tool-choice")
        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_res = MagicMock()
        mock_res.choices = [mock_choice]
        return mock_res

    with patch("litellm.completion", side_effect=mock_completion):
        res = await test_llm_connection(
            provider="vllm",
            model="openai/meta-llama/Llama-3-8B-Instruct",
            base_url="http://localhost:8000/v1",
        )
        assert res["ok"] is False
        assert res["status"] == "error"
        assert "Tool calling probe failed:" in res["message"]
        assert "--enable-auto-tool-choice --tool-call-parser hermes" in res["message"]

    # Successful tool probe on local Ollama
    call_count = 0

    def mock_completion_success(**kwargs):
        nonlocal call_count
        call_count += 1
        mock_choice = MagicMock()
        mock_choice.message.content = "OK"
        mock_res = MagicMock()
        mock_res.choices = [mock_choice]
        return mock_res

    with patch("litellm.completion", side_effect=mock_completion_success):
        res = await test_llm_connection(
            provider="ollama",
            model="ollama_chat/lfm2.5-thinking:1.2b",
            base_url="http://localhost:11434",
        )
        assert res["ok"] is True
        assert res["status"] == "ok"
        assert "Connection successful!" in res["message"]





