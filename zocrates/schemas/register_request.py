from pydantic import BaseModel


class RegisterRequest(BaseModel):
    """Request body for ``POST /register``."""

    zotero_data_dir: str
    scholar_polite_email: str | None = None
    openalex_api_key: str | None = None
    openrouter_api_key: str | None = None
    datalab_api_key: str | None = None
