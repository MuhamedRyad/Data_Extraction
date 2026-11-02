from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

import fitz
from PIL import Image

from ..providers.contracts import OCRLayoutProvider, OCRProvider, OCRTextRegion
from ..shared.models import BoundingBox, ExtractionMethod, ExtractionResult, FileKind, RegionKind
from .errors import FieldVerificationError
from .options import FieldVerificationOptions
from .request import FieldDefinition, FieldVerificationRequest
from .response import (
    FieldCandidate,
    FieldVerificationResult,
    FieldVerificationStatus,
    VerifiedField,
)
from .validators import validate_request


_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
_DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
_LETTER_TO_DIGIT = {
    "O": "0", "Q": "0", "I": "1", "L": "1", "Z": "2",
    "S": "5", "G": "6", "B": "8",
}


@dataclass(slots=True)
class _VerificationBudget:
    field_ocr_calls: int = 0


@dataclass(frozen=True, slots=True)
class _LocatedText:
    page_number: int
    text: str
    bbox: BoundingBox | None
    confidence: float | None
    source: str
    coordinate_space: str


class FieldVerificationService:
    """Independent, non-destructive verification for sensitive field values.

    Source extraction is never modified. A field crop is re-read by one or more
    OCR providers and the alternatives are retained. A schema/template proposal
    is accepted as verified only when independent evidence reaches consensus.
    """

    def __init__(
        self,
        ocr_providers: Iterable[OCRProvider] = (),
        *,
        layout_provider: OCRLayoutProvider | None = None,
        options: FieldVerificationOptions | None = None,
    ) -> None:
        self._providers: tuple[OCRProvider, ...] = tuple(ocr_providers)
        self._layout_provider: OCRLayoutProvider | None = layout_provider
        self._options: FieldVerificationOptions = options or FieldVerificationOptions()

    def verify(
        self,
        request: FieldVerificationRequest,
        extraction: ExtractionResult,
    ) -> FieldVerificationResult:
        validate_request(request, self._options)
        source_regions: dict[int, tuple[_LocatedText, ...]] = self._source_regions(extraction)
        diagnostics: list[str] = []

        if extraction.manifest.file_kind in (FileKind.PDF, FileKind.IMAGE):
            missing_label_pages: set[int] = self._pages_needing_layout_ocr(
                request.fields, source_regions
            )
            if missing_label_pages and self._layout_provider is not None:
                for page_number in sorted(missing_label_pages):
                    try:
                        raster: bytes = self._render_page(request, extraction, page_number)
                        layout_regions: tuple[OCRTextRegion, ...] = self._layout_provider.recognize_regions(
                            raster, "image/png"
                        )
                    except Exception:
                        diagnostics.append(f"field_layout_ocr_failed:p{page_number}")
                        continue
                    source_regions[page_number] = source_regions.get(page_number, ()) + tuple(
                        _LocatedText(
                            page_number=page_number,
                            text=region.text,
                            bbox=region.bbox,
                            confidence=region.confidence,
                            source="layout_ocr",
                            coordinate_space="image",
                        )
                        for region in layout_regions
                        if region.text.strip()
                    )

        verified: list[VerifiedField] = []
        budget = _VerificationBudget()
        total_occurrences: int = 0
        for definition in request.fields:
            if definition.normalized_value_bbox is not None and definition.page_number is not None:
                verified.append(self._verify_direct_bbox(
                    request, extraction, definition, diagnostics, budget
                ))
                continue
            labels = self._find_labels(definition, source_regions)
            if not labels:
                verified.append(VerifiedField(
                    field_name=definition.name,
                    page_number=None,
                    status=FieldVerificationStatus.LABEL_NOT_FOUND,
                    label_text=None,
                    original_value=None,
                    suggested_value=None,
                    confidence=None,
                    label_bbox=None,
                    value_bbox=None,
                    candidates=(),
                    review_reasons=("field_label_not_found",),
                ))
                continue
            total_occurrences += len(labels)
            if total_occurrences > self._options.max_field_occurrences:
                raise FieldVerificationError(
                    "FIELD_OCCURRENCE_LIMIT_EXCEEDED",
                    "Field occurrence count exceeds configured limit",
                )
            for label in labels:
                verified.append(self._verify_occurrence(
                    request, extraction, definition, label,
                    source_regions.get(label.page_number, ()), diagnostics, budget,
                ))

        requires_review: bool = any(
            item.status not in (
                FieldVerificationStatus.VERIFIED_SOURCE,
                FieldVerificationStatus.VERIFIED_CONSENSUS,
            )
            for item in verified
        )
        return FieldVerificationResult(
            fields=tuple(verified),
            requires_review=requires_review,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )

    @staticmethod
    def _source_regions(extraction: ExtractionResult) -> dict[int, tuple[_LocatedText, ...]]:
        pages: dict[int, tuple[_LocatedText, ...]] = {}
        for page in extraction.pages:
            regions: list[_LocatedText] = []
            for region in page.regions:
                if region.kind != RegionKind.TEXT or not (region.text or "").strip():
                    continue
                # Raster OCR usually represents a whole image/page as one region;
                # it is useful content evidence but not field geometry. Field-level
                # verification obtains word/line geometry from OCRLayoutProvider.
                if extraction.manifest.file_kind in (FileKind.PDF, FileKind.IMAGE) \
                        and region.method != ExtractionMethod.NATIVE:
                    continue
                coordinate_space: str = "pdf" if extraction.manifest.file_kind == FileKind.PDF else "logical"
                regions.append(_LocatedText(
                    page_number=page.number,
                    text=region.text or "",
                    bbox=region.bbox,
                    confidence=region.confidence,
                    source=f"extraction:{region.method.value}",
                    coordinate_space=coordinate_space,
                ))
            pages[page.number] = tuple(regions)
        return pages

    def _pages_needing_layout_ocr(
        self,
        definitions: tuple[FieldDefinition, ...],
        pages: dict[int, tuple[_LocatedText, ...]],
    ) -> set[int]:
        output: set[int] = set()
        for page_number, regions in pages.items():
            for definition in definitions:
                if definition.normalized_value_bbox is not None and (definition.page_number is None or definition.page_number == page_number):
                    continue
                if not any(self._matches_any_label(region.text, definition.labels) for region in regions):
                    output.add(page_number)
                    break
        return output

    def _find_labels(
        self,
        definition: FieldDefinition,
        pages: dict[int, tuple[_LocatedText, ...]],
    ) -> tuple[_LocatedText, ...]:
        found: list[_LocatedText] = []
        seen: set[tuple[int, str, BoundingBox | None]] = set()
        for page_number in sorted(pages):
            for region in pages[page_number]:
                if not self._matches_any_label(region.text, definition.labels):
                    continue
                key = (region.page_number, self._label_key(region.text), None)
                if key not in seen:
                    seen.add(key)
                    found.append(region)
        return tuple(found)

    def _verify_direct_bbox(
        self,
        request: FieldVerificationRequest,
        extraction: ExtractionResult,
        definition: FieldDefinition,
        diagnostics: list[str],
        budget: _VerificationBudget,
    ) -> VerifiedField:
        page_number = definition.page_number
        assert page_number is not None
        page = next((item for item in extraction.pages if item.number == page_number), None)
        if page is None or page.width is None or page.height is None:
            return VerifiedField(
                definition.name, page_number, FieldVerificationStatus.VALUE_REGION_NOT_FOUND,
                None, None, None, None, None, None, (), ("template_field_page_geometry_unavailable",),
            )
        normalized = definition.normalized_value_bbox
        assert normalized is not None
        value_bbox: BoundingBox = (
            normalized[0] * page.width, normalized[1] * page.height,
            normalized[2] * page.width, normalized[3] * page.height,
        )
        label_bbox: BoundingBox | None = None
        if definition.normalized_label_bbox is not None:
            source = definition.normalized_label_bbox
            label_bbox = (source[0] * page.width, source[1] * page.height, source[2] * page.width, source[3] * page.height)
        try:
            crop = self._crop_value(
                request, extraction, page_number, value_bbox,
                "pdf" if extraction.manifest.file_kind == FileKind.PDF else "image",
            )
        except Exception:
            diagnostics.append(f"template_field_crop_failed:{definition.name}:p{page_number}")
            return VerifiedField(
                definition.name, page_number, FieldVerificationStatus.VALUE_REGION_NOT_FOUND,
                None, None, None, None, label_bbox, value_bbox, (), ("template_field_crop_failed",),
            )
        candidates: list[FieldCandidate] = []
        retry_crop: bytes | None = None
        for index, provider in enumerate(self._providers, start=1):
            if budget.field_ocr_calls >= self._options.max_field_ocr_calls:
                diagnostics.append("field_ocr_budget_exhausted")
                break
            budget.field_ocr_calls += 1
            try:
                result = provider.recognize(crop, "image/png")
            except Exception:
                diagnostics.append(f"template_field_ocr_failed:{definition.name}:p{page_number}:attempt{index}")
                continue
            provider_name = result.provider_name or type(provider).__name__
            attempt_candidates: list[FieldCandidate] = []
            value = self._normalize_value(result.text, definition)
            if value and len(value) <= self._options.max_candidate_chars:
                attempt_candidates.append(self._candidate(
                    value, result.confidence, "template_field_crop", provider_name, definition, transformed=False
                ))
                transformed = self._template_transform(value, definition.template)
                if transformed is not None and transformed != value:
                    attempt_candidates.append(self._candidate(
                        transformed, result.confidence, "template_field_crop+template_transform",
                        provider_name, definition, transformed=True
                    ))
            candidates.extend(attempt_candidates)

            schema_retry_text_safe: bool = not value or not any(character.isalpha() for character in value)
            if self._options.enable_schema_retry_upscale \
                    and schema_retry_text_safe \
                    and not any(item.schema_valid for item in attempt_candidates) \
                    and budget.field_ocr_calls < self._options.max_field_ocr_calls:
                if retry_crop is None:
                    retry_crop = self._upscale_crop_for_schema_retry(crop)
                if retry_crop is not None:
                    budget.field_ocr_calls += 1
                    try:
                        retry_result = provider.recognize(retry_crop, "image/png")
                    except Exception:
                        diagnostics.append(
                            f"template_field_schema_retry_failed:{definition.name}:p{page_number}:attempt{index}"
                        )
                    else:
                        retry_value = self._normalize_value(retry_result.text, definition)
                        retry_provider_name = retry_result.provider_name or provider_name
                        if retry_value and len(retry_value) <= self._options.max_candidate_chars:
                            candidates.append(self._candidate(
                                retry_value, retry_result.confidence,
                                "template_field_crop+schema_retry_upscale",
                                retry_provider_name, definition, transformed=False
                            ))
                            retry_transformed = self._template_transform(retry_value, definition.template)
                            if retry_transformed is not None and retry_transformed != retry_value:
                                candidates.append(self._candidate(
                                    retry_transformed, retry_result.confidence,
                                    "template_field_crop+schema_retry_upscale+template_transform",
                                    retry_provider_name, definition, transformed=True
                                ))
                        diagnostics.append(
                            f"template_field_schema_retry_upscaled:{definition.name}:p{page_number}"
                        )
        candidates = self._deduplicate_candidates(candidates)[:self._options.max_candidates_per_field]
        valid = tuple(item for item in candidates if item.schema_valid)
        grouped: dict[str, list[FieldCandidate]] = {}
        for candidate in valid:
            grouped.setdefault(candidate.value, []).append(candidate)
        consensus: list[tuple[str, tuple[FieldCandidate, ...]]] = []
        for value, occurrences in grouped.items():
            independent = {(item.provider_name or item.source) for item in occurrences if not item.transformed}
            if len(independent) >= self._options.consensus_count:
                consensus.append((value, tuple(occurrences)))
        if len(consensus) == 1:
            value, evidence = consensus[0]
            confidence_values = [float(item.confidence) for item in evidence if item.confidence is not None]
            confidence = max(confidence_values) if confidence_values else None
            if confidence is None or confidence >= definition.minimum_confidence:
                return VerifiedField(
                    definition.name, page_number, FieldVerificationStatus.VERIFIED_CONSENSUS,
                    None, None, value, confidence, label_bbox, value_bbox, tuple(candidates), (),
                )
        reasons: list[str] = ["template_direct_crop_requires_independent_consensus"]
        if not self._providers:
            reasons.append("field_ocr_provider_not_configured")
        if not valid:
            reasons.append("no_schema_valid_candidate")
        elif len(grouped) > 1:
            reasons.append("conflicting_schema_valid_candidates")
        else:
            reasons.append("insufficient_independent_consensus")
        suggested = max(
            valid, key=lambda item: (item.confidence if item.confidence is not None else -1.0, not item.transformed)
        ).value if valid else None
        status = FieldVerificationStatus.PROVIDER_UNAVAILABLE if not self._providers else FieldVerificationStatus.REVIEW_REQUIRED
        return VerifiedField(
            definition.name, page_number, status, None, None, suggested,
            max((item.confidence for item in valid if item.confidence is not None), default=None),
            label_bbox, value_bbox, tuple(candidates), tuple(dict.fromkeys(reasons)),
        )

    def _verify_occurrence(
        self,
        request: FieldVerificationRequest,
        extraction: ExtractionResult,
        definition: FieldDefinition,
        label: _LocatedText,
        page_regions: tuple[_LocatedText, ...],
        diagnostics: list[str],
        budget: _VerificationBudget,
    ) -> VerifiedField:
        inline_value: str | None = self._inline_value(label.text, definition.labels)
        value_region: _LocatedText | None = None
        if inline_value:
            value_region = label
        else:
            value_region = self._locate_value_region(definition, label, page_regions)
        if value_region is None:
            return VerifiedField(
                definition.name, label.page_number,
                FieldVerificationStatus.VALUE_REGION_NOT_FOUND,
                label.text, None, None, None, label.bbox, None, (),
                ("field_value_region_not_found",),
            )

        original_value: str = self._normalize_value(
            inline_value if inline_value is not None else value_region.text,
            definition,
        )
        if len(original_value) > self._options.max_candidate_chars:
            diagnostics.append(f"field_source_value_too_long:{definition.name}:p{label.page_number}")
            original_value = ""
        candidates: list[FieldCandidate] = []
        if original_value:
            candidates.append(self._candidate(
                original_value,
                value_region.confidence,
                value_region.source,
                None,
                definition,
                transformed=False,
            ))

        crop: bytes | None = None
        if value_region.bbox is not None and extraction.manifest.file_kind in (FileKind.PDF, FileKind.IMAGE):
            try:
                crop = self._crop_value(
                    request, extraction, label.page_number,
                    value_region.bbox, value_region.coordinate_space,
                )
            except Exception:
                diagnostics.append(f"field_crop_failed:{definition.name}:p{label.page_number}")

        if crop is not None:
            for index, provider in enumerate(self._providers, start=1):
                if budget.field_ocr_calls >= self._options.max_field_ocr_calls:
                    diagnostics.append("field_ocr_budget_exhausted")
                    break
                budget.field_ocr_calls += 1
                try:
                    result = provider.recognize(crop, "image/png")
                except Exception:
                    diagnostics.append(
                        f"field_ocr_failed:{definition.name}:p{label.page_number}:attempt{index}"
                    )
                    continue
                value: str = self._normalize_value(result.text, definition)
                if not value or len(value) > self._options.max_candidate_chars:
                    if len(value) > self._options.max_candidate_chars:
                        diagnostics.append(
                            f"field_ocr_value_too_long:{definition.name}:p{label.page_number}:attempt{index}"
                        )
                    continue
                provider_name: str = result.provider_name or type(provider).__name__
                candidates.append(self._candidate(
                    value, result.confidence, "field_ocr", provider_name,
                    definition, transformed=False,
                ))
                transformed: str | None = self._template_transform(value, definition.template)
                if transformed is not None and transformed != value:
                    candidates.append(self._candidate(
                        transformed,
                        result.confidence,
                        "field_ocr+template_transform",
                        provider_name,
                        definition,
                        transformed=True,
                    ))

        candidates = self._deduplicate_candidates(candidates)[:self._options.max_candidates_per_field]
        valid: tuple[FieldCandidate, ...] = tuple(item for item in candidates if item.schema_valid)
        source_valid: bool = bool(
            candidates
            and candidates[0].source == "extraction:native"
            and candidates[0].schema_valid
        )

        competing_high_confidence: bool = any(
            item.value != original_value
            and item.schema_valid
            and item.confidence is not None
            and item.confidence >= definition.minimum_confidence
            and not item.transformed
            for item in candidates
        )
        if source_valid and not competing_high_confidence:
            return VerifiedField(
                definition.name, label.page_number,
                FieldVerificationStatus.VERIFIED_SOURCE,
                label.text, original_value, original_value,
                candidates[0].confidence, label.bbox, value_region.bbox,
                tuple(candidates), (),
            )

        grouped: dict[str, list[FieldCandidate]] = {}
        for candidate in valid:
            grouped.setdefault(candidate.value, []).append(candidate)
        consensus: list[tuple[str, tuple[FieldCandidate, ...]]] = []
        for value, occurrences in grouped.items():
            independent_sources: set[str] = {
                (item.provider_name or item.source)
                for item in occurrences
                if not item.transformed
            }
            # A deterministic transform can support a value but never counts as
            # an independent observation by itself.
            if len(independent_sources) >= self._options.consensus_count:
                consensus.append((value, tuple(occurrences)))
        if len(consensus) == 1:
            value, evidence = consensus[0]
            confidence_values: list[float] = [
                float(item.confidence) for item in evidence if item.confidence is not None
            ]
            confidence: float | None = max(confidence_values) if confidence_values else None
            if confidence is None or confidence >= definition.minimum_confidence:
                return VerifiedField(
                    definition.name, label.page_number,
                    FieldVerificationStatus.VERIFIED_CONSENSUS,
                    label.text, original_value or None, value, confidence,
                    label.bbox, value_region.bbox, tuple(candidates), (),
                )

        reasons: list[str] = []
        if not self._providers:
            reasons.append("field_ocr_provider_not_configured")
        if not valid:
            reasons.append("no_schema_valid_candidate")
        elif len(grouped) > 1:
            reasons.append("conflicting_schema_valid_candidates")
        else:
            reasons.append("insufficient_independent_consensus")
        if any(item.transformed for item in valid):
            reasons.append("template_transform_requires_independent_evidence")
        status: FieldVerificationStatus = (
            FieldVerificationStatus.PROVIDER_UNAVAILABLE
            if not self._providers and not source_valid
            else FieldVerificationStatus.REVIEW_REQUIRED
        )
        suggested: str | None = None
        if valid:
            suggested = max(
                valid,
                key=lambda item: (item.confidence if item.confidence is not None else -1.0, not item.transformed),
            ).value
        return VerifiedField(
            definition.name, label.page_number, status,
            label.text, original_value or None, suggested,
            max((item.confidence for item in valid if item.confidence is not None), default=None),
            label.bbox, value_region.bbox, tuple(candidates), tuple(dict.fromkeys(reasons)),
        )

    def _locate_value_region(
        self,
        definition: FieldDefinition,
        label: _LocatedText,
        regions: tuple[_LocatedText, ...],
    ) -> _LocatedText | None:
        if label.bbox is None:
            try:
                index: int = regions.index(label)
            except ValueError:
                return None
            for candidate in regions[index + 1:index + 4]:
                if not self._matches_any_known_label(candidate.text, definition.labels):
                    return candidate
            return None
        candidates: list[_LocatedText] = []
        for candidate in regions:
            if candidate is label or candidate.bbox is None:
                continue
            if self._matches_any_known_label(candidate.text, definition.labels):
                continue
            vertical: float = self._vertical_distance(label.bbox, candidate.bbox)
            if vertical > self._options.max_vertical_distance:
                continue
            candidates.append(candidate)
        if not candidates:
            return None
        return min(candidates, key=lambda candidate: self._geometry_score(label, candidate))

    def _geometry_score(
        self, label: _LocatedText, value: _LocatedText,
    ) -> tuple[int, int, float, float]:
        if label.bbox is None or value.bbox is None:
            return (3, 2, math.inf, math.inf)
        overlap: float = self._vertical_overlap(label.bbox, value.bbox)
        same_band: bool = overlap >= self._options.minimum_vertical_overlap
        label_is_rtl: bool = self._is_rtl_text(label.text)
        expected_side: bool = (
            value.bbox[2] <= label.bbox[2]
            if label_is_rtl
            else value.bbox[0] >= label.bbox[0]
        )
        horizontal_gap: float = min(
            abs(value.bbox[0] - label.bbox[2]),
            abs(label.bbox[0] - value.bbox[2]),
        )
        vertical_gap: float = self._vertical_distance(label.bbox, value.bbox)
        return (0 if same_band else 1, 0 if expected_side else 1, horizontal_gap, vertical_gap)

    @staticmethod
    def _vertical_overlap(first: BoundingBox, second: BoundingBox) -> float:
        overlap: float = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
        height: float = min(first[3] - first[1], second[3] - second[1])
        return overlap / height if height > 0 else 0.0

    @staticmethod
    def _vertical_distance(first: BoundingBox, second: BoundingBox) -> float:
        first_center: float = (first[1] + first[3]) / 2.0
        second_center: float = (second[1] + second[3]) / 2.0
        return abs(first_center - second_center)

    def _crop_value(
        self,
        request: FieldVerificationRequest,
        extraction: ExtractionResult,
        page_number: int,
        bbox: BoundingBox,
        coordinate_space: str,
    ) -> bytes:
        if extraction.manifest.file_kind == FileKind.PDF:
            with fitz.open(stream=request.document.content, filetype="pdf") as pdf:
                page = pdf[page_number - 1]
                if coordinate_space == "image":
                    page_result = next(page_item for page_item in extraction.pages if page_item.number == page_number)
                    width: float = page_result.width or float(page.rect.width)
                    scale_from_image: float = width / max(1.0, self._render_width(page))
                    pdf_bbox: BoundingBox = tuple(float(value) * scale_from_image for value in bbox)  # type: ignore[assignment]
                else:
                    pdf_bbox = bbox
                clip = fitz.Rect(
                    pdf_bbox[0] - self._options.crop_padding_x,
                    pdf_bbox[1] - self._options.crop_padding_y,
                    pdf_bbox[2] + self._options.crop_padding_x,
                    pdf_bbox[3] + self._options.crop_padding_y,
                ) & page.rect
                scale: float = self._options.render_dpi / 72.0
                pixels: float = clip.width * scale * clip.height * scale
                if pixels > self._options.max_crop_pixels:
                    raise FieldVerificationError(
                        "FIELD_CROP_TOO_LARGE", "Field crop exceeds configured pixel budget"
                    )
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
                return pixmap.tobytes("png")
        if extraction.manifest.file_kind == FileKind.IMAGE:
            with Image.open(io.BytesIO(request.document.content)) as image:
                frame = image.convert("RGB")
                crop_box = (
                    max(0, int(math.floor(bbox[0] - self._options.crop_padding_x))),
                    max(0, int(math.floor(bbox[1] - self._options.crop_padding_y))),
                    min(frame.width, int(math.ceil(bbox[2] + self._options.crop_padding_x))),
                    min(frame.height, int(math.ceil(bbox[3] + self._options.crop_padding_y))),
                )
                if max(0, crop_box[2] - crop_box[0]) * max(0, crop_box[3] - crop_box[1]) \
                        > self._options.max_crop_pixels:
                    raise FieldVerificationError(
                        "FIELD_CROP_TOO_LARGE", "Field crop exceeds configured pixel budget"
                    )
                crop = frame.crop(crop_box)
                buffer = io.BytesIO()
                crop.save(buffer, format="PNG")
                return buffer.getvalue()
        raise ValueError("field crop is only available for PDF and image sources")

    def _upscale_crop_for_schema_retry(self, crop: bytes) -> bytes | None:
        try:
            with Image.open(io.BytesIO(crop)) as image:
                frame = image.convert("RGB")
                factor: int = self._options.schema_retry_upscale_factor
                source_pixels: int = frame.width * frame.height
                if source_pixels <= 0:
                    return None
                while factor >= 2 and source_pixels * factor * factor > self._options.max_crop_pixels:
                    factor -= 1
                if factor < 2:
                    return None
                enlarged = frame.resize(
                    (frame.width * factor, frame.height * factor), Image.Resampling.LANCZOS
                )
                output = io.BytesIO()
                enlarged.save(output, format="PNG")
                return output.getvalue()
        except Exception:
            return None

    def _render_page(
        self,
        request: FieldVerificationRequest,
        extraction: ExtractionResult,
        page_number: int,
    ) -> bytes:
        if extraction.manifest.file_kind == FileKind.PDF:
            with fitz.open(stream=request.document.content, filetype="pdf") as pdf:
                page = pdf[page_number - 1]
                scale: float = self._options.render_dpi / 72.0
                pixels: float = page.rect.width * scale * page.rect.height * scale
                if pixels > self._options.max_page_pixels:
                    raise FieldVerificationError(
                        "FIELD_PAGE_TOO_LARGE", "Field verification page exceeds pixel budget"
                    )
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                return pixmap.tobytes("png")
        if extraction.manifest.file_kind == FileKind.IMAGE:
            with Image.open(io.BytesIO(request.document.content)) as image:
                if image.width * image.height > self._options.max_page_pixels:
                    raise FieldVerificationError(
                        "FIELD_PAGE_TOO_LARGE", "Field verification image exceeds pixel budget"
                    )
                buffer = io.BytesIO()
                image.convert("RGB").save(buffer, format="PNG")
                return buffer.getvalue()
        raise ValueError("layout OCR requires a rasterizable source")

    def _render_width(self, page: fitz.Page) -> float:
        return float(page.rect.width * self._options.render_dpi / 72.0)

    @classmethod
    def _candidate(
        cls,
        value: str,
        confidence: float | None,
        source: str,
        provider_name: str | None,
        definition: FieldDefinition,
        *,
        transformed: bool,
    ) -> FieldCandidate:
        valid: bool = re.fullmatch(definition.pattern, value) is not None
        normalized_confidence: float | None = None
        if confidence is not None and isinstance(confidence, (int, float)) and math.isfinite(float(confidence)):
            normalized_confidence = min(1.0, max(0.0, float(confidence)))
        return FieldCandidate(
            value=value,
            confidence=normalized_confidence,
            source=source,
            provider_name=provider_name,
            schema_valid=valid,
            transformed=transformed,
        )

    @staticmethod
    def _deduplicate_candidates(candidates: list[FieldCandidate]) -> list[FieldCandidate]:
        unique: dict[tuple[str, str | None, str, bool], FieldCandidate] = {}
        for candidate in candidates:
            key = (candidate.value, candidate.provider_name, candidate.source, candidate.transformed)
            existing = unique.get(key)
            if existing is None or (candidate.confidence or -1.0) > (existing.confidence or -1.0):
                unique[key] = candidate
        return list(unique.values())

    @staticmethod
    def _normalize_value(value: str, definition: FieldDefinition) -> str:
        normalized: str = unicodedata.normalize("NFKC", value)
        if definition.normalize_arabic_digits:
            normalized = normalized.translate(_ARABIC_DIGITS)
        normalized = " ".join(normalized.strip().split())
        if definition.compact_punctuation:
            normalized = re.sub(r"\s*([@./:,\-+٫])\s*", r"\1", normalized)
        return normalized.strip(" :؛")

    @classmethod
    def _template_transform(cls, value: str, template: str | None) -> str | None:
        if template is None or len(value) != len(template):
            return None
        output: list[str] = []
        for character, slot in zip(value, template, strict=True):
            if slot == "9":
                if character.isdecimal():
                    output.append(character)
                elif character.upper() in _LETTER_TO_DIGIT:
                    output.append(_LETTER_TO_DIGIT[character.upper()])
                else:
                    return None
            elif slot == "A":
                if "A" <= character.upper() <= "Z":
                    output.append(character.upper())
                elif character in _DIGIT_TO_LETTER:
                    output.append(_DIGIT_TO_LETTER[character])
                else:
                    return None
            elif slot == "a":
                if "a" <= character.lower() <= "z":
                    output.append(character.lower())
                elif character in _DIGIT_TO_LETTER:
                    output.append(_DIGIT_TO_LETTER[character].lower())
                else:
                    return None
            elif slot == "X":
                if character.isalnum():
                    output.append(character)
                else:
                    return None
            else:
                if character == slot:
                    output.append(slot)
                elif cls._literal_confusion(character, slot):
                    output.append(slot)
                else:
                    return None
        return "".join(output)

    @staticmethod
    def _literal_confusion(character: str, literal: str) -> bool:
        if literal.upper() in _LETTER_TO_DIGIT:
            return _LETTER_TO_DIGIT[literal.upper()] == character
        if character.upper() in _LETTER_TO_DIGIT and literal.isdecimal():
            return _LETTER_TO_DIGIT[character.upper()] == literal
        if character in _DIGIT_TO_LETTER and literal.upper() == _DIGIT_TO_LETTER[character]:
            return True
        return False

    @classmethod
    def _matches_any_label(cls, text: str, labels: tuple[str, ...]) -> bool:
        normalized: str = cls._label_key(text)
        for label in labels:
            key: str = cls._label_key(label)
            if normalized == key or normalized.startswith(f"{key} "):
                return True
            if normalized.endswith(f" {key}"):
                prefix: str = normalized[:-len(key)].strip()
                if any(character.isdigit() or (character.isascii() and character.isalpha())
                       for character in prefix) or "@" in text:
                    return True
        return False

    @classmethod
    def _matches_any_known_label(cls, text: str, labels: tuple[str, ...]) -> bool:
        return cls._matches_any_label(text, labels)

    @staticmethod
    def _is_rtl_text(text: str) -> bool:
        rtl: int = sum(
            unicodedata.bidirectional(character) in {"R", "AL"}
            for character in text
        )
        ltr: int = sum(
            unicodedata.bidirectional(character) == "L"
            for character in text
        )
        return rtl > ltr

    @staticmethod
    def _label_key(text: str) -> str:
        normalized: str = unicodedata.normalize("NFKC", text).casefold()
        normalized = re.sub(r"[\s:：|]+", " ", normalized)
        return normalized.strip()

    @classmethod
    def _inline_value(cls, text: str, labels: tuple[str, ...]) -> str | None:
        for label in sorted(labels, key=len, reverse=True):
            match = re.search(re.escape(label), text, flags=re.IGNORECASE)
            if match is None:
                continue
            remainder: str = text[match.end():].strip(" \t\r\n:：-|–—")
            if remainder:
                return remainder
        return None
