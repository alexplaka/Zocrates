"""Zocrates multi-agent systems package."""

import importlib
from typing import Any

__all__ = [
    "app",
    "coordinator_agent",
    "root_agent",
    "set_agent_models",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        module = importlib.import_module("zocrates.agents.app")
        val = getattr(module, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
