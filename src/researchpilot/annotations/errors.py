"""P0-04 error taxonomy and HTTP serialization."""

from __future__ import annotations

from typing import Any


ERROR_STATUS = {
    "DOCUMENT_MISMATCH": 409,
    "FILE_MISMATCH": 409,
    "INVALID_TYPE": 422,
    "INVALID_COLOR": 422,
    "EMPTY_SELECTION": 422,
    "ANCHOR_INVALID": 422,
    "ANCHOR_NOT_RESOLVABLE": 404,
    "REVISION_CONFLICT": 409,
    "ANNOTATION_NOT_FOUND": 404,
    "READ_ONLY_ANNOTATION": 403,
    "TEXT_LAYER_UNAVAILABLE": 409,
    "OCR_TEXT_UNAVAILABLE": 422,
    "BATCH_PARTIAL_FAILURE": 409,
    "SEARCH_INDEX_UNAVAILABLE": 503,
    "INTERNAL_ERROR": 500,
}


class AnnotationError(RuntimeError):
    """A service error whose code is part of the P0-04 public contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        current: dict[str, Any] | None = None,
        selection_state: dict[str, Any] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in ERROR_STATUS:
            raise ValueError(f"unknown annotation error code: {code}")
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current
        self.selection_state = selection_state
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return ERROR_STATUS[self.code]

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.current is not None:
            payload["current"] = self.current
        if self.selection_state is not None:
            payload["selectionState"] = self.selection_state
        if self.details:
            payload["details"] = self.details
        return payload


def conflict_error(
    annotation: dict[str, Any],
    message: str = "这条批注已在其他窗口更新。",
) -> AnnotationError:
    return AnnotationError(
        "REVISION_CONFLICT",
        message,
        current={"annotation": annotation},
    )


def not_found(annotation_id: str) -> AnnotationError:
    return AnnotationError(
        "ANNOTATION_NOT_FOUND",
        f"annotation not found: {annotation_id}",
    )


def read_only(message: str = "annotation is read-only") -> AnnotationError:
    return AnnotationError("READ_ONLY_ANNOTATION", message)


def invalid_anchor(message: str) -> AnnotationError:
    return AnnotationError("ANCHOR_INVALID", message)


def invalid_type(message: str = "invalid text annotation type") -> AnnotationError:
    return AnnotationError("INVALID_TYPE", message)


def invalid_color(message: str = "invalid annotation color") -> AnnotationError:
    return AnnotationError("INVALID_COLOR", message)


def internal_error(
    message: str,
    *,
    selection_state: dict[str, Any] | None = None,
) -> AnnotationError:
    return AnnotationError(
        "INTERNAL_ERROR",
        message,
        selection_state=selection_state,
    )
