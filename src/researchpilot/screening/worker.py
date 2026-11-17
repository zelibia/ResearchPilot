"""Database-lease worker for asynchronous screening runs."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..storage.v116.orm import now_iso
from .orm import CandidateAIRun, ImportCandidate
from .service import CandidateService


class ScreeningWorker:
    def __init__(
        self,
        service: CandidateService,
        factory: sessionmaker[Session],
        *,
        lease_seconds: int = 90,
    ) -> None:
        self.service = service
        self.factory = factory
        self.lease_seconds = lease_seconds

    def recover_expired(self, *, now: str | None = None) -> int:
        current = now or now_iso()
        recovered = 0
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            runs = session.scalars(
                sa.select(CandidateAIRun).where(
                    CandidateAIRun.status == "running",
                    CandidateAIRun.lease_expires_at.is_not(None),
                    CandidateAIRun.lease_expires_at < current,
                )
            ).all()
            for run in runs:
                run.status = "queued"
                run.lease_owner = ""
                run.lease_expires_at = None
                run.updated_at = now_iso()
                candidate = session.get(ImportCandidate, run.candidate_id)
                if candidate is not None and candidate.status == "screening":
                    self.service._transition(
                        session,
                        candidate,
                        "screening_queued",
                        "screening_lease_recovered",
                        "system",
                        {"runId": run.id},
                    )
                recovered += 1
            session.commit()
        return recovered

    def claim_next(self, *, worker_id: str) -> CandidateAIRun | None:
        with self.factory() as session:
            session.execute(sa.text("BEGIN IMMEDIATE"))
            run = session.scalar(
                sa.select(CandidateAIRun)
                .where(CandidateAIRun.status == "queued")
                .order_by(CandidateAIRun.created_at)
                .limit(1)
            )
            if run is None:
                return None
            run.status = "running"
            run.started_at = run.started_at or now_iso()
            run.lease_owner = worker_id
            run.lease_expires_at = (
                datetime.now() + timedelta(seconds=self.lease_seconds)
            ).isoformat(timespec="seconds")
            run.updated_at = now_iso()
            session.commit()
            session.refresh(run)
            return run

    def process_one(self, *, worker_id: str) -> dict[str, Any] | None:
        run = self.claim_next(worker_id=worker_id)
        if run is None:
            return None
        return self.service.execute_run(run.id, actor_id=worker_id)
