from pydantic import BaseModel


class DecisionModelTestRequest(BaseModel):
    """Request body for ``POST /llm/test_decision_model``."""

    provider: str | None = "openrouter"
    model: str | None = None
    local_url: str | None = None
    openrouter_api_key: str | None = None
