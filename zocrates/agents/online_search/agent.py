"""Subagent factories for Online Literature Discovery and Synthesis."""

import logging
from typing import Any

from google.adk.agents import Agent
from google.adk.tools import FunctionTool
from google.genai import types

from zocrates.agents.common.config import get_agent_generate_content_config
from zocrates.agents.online_search.prompts import (
    ONLINE_SEARCH_AGENT_INSTRUCTION,
    ONLINE_SYNTHESIZER_AGENT_INSTRUCTION,
)
from zocrates.agents.online_search.tools import (
    conclude_online_search,
    request_search_extension,
    search_openalex,
)

logger = logging.getLogger(__name__)


def create_online_search_agent(
    model: Any = None,
    thinking_level: str | None = "low",
    generate_content_config: types.GenerateContentConfig | None = None,
) -> Agent:
    """Create the stateless online literature search subagent.

    Equipped with OpenAlex retrieval, candidate conclusion, and extension request tools.
    """
    if model is None:
        from zocrates.llm import get_llm_model

        model = get_llm_model()

    if generate_content_config is None and thinking_level is not None:
        generate_content_config = get_agent_generate_content_config(thinking_level)

    return Agent(
        name="online_search_agent",
        model=model,
        instruction=ONLINE_SEARCH_AGENT_INSTRUCTION,
        tools=[
            FunctionTool(search_openalex),
            FunctionTool(conclude_online_search),
            FunctionTool(request_search_extension),
        ],
        generate_content_config=generate_content_config,
        mode="single_turn",
        include_contents="default",
    )


def create_online_synthesizer_agent(
    model: Any = None,
    thinking_level: str | None = "high",
    generate_content_config: types.GenerateContentConfig | None = None,
) -> Agent:
    """Create the online synthesizer subagent.

    Produces an inquiry overview and structured candidate recommendations with numbered references.
    """
    if model is None:
        from zocrates.llm import get_llm_model

        model = get_llm_model()

    if generate_content_config is None and thinking_level is not None:
        generate_content_config = get_agent_generate_content_config(thinking_level)

    return Agent(
        name="online_synthesizer_agent",
        model=model,
        instruction=ONLINE_SYNTHESIZER_AGENT_INSTRUCTION,
        tools=[],
        generate_content_config=generate_content_config,
        mode="single_turn",
        include_contents="none",
    )
