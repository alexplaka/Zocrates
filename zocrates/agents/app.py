"""Application assembly and lifecycle management for Zocrates multi-agent systems."""

import logging
from typing import Any

from google.adk.apps import App
from google.adk.models import BaseLlm

from zocrates.agents.common.adk_compat import (
    patch_adk_active_task_scope_rewind,
    patch_adk_request_input_context_filter,
    patch_compaction_subagent_isolation,
)
from zocrates.agents.common.compaction import (
    FailOpenLlmEventSummarizer,
    ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
    build_compaction_llm,
    build_events_compaction_config,
)
from zocrates.agents.common.config import (
    ROLE_DEFAULT_THINKING,
    get_agent_generate_content_config,
)

# Apply runtime compatibility patches (e.g. filter adk_request_input from agent prompts, isolate compaction to coordinator, rewind active task scopes)
patch_adk_request_input_context_filter()
patch_compaction_subagent_isolation()
patch_adk_active_task_scope_rewind()
from zocrates.agents.coordinator.agent import (
    create_coordinator_agent,
    set_agent_models as set_coordinator_agent_models,
)
from zocrates.llm import build_agent_llm, get_llm_model
from zocrates.config import load_zocrates_config

logger = logging.getLogger(__name__)


# Root conversational coordinator agent
coordinator_agent = create_coordinator_agent()

# Root agent reference
root_agent = coordinator_agent

# ADK Application instance (events_compaction_config is None to deactivate native ADK pre-invocation triggers)
app: App = App(
    name="zocrates",
    root_agent=root_agent,
    events_compaction_config=None,
)

# Standalone summarizer instance for post-turn adaptive context compaction
standalone_summarizer: FailOpenLlmEventSummarizer = FailOpenLlmEventSummarizer(
    llm=build_compaction_llm(load_zocrates_config()),
    prompt_template=ZOCRATES_COMPACTION_PROMPT_TEMPLATE,
)


def get_compaction_summarizer() -> FailOpenLlmEventSummarizer:
    """Return the active standalone event summarizer instance."""
    return standalone_summarizer


def set_agent_models(new_model: Any = None, zotero_data_dir: Any = None) -> Any:
    """Propagate updated LLM model(s) across coordinator, subagents, and compaction summarizer.

    Args:
        new_model: BaseLlm instance, model name string, or dict mapping role keys to models.
                   If None, re-instantiates default model via get_llm_model().
        zotero_data_dir: Optional Zotero data directory path for config loading.

    Returns:
        The updated model or model configuration.
    """
    cfg = load_zocrates_config(zotero_data_dir)

    if isinstance(new_model, dict):
        set_coordinator_agent_models(coordinator_agent, new_model)
        app.events_compaction_config = None
        standalone_summarizer._llm = build_compaction_llm(
            cfg, zotero_data_dir=zotero_data_dir, default_model=new_model
        )
        return new_model

    global_model = new_model or get_llm_model(zotero_data_dir=zotero_data_dir)
    overrides = cfg.get("override_agent_models") or {}

    agent_config: dict[str, dict[str, Any]] = {}
    is_mock_llm = hasattr(new_model, "script") or getattr(new_model, "model", None) == "mock-llm"

    for role in (
        "coordinator",
        "library_search",
        "library_synthesizer",
        "online_search",
        "online_synthesizer",
    ):
        override = overrides.get(role) or {}
        model_override = override.get("model")
        has_explicit_model = bool(model_override and str(model_override).strip())

        if not is_mock_llm and has_explicit_model:
            role_model = build_agent_llm(override, cfg, zotero_data_dir=zotero_data_dir)
        elif not is_mock_llm and any(
            k in override
            for k in (
                "provider",
                "temperature",
                "max_tokens",
                "extra_options",
                "base_url",
            )
        ):
            role_model = build_agent_llm(override, cfg, zotero_data_dir=zotero_data_dir)
        else:
            role_model = global_model

        default_thinking = ROLE_DEFAULT_THINKING.get(role, "medium")
        thinking_level = override.get("thinking_level", default_thinking)
        gen_config = get_agent_generate_content_config(
            thinking_level, zotero_data_dir=zotero_data_dir, model=role_model
        )
        agent_config[role] = {
            "model": role_model,
            "generate_content_config": gen_config,
        }

    set_coordinator_agent_models(coordinator_agent, agent_config)
    app.events_compaction_config = None
    standalone_summarizer._llm = build_compaction_llm(
        cfg, zotero_data_dir=zotero_data_dir, default_model=global_model
    )
    return global_model


# Apply role defaults and config overrides on initial module load
set_agent_models()

__all__ = [
    "app",
    "coordinator_agent",
    "root_agent",
    "set_agent_models",
    "standalone_summarizer",
    "get_compaction_summarizer",
    "FailOpenLlmEventSummarizer",
    "build_events_compaction_config",
    "build_compaction_llm",
    "ZOCRATES_COMPACTION_PROMPT_TEMPLATE",
]
