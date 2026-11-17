"""Project-scoped conversation grouping for the global assistant.

The existing DiscussionMessage table remains the source of truth. New
conversation ids live in its context metadata, so this module adds no schema
or business-logic coupling and legacy assistant messages stay readable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..storage import repository as repo
from ..storage.models import DiscussionMessage, MessageRole, Project

AGENT_ROLE = "project_assistant"


def _is_assistant_message(item: DiscussionMessage) -> bool:
    return (item.context or {}).get("agent_role") == AGENT_ROLE


def _legacy_id(project_id: str) -> str:
    return f"legacy:{project_id}"


def _id_for(item: DiscussionMessage, project_id: str) -> str:
    value = str((item.context or {}).get("conversation_id") or "").strip()
    return value or _legacy_id(project_id)


def _summary(item: DiscussionMessage) -> str:
    text = " ".join(str(item.content or "").split())
    return text[:56] + ("…" if len(text) > 56 else "")


def list_conversations(project: Project) -> list[dict[str, Any]]:
    """Return project assistant threads, newest activity first."""
    groups: dict[str, list[DiscussionMessage]] = {}
    for item in repo.list_all(DiscussionMessage, project.id):
        if not _is_assistant_message(item):
            continue
        groups.setdefault(_id_for(item, project.id), []).append(item)
    result: list[dict[str, Any]] = []
    for conversation_id, messages in groups.items():
        messages.sort(key=lambda item: (item.created_at, item.id))
        first = messages[0]
        first_user = next(
            (item for item in messages if item.role is MessageRole.USER),
            first,
        )
        result.append({
            "id": conversation_id,
            "summary": _summary(first_user) or "未命名对话",
            "created_at": first.created_at,
            "last_at": messages[-1].created_at,
            "page_type": str(first.context.get("screen") or "文献阅读"),
            "message_count": len(messages),
        })
    return sorted(
        result,
        key=lambda item: (item["last_at"], item["created_at"], item["id"]),
        reverse=True,
    )


def thread(
    project: Project,
    conversation_id: str,
) -> list[dict[str, Any]]:
    messages = [
        item for item in repo.list_all(DiscussionMessage, project.id)
        if _is_assistant_message(item)
        and _id_for(item, project.id) == conversation_id
    ]
    messages.sort(key=lambda item: (item.created_at, item.id))
    rows: list[dict[str, Any]] = []
    for item in messages:
        row = {
            "id": item.id,
            "role": item.role.value,
            "text": item.content,
            "meta": (
                item.error
                or "　".join(
                    value for value in (item.provider, item.model, item.created_at)
                    if value
                )
            ),
        }
        if item.error:
            row["error"] = True
        rows.append(row)
    return rows


def delete_conversation(project: Project, conversation_id: str) -> int:
    count = 0
    for item in repo.list_all(DiscussionMessage, project.id):
        if (
            _is_assistant_message(item)
            and _id_for(item, project.id) == conversation_id
        ):
            repo.delete(DiscussionMessage, item.id)
            count += 1
    return count


def delete_all(project: Project) -> int:
    count = 0
    for item in repo.list_all(DiscussionMessage, project.id):
        if _is_assistant_message(item):
            repo.delete(DiscussionMessage, item.id)
            count += 1
    return count


def new_id() -> str:
    return repo.new_id("conv")


def created_label(value: str) -> str:
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return value or "时间未知"
    return moment.strftime("%m-%d %H:%M")
