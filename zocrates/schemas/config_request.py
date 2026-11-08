from typing import Any

from pydantic import BaseModel, Field


class ConfigRequest(BaseModel):
    """Request body for ``POST /config``."""

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_temperature: float | None = None
    llm_max_tokens: int | None = None
    llm_num_retries: int | None = None
    llm_extra_options: dict | None = None
    ollama_num_ctx: int | None = Field(default=None, ge=1)
    override_agent_models: dict[str, Any] | None = None
    embedding_provider: str | None = None
    embedding_model: str | None = None
    openrouter_api_key: str | None = None
    reranker_model: str | None = None
    citations_enabled: bool | None = None
    references_section_enabled: bool | None = None
    auto_render_mermaid_diagrams: bool | None = None
    enable_mcp_server: bool | None = None
    citation_verification_enabled: bool | None = None
    citation_verification_provider: str | None = None
    citation_verification_model: str | None = None
    citation_verification_threshold: float | None = None
    citation_verification_local_url: str | None = None
    citation_verification_local_port: int | None = None
    citation_verification_auto_start_local: bool | None = None
    citation_verification_timeout_sec: float | None = None
    citation_verification_turn_budget_sec: float | None = None
    scholar_polite_email: str | None = None
    openalex_api_key: str | None = None
    enable_predictive_ghost_input: bool | None = None
    predictive_prompt_model: str | None = None
    sentence_attribution_model: str | None = None
    user_research_profile: str | None = None
    enable_adaptive_compaction: bool | None = None
    adaptive_compaction_retention_turns: int | None = None
    adaptive_compaction_min_turns: int | None = None
    adaptive_compaction_token_floor: int | None = None
    adaptive_compaction_token_ceiling: int | None = None
    adaptive_compaction_threshold: float | None = None
    adaptive_compaction_decision_model: str | None = None
    compaction_summarizer_model: str | None = None
    max_saved_sessions: int | None = Field(default=None, ge=0)
    session_retention_days: int | None = Field(default=None, ge=0)
    session_db_auto_vacuum_on_startup: bool | None = None
    prune_empty_sessions: bool | None = None
    backend_port: int | None = Field(default=None, ge=1, le=65535)
    auto_spawn_backend: bool | None = None
    datalab_merge_cross_page: bool | None = None
    datalab_api_key: str | None = None
