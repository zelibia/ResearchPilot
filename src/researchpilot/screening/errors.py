"""P0-02 error taxonomy shared by services and HTTP adapters."""

from __future__ import annotations

from typing import Any


class ScreeningError(RuntimeError):
    code = "INTERNAL_ERROR"
    http_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.details = details or {}


class CandidateNotFound(ScreeningError):
    code = "NOT_FOUND"
    http_status = 404


class CandidateValidationError(ScreeningError):
    code = "VALIDATION_ERROR"
    http_status = 422


class UploadRejected(ScreeningError):
    http_status = 400


class CandidateStateError(ScreeningError):
    code = "CANDIDATE_NOT_READY"
    http_status = 409


class CandidateRevisionConflict(ScreeningError):
    code = "REVISION_CONFLICT"
    http_status = 409

    def __init__(self, current_revision: int, message: str = "candidate revision conflict") -> None:
        super().__init__(
            message,
            details={"currentRevision": current_revision},
        )
        self.current_revision = current_revision


class DuplicateConflict(ScreeningError):
    code = "DUPLICATE_CONFLICT"
    http_status = 409


class AIError(ScreeningError):
    http_status = 502


def api_error_payload(
    exc: ScreeningError,
    *,
    request_id: str = "",
) -> dict[str, Any]:
    return {
        "error": {
            "code": exc.code,
            "message": str(exc),
            "details": exc.details,
            "requestId": request_id,
        }
    }
