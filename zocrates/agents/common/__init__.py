"""Common utilities, state keys, prompts, and tool adapters for Zocrates agents."""

import importlib
from typing import Any

_SYMBOL_MAP: dict[str, str] = {
    # State keys
    "ACTIVE_MODE": "zocrates.agents.common.state_keys",
    "ACTIVE_CONTEXT_ITEMS": "zocrates.agents.common.state_keys",
    "ACTIVE_CONTEXT_ITEMS_STR": "zocrates.agents.common.state_keys",
    "USER_PROMPT": "zocrates.agents.common.state_keys",
    "CACHED_TOCS": "zocrates.agents.common.state_keys",
    "RETRIEVED_EVIDENCE": "zocrates.agents.common.state_keys",
    "TEMP_SEARCH_QUERY": "zocrates.agents.common.state_keys",
    "TEMP_SEEN_CHUNK_IDS": "zocrates.agents.common.state_keys",
    "TEMP_SEEN_ASSETS": "zocrates.agents.common.state_keys",
    "TEMP_SEEN_TOC_ITEM_KEYS": "zocrates.agents.common.state_keys",
    "TEMP_RETRIEVED_EVIDENCE": "zocrates.agents.common.state_keys",
    "TEMP_RETRIEVAL_TOOL_COUNT": "zocrates.agents.common.state_keys",
    "TEMP_SEARCH_REPORT": "zocrates.agents.common.state_keys",
    "HITL_SNAPSHOT": "zocrates.agents.common.state_keys",
    "TEMP_EXTENSION_COUNT": "zocrates.agents.common.state_keys",
    "TEMP_EXTENSION_REQUESTED": "zocrates.agents.common.state_keys",
    "TEMP_RETRIEVAL_TOOL_BUDGET": "zocrates.agents.common.state_keys",
    "TEMP_WORD_CEILING": "zocrates.agents.common.state_keys",
    "TEMP_MAX_RERANK_CHUNKS": "zocrates.agents.common.state_keys",
    # Toolkit core functions
    "search_corpus_core": "zocrates.agents.common.toolkit",
    "get_document_toc_core": "zocrates.agents.common.toolkit",
    "read_document_core": "zocrates.agents.common.toolkit",
    "normalize_asset_ids": "zocrates.agents.common.toolkit",
    "get_related_items_core": "zocrates.agents.common.toolkit",
    # ADK tools
    "search_corpus": "zocrates.agents.common.tools",
    "get_document_toc": "zocrates.agents.common.tools",
    "read_document": "zocrates.agents.common.tools",
    "get_related_items": "zocrates.agents.common.tools",
    "conclude_search": "zocrates.agents.common.tools",
    "request_search_extension": "zocrates.agents.common.tools",
    "get_common_tools": "zocrates.agents.common.tools",
    "MAX_RETRIEVAL_TOOLS_PER_RUN": "zocrates.agents.common.tools",
    "MAX_ABSOLUTE_RETRIEVAL_LIMIT": "zocrates.agents.common.tools",
    # Prompts & formatters
    "format_active_items": "zocrates.agents.common.prompts",
    "format_retrieved_evidence_str": "zocrates.agents.common.prompts",
    "strip_snippet_keys": "zocrates.agents.common.prompts",
    # Thinking & config
    "DEFAULT_THINKING_LEVEL": "zocrates.agents.common.config",
    "clear_ollama_capabilities_cache": "zocrates.agents.common.config",
    "get_agent_generate_content_config": "zocrates.agents.common.config",
    "is_ollama_thinking_supported": "zocrates.agents.common.config",
    # Reranking & budget ceilings
    "rerank_evidence": "zocrates.agents.common.reranker",
    "is_substantive_chunk": "zocrates.agents.common.reranker",
    "filter_substantive_evidence": "zocrates.agents.common.reranker",
    "HARD_WORD_CEILING": "zocrates.agents.common.reranker",
    "MAX_RERANK_CHUNKS": "zocrates.agents.common.reranker",
    # Compaction
    "FailOpenLlmEventSummarizer": "zocrates.agents.common.compaction",
    "build_events_compaction_config": "zocrates.agents.common.compaction",
    "build_compaction_llm": "zocrates.agents.common.compaction",
    "ZOCRATES_COMPACTION_PROMPT_TEMPLATE": "zocrates.agents.common.compaction",
    "DECISION_PROMPT_INSTRUCTIONS": "zocrates.agents.common.adaptive_decision",
    "CompletedTurn": "zocrates.agents.common.adaptive_decision",
    "count_completed_turns_since_last_compaction": "zocrates.agents.common.adaptive_decision",
    "evaluate_adaptive_compaction_rubric": "zocrates.agents.common.adaptive_decision",
    "extract_decision_state": "zocrates.agents.common.adaptive_decision",
    "get_completed_turns": "zocrates.agents.common.adaptive_decision",
    "get_coordinator_prompt_tokens": "zocrates.agents.common.adaptive_decision",
    "is_local_model": "zocrates.agents.common.adaptive_decision",
    "probe_local_laya_health": "zocrates.agents.common.adaptive_decision",
    "should_trigger_adaptive_compaction": "zocrates.agents.common.adaptive_decision",
}

__all__ = [
    # State keys
    "ACTIVE_MODE",
    "ACTIVE_CONTEXT_ITEMS",
    "ACTIVE_CONTEXT_ITEMS_STR",
    "USER_PROMPT",
    "CACHED_TOCS",
    "RETRIEVED_EVIDENCE",
    "TEMP_SEARCH_QUERY",
    "TEMP_SEEN_CHUNK_IDS",
    "TEMP_SEEN_ASSETS",
    "TEMP_SEEN_TOC_ITEM_KEYS",
    "TEMP_RETRIEVED_EVIDENCE",
    "TEMP_RETRIEVAL_TOOL_COUNT",
    "TEMP_SEARCH_REPORT",
    # Toolkit core functions
    "search_corpus_core",
    "get_document_toc_core",
    "read_document_core",
    "normalize_asset_ids",
    "get_related_items_core",
    # ADK tools
    "search_corpus",
    "get_document_toc",
    "read_document",
    "get_related_items",
    "conclude_search",
    "request_search_extension",
    "get_common_tools",
    "MAX_RETRIEVAL_TOOLS_PER_RUN",
    "MAX_ABSOLUTE_RETRIEVAL_LIMIT",
    # Prompts & formatters
    "format_active_items",
    "format_retrieved_evidence_str",
    "strip_snippet_keys",
    # Thinking & config
    "DEFAULT_THINKING_LEVEL",
    "clear_ollama_capabilities_cache",
    "get_agent_generate_content_config",
    "is_ollama_thinking_supported",
    # Reranking & budget ceilings
    "rerank_evidence",
    "is_substantive_chunk",
    "filter_substantive_evidence",
    "HARD_WORD_CEILING",
    "MAX_RERANK_CHUNKS",
    # Compaction
    "FailOpenLlmEventSummarizer",
    "build_events_compaction_config",
    "build_compaction_llm",
    "ZOCRATES_COMPACTION_PROMPT_TEMPLATE",
    "DECISION_PROMPT_INSTRUCTIONS",
    "CompletedTurn",
    "count_completed_turns_since_last_compaction",
    "evaluate_adaptive_compaction_rubric",
    "extract_decision_state",
    "get_completed_turns",
    "get_coordinator_prompt_tokens",
    "is_local_model",
    "probe_local_laya_health",
    "should_trigger_adaptive_compaction",
]


def __getattr__(name: str) -> Any:
    if name in _SYMBOL_MAP:
        module_path = _SYMBOL_MAP[name]
        module = importlib.import_module(module_path)
        val = getattr(module, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
