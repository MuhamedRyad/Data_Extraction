"""Optional local Tesseract language-profile selection with conflict evidence."""
from __future__ import annotations

from dataclasses import dataclass
import re

from ...shared.models import ProviderTextResult
from ..contracts import OCRTextRegion
from .tesseract import TesseractOCRProvider


def _normalized_text(value: str) -> str:
    return " ".join(value.split()).casefold()


def _numeric_signature(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\d+(?:[.,:/-]\d+)*", value))


@dataclass(frozen=True, slots=True)
class AdaptiveTesseractOCRProvider:
    languages: tuple[str, ...] = ("ara+eng", "eng")
    reconstruct_raster_tables: bool = False

    def __post_init__(self) -> None:
        if not self.languages or len(self.languages) > 4:
            raise ValueError("Adaptive Tesseract requires 1-4 language profiles")
        if any(not language.strip() for language in self.languages):
            raise ValueError("Invalid Tesseract language profile")

    def _attempts(self, image_bytes: bytes, mime_type: str) -> tuple[tuple[str, ProviderTextResult], ...]:
        output: list[tuple[str, ProviderTextResult]] = []
        for language in self.languages:
            try:
                result = TesseractOCRProvider(
                    language=language,
                    reconstruct_raster_tables=self.reconstruct_raster_tables,
                ).recognize(image_bytes, mime_type)
            except Exception:
                continue
            if result.text.strip() or result.structured_table is not None:
                output.append((language, result))
        return tuple(output)

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        attempts = self._attempts(image_bytes, mime_type)
        if not attempts:
            return ProviderTextResult("", None, provider_name="adaptive_tesseract_unavailable")
        ranked = sorted(
            attempts,
            key=lambda item: (item[1].confidence if item[1].confidence is not None else -1.0, len(item[1].text)),
            reverse=True,
        )
        language, selected = ranked[0]
        if len(ranked) == 1:
            return ProviderTextResult(
                selected.text, selected.confidence, selected.structured_table,
                provider_name=f"tesseract:{language}", model_id=selected.model_id,
            )
        normalized = {_normalized_text(result.text) for _, result in ranked}
        numeric = {_numeric_signature(result.text) for _, result in ranked}
        if len(normalized) == 1:
            provider_name = "adaptive_tesseract_consensus"
        elif len(numeric) > 1:
            provider_name = "adaptive_tesseract_numeric_conflict"
        else:
            provider_name = "adaptive_tesseract_text_conflict"
        confidence_values = [result.confidence for _, result in ranked if result.confidence is not None]
        conservative_confidence = min(confidence_values) if confidence_values else None
        return ProviderTextResult(
            selected.text,
            conservative_confidence,
            selected.structured_table,
            provider_name=provider_name,
            model_id=selected.model_id,
        )

    def _selected_language(self, image_bytes: bytes, mime_type: str) -> str:
        attempts = self._attempts(image_bytes, mime_type)
        if not attempts:
            return self.languages[0]
        return max(
            attempts,
            key=lambda item: (item[1].confidence if item[1].confidence is not None else -1.0, len(item[1].text)),
        )[0]

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        language = self._selected_language(image_bytes, mime_type)
        return TesseractOCRProvider(language=language).recognize_regions(image_bytes, mime_type)

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        language = self._selected_language(image_bytes, mime_type)
        return TesseractOCRProvider(language=language).recognize_tokens(image_bytes, mime_type)

    def recognize_cell(self, image_bytes: bytes, mime_type: str, value_kind: str) -> ProviderTextResult:
        language = self._selected_language(image_bytes, mime_type)
        return TesseractOCRProvider(language=language).recognize_cell(image_bytes, mime_type, value_kind)
