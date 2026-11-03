"""Coordinator agent factory and model configuration helpers."""

import logging
from typing import Any

from google.adk.agents import Agent
from google.adk.models import BaseLlm
from google.adk.tools import FunctionTool, ToolContext
from google.genai import types

from zocrates.agents.common.config import get_agent_generate_content_config
from zocrates.agents.coordinator.prompts import (
    COORDINATOR_INSTRUCTION,
    get_coordinator_instruction,
)
from zocrates.agents.library_search.search_pipeline import (
    create_library_search_agent,
    create_library_synthesizer_agent,
    search_library,
)
from zocrates.agents.online_search import (
    create_online_search_agent,
    create_online_synthesizer_agent,
    search_online_literature,
)
from zocrates.llm import get_llm_model

logger = logging.getLogger(__name__)


def create_coordinator_agent(
    model: Any = None,
    thinking_level: str | None = "medium",
    generate_content_config: types.GenerateContentConfig | None = None,
    search_library_tool: Any = None,
    search_online_tool: Any = None,
) -> Agent:
    """Create the root Coordinator agent.

    The coordinator answers conversational turns directly and delegates
    literature retrieval queries to the autonomous search pipeline(s).
    `model` can be a single model or a dict specifying per-agent models.
    """
    if model is None:
        base_model = get_llm_model()
    elif isinstance(model, dict):
        base_model = (
            model.get("default")
            or model.get("coordinator")
            or get_llm_model()
        )
    else:
        base_model = model

    if not isinstance(base_model, (str, BaseLlm)) and hasattr(base_model, "_mock_name"):
        base_model = "test-model"

    library_search_subagent = create_library_search_agent(base_model, thinking_level="low")
    library_synth_subagent = create_library_synthesizer_agent(base_model, thinking_level="high")
    online_search_subagent = create_online_search_agent(base_model, thinking_level="low")
    online_synth_subagent = create_online_synthesizer_agent(base_model, thinking_level="high")

    if search_library_tool is None:
        search_library_tool = FunctionTool(func=search_library)
    if search_online_tool is None:
        search_online_tool = FunctionTool(func=search_online_literature)

    tools = [search_library_tool, search_online_tool]

    if generate_content_config is None and thinking_level is not None:
        generate_content_config = get_agent_generate_content_config(thinking_level)

    coordinator = Agent(
        name="coordinator_agent",
        model=base_model,
        static_instruction=COORDINATOR_INSTRUCTION,
        instruction=get_coordinator_instruction,
        tools=tools,
        generate_content_config=generate_content_config,
        mode="chat",
    )
    coordinator._search_tool = search_library_tool
    coordinator._online_search_tool = search_online_tool
    coordinator._subagents = {
        "library_search": library_search_subagent,
        "library_synthesizer": library_synth_subagent,
        "online_search": online_search_subagent,
        "online_synthesizer": online_synth_subagent,
    }

    if isinstance(model, dict):
        set_agent_models(coordinator, model)

    return coordinator


def _apply_agent_model_or_config(agent_obj: Agent, target: Any) -> None:
    """Apply a model instance/string or a role config dict to an Agent."""
    if target is None:
        return

    if isinstance(target, dict) and any(
        k in target for k in ("model", "generate_content_config", "thinking_level")
    ):
        if "model" in target and target["model"] is not None:
            agent_obj.model = target["model"]
        if "generate_content_config" in target:
            agent_obj.generate_content_config = target["generate_content_config"]
        elif "thinking_level" in target:
            agent_obj.generate_content_config = get_agent_generate_content_config(
                target["thinking_level"]
            )
    else:
        agent_obj.model = target


def set_agent_models(coordinator: Agent, new_model: Any) -> None:
    """Propagate updated LLM model(s) and content configs across coordinator and subagents.

    Resolves 1:1 canonical role keys (coordinator, library_search, library_synthesizer,
    online_search, online_synthesizer).
    """
    if not isinstance(new_model, dict):
        coordinator.model = new_model
        if hasattr(coordinator, "_subagents") and isinstance(
            coordinator._subagents, dict
        ):
            for subagent in coordinator._subagents.values():
                if hasattr(subagent, "model"):
                    subagent.model = new_model
        logger.debug(
            "Propagated uniform model update across coordinator agent tree: %s",
            new_model,
        )
        return

    default_entry = new_model.get("default")

    # 1. Resolve coordinator model/config
    coord_entry = new_model.get("coordinator")
    if coord_entry is None and default_entry is not None:
        coord_entry = default_entry
    if coord_entry is not None:
        _apply_agent_model_or_config(coordinator, coord_entry)

    # 2. Resolve subagents models/configs
    if hasattr(coordinator, "_subagents") and isinstance(
        coordinator._subagents, dict
    ):
        subagents = coordinator._subagents
        roles = (
            "library_search",
            "library_synthesizer",
            "online_search",
            "online_synthesizer",
        )

        for role_name in roles:
            agent_obj = subagents.get(role_name)
            if agent_obj is None:
                continue
            target_entry = new_model.get(role_name)
            if target_entry is None and default_entry is not None:
                target_entry = default_entry

            if target_entry is not None:
                _apply_agent_model_or_config(agent_obj, target_entry)

    logger.debug(
        "Propagated per-agent model updates across coordinator agent tree: %s",
        new_model,
    )

