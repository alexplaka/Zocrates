import time
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

ActiveItemType = Literal["item", "collection", "library", "note"]


class ActiveItem(BaseModel):
    """A Zotero context chip sent by the sidebar representing a selected item, collection, or library.

    Attributes:
        key: The item key in the Zotero library.
        title: The title of the item.
        type: Scope or kind of the selection:
            ``"item"``, ``"collection"``, ``"library"``, or ``"note"``.
    """

    model_config = ConfigDict(populate_by_name=True)

    key: str = Field(validation_alias=AliasChoices("key", "item_key", "itemKey"))
    title: str
    type: ActiveItemType | None = "item"


class SnippetItem(BaseModel):
    """A user-curated text or image snippet extracted from an attachment."""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    id: str = Field(default_factory=lambda: f"snip_{int(time.time() * 1000)}")
    type: Literal["text", "image"]
    item_key: str | None = None
    parent_key: str | None = None
    paper_title: str | None = None
    author_str: str | None = None
    authors: list[str] | None = None
    page: int | str | None = None
    text: str | None = None
    image_data: str | None = Field(
        default=None,
        max_length=5_000_000,
        description="Base64 data URI (data:image/...;base64,...), capped at ~3.75MB image payload",
    )
    mime_type: str = "image/jpeg"


class ItemDeleteRequest(BaseModel):
    """Request body for ``POST /items/delete``."""

    item_keys: list[str]
