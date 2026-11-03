from typing import Literal

from pydantic import BaseModel, SecretStr


class ConverterChangeRequest(BaseModel):
    """Request body for ``POST /rag/converter``."""

    converter_id: str | None = None
    document_converter_id: str | None = None
    document_converter_mode: Literal["local", "cloud"] | None = None
    docling_mode: Literal["auto", "fast", "full"] | None = None
    datalab_non_pdf_fallback: Literal["markitdown", "docling_local"] | None = None
    datalab_merge_cross_page: bool | None = None
    datalab_api_key: SecretStr | str | None = None
    conversion_timeout_seconds: int | None = None
    enable_vlm_captioning: bool | None = None
    vlm_captioning_provider: Literal["openrouter", "ollama"] | None = None
    vlm_captioning_model: str | None = None
    vlm_captioning_backup_model: str | None = None
    vlm_captioning_local_model: str | None = None
    vlm_max_images_per_doc: int | None = None


class ConverterTestRequest(BaseModel):
    """Request body for ``POST /converters/test-connection``."""

    converter_id: str
    api_key: str | None = None
    datalab_api_key: str | None = None


class ConvertRequest(BaseModel):
    """Request body for ``POST /convert/{item_key}``."""

    converter_id: str | None = None
    force: bool = False


class BatchConvertRequest(BaseModel):
    """Request body for ``POST /batch-convert``."""

    item_keys: list[str]
    converter_id: str | None = None
    force: bool = False
