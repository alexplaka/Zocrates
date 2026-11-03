from typing import Literal

from pydantic import BaseModel, SecretStr


class EmbeddingTestRequest(BaseModel):
    """Request body for ``POST /llm/test_embedding``."""

    embedding_provider: str | None = None
    embedding_model: str | None = None
    openrouter_api_key: str | None = None


class RAGResetRequest(BaseModel):
    """Request body for ``POST /rag/reset``.

    Attributes:
        target: ``"active"`` (default) to reset the active collection,
            ``"all"`` to wipe every collection and the FTS database, or a
            specific collection name string.
    """

    target: str = "active"  # "active", "all", or a specific collection name


class EmbeddingModelChangeRequest(BaseModel):
    """Request body for ``POST /rag/model``."""

    model_name: str | None = None
    mode: Literal["local", "cloud"] | None = None
    dimension: int | None = None
    force_cpu: bool | None = None
    cloud_api_key: SecretStr | str | None = None
    openrouter_api_key: SecretStr | str | None = None


class BatchIndexRequest(BaseModel):
    """Request body for ``POST /batch-index``."""

    keys: list[str] | None = None
    all_pending: bool = False
