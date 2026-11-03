from pydantic import BaseModel


class LLMTestRequest(BaseModel):
    """Request body for ``POST /llm/test``."""

    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    openrouter_api_key: str | None = None


class OpenAlexTestRequest(BaseModel):
    """Request body for ``POST /discovery/test_openalex``."""

    openalex_api_key: str | None = None


class OpenRouterKeyTestRequest(BaseModel):
    """Request body for ``POST /llm/test_openrouter_key``."""

    openrouter_api_key: str | None = None


class SuggestPromptRequest(BaseModel):
    """Request body for ``POST /sessions/{session_id}/suggest_next_prompt``."""

    invocation_id: str
