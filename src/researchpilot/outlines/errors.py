"""P0-05 error taxonomy."""

from __future__ import annotations

from typing import Any


ERROR_STATUS = {
    "OUTLINE_NOT_FOUND": 404,
    "REVISION_NOT_FOUND": 404,
    "NODE_NOT_FOUND": 404,
    "DOCUMENT_MISMATCH": 409,
    "ANNOTATION_MISMATCH": 422,
    "REVISION_CONFLICT": 409,
    "OUTLINE_NODE_CYCLE": 422,
    "OUTLINE_INVALID_TREE": 422,
    "OUTLINE_NODE_LOCKED": 423,
    "MARKDOWN_INVALID": 422,
    "AI_RUN_NOT_READY": 409,
    "MERGE_CONFLICT_PENDING": 409,
    "INTERNAL_ERROR": 500,
}


class OutlineError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        current: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown outline error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return ERROR_STATUS[self.code]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.current is not None:
            payload["current"] = self.current
        if self.details:
            payload["details"] = self.details
        return payload


def revision_conflict(outline: Any) -> OutlineError:
    return OutlineError(
        "REVISION_CONFLICT",
        "大纲已在其他窗口更新。",
        current={
            "currentRevisionNo": outline.current_revision_no,
            "currentRevisionId": outline.current_revision_id,
        },
    )
