"""Unit tests for Zocrates App assembly, coordinator root agent, and model propagation."""

import pytest
from google.adk.apps import App
from google.adk.models import BaseLlm, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from zocrates.agents.app import app, coordinator_agent, root_agent, set_agent_models
from zocrates.config import save_zocrates_config


class HistoryTrackingLlm(BaseLlm):
    """Mock BaseLlm tracking all incoming generate_content requests."""

    model: str = "history-tracker"
    seen_requests: list = []

    def __init__(self, **kwargs):
        super().__init__(model="history-tracker", **kwargs)
        self.seen_requests = []

    async def generate_content_async(self, req, stream: bool = False):
        self.seen_requests.append(req)
        reply = f"response to turn {len(self.seen_requests)}"
        content = types.Content(
            role="model", parts=[types.Part.from_text(text=reply)]
        )
        yield LlmResponse(content=content)


def test_app_assembly_and_root_coordinator():
    """Verify App is created with canonical root_agent = coordinator_agent in chat mode."""
    assert isinstance(app, App)
    assert app.name == "zocrates"
    assert app.root_agent is coordinator_agent
    assert root_agent is coordinator_agent
    assert coordinator_agent.name == "coordinator_agent"
    assert coordinator_agent.mode == "chat"
    assert hasattr(coordinator_agent, "_subagents")
    assert set(coordinator_agent._subagents.keys()) == {
        "library_search",
        "library_synthesizer",
        "online_search",
        "online_synthesizer",
    }


@pytest.mark.asyncio
async def test_coordinator_preserves_multi_turn_history():
    """Verify coordinator configured with mode='chat' receives full conversation history across turns."""
    tracking_model = HistoryTrackingLlm()
    set_agent_models(tracking_model)

    session_service = InMemorySessionService()
    session = await session_service.create_session(
        user_id="test_user", app_name="zocrates"
    )
    runner = Runner(app=app, session_service=session_service)

    # Turn 1
    async for _ in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="What is transformers?")]
        ),
    ):
        pass

    assert len(tracking_model.seen_requests) == 1
    turn1_contents = tracking_model.seen_requests[0].contents
    assert any(
        "What is transformers?" in p.text
        for c in turn1_contents
        for p in (c.parts or [])
        if p.text
    )

    # Turn 2
    async for _ in runner.run_async(
        user_id="test_user",
        session_id=session.id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text="How does attention work?")]
        ),
    ):
        pass

    assert len(tracking_model.seen_requests) == 2
    turn2_contents = tracking_model.seen_requests[1].contents
    # Turn 2 LLM receives full conversation history including turn 1 prompt and turn 1 response!
    assert any(
        "What is transformers?" in p.text
        for c in turn2_contents
        for p in (c.parts or [])
        if p.text
    )
    assert any(
        "response to turn 1" in p.text
        for c in turn2_contents
        for p in (c.parts or [])
        if p.text
    )
    assert any(
        "How does attention work?" in p.text
        for c in turn2_contents
        for p in (c.parts or [])
        if p.text
    )


def test_set_agent_models_uniform(monkeypatch):
    """Verify set_agent_models updates coordinator and all subagents uniformly."""
    # Isolate from the developer's real profile config: unit tests must not
    # observe onboarding-written override_agent_models (which pin
    # synthesizer roles and would break the uniform-propagation assertion).
    from importlib import import_module

    app_module = import_module("zocrates.agents.app")

    monkeypatch.setattr(app_module, "load_zocrates_config", lambda *_a, **_k: {})
    model_obj = "test-provider/test-model:high"
    updated = set_agent_models(model_obj)
    assert updated == model_obj

    assert coordinator_agent.model == model_obj
    assert coordinator_agent._subagents["library_search"].model == model_obj
    assert coordinator_agent._subagents["library_synthesizer"].model == model_obj


def test_set_agent_models_dict(monkeypatch):
    """Verify set_agent_models dispatches role-specific models from a dictionary."""
    from importlib import import_module

    app_module = import_module("zocrates.agents.app")

    monkeypatch.setattr(app_module, "load_zocrates_config", lambda *_a, **_k: {})
    model_dict = {
        "coordinator": "test-provider/coord-model",
        "library_search": "test-provider/search-model",
        "library_synthesizer": "test-provider/synth-model",
    }
    set_agent_models(model_dict)

    assert coordinator_agent.model == "test-provider/coord-model"
    assert coordinator_agent._subagents["library_search"].model == "test-provider/search-model"
    assert coordinator_agent._subagents["library_synthesizer"].model == "test-provider/synth-model"


def test_set_agent_models_config_propagation(tmp_path):
    """Verify set_agent_models propagates config overrides and thinking levels across agent tree."""
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
                "thinking_level": "low",
            },
            "library_synthesizer": {
                "model": None,
                "thinking_level": "high",
            },
        },
    }
    save_zocrates_config(config_data, tmp_path)
    set_agent_models(zotero_data_dir=tmp_path)

    assert coordinator_agent.model.model == "openrouter/google/gemini-3.8-flash"
    assert coordinator_agent.generate_content_config.thinking_config.thinking_level.name.lower() == "medium"

    search_sub = coordinator_agent._subagents["library_search"]
    assert search_sub.model.model == "ollama_chat/llama3.1:8b"
    assert search_sub.generate_content_config.thinking_config.thinking_level.name.lower() == "low"

    synth_sub = coordinator_agent._subagents["library_synthesizer"]
    assert synth_sub.model.model == "openrouter/google/gemini-3.8-flash"
    assert synth_sub.generate_content_config.thinking_config.thinking_level.name.lower() == "high"


def test_set_agent_models_inherits_model_with_custom_temperature(tmp_path):
    """Verify set_agent_models builds a dedicated model when model is None but temperature is customized."""
    config_data = {
        "llm_provider": "openrouter_llm",
        "llm_model": "google/gemini-3.8-flash",
        "override_agent_models": {
            "library_synthesizer": {
                "model": None,
                "temperature": 0.2,
                "thinking_level": "high",
            },
        },
    }
    save_zocrates_config(config_data, tmp_path)
    set_agent_models(zotero_data_dir=tmp_path)

    synth_sub = coordinator_agent._subagents["library_synthesizer"]
    assert synth_sub.model.model == "openrouter/google/gemini-3.8-flash"
    assert getattr(synth_sub.model, "_additional_args", {}).get("temperature") == 0.2

