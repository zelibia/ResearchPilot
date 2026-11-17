"""P0-06 error taxonomy."""

from __future__ import annotations

from typing import Any


STATUS = {
    "SCOPE_EMPTY": 422,
    "SCOPE_INVALID_DOCUMENT": 422,
    "INCREMENTAL_BASE_REQUIRED": 422,
    "BASE_REVISION_NOT_OFFICIAL": 409,
    "BASE_REVISION_MISMATCH": 409,
    "AI_NOT_CONNECTED": 503,
    "AI_TIMEOUT": 504,
    "AI_RATE_LIMIT": 429,
    "AI_AUTH_FAILED": 502,
    "AI_BAD_JSON": 502,
    "AI_SCHEMA_INVALID": 502,
    "EVIDENCE_VALIDATION_FAILED": 422,
    "REVISION_CONFLICT": 409,
    "SUMMARY_NOT_FOUND": 404,
    "REVISION_NOT_FOUND": 404,
    "CANDIDATE_REQUIRED": 409,
    "INTERNAL_ERROR": 500,
}


class SummaryError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        current: dict[str, Any] | None = None,
    ) -> None:
        if code not in STATUS:
            raise ValueError(code)
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current

    @property
    def http_status(self) -> int:
        return STATUS[self.code]

    def as_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message}
        if self.current is not None:
            payload["current"] = self.current
        return payload
