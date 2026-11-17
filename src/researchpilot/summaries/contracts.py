"""Pydantic DTOs for P0-06."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _camel(name: str) -> str:
    first, *rest = name.split("_")
    return first + "".join(item[:1].upper() + item[1:] for item in rest)


class Contract(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        populate_by_name=True,
        extra="ignore",
    )


class ActorContext(Contract):
    actor_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class SummaryScope(Contract):
    type: Literal["single_document", "multi_document", "day", "project"]
    document_ids: list[str] = Field(default_factory=list)
    day: str | None = None
    excluded_documents: list[dict[str, str]] = Field(default_factory=list)


class GenerateSummaryCommand(Contract):
    project_id: str = ""
    mode: Literal["independent", "incremental"]
    scope: SummaryScope
    base_revision_no: int | None = None
    prompt_version: str = "summary.v1"
    provider: str = ""
    model: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class SummaryAIResult(Contract):
    schema_version: Literal["summary.v1"] = "summary.v1"
    title: str = "阶段总结"
    overview_markdown: str = ""
    sections: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CompleteGenerationRequest(Contract):
    result: SummaryAIResult
    actor: ActorContext


class UpdateSectionCommand(Contract):
    summary_id: str = ""
    revision_id: str = ""
    base_revision_no: int = Field(ge=1)
    section_id: str
    title: str | None = None
    body_markdown: str | None = None
    evidence_ids: list[str] | None = None
    change_summary: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class SaveMarkdownCommand(Contract):
    summary_id: str = ""
    revision_id: str = ""
    base_revision_no: int = Field(ge=1)
    markdown: str
    parser_mode: Literal["replace_sections", "preserve_section_ids"] = "preserve_section_ids"
    change_summary: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class ConfirmRevisionCommand(Contract):
    summary_id: str = ""
    revision_id: str = ""
    base_revision_no: int = Field(ge=0)
    change_summary: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class RejectRevisionCommand(Contract):
    summary_id: str = ""
    revision_id: str = ""
    base_revision_no: int = Field(ge=0)
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)


class RestoreSummaryRevisionCommand(Contract):
    summary_id: str = ""
    source_revision_no: int = Field(ge=1)
    base_revision_no: int = Field(ge=0)
    change_summary: str = ""
    actor: ActorContext
    client_mutation_id: str = Field(min_length=1)
