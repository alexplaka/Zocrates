"""Unit tests for multi-agent turn and session cost accounting."""

import json
from pathlib import Path
import pytest
from unittest.mock import MagicMock, patch

from zocrates.utils.cost_calculator import (
    clear_rate_cache,
    calculate_event_cost,
    get_model_rates,
    aggregate_turn_cost,
    normalize_agent_role,
)
from zocrates.utils.session_formatting import format_session_for_ui


@pytest.fixture
def mock_zotero_dir(tmp_path: Path) -> Path:
    """Create a hermetic mock catalog in tmp_path so tests never depend on host caches."""
    cache_dir = tmp_path / "openrouter_models_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    catalog = {
        "timestamp": 1789599402,
        "data": [
            {
                "id": "google/gemini-2.5-flash",
                "pricing": {
                    "prompt": "0.0000003",
                    "completion": "0.0000025",
                    "input_cache_read": "0.00000003",
                },
            },
            {
                "id": "~deepseek/deepseek-flash-latest",
                "pricing": {
                    "prompt": "0.00000015",
                    "completion": "0.0000006",
                    "input_cache_read": "0.000000015",
                },
            },
            {
                "id": "anthropic/claude-3-haiku",
                "pricing": {
                    "prompt": "0.00000025",
                    "completion": "0.00000125",
                    "input_cache_read": "0.00000003",
                },
            },
        ],
    }
    with open(cache_dir / "llm_models.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f)
    clear_rate_cache()
    with patch("zocrates.llm._OPENROUTER_LLM_CACHE", {}):
        yield tmp_path
    clear_rate_cache()


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear rate caches before each test."""
    clear_rate_cache()
    yield
    clear_rate_cache()


def test_free_and_local_models():
    """Verify that free and local models strictly resolve to $0.000000."""
    free_models = [
        "nvidia/nemotron-3.5-lightning:free",
        "google/gemini-2.0-flash:free",
        "deepseek/deepseek-r1:FREE",
        "ollama/llama3.1:8b",
        "ollama_chat/qwen2.5",
        "ollama_chat/lfm2.5-thinking:1.2b",
        "vllm/mistral-7b-instruct",
        "local/my-finetuned-model",
        "local",
        "none",
        "",
        None,
    ]
    for m in free_models:
        rates = get_model_rates(m)
        assert rates == (0.0, 0.0, 0.0), f"Model {m} did not resolve to 0.0 rates: {rates}"
        cost = calculate_event_cost(m, prompt_tokens=5000, candidates_tokens=1000, cached_tokens=2000)
        assert cost == 0.0, f"Model {m} did not calculate to 0.0 cost: {cost}"

    # Test local provider dispatch (e.g. vllm or openai_compatible)
    with patch("zocrates.config.load_zocrates_config", return_value={"llm_provider": "vllm"}):
        vllm_model = "openai/meta-llama/Llama-3-8B-Instruct"
        rates = get_model_rates(vllm_model)
        assert rates == (0.0, 0.0, 0.0)
        cost = calculate_event_cost(vllm_model, prompt_tokens=5000, candidates_tokens=1000)
        assert cost == 0.0

    with patch("zocrates.config.load_zocrates_config", return_value={"llm_provider": "openai_compatible"}):
        compat_model = "openai/custom-local-model"
        rates = get_model_rates(compat_model)
        assert rates == (0.0, 0.0, 0.0)
        cost = calculate_event_cost(compat_model, prompt_tokens=5000, candidates_tokens=1000)
        assert cost == 0.0


def test_prefix_normalization_and_catalog_lookup(mock_zotero_dir: Path):
    """Verify that OpenRouter prefix variants and slugs resolve to the same catalog rates."""
    # Test prefix stripping
    rates_prefixed = get_model_rates("openrouter/google/gemini-2.5-flash", zotero_data_dir=mock_zotero_dir)
    rates_raw = get_model_rates("google/gemini-2.5-flash", zotero_data_dir=mock_zotero_dir)
    assert rates_prefixed == rates_raw
    assert rates_prefixed[0] == 0.0000003  # prompt rate
    assert rates_prefixed[1] == 0.0000025  # completion rate
    assert rates_prefixed[2] == 0.00000003  # cache read rate

    # Test tilde model in catalog
    rates_tilde_prefixed = get_model_rates("openrouter/deepseek/deepseek-flash-latest", zotero_data_dir=mock_zotero_dir)
    rates_tilde_clean = get_model_rates("deepseek/deepseek-flash-latest", zotero_data_dir=mock_zotero_dir)
    assert rates_tilde_prefixed == rates_tilde_clean
    assert rates_tilde_prefixed[0] == 0.00000015

    # Test unique slug resolution
    rates_slug = get_model_rates("claude-3-haiku", zotero_data_dir=mock_zotero_dir)
    rates_full = get_model_rates("anthropic/claude-3-haiku", zotero_data_dir=mock_zotero_dir)
    assert rates_slug == rates_full
    assert rates_slug[0] == 0.00000025


def test_cache_aware_pricing_calculation(mock_zotero_dir: Path):
    """Verify that prompt cache hits receive the discounted cache_read_rate."""
    model = "deepseek/deepseek-flash-latest"
    p_rate, c_rate, ca_rate = get_model_rates(model, zotero_data_dir=mock_zotero_dir)

    prompt_tokens = 2468
    cached_tokens = 2048
    candidates_tokens = 31

    uncached_prompt = prompt_tokens - cached_tokens  # 420
    expected_cost = (
        (uncached_prompt * p_rate)
        + (cached_tokens * ca_rate)
        + (candidates_tokens * c_rate)
    )

    actual_cost = calculate_event_cost(
        model,
        prompt_tokens=prompt_tokens,
        candidates_tokens=candidates_tokens,
        cached_tokens=cached_tokens,
        zotero_data_dir=mock_zotero_dir,
    )
    assert pytest.approx(actual_cost, rel=1e-6) == expected_cost

    # When cached_tokens == 0, full prompt is charged at p_rate
    cost_no_cache = calculate_event_cost(
        model,
        prompt_tokens=prompt_tokens,
        candidates_tokens=candidates_tokens,
        cached_tokens=0,
        zotero_data_dir=mock_zotero_dir,
    )
    expected_no_cache = (prompt_tokens * p_rate) + (candidates_tokens * c_rate)
    assert pytest.approx(cost_no_cache, rel=1e-6) == expected_no_cache

    # Cache hit must be significantly cheaper than non-cache
    assert actual_cost < cost_no_cache


def test_thoughts_tokens_not_double_counted(mock_zotero_dir: Path):
    """Verify that candidates_tokens is not inflated by double-counting thoughts."""
    model = "google/gemini-2.5-flash"
    prompt_tokens = 1000
    candidates_tokens = 500  # Total completion tokens (includes internal thoughts)

    cost_1 = calculate_event_cost(
        model,
        prompt_tokens=prompt_tokens,
        candidates_tokens=candidates_tokens,
        zotero_data_dir=mock_zotero_dir,
    )

    # Cost must be strictly (prompt * p_rate) + (candidates * c_rate)
    p_rate, c_rate, _ = get_model_rates(model, zotero_data_dir=mock_zotero_dir)
    expected = (1000 * p_rate) + (500 * c_rate)
    assert pytest.approx(cost_1, rel=1e-6) == expected


def test_normalize_agent_role():
    """Verify normalization of internal author strings to standard role names and node exclusions."""
    # Standard LLM agents
    assert normalize_agent_role("coordinator_agent") == "coordinator"
    assert normalize_agent_role("coordinator") == "coordinator"
    assert normalize_agent_role("library_search_agent") == "library_search"
    assert normalize_agent_role("library_search") == "library_search"
    assert normalize_agent_role("search_agent") == "library_search"
    assert normalize_agent_role("search") == "library_search"
    assert normalize_agent_role("library_synthesizer_agent") == "library_synthesizer"
    assert normalize_agent_role("library_synthesizer") == "library_synthesizer"
    assert normalize_agent_role("synthesizer_agent") == "library_synthesizer"
    assert normalize_agent_role("synthesizer") == "library_synthesizer"
    assert normalize_agent_role("online_search_agent") == "online_search"
    assert normalize_agent_role("online_synthesizer_agent") == "online_synthesizer"
    assert normalize_agent_role("user") == "user"
    assert normalize_agent_role("Human") == "user"
    assert normalize_agent_role(None) == "coordinator"

    # Non-LLM workflow nodes and system must return None
    assert normalize_agent_role("search_library_pipeline_node") is None
    assert normalize_agent_role("search_online_pipeline_node") is None
    assert normalize_agent_role("system") is None
    assert normalize_agent_role("custom_node") is None
    assert normalize_agent_role("node_retrieval") is None
    assert normalize_agent_role("retrieval_pipeline") is None
    assert normalize_agent_role("search_budget_checkpoint") is None
    assert normalize_agent_role("online_search_budget_checkpoint") is None
    assert normalize_agent_role("checkpoint_0") is None


def test_aggregate_turn_cost_excludes_nodes_and_user(mock_zotero_dir: Path):
    """Verify that aggregate_turn_cost excludes non-LLM nodes and user events from agent_models."""
    evt_user = MagicMock()
    evt_user.author = "user"
    evt_user.model_version = None
    evt_user.usage_metadata = None

    evt_sys = MagicMock()
    evt_sys.author = "system"
    evt_sys.model_version = "google/gemini-2.5-flash"
    evt_sys.usage_metadata = {
        "prompt_token_count": 500,
        "candidates_token_count": 100,
        "total_token_count": 600,
    }

    evt_pipeline = MagicMock()
    evt_pipeline.author = "search_library_pipeline_node"
    evt_pipeline.model_version = "deepseek/deepseek-flash-latest"
    evt_pipeline.usage_metadata = {
        "prompt_token_count": 1000,
        "candidates_token_count": 200,
        "total_token_count": 1200,
    }

    evt_synth = MagicMock()
    evt_synth.author = "synthesizer_agent"
    evt_synth.model_version = "google/gemini-2.5-flash"
    evt_synth.usage_metadata = {
        "prompt_token_count": 2000,
        "candidates_token_count": 400,
        "cached_content_token_count": 500,
        "total_token_count": 2400,
    }

    events = [evt_user, evt_sys, evt_pipeline, evt_synth]
    turn_cost, agent_models = aggregate_turn_cost(
        events,
        default_model="google/gemini-2.5-flash",
        zotero_data_dir=mock_zotero_dir,
    )

    # Cost should strictly be from the synthesizer_agent
    expected_cost = calculate_event_cost(
        "google/gemini-2.5-flash",
        2000,
        400,
        cached_tokens=500,
        zotero_data_dir=mock_zotero_dir,
    )
    assert pytest.approx(turn_cost, rel=1e-6) == expected_cost

    # agent_models must only contain library_synthesizer, not system or pipeline or user
    assert "library_synthesizer" in agent_models
    assert agent_models["library_synthesizer"] == "google/gemini-2.5-flash"
    assert "system" not in agent_models
    assert "search_library_pipeline_node" not in agent_models
    assert "user" not in agent_models
    assert len(agent_models) == 1


def test_multi_agent_turn_cost_aggregation(mock_zotero_dir: Path):
    """Verify turn cost calculation across heterogeneous agents and models."""
    # 1. Free Coordinator
    evt_coord = MagicMock()
    evt_coord.author = "coordinator_agent"
    evt_coord.model_version = "nvidia/nemotron-3.5-lightning:free"
    evt_coord.usage_metadata = {
        "prompt_token_count": 800,
        "candidates_token_count": 100,
        "thoughts_token_count": 50,
        "cached_content_token_count": 0,
        "total_token_count": 900,
    }

    # 2. Search Agent on DeepSeek
    evt_search = MagicMock()
    evt_search.author = "library_search_agent"
    evt_search.model_version = "deepseek/deepseek-flash-latest"
    evt_search.usage_metadata = {
        "prompt_token_count": 5000,
        "candidates_token_count": 200,
        "thoughts_token_count": 150,
        "cached_content_token_count": 3000,
        "total_token_count": 5200,
    }

    # 3. Synthesis Agent on Gemini
    evt_synth = MagicMock()
    evt_synth.author = "synthesizer_agent"
    evt_synth.model_version = "google/gemini-2.5-flash"
    evt_synth.usage_metadata = {
        "prompt_token_count": 4000,
        "candidates_token_count": 600,
        "thoughts_token_count": 400,
        "cached_content_token_count": 1000,
        "total_token_count": 4600,
    }

    events = [evt_coord, evt_search, evt_synth]
    turn_cost, agent_models = aggregate_turn_cost(
        events,
        default_model="deepseek/deepseek-chat",
        zotero_data_dir=mock_zotero_dir,
    )

    # Coordinator was free -> 0.0
    cost_coord = 0.0
    cost_search = calculate_event_cost(
        "deepseek/deepseek-flash-latest",
        5000,
        200,
        cached_tokens=3000,
        zotero_data_dir=mock_zotero_dir,
    )
    cost_synth = calculate_event_cost(
        "google/gemini-2.5-flash",
        4000,
        600,
        cached_tokens=1000,
        zotero_data_dir=mock_zotero_dir,
    )

    expected_turn_cost = cost_coord + cost_search + cost_synth
    assert pytest.approx(turn_cost, rel=1e-6) == expected_turn_cost
    assert turn_cost > 0.0

    assert agent_models["coordinator"] == "nvidia/nemotron-3.5-lightning:free"
    assert agent_models["library_search"] == "deepseek/deepseek-flash-latest"
    assert agent_models["library_synthesizer"] == "google/gemini-2.5-flash"


def test_format_session_for_ui_cost_integration(mock_zotero_dir: Path):
    """Verify that format_session_for_ui includes turn and session USD costs and tokens."""
    user_part = MagicMock()
    user_part.thought = False
    user_part.text = "Hello Zocrates"
    user_part.function_call = None
    user_part.function_response = None

    evt_user = MagicMock()
    evt_user.author = "user"
    evt_user.timestamp = 100.0
    evt_user.content = MagicMock()
    evt_user.content.parts = [user_part]
    evt_user.actions = None
    evt_user.usage_metadata = None

    asst_part = MagicMock()
    asst_part.thought = False
    asst_part.text = "Hello! Here is your answer."
    asst_part.function_call = None
    asst_part.function_response = None

    evt_asst = MagicMock()
    evt_asst.author = "synthesizer_agent"
    evt_asst.model_version = "google/gemini-2.5-flash"
    evt_asst.timestamp = 105.0
    evt_asst.content = MagicMock()
    evt_asst.content.parts = [asst_part]
    evt_asst.actions = None
    evt_asst.usage_metadata = {
        "prompt_token_count": 2000,
        "candidates_token_count": 300,
        "cached_content_token_count": 500,
        "total_token_count": 2300,
    }

    session = MagicMock()
    session.id = "test_session_cost"
    session.state = {"session_title": "Cost Test"}
    session.events = [evt_user, evt_asst]

    ui_data = format_session_for_ui(session, zotero_data_dir=mock_zotero_dir)

    # Top-level session total cost and total tokens
    assert "total_cost_usd" in ui_data
    assert ui_data["total_cost_usd"] > 0.0
    assert ui_data["total_tokens"] == 2300
    assert ui_data["session_metadata"]["total_cost_usd"] == ui_data["total_cost_usd"]
    assert ui_data["session_metadata"]["total_tokens"] == 2300

    # Assistant message turn cost and agent models
    asst_msg = [m for m in ui_data["messages"] if m["role"] == "assistant"][0]
    assert "turn_cost_usd" in asst_msg
    assert asst_msg["turn_cost_usd"] == ui_data["total_cost_usd"]
    assert "agent_models" in asst_msg
    assert asst_msg["agent_models"]["library_synthesizer"] == "google/gemini-2.5-flash"

    assert "cost_usd" in asst_msg["usage"]
    assert asst_msg["usage"]["cost_usd"] == asst_msg["turn_cost_usd"]
    assert "agent_models" in asst_msg["usage"]
