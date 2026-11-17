"""Pydantic DTOs mirrored from P0-04_contracts_v1.16.ts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


ANNOTATION_PROTOCOL_VERSION = "rp.annotation.v1"
TEXT_ANNOTATION_TYPES = ("highlight", "underline", "strikeout")
TEXT_SOURCES = ("pdf_text", "ocr")
STANDARD_COLORS = (
    "#FFD400",
    "#FF6666",
    "#5FB236",
    "#2EA8E5",
    "#A28AE5",
    "#F19837",
)
ANCHOR_STATUSES = ("exact", "partial", "none", "no_text_layer", "out_of_range")


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(item[:1].upper() + item[1:] for item in rest)


class ContractModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="ignore",
    )


class ActorContext(ContractModel):
    actor_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class PageViewBox(ContractModel):
    width: float = Field(gt=0)
    height: float = Field(gt=0)
    rotation: Literal[0, 90, 180, 270] = 0


class PageNormalizedRect(ContractModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(ge=0, le=1)
    height: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _inside_page(self) -> PageNormalizedRect:
        epsilon = 1e-9
        if self.x + self.width > 1 + epsilon or self.y + self.height > 1 + epsilon:
            raise ValueError("normalized rect exceeds page bounds")
        return self


class ReaderAnchorSegment(ContractModel):
    page_index: int = Field(ge=0)
    quote: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    rects: list[PageNormalizedRect] = Field(default_factory=list)
    page_view_box: PageViewBox
    text_source: Literal["pdf_text", "ocr"]
    text_hash: str

    @model_validator(mode="after")
    def _char_order(self) -> ReaderAnchorSegment:
        if self.char_end < self.char_start:
            raise ValueError("segment charEnd must be >= charStart")
        if not self.quote:
            raise ValueError("segment quote must not be empty")
        return self


class AnnotationAnchorSelector(ContractModel):
    coordinate_version: Literal["page-normalized-v1"] = "page-normalized-v1"
    page_view_box: PageViewBox
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text_hash: str
    text_source: Literal["pdf_text", "ocr"]
    segments: list[ReaderAnchorSegment] = Field(default_factory=list)

class AnnotationDraft(ContractModel):
    document_id: str = Field(min_length=1)
    file_asset_id: str = Field(min_length=1)
    type: Literal["highlight", "underline", "strikeout"]
    color: str
    quote: str
    comment_markdown: str
    page_index: int = Field(ge=0)
    page_label: str
    rects: list[PageNormalizedRect] = Field(min_length=1)
    selector: AnnotationAnchorSelector
    sort_index: str = Field(min_length=1)
    anchor_status: Literal["exact", "partial", "none", "no_text_layer", "out_of_range"]


class CreateAnnotationCommand(ContractModel):
    draft: AnnotationDraft
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class BaseRevisionContract(ContractModel):
    base_revision: int = Field(
        ge=1,
        validation_alias=AliasChoices(
            "baseRevision",
            "baseRevisionNo",
            "base_revision",
            "base_revision_no",
        ),
    )


class UpdateAnnotationCommentCommand(BaseRevisionContract):
    annotation_id: str = ""
    comment_markdown: str = Field(max_length=200_000)
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class UpdateAnnotationStylePatch(ContractModel):
    type: Literal["highlight", "underline", "strikeout"] | None = None
    color: str | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> UpdateAnnotationStylePatch:
        if self.type is None and self.color is None:
            raise ValueError("style patch must contain type or color")
        return self


class UpdateAnnotationStyleCommand(BaseRevisionContract):
    annotation_id: str = ""
    patch: UpdateAnnotationStylePatch
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class DeleteAnnotationCommand(BaseRevisionContract):
    annotation_id: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class RestoreAnnotationCommand(BaseRevisionContract):
    annotation_id: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)
    undo_token: str | None = None


class AnnotationSidebarFilter(ContractModel):
    types: list[Literal["highlight", "underline", "strikeout"]] = Field(
        default_factory=list
    )
    colors: list[str] = Field(default_factory=list)
    text_sources: list[Literal["pdf_text", "ocr"]] = Field(default_factory=list)
    edited_only: bool = False
    comment_only: bool = False
    search_query: str = ""

    @field_validator("colors")
    @classmethod
    def _normalize_colors(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.upper() for value in values))


class AnnotationSidebarQuery(ContractModel):
    document_id: str = Field(min_length=1)
    file_asset_id: str = Field(min_length=1)
    filter: AnnotationSidebarFilter = Field(default_factory=AnnotationSidebarFilter)
    sort: Literal["page_asc", "updated_desc", "created_desc"] = "page_asc"
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = None


class BatchDeleteAnnotationsCommand(ContractModel):
    document_id: str = Field(min_length=1)
    file_asset_id: str = ""
    annotation_ids: list[str] = Field(min_length=1, max_length=500)
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)
    base_revisions: dict[str, int] = Field(default_factory=dict)
    base_revision_no: int | None = Field(default=None, ge=1)

    @field_validator("annotation_ids")
    @classmethod
    def _unique_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in values if item))


class BatchSetAnnotationColorCommand(ContractModel):
    document_id: str = Field(min_length=1)
    file_asset_id: str = ""
    annotation_ids: list[str] = Field(min_length=1, max_length=500)
    color: str
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)
    base_revisions: dict[str, int] = Field(default_factory=dict)
    base_revision_no: int | None = Field(default=None, ge=1)

    @field_validator("annotation_ids")
    @classmethod
    def _unique_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(item for item in values if item))


class UndoBatchCommand(ContractModel):
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class LocateFromSidebarInput(ContractModel):
    annotation_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    file_asset_id: str = Field(min_length=1)


class AnnotationModeCapabilities(ContractModel):
    mode: Literal["pdf_text", "ocr_text", "png_fallback"]
    can_create_text_annotation: bool
    can_edit_annotation: bool
    can_delete_annotation: bool
    can_comment: bool
    can_search_sidebar: bool
    can_locate_from_sidebar: bool
    shows_text_source_badge: bool


class AnnotationEventEnvelope(ContractModel):
    event: dict[str, Any]
    bridge: Literal["trigger", "state"]


def event_envelope(event: dict[str, Any]) -> dict[str, Any]:
    return AnnotationEventEnvelope(event=event, bridge="trigger").model_dump(
        by_alias=True
    )
