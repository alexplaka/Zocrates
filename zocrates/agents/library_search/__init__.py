"""Library search agent package for Zocrates."""

from zocrates.agents.library_search.prompts import (
    LIBRARY_SEARCH_AGENT_INSTRUCTION,
    LIBRARY_SYNTHESIZER_AGENT_INSTRUCTION,
)
from zocrates.agents.library_search.search_pipeline import (
    MAX_AUDIT_HISTORY_TURNS,
    create_library_search_agent,
    create_library_synthesizer_agent,
    get_synthesizer_instruction,
    search_library,
    search_library_pipeline_node,
)

__all__ = [
    "search_library",
    "search_library_pipeline_node",
    "create_library_search_agent",
    "create_library_synthesizer_agent",
    "get_synthesizer_instruction",
    "MAX_AUDIT_HISTORY_TURNS",
    "LIBRARY_SEARCH_AGENT_INSTRUCTION",
    "LIBRARY_SYNTHESIZER_AGENT_INSTRUCTION",
]
