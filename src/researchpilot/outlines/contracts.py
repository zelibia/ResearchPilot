"""Pydantic DTOs mirrored from P0-05_outline_contracts_v1.16.ts."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


OUTLINE_PROTOCOL_VERSION = "rp.outline.v1"
HeadingLevel = Literal[1, 2, 3, 4, 5, 6]


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(item[:1].upper() + item[1:] for item in rest)


class OutlineContract(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="ignore",
    )


class ActorContext(OutlineContract):
    actor_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class OutlineTreeNode(OutlineContract):
    node_id: str = Field(min_length=1)
    title: str
    heading_level: HeadingLevel
    order_index: int = Field(ge=0)
    body_markdown: str = ""
    anchor: dict[str, Any] | None = None
    annotation_ids: list[str] = Field(default_factory=list)
    note_markdown: str = ""
    locked: bool = False
    origin: Literal["ai", "user", "mixed", "legacy"] = "user"
    children: list[OutlineTreeNode] = Field(default_factory=list)

    @model_validator(mode="after")
    def _title(self) -> OutlineTreeNode:
        if not self.title.strip():
            raise ValueError("outline node title must not be empty")
        return self


class OutlineAIRunRequest(OutlineContract):
    outline_id: str = ""
    base_revision_no: int = Field(ge=1)
    include_annotations: bool = True
    prompt_version: str = "outline.v1"
    provider: str = ""
    model: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class OutlineAIGeneratedNode(OutlineContract):
    temp_id: str
    parent_temp_id: str | None = None
    order_index: int = Field(ge=0)
    heading_level: HeadingLevel
    title: str
    body_markdown: str = ""
    anchor_hint: dict[str, Any] | None = None


class OutlineAIResult(OutlineContract):
    schema_version: Literal["outline.v1"] = "outline.v1"
    summary: str = ""
    nodes: list[OutlineAIGeneratedNode] = Field(default_factory=list)
    markdown: str = ""
    warnings: list[str] = Field(default_factory=list)


class CompleteOutlineAIRunRequest(OutlineContract):
    result: OutlineAIResult
    actor: ActorContext


class MarkdownPreviewRequest(OutlineContract):
    revision_no: int = Field(ge=1)
    markdown: str
    parser_mode: Literal["replace_tree", "preserve_node_ids"] = "preserve_node_ids"
    actor: ActorContext | None = None


class BaseOutlineEditCommand(OutlineContract):
    outline_id: str = ""
    base_revision_no: int = Field(
        ge=1,
        validation_alias=AliasChoices(
            "baseRevisionNo",
            "baseRevision",
            "base_revision_no",
            "base_revision",
        ),
    )
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)
    change_summary: str = ""


class AddOutlineNodeCommand(BaseOutlineEditCommand):
    type: Literal["add_node"]
    parent_node_id: str | None = None
    insert_after_node_id: str | None = None
    title: str = Field(min_length=1)
    heading_level: HeadingLevel
    body_markdown: str = ""
    anchor: dict[str, Any] | None = None
    note_markdown: str = ""


class DeleteOutlineNodesCommand(BaseOutlineEditCommand):
    type: Literal["delete_nodes"]
    node_ids: list[str] = Field(min_length=1)


class RenameOutlineNodeCommand(BaseOutlineEditCommand):
    type: Literal["rename_node"]
    node_id: str
    title: str = Field(min_length=1)


class MoveOutlineNodesCommand(BaseOutlineEditCommand):
    class Move(OutlineContract):
        node_id: str
        parent_node_id: str | None = None
        order_index: int = Field(ge=0)

    type: Literal["move_nodes"]
    moves: list[Move] = Field(min_length=1)


class IndentOutlineNodesCommand(BaseOutlineEditCommand):
    type: Literal["indent_nodes", "outdent_nodes"]
    node_ids: list[str] = Field(min_length=1)


class UpdateOutlineNodeContentCommand(BaseOutlineEditCommand):
    type: Literal["update_node_content"]
    node_id: str
    body_markdown: str | None = None
    note_markdown: str | None = None

    @model_validator(mode="after")
    def _content_present(self) -> UpdateOutlineNodeContentCommand:
        if self.body_markdown is None and self.note_markdown is None:
            raise ValueError("bodyMarkdown or noteMarkdown is required")
        return self


class SetOutlineNodeLockCommand(BaseOutlineEditCommand):
    type: Literal["set_lock"]
    node_id: str
    locked: bool


class AttachOutlineAnnotationsCommand(BaseOutlineEditCommand):
    type: Literal["attach_annotations"]
    node_id: str
    annotation_ids: list[str]
    relation_type: Literal["evidence", "related", "follow_up"] = "evidence"


class SaveOutlineMarkdownCommand(BaseOutlineEditCommand):
    type: Literal["save_markdown"]
    markdown: str
    parser_mode: Literal["replace_tree", "preserve_node_ids"] = "preserve_node_ids"


OutlineEditCommand = Annotated[
    Union[
        AddOutlineNodeCommand,
        DeleteOutlineNodesCommand,
        RenameOutlineNodeCommand,
        MoveOutlineNodesCommand,
        IndentOutlineNodesCommand,
        UpdateOutlineNodeContentCommand,
        SetOutlineNodeLockCommand,
        AttachOutlineAnnotationsCommand,
        SaveOutlineMarkdownCommand,
    ],
    Field(discriminator="type"),
]
OUTLINE_EDIT_ADAPTER = TypeAdapter(OutlineEditCommand)


class ResolveOutlineConflictCommand(OutlineContract):
    conflict_id: str = ""
    base_revision_no: int = Field(ge=1)
    resolution: Literal["keep_current", "accept_ai", "manual_merge", "dismissed"]
    merged_node: OutlineTreeNode | None = None
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class ApplyOutlineMergeCommand(OutlineContract):
    outline_id: str = ""
    candidate_revision_id: str
    base_revision_no: int = Field(ge=1)
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class RestoreOutlineRevisionCommand(OutlineContract):
    outline_id: str = ""
    source_revision_no: int = Field(ge=1)
    base_revision_no: int = Field(ge=1)
    change_summary: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class SetOutlineNodeCompletedCommand(OutlineContract):
    project_id: str
    outline_id: str
    node_id: str
    completed: bool
    actor: ActorContext
