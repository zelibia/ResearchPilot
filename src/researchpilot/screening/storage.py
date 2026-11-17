"""Quarantine storage with streamed size, type, and SHA-256 validation."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .errors import UploadRejected

PDF_MAX_BYTES = int(os.environ.get("SCREENING_PDF_MAX_MB", "200")) * 1024 * 1024
IMAGE_MAX_BYTES = int(os.environ.get("SCREENING_IMAGE_MAX_MB", "25")) * 1024 * 1024

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-\u3400-\u9fff]+")


@dataclass(frozen=True)
class StoredUpload:
    storage_key: str
    sha256: str
    size_bytes: int
    source_kind: str
    mime_type: str
    file_name: str


class QuarantineStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def classify(file_name: str, mime_type: str) -> tuple[str, str, int]:
        suffix = Path(file_name).suffix.lower()
        mime = (mime_type or "").split(";", 1)[0].strip().lower()
        if suffix == ".pdf":
            if mime not in {"application/pdf", "application/octet-stream", ""}:
                raise UploadRejected(
                    f"文件扩展名为 PDF，但 MIME 为 {mime or '空'}",
                    code="UPLOAD_INVALID_TYPE",
                )
            return "pdf", "application/pdf", PDF_MAX_BYTES
        image_mimes = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".tif": "image/tiff",
            ".tiff": "image/tiff",
        }
        expected = image_mimes.get(suffix)
        if expected and mime in {expected, "application/octet-stream", ""}:
            return "image", expected, IMAGE_MAX_BYTES
        raise UploadRejected(
            f"不支持的文件类型：{file_name} ({mime or '未知 MIME'})",
            code="UPLOAD_INVALID_TYPE",
        )

    def save_stream(
        self,
        *,
        project_id: str,
        candidate_id: str,
        file_name: str,
        mime_type: str,
        chunks: Iterable[bytes],
    ) -> StoredUpload:
        source_kind, canonical_mime, limit = self.classify(file_name, mime_type)
        safe_name = self._safe_name(file_name)
        relative = Path(project_id) / candidate_id / safe_name
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise UploadRejected("上传路径越界", code="UPLOAD_STORAGE_FAILED")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        size = 0
        prefix = b""
        try:
            with partial.open("wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise UploadRejected(
                            f"文件超过上限 {limit // 1024 // 1024}MB",
                            code="UPLOAD_TOO_LARGE",
                        )
                    if len(prefix) < 16:
                        prefix += chunk[: 16 - len(prefix)]
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size == 0:
                raise UploadRejected("文件为空", code="UPLOAD_INVALID_TYPE")
            self._validate_magic(source_kind, prefix)
            os.replace(partial, target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return StoredUpload(
            storage_key=relative.as_posix(),
            sha256=digest.hexdigest(),
            size_bytes=size,
            source_kind=source_kind,
            mime_type=canonical_mime,
            file_name=safe_name,
        )

    def path_for(self, storage_key: str) -> Path:
        target = (self.root / storage_key).resolve()
        if not target.is_relative_to(self.root):
            raise UploadRejected("无效的存储键", code="UPLOAD_STORAGE_FAILED")
        return target

    def save_text(
        self,
        *,
        project_id: str,
        candidate_id: str,
        file_name: str,
        text: str,
    ) -> StoredUpload:
        safe_name = self._safe_name(file_name)
        relative = Path(project_id) / candidate_id / safe_name
        target = (self.root / relative).resolve()
        if not target.is_relative_to(self.root):
            raise UploadRejected("OCR 文本路径越界", code="UPLOAD_STORAGE_FAILED")
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = text.encode("utf-8")
        partial = target.with_name(target.name + ".part")
        try:
            partial.write_bytes(encoded)
            os.replace(partial, target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise UploadRejected("OCR 文本写入失败", code="UPLOAD_STORAGE_FAILED")
        return StoredUpload(
            storage_key=relative.as_posix(),
            sha256=hashlib.sha256(encoded).hexdigest(),
            size_bytes=len(encoded),
            source_kind="text",
            mime_type="text/plain",
            file_name=safe_name,
        )

    def remove(self, storage_key: str) -> None:
        path = self.path_for(storage_key)
        path.unlink(missing_ok=True)

    @staticmethod
    def _safe_name(file_name: str) -> str:
        name = Path(str(file_name or "").replace("\x00", "")).name.strip()
        name = _SAFE_NAME_RE.sub("_", name).strip("._")
        if not name:
            raise UploadRejected("文件名无效", code="UPLOAD_INVALID_TYPE")
        if len(name) > 180:
            suffix = Path(name).suffix
            name = name[: 180 - len(suffix)] + suffix
        return name

    @staticmethod
    def _validate_magic(source_kind: str, prefix: bytes) -> None:
        if source_kind == "pdf" and not prefix.startswith(b"%PDF-"):
            raise UploadRejected(
                "文件内容不是有效 PDF（缺少 %PDF- 文件头）",
                code="UPLOAD_INVALID_TYPE",
            )
        if source_kind == "image":
            known = (
                prefix.startswith(b"\x89PNG\r\n\x1a\n")
                or prefix.startswith(b"\xff\xd8\xff")
                or (prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP")
                or prefix.startswith((b"II*\x00", b"MM\x00*"))
            )
            if not known:
                raise UploadRejected(
                    "图片文件头无法识别",
                    code="UPLOAD_INVALID_TYPE",
                )
