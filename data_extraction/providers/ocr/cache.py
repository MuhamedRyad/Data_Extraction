"""Opt-in content-addressed OCR cache with atomic write and scoped keys.

Original image bytes are never stored. Caller MUST choose a stable signature
including provider/model/language/version and a tenant-isolated scope.
Cache directory must be trusted. For very sensitive documents, disable caching.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from threading import Lock
from uuid import uuid4
import json
import os

from ...shared.models import ProviderTextResult
from ..contracts import OCRProvider

_LOCAL_LOCK: Lock = Lock()
_MAX_CACHE_BYTES: int = 2_000_000


@dataclass(frozen=True, slots=True)
class CachedOCRProvider:
    provider: OCRProvider
    cache_directory: Path
    provider_signature: str
    scope: str

    def __post_init__(self) -> None:
        if not self.provider_signature.strip() or not self.scope.strip():
            raise ValueError("provider_signature and scope must be nonempty")

    def _entry_path(self, image_bytes: bytes, mime_type: str) -> Path:
        digest = sha256()
        for field in (self.scope, self.provider_signature, mime_type):
            encoded: bytes = field.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
        digest.update(image_bytes)
        hex_digest: str = digest.hexdigest()
        return self.cache_directory / hex_digest[:2] / f"{hex_digest}.json"

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        target: Path = self._entry_path(image_bytes, mime_type)
        with _LOCAL_LOCK:
            if target.is_file():
                try:
                    if target.stat().st_size <= _MAX_CACHE_BYTES:
                        document: object = json.loads(target.read_text(encoding="utf-8"))
                        if isinstance(document, dict) and document.get("version") == 1:
                            result: ProviderTextResult = self._decode(document.get("result"))
                            return result
                except (ValueError, TypeError, OSError, UnicodeError):
                    pass
        result = self.provider.recognize(image_bytes, mime_type)
        if not isinstance(result, ProviderTextResult):
            raise TypeError("Underlying OCR provider returned an invalid response")
        content: bytes = json.dumps({"version": 1, "result": {
            "text": result.text,
            "confidence": result.confidence,
            "structured_table": result.structured_table,
            "provider_name": result.provider_name,
            "model_id": result.model_id,
        }}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(content) > _MAX_CACHE_BYTES:
            return result
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary: Path = target.with_name(target.name + "." + uuid4().hex + ".tmp")
            try:
                with temporary.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        except OSError:
            # Cache unavailability must not erase a successful OCR extraction.
            pass
        return result

    @staticmethod
    def _decode(data: object) -> ProviderTextResult:
        if not isinstance(data, dict):
            raise ValueError("Invalid OCR cache payload")
        text: object = data.get("text")
        confidence: object = data.get("confidence")
        table: object = data.get("structured_table")
        name: object = data.get("provider_name")
        model: object = data.get("model_id")
        if not isinstance(text, str) or len(text) > 1_000_000:
            raise ValueError("Invalid cached text")
        if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                                       or not 0 <= confidence <= 1):
            raise ValueError("Invalid cached confidence")
        if name is not None and (not isinstance(name, str) or len(name) > 256):
            raise ValueError("Invalid cached provider name")
        if model is not None and (not isinstance(model, str) or len(model) > 256):
            raise ValueError("Invalid cached model")
        if table is not None and (not isinstance(table, list) or any(
            not isinstance(row, list) or any(not (cell is None or isinstance(cell, str))
                                             for cell in row) for row in table
        )):
            raise ValueError("Invalid cached matrix")
        return ProviderTextResult(text, confidence, table, name, model)
