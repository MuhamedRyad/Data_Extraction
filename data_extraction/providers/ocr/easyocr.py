"""Lazy optional EasyOCR implementation of the generic OCR contracts.

Adapter does not auto-download weights; install models explicitly. Results are
reviewable OCR evidence, not guaranteed authoritative Arabic reading order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any
import io
import math
import re
import unicodedata

from PIL import Image

from ...shared.models import ProviderTextResult
from ..contracts import OCRTextRegion


@dataclass(slots=True)
class EasyOCRProvider:
    languages: tuple[str, ...] = ("ar", "en")
    gpu: bool = False
    minimum_confidence: float = 0.0
    max_detections: int = 1500
    _reader: Any | None = field(default=None, init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.languages or any(not re.fullmatch(r"[a-z][a-z0-9_]*", lang) for lang in self.languages):
            raise ValueError("Invalid EasyOCR language codes")
        if not 0 <= self.minimum_confidence <= 1:
            raise ValueError("Invalid OCR confidence threshold")
        if not 1 <= self.max_detections <= 10000:
            raise ValueError("Invalid detection budget")

    def _raw_items(self, image_bytes: bytes) -> list[tuple[float, float, float, float, str, float]]:
        import easyocr
        import numpy

        with self._lock:
            if self._reader is None:
                self._reader = easyocr.Reader(
                    list(self.languages), gpu=self.gpu, download_enabled=False
                )
            reader = self._reader
        with Image.open(io.BytesIO(image_bytes)) as source:
            if source.width * source.height > 8_000_000:
                raise ValueError("OCR image exceeds pixel limit")
            frame = numpy.asarray(source.convert("RGB"))
        raw: object = reader.readtext(frame, detail=1, paragraph=False)
        if not isinstance(raw, (tuple, list)) or len(raw) > self.max_detections:
            raise ValueError("Invalid EasyOCR detection count")
        items: list[tuple[float, float, float, float, str, float]] = []
        for detection in raw:
            if not isinstance(detection, (tuple, list)) or len(detection) != 3:
                raise ValueError("Invalid OCR detection")
            vertices, text, confidence = detection
            if not isinstance(text, str) or len(text) > 10000 or not isinstance(vertices, (tuple, list)):
                raise ValueError("Invalid OCR text or polygon")
            try:
                x: list[float] = [float(pt[0]) for pt in vertices]
                y: list[float] = [float(pt[1]) for pt in vertices]
                score: float = float(confidence)
            except (TypeError, IndexError, ValueError) as error:
                raise ValueError("Invalid OCR geometry/confidence") from error
            if len(x) != 4 or not all(math.isfinite(v) for v in x + y) or not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError("Invalid OCR geometry/confidence")
            if text.strip() and score >= self.minimum_confidence:
                items.append((min(x), min(y), max(x), max(y), text.strip(), score))
        return items

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return tuple(
            OCRTextRegion(text, (left, top, right, bottom), confidence)
            for left, top, right, bottom, text, confidence in self._raw_items(image_bytes)
        )

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        items: list[tuple[float, float, float, float, str, float]] = self._raw_items(image_bytes)
        if not items:
            return ()
        items.sort(key=lambda entry: ((entry[1] + entry[3]) / 2.0, entry[0]))
        groups: list[list[tuple[float, float, float, float, str, float]]] = []
        for entry in items:
            mid_y: float = (entry[1] + entry[3]) / 2.0
            if groups:
                prev_y: float = sum((item[1] + item[3]) / 2.0 for item in groups[-1]) / len(groups[-1])
                mean_height: float = max(3.0, sum(item[3] - item[1] for item in groups[-1]) / len(groups[-1]))
                if abs(prev_y - mid_y) <= mean_height * 0.50:
                    groups[-1].append(entry)
                    continue
            groups.append([entry])
        output: list[OCRTextRegion] = []
        for group in groups:
            rtl_chars: int = sum(
                unicodedata.bidirectional(character) in ("R", "AL")
                for item in group for character in item[4]
            )
            ltr_chars: int = sum(
                unicodedata.bidirectional(character) == "L"
                for item in group for character in item[4]
            )
            rtl: bool = rtl_chars > ltr_chars
            ordered = sorted(group, key=lambda item: item[0], reverse=rtl)
            text: str = " ".join(item[4] for item in ordered)
            weights: list[int] = [max(1, len(item[4])) for item in ordered]
            confidence: float = sum(item[5] * weight for item, weight in zip(ordered, weights)) / sum(weights)
            output.append(OCRTextRegion(
                text=text,
                bbox=(
                    min(item[0] for item in group),
                    min(item[1] for item in group),
                    max(item[2] for item in group),
                    max(item[3] for item in group),
                ),
                confidence=confidence,
            ))
        return tuple(output)

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        regions: tuple[OCRTextRegion, ...] = self.recognize_regions(image_bytes, mime_type)
        if not regions:
            return ProviderTextResult("", None, provider_name="easyocr")
        weights: list[int] = [max(1, len(region.text)) for region in regions]
        known: list[tuple[float, int]] = [
            (float(region.confidence), weight)
            for region, weight in zip(regions, weights)
            if region.confidence is not None
        ]
        confidence: float | None = (
            sum(score * weight for score, weight in known) / sum(weight for _, weight in known)
            if known else None
        )
        return ProviderTextResult(
            "\n".join(region.text for region in regions),
            confidence,
            provider_name="easyocr",
        )
