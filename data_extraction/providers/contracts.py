"""Vendor-neutral, typed OCR/Vision interfaces."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..shared.models import BoundingBox, ProviderTextResult


@dataclass(frozen=True, slots=True)
class OCRTextRegion:
    text: str
    bbox: BoundingBox
    confidence: float | None


class OCRProvider(Protocol):
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult: ...


class OCRLayoutProvider(OCRProvider, Protocol):
    """Optional richer OCR contract used by field-level verification.

    Coordinates are image pixels in the input raster's coordinate system.
    """

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]: ...


class OCRTokenProvider(OCRLayoutProvider, Protocol):
    """Optional token/box contract for conservative raster-table recovery."""

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]: ...


class OCRTableCellProvider(OCRTokenProvider, Protocol):
    """Optional cell-aware OCR hint used only after template row/column geometry is known."""

    def recognize_cell(self, image_bytes: bytes, mime_type: str, value_kind: str) -> ProviderTextResult: ...


class VisionProvider(Protocol):
    def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult: ...
