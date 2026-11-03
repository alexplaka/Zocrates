"""Centralized session and invocation state key constants for Zocrates agents."""

# Session-persistent state keys (persisted across turns in DatabaseSessionService)
ACTIVE_MODE = "active_mode"
ACTIVE_CONTEXT_ITEMS = "active_context_items"
ACTIVE_CONTEXT_ITEMS_STR = "active_context_items_str"
USER_PROMPT = "user_prompt"
RETRIEVED_EVIDENCE = "retrieved_evidence"
HITL_SNAPSHOT = "hitl_snapshot"
CACHED_TOCS = "cached_tocs"
CANDIDATE_PAPERS = "candidates"
RESEARCH_TURN_HISTORY = "research_turn_history"
USER_RESEARCH_PROFILE = "user_research_profile"
USER_RESEARCH_PROFILE_STR = "user_research_profile_str"

# Temporary / invocation state keys (prefixed with 'temp:', cleared or scoped per invocation)
TEMP_SEARCH_QUERY = "temp:search_query"
TEMP_SEEN_CHUNK_IDS = "temp:seen_chunk_ids"
TEMP_SEEN_ASSETS = "temp:seen_assets"
TEMP_SEEN_TOC_ITEM_KEYS = "temp:seen_toc_item_keys"
TEMP_RETRIEVED_EVIDENCE = "temp:retrieved_evidence"
TEMP_RETRIEVAL_TOOL_COUNT = "temp:retrieval_tool_count"
TEMP_SEARCH_REPORT = "temp:search_report"
TEMP_RETRIEVAL_TOOL_BUDGET = "temp:retrieval_tool_budget"
TEMP_WORD_CEILING = "temp:word_ceiling"
TEMP_MAX_RERANK_CHUNKS = "temp:max_rerank_chunks"
TEMP_EXTENSION_COUNT = "temp:extension_count"
TEMP_EXTENSION_REQUESTED = "temp:extension_requested"
TEMP_EXTENSION_REQUEST_PASS = "temp:extension_request_pass"

# Online discovery state keys
TEMP_ONLINE_CANDIDATES = "temp_online_candidates"

