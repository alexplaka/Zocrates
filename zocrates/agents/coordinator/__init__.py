"""Coordinator agent package for Zocrates conversational layer."""

import importlib
from typing import Any

_SYMBOL_MAP: dict[str, str] = {
    "create_coordinator_agent": "zocrates.agents.coordinator.agent",
    "search_library": "zocrates.agents.coordinator.agent",
    "set_agent_models": "zocrates.agents.coordinator.agent",
    "COORDINATOR_INSTRUCTION": "zocrates.agents.coordinator.prompts",
    "COORDINATOR_INSTRUCTION_dynamic": "zocrates.agents.coordinator.prompts",
    "coordinator_instruction": "zocrates.agents.coordinator.prompts",
    "get_coordinator_instruction": "zocrates.agents.coordinator.prompts",
}

__all__ = [
    "COORDINATOR_INSTRUCTION",
    "COORDINATOR_INSTRUCTION_dynamic",
    "coordinator_instruction",
    "get_coordinator_instruction",
    "create_coordinator_agent",
    "search_library",
    "set_agent_models",
]


def __getattr__(name: str) -> Any:
    if name in _SYMBOL_MAP:
        module = importlib.import_module(_SYMBOL_MAP[name])
        val = getattr(module, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)

