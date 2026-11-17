"""Durable P0-04 standard event creation and replay."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..storage.v116.orm import AnnotationEventOutbox, AnnotationEventSequence
from .contracts import ANNOTATION_PROTOCOL_VERSION

ALLOWED_EVENT_TYPES = {
    "annotation_created",
    "annotation_updated",
    "annotation_deleted",
    "annotation_restored",
    "annotation_sidebar_located",
    "annotation_batch_changed",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def event_from_row(row: AnnotationEventOutbox) -> dict[str, Any]:
    return {
        "protocolVersion": ANNOTATION_PROTOCOL_VERSION,
        "eventId": row.event_id,
        "sequence": row.sequence,
        "type": row.type,
        "documentId": row.document_id,
        "fileAssetId": row.file_asset_id,
        "occurredAt": row.occurred_at,
        "payload": row.payload_json,
    }


def emit_event(
    session: Session,
    *,
    event_type: str,
    document_id: str,
    file_asset_id: str,
    payload: dict[str, Any],
) -> AnnotationEventOutbox:
    if event_type not in ALLOWED_EVENT_TYPES:
        raise ValueError(f"unsupported annotation event: {event_type}")
    sequence_row = AnnotationEventSequence()
    session.add(sequence_row)
    session.flush()
    row = AnnotationEventOutbox(
        event_id=f"aevt_{uuid.uuid4().hex}",
        sequence=int(sequence_row.id),
        type=event_type,
        document_id=document_id,
        file_asset_id=file_asset_id,
        occurred_at=utc_now_iso(),
        payload_json=payload,
        created_at=utc_now_iso(),
    )
    session.add(row)
    session.flush()
    return row


def list_events(
    session: Session,
    *,
    after_sequence: int = 0,
    document_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    statement = sa.select(AnnotationEventOutbox).where(
        AnnotationEventOutbox.sequence > after_sequence
    )
    if document_id:
        statement = statement.where(AnnotationEventOutbox.document_id == document_id)
    rows = session.scalars(
        statement.order_by(AnnotationEventOutbox.sequence).limit(limit)
    ).all()
    return [event_from_row(row) for row in rows]


def canonical_request_hash(value: Any) -> str:
    import hashlib

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
