"""P0-06 event outbox."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..storage.v116.orm import SummaryEventOutbox, SummaryEventSequence


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def hash_json(value: Any) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def emit(
    session: Session,
    *,
    event_type: str,
    project_id: str,
    summary_id: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    seq = SummaryEventSequence()
    session.add(seq)
    session.flush()
    row = SummaryEventOutbox(
        event_id=f"sevt_{uuid.uuid4().hex}",
        sequence=seq.id,
        type=event_type,
        project_id=project_id,
        summary_id=summary_id,
        occurred_at=now(),
        payload_json=payload,
        created_at=now(),
    )
    session.add(row)
    session.flush()
    return out(row)


def out(row: SummaryEventOutbox) -> dict[str, Any]:
    return {
        "protocolVersion": "rp.summary.v1",
        "eventId": row.event_id,
        "sequence": row.sequence,
        "type": row.type,
        "projectId": row.project_id,
        "summaryId": row.summary_id,
        "occurredAt": row.occurred_at,
        "payload": row.payload_json,
    }
