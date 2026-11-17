"""P0-05 durable standard events."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..storage.v116.orm import OutlineEventOutbox, OutlineEventSequence
from .contracts import OUTLINE_PROTOCOL_VERSION

EVENT_TYPES = {
    "outline_node_clicked",
    "outline_updated",
    "revision_switch",
    "ai_regenerate_request",
    "outline_generation_state",
    "outline_merge_ready",
    "outline_progress_updated",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def event_out(row: OutlineEventOutbox) -> dict[str, Any]:
    return {
        "protocolVersion": OUTLINE_PROTOCOL_VERSION,
        "eventId": row.event_id,
        "sequence": row.sequence,
        "type": row.type,
        "documentId": row.document_id,
        "outlineId": row.outline_id,
        "occurredAt": row.occurred_at,
        "payload": row.payload_json,
    }


def emit_outline_event(
    session: Session,
    *,
    event_type: str,
    document_id: str,
    outline_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported outline event: {event_type}")
    sequence = OutlineEventSequence()
    session.add(sequence)
    session.flush()
    row = OutlineEventOutbox(
        event_id=f"oevt_{uuid.uuid4().hex}",
        sequence=int(sequence.id),
        type=event_type,
        document_id=document_id,
        outline_id=outline_id,
        occurred_at=utc_now_iso(),
        payload_json=payload,
        created_at=utc_now_iso(),
    )
    session.add(row)
    session.flush()
    return event_out(row)


def canonical_hash(value: Any) -> str:
    import hashlib

    body = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()
