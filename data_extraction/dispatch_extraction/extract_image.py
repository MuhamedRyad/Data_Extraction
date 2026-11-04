"""Explicit image routing: OCR first for text, Vision first for raster table grids.

This layer returns extraction results, NOT a summary or inferred business facts.
Providers are injected, budgeted and untrusted; their outputs cannot silently
become authoritative when an image is ambiguous or contains sensitive IDs.
"""
from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable

from PIL import Image, ImageChops, ImageFilter, ImageOps

from ..providers.contracts import OCRProvider, VisionProvider
from ..shared.models import (CandidateRegion, ExtractedRegion, ExtractionMethod,
                      ImageRoute, ProviderTextResult, RegionKind, TableCells)
from ..options import ExtractionOptions
from ..validate_extraction.provider_validation import validate_provider_result, InvalidProviderResult
from ..providers.ocr.quality import assess_ocr_quality
from ..providers.ocr.table_geometry import (OCRWord, reconstruct_candidate_table,
                                               reconstruct_financial_candidate_table)
from ..structured_documents.service import looks_like_financial_document, looks_like_structured_document
from ..parallel_execution import BoundedCallGate


@dataclass(slots=True)
class VisionBudget:
    calls: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def try_reserve(self, maximum: int) -> bool:
        with self._lock:
            if self.calls >= maximum:
                return False
            self.calls += 1
            return True


class ExtractImageAction:
    def __init__(
        self,
        ocr: OCRProvider | None,
        vision: VisionProvider | None,
        *,
        ocr_gate: BoundedCallGate | None = None,
        vision_gate: BoundedCallGate | None = None,
    ) -> None:
        self._ocr: OCRProvider | None = ocr
        self._vision: VisionProvider | None = vision
        self._ocr_gate: BoundedCallGate | None = ocr_gate
        self._vision_gate: BoundedCallGate | None = vision_gate

    def _call_ocr(self, operation: Callable[..., Any], /, *args: Any) -> Any:
        if self._ocr_gate is None:
            return operation(*args)
        return self._ocr_gate.call(operation, *args)

    def _call_vision(self, operation: Callable[..., Any], /, *args: Any) -> Any:
        if self._vision_gate is None:
            return operation(*args)
        return self._vision_gate.call(operation, *args)

    def execute(self, region: CandidateRegion, options: ExtractionOptions,
                budget: VisionBudget) -> ExtractedRegion:
        if region.kind != RegionKind.IMAGE:
            raise ValueError("ExtractImageAction accepts image candidates only")
        image_bytes: bytes = region.image_bytes or b""
        mime: str = region.image_mime_type or "image/png"
        ocr_image_bytes, ocr_mime = image_bytes, mime
        if self._ocr is not None and options.image_route in (ImageRoute.AUTO, ImageRoute.OCR_ONLY):
            ocr_image_bytes, ocr_mime = self._original_image_for_ocr(region, options)
        if not image_bytes or options.image_route == ImageRoute.DISABLED:
            return self._unresolved(region, "image_extraction_disabled_or_unavailable", ())

        attempts: list[ExtractionMethod] = []
        best_ocr: ProviderTextResult | None = None
        unstructured_vision: ProviderTextResult | None = None
        ocr_failed: bool = False
        provider_invalid: bool = False
        raster_table: bool = region.content_hint == RegionKind.TABLE
        multipass_conflict: bool = False

        # A visibly bordered raster table needs a structural extractor first.
        # OCR text alone cannot guarantee cell boundaries, even with high confidence.
        provider_errors: list[str] = []
        if raster_table and options.image_route in (ImageRoute.AUTO, ImageRoute.VISION_ONLY):
            vision_table: ProviderTextResult | None = self._try_vision(
                image_bytes, mime, "raster_grid_table", options, budget, attempts, provider_errors
            )
            if vision_table is not None:
                if vision_table.structured_table is not None:
                    return self._from_provider(
                        region, ExtractionMethod.VISION_MODEL, vision_table,
                        ("model_output_requires_verification",),
                        "raster_table_vision_first", tuple(attempts), options,
                    )
                unstructured_vision = vision_table

        if options.image_route in (ImageRoute.AUTO, ImageRoute.OCR_ONLY) and self._ocr is not None:
            attempts.append(ExtractionMethod.OCR)
            try:
                candidate: ProviderTextResult = validate_provider_result(
                    self._call_ocr(self._ocr.recognize, ocr_image_bytes, ocr_mime), options)
                if candidate.provider_name in ("multi_pass_conflict", "multi_pass_no_consensus"):
                    multipass_conflict = True
                if self._has_content(candidate):
                    semantic_table_candidate: bool = (
                        options.enable_semantic_raster_table_recovery
                        and looks_like_financial_document(candidate.text)
                    )
                    structured_document_candidate: bool = (
                        options.auto_ai_for_structured_images
                        and looks_like_structured_document(candidate.text)
                    )
                    if (raster_table or semantic_table_candidate) and candidate.structured_table is None \
                            and options.enable_ocr_table_geometry_recovery:
                        candidate = self._try_ocr_table_geometry(candidate, ocr_image_bytes, ocr_mime)
                    best_ocr = candidate
                    strong: bool = self._strong_confidence(candidate.confidence, options)
                    ai_structure_needed: bool = (
                        options.image_route == ImageRoute.AUTO
                        and self._vision is not None
                        and structured_document_candidate
                        and candidate.structured_table is None
                    )
                    if (strong and not ai_structure_needed) or options.image_route == ImageRoute.OCR_ONLY:
                        reasons: tuple[str, ...] = () if strong else ("ocr_failed_quality_gate",)
                        if raster_table and candidate.structured_table is None:
                            reasons += ("raster_table_structure_not_reconstructed",)
                        elif candidate.structured_table is not None and (raster_table or semantic_table_candidate):
                            reasons += ("ocr_table_geometry_reconstructed_review",)
                        return self._from_provider(
                            region, ExtractionMethod.OCR, candidate, reasons,
                            "ocr_only" if options.image_route == ImageRoute.OCR_ONLY else "ocr_confidence_gate",
                            tuple(attempts), options,
                        )
            except InvalidProviderResult:
                provider_invalid = True
            except Exception:
                ocr_failed = True

            if best_ocr is not None and not self._strong_confidence(best_ocr.confidence, options) \
                    and options.enable_low_confidence_ocr_preprocessing \
                    and options.max_ocr_preprocess_attempts > 0 \
                    and (self._vision is None or options.image_route == ImageRoute.OCR_ONLY):
                recovered, text_changed = self._try_preprocessed_ocr(
                    ocr_image_bytes, ocr_mime, best_ocr, options
                )
                if recovered is not None and self._has_content(recovered):
                    if raster_table and recovered.structured_table is None \
                            and options.enable_ocr_table_geometry_recovery:
                        recovered = self._try_ocr_table_geometry(recovered, ocr_image_bytes, ocr_mime)
                    improved: bool = (recovered.confidence or -1.0) > (best_ocr.confidence or -1.0)
                    if improved:
                        best_ocr = recovered
                        if self._strong_confidence(recovered.confidence, options):
                            reasons = ["ocr_preprocessing_recovery_review"]
                            if text_changed:
                                reasons.append("ocr_preprocessing_text_changed_review")
                            if raster_table and recovered.structured_table is None:
                                reasons.append("raster_table_structure_not_reconstructed")
                            elif raster_table and recovered.structured_table is not None:
                                reasons.append("ocr_table_geometry_reconstructed_review")
                            return self._from_provider(
                                region, ExtractionMethod.OCR, recovered, tuple(reasons),
                                "ocr_preprocessing_recovery", tuple(attempts), options,
                                recovery_applied=True, recovery_notes=("ocr_preprocessing",),
                            )

            # v0.10: bounded tile recovery for large/low-confidence text images.
            # Tiling is review evidence, never used for raster table structure.
            if not raster_table and options.enable_ocr_tiling:
                tiled_ocr: ProviderTextResult | None = self._try_tiled_ocr(
                    ocr_image_bytes, ocr_mime, options
                )
                if tiled_ocr is not None and self._has_content(tiled_ocr):
                    tiled_strong: bool = self._strong_confidence(tiled_ocr.confidence, options)
                    if tiled_strong:
                        return self._from_provider(
                            region, ExtractionMethod.OCR, tiled_ocr,
                            ("ocr_tiled_recovery_review",),
                            "ocr_tiled_recovery", tuple(attempts), options,
                            recovery_applied=True, recovery_notes=("tiled_ocr",),
                        )
                    if best_ocr is None or (tiled_ocr.confidence or -1.0) > (best_ocr.confidence or -1.0):
                        best_ocr = tiled_ocr

        if options.image_route in (ImageRoute.AUTO, ImageRoute.VISION_ONLY) and not raster_table:
            vision_text: ProviderTextResult | None = self._try_vision(
                image_bytes, mime, region.image_role or "image", options, budget, attempts, provider_errors
            )
            if vision_text is not None:
                return self._from_provider(
                    region, ExtractionMethod.VISION_MODEL, vision_text,
                    ("model_output_requires_verification",),
                    "vision_only" if options.image_route == ImageRoute.VISION_ONLY else "ocr_insufficient_vision_fallback",
                    tuple(attempts), options,
                )

        if best_ocr is not None:
            reasons = ("ocr_low_confidence_no_verified_fallback",)
            if raster_table and best_ocr.structured_table is None:
                reasons += ("raster_table_structure_not_reconstructed",)
            return self._from_provider(
                region, ExtractionMethod.OCR, best_ocr, reasons,
                "ocr_fallback_after_failed_model", tuple(attempts), options,
            )
        if unstructured_vision is not None:
            return self._from_provider(
                region, ExtractionMethod.VISION_MODEL, unstructured_vision,
                ("model_output_requires_verification", "raster_table_structure_not_reconstructed"),
                "vision_returned_unstructured_table", tuple(attempts), options,
            )

        if options.image_route == ImageRoute.OCR_ONLY and self._ocr is None:
            reason: str = "ocr_provider_not_configured"
        elif options.image_route == ImageRoute.VISION_ONLY and self._vision is None:
            reason = "vision_provider_not_configured"
        elif raster_table and self._vision is None and self._ocr is None:
            reason = "table_image_providers_not_configured"
        elif (options.image_route != ImageRoute.OCR_ONLY and self._vision is not None
              and budget.calls >= options.max_vision_calls):
            reason = "vision_budget_exhausted"
        elif multipass_conflict:
            reason = "ocr_pass_conflict_or_incomplete_requires_review"
        elif provider_invalid or provider_errors:
            reason = "provider_invalid_output"
        elif ocr_failed:
            reason = "ocr_provider_failed"
        else:
            reason = "image_content_unresolved"
        return self._unresolved(region, reason, tuple(attempts))

    @staticmethod
    def _original_image_for_ocr(
        region: CandidateRegion, options: ExtractionOptions,
    ) -> tuple[bytes, str]:
        """Use the embedded image when its placement preserves its orientation."""
        original: bytes | None = region.original_image_bytes
        if not original or not region.original_image_ocr_safe:
            return region.image_bytes or b"", region.image_mime_type or "image/png"
        try:
            with Image.open(io.BytesIO(original)) as source:
                if source.width * source.height > options.max_image_pixels:
                    return region.image_bytes or b"", region.image_mime_type or "image/png"
                rgba: Image.Image = source.convert("RGBA")
                if region.original_image_mask_bytes:
                    with Image.open(io.BytesIO(region.original_image_mask_bytes)) as source_mask:
                        mask: Image.Image = source_mask.convert("L")
                        if mask.size != rgba.size:
                            return region.image_bytes or b"", region.image_mime_type or "image/png"
                        alpha: Image.Image = ImageChops.multiply(rgba.getchannel("A"), mask)
                        rgba.putalpha(alpha)
                background: Image.Image = Image.new("RGB", rgba.size, "white")
                background.paste(rgba, mask=rgba.getchannel("A"))
                output = io.BytesIO()
                background.save(output, format="PNG")
                return output.getvalue(), "image/png"
        except (OSError, ValueError, Image.DecompressionBombError):
            return region.image_bytes or b"", region.image_mime_type or "image/png"

    def _try_ocr_table_geometry(
        self, candidate: ProviderTextResult, image_bytes: bytes, mime: str
    ) -> ProviderTextResult:
        if self._ocr is None:
            return candidate
        recognize_tokens = getattr(self._ocr, "recognize_tokens", None)
        if not callable(recognize_tokens):
            return candidate
        try:
            tokens = self._call_ocr(recognize_tokens, image_bytes, mime)
            words: tuple[OCRWord, ...] = tuple(
                OCRWord(token.text, *token.bbox, token.confidence)
                for token in tokens if token.text.strip()
            )
            table = reconstruct_candidate_table(words)
            if table is None and looks_like_financial_document(candidate.text):
                table = reconstruct_financial_candidate_table(words)
        except Exception:
            return candidate
        if table is None:
            return candidate
        return ProviderTextResult(
            candidate.text, candidate.confidence, table,
            provider_name=candidate.provider_name, model_id=candidate.model_id,
        )

    def _try_preprocessed_ocr(
        self, image_bytes: bytes, mime: str, baseline: ProviderTextResult,
        options: ExtractionOptions,
    ) -> tuple[ProviderTextResult | None, bool]:
        if self._ocr is None:
            return None, False
        modes = ("autocontrast", "sharpen", "grayscale")[:options.max_ocr_preprocess_attempts]
        best: ProviderTextResult = baseline
        changed: bool = False
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                grayscale: Image.Image = ImageOps.grayscale(source)
                for mode in modes:
                    processed: Image.Image
                    if mode == "autocontrast":
                        processed = ImageOps.autocontrast(grayscale)
                    elif mode == "sharpen":
                        processed = grayscale.filter(ImageFilter.UnsharpMask(radius=2, percent=140))
                    else:
                        processed = grayscale
                    stream = io.BytesIO()
                    processed.save(stream, format="PNG")
                    try:
                        result = validate_provider_result(
                            self._call_ocr(self._ocr.recognize, stream.getvalue(), "image/png"), options
                        )
                    except Exception:
                        continue
                    if not self._has_content(result):
                        continue
                    if result.text.strip() != baseline.text.strip():
                        changed = True
                    if (result.confidence or -1.0) > (best.confidence or -1.0):
                        best = result
                        if self._strong_confidence(best.confidence, options):
                            break
        except Exception:
            return None, False
        return (best if best is not baseline else None), changed

    def _try_tiled_ocr(self, image_bytes: bytes, mime: str,
                       options: ExtractionOptions) -> ProviderTextResult | None:
        if self._ocr is None or options.max_ocr_tile_calls_per_region <= 0:
            return None
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                frame: Image.Image = source.convert("RGB")
                pixels: int = frame.width * frame.height
                if pixels < options.ocr_tile_trigger_pixels:
                    return None
                edge: int = max(64, int(math.sqrt(options.ocr_tile_max_pixels)))
                columns: int = max(1, math.ceil(frame.width / edge))
                rows: int = max(1, math.ceil(frame.height / edge))
                total_tiles: int = columns * rows
                if total_tiles <= 1 or total_tiles > options.max_ocr_tile_calls_per_region:
                    return None
                texts: list[str] = []
                weighted_confidence: float = 0.0
                confidence_weight: int = 0
                provider_names: list[str] = []
                model_ids: list[str] = []
                for row in range(rows):
                    for column in range(columns):
                        left: int = column * edge
                        top: int = row * edge
                        right: int = min(frame.width, (column + 1) * edge)
                        bottom: int = min(frame.height, (row + 1) * edge)
                        tile: Image.Image = frame.crop((left, top, right, bottom))
                        stream: io.BytesIO = io.BytesIO()
                        tile.save(stream, format="PNG")
                        try:
                            result: ProviderTextResult = validate_provider_result(
                                self._call_ocr(self._ocr.recognize, stream.getvalue(), "image/png"), options
                            )
                        except Exception:
                            continue
                        value: str = result.text.strip()
                        if not value:
                            continue
                        texts.append(value)
                        if result.confidence is not None:
                            weight: int = max(1, len(value))
                            weighted_confidence += float(result.confidence) * weight
                            confidence_weight += weight
                        if result.provider_name:
                            provider_names.append(result.provider_name)
                        if result.model_id:
                            model_ids.append(result.model_id)
                if not texts:
                    return None
                confidence: float | None = (
                    weighted_confidence / confidence_weight if confidence_weight else None
                )
                provider: str = "tiled_ocr"
                if provider_names and len(set(provider_names)) == 1:
                    provider = f"tiled:{provider_names[0]}"
                model: str | None = model_ids[0] if model_ids and len(set(model_ids)) == 1 else None
                return ProviderTextResult("\n".join(texts), confidence,
                                          provider_name=provider, model_id=model)
        except Exception:
            return None

    def _try_vision(self, image_bytes: bytes, mime: str, role: str,
                    options: ExtractionOptions, budget: VisionBudget,
                    attempts: list[ExtractionMethod],
                    provider_errors: list[str]) -> ProviderTextResult | None:
        if self._vision is None or not budget.try_reserve(options.max_vision_calls):
            return None
        attempts.append(ExtractionMethod.VISION_MODEL)
        try:
            candidate: ProviderTextResult = validate_provider_result(
                self._call_vision(self._vision.analyze, image_bytes, mime, role), options)
            if self._has_content(candidate):
                return candidate
        except InvalidProviderResult:
            provider_errors.append("provider_invalid_output")
        except Exception:
            # Don't include provider errors or raw content in user-visible diagnostics.
            pass
        return None

    @staticmethod
    def _has_content(response: ProviderTextResult) -> bool:
        return bool(response.text.strip()) or response.structured_table is not None

    @staticmethod
    def _strong_confidence(confidence: float | None, options: ExtractionOptions) -> bool:
        return (confidence is not None and isinstance(confidence, (float, int))
                and 0 <= confidence <= 1 and confidence >= options.ocr_confidence_threshold)

    def _from_provider(self, region: CandidateRegion, method: ExtractionMethod,
                       response: ProviderTextResult, extra_reasons: tuple[str, ...],
                       route_reason: str, attempts: tuple[ExtractionMethod, ...],
                       options: ExtractionOptions, *, recovery_applied: bool = False,
                       recovery_notes: tuple[str, ...] = ()) -> ExtractedRegion:
        table: TableCells | None = response.structured_table
        numeric: bool = bool(re.search(r"\d", response.text)) or any(
            bool(re.search(r"\d", cell or ""))
            for row in (table or []) for cell in row
        )
        reasons: list[str] = list(extra_reasons)
        if method == ExtractionMethod.OCR:
            quality = assess_ocr_quality(response, options.ocr_confidence_threshold)
            if quality.mixes_rtl_and_ltr:
                reasons.append("ocr_mixed_direction_review")
            if response.provider_name in {"adaptive_tesseract_numeric_conflict", "adaptive_tesseract_text_conflict"}:
                reasons.append("ocr_language_profile_conflict_review")
            if response.provider_name == "adaptive_tesseract_numeric_conflict":
                reasons.append("ocr_numeric_profile_conflict_review")
        if numeric:
            reasons.append("numeric_content_requires_review")
        if region.image_role == "native_text_overlay":
            reasons.append("native_overlay_requires_review")
        if region.native_text_masked:
            reasons.append("masked_native_overlay_requires_review")
        if response.confidence is not None and (not isinstance(response.confidence, (int, float))
                                                or not 0 <= response.confidence <= 1):
            reasons.append("invalid_provider_confidence")
        if table is not None:
            row_widths: set[int] = {len(row) for row in table}
            if not table or not row_widths or 0 in row_widths or len(row_widths) > 1:
                reasons.append("table_shape_requires_review")
            if sum(len(row) for row in table) > options.max_sheet_cells:
                reasons.append("table_exceeds_output_cell_limit")
            if method == ExtractionMethod.OCR:
                reasons.append("ocr_table_structure_requires_verification")
        kind: RegionKind = (RegionKind.TABLE if table is not None else
                            RegionKind.TEXT if response.text.strip() else RegionKind.IMAGE)
        actual_provider: OCRProvider | VisionProvider | None = (
            self._vision if method == ExtractionMethod.VISION_MODEL else self._ocr
        )
        provider_name: str | None = response.provider_name or (
            type(actual_provider).__name__ if actual_provider is not None else None
        )
        if region.image_role == "vector_graphic_candidate":
            recovery_applied = True
            recovery_notes = tuple(dict.fromkeys(recovery_notes + ("vector_raster_recovery",)))
        elif region.image_role == "rendered_page":
            recovery_applied = True
            recovery_notes = tuple(dict.fromkeys(recovery_notes + ("page_raster_recovery",)))
        return ExtractedRegion(
            region.region_id, kind, region.bbox, method, response.text, table,
            response.confidence, bool(reasons), tuple(dict.fromkeys(reasons)),
            route_reason, attempts, RegionKind.IMAGE,
            selected_provider_name=provider_name,
            selected_model_id=response.model_id,
            content_complete=True,
            recovery_applied=recovery_applied,
            recovery_notes=recovery_notes,
        )

    @staticmethod
    def _unresolved(region: CandidateRegion, reason: str,
                    attempts: tuple[ExtractionMethod, ...]) -> ExtractedRegion:
        return ExtractedRegion(
            region.region_id, RegionKind.IMAGE, region.bbox, ExtractionMethod.UNRESOLVED,
            None, None, None, True, (reason,), "no_verified_extraction", attempts,
            RegionKind.IMAGE, content_complete=False,
            recovery_applied=bool(attempts),
            recovery_notes=("recovery_exhausted",) if attempts else (),
        )
