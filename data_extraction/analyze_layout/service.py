"""Optional image-based layout provider, fail-closed and auditable.

Inspect remains offline and model-free. Only extract() invokes this action.
Predictions may ONLY reorder already-discovered regions, never replace text.
"""
from __future__ import annotations

from dataclasses import replace
import math

import fitz

from .contracts import LayoutOrderProvider, LayoutOrderPrediction, LayoutPageRequest, LayoutRegion
from ..shared.models import DocumentInput, FileKind, InspectedDocument, PageCandidates, PageKind
from ..options import ExtractionOptions


def _needs_layout_fallback(page: PageCandidates) -> bool:
    """Model fallback gate based only on explicit deterministic evidence."""
    warnings: set[str] = set(page.warnings)
    if warnings & {
        "reading_order_complex_columns_review_required",
        "reading_order_diagram_hierarchy_review_required",
        "page_semantic_diagram_hierarchy",
        "page_semantic_mixed_layout",
    }:
        return True
    return False


def _render_layout_page(
    page: fitz.Page,
    options: ExtractionOptions,
) -> tuple[bytes, bool]:
    """Render a layout input in the same unrotated coordinate space as regions."""
    rotation: int = page.rotation
    cropbox: fitz.Rect = fitz.Rect(page.cropbox)
    try:
        # Inspect stores PDF geometry in source/unrotated coordinates. The
        # layout model must receive pixels with the same orientation, otherwise
        # its returned order is grounded against a different coordinate space.
        if rotation:
            page.set_rotation(0)
        page_rect: fitz.Rect = page.rect
        requested_scale: float = options.layout_render_dpi / 72.0
        scale: float = requested_scale
        downscaled: bool = False
        pixels: float = page_rect.width * page_rect.height * scale * scale
        if pixels > options.max_image_pixels:
            if not options.adaptive_image_recovery:
                raise ValueError("layout_render_pixel_budget_exceeded")
            maximum_scale: float = (
                options.max_image_pixels / max(1.0, page_rect.width * page_rect.height)
            ) ** 0.5
            minimum_scale: float = options.min_image_dpi / 72.0
            if maximum_scale < minimum_scale:
                raise ValueError("layout_render_pixel_budget_exceeded")
            scale = min(requested_scale, max(minimum_scale, maximum_scale * 0.995))
            downscaled = scale < requested_scale

        minimum_scale = options.min_image_dpi / 72.0
        pixmap: fitz.Pixmap | None = None
        for attempt in range(4):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            if pixmap.width * pixmap.height <= options.max_image_pixels:
                break
            if not options.adaptive_image_recovery or scale <= minimum_scale or attempt == 3:
                raise ValueError("layout_render_pixel_budget_exceeded")
            actual_pixels: int = pixmap.width * pixmap.height
            next_scale: float = scale * (
                options.max_image_pixels / max(1, actual_pixels)
            ) ** 0.5 * 0.995
            if attempt == 2:
                next_scale = minimum_scale
            next_scale = max(minimum_scale, next_scale)
            if next_scale >= scale:
                raise ValueError("layout_render_pixel_budget_exceeded")
            del pixmap
            pixmap = None
            scale = next_scale
            downscaled = True
        if pixmap is None:
            raise ValueError("layout_render_failed")
        return pixmap.tobytes("png"), downscaled
    finally:
        if page.cropbox != cropbox:
            page.set_cropbox(cropbox)
        if page.rotation != rotation:
            page.set_rotation(rotation)


class AnalyzeLayoutAction:
    def __init__(self, provider: LayoutOrderProvider) -> None:
        self._provider: LayoutOrderProvider = provider

    def execute(self, source: DocumentInput, document: InspectedDocument,
                options: ExtractionOptions) -> InspectedDocument:
        if document.file_kind != FileKind.PDF:
            return document
        pages: list[PageCandidates] = []
        calls: int = 0
        with fitz.open(stream=source.content, filetype="pdf") as pdf:
            for page in document.pages:
                if page.kind != PageKind.PHYSICAL_PAGE or len(page.regions) < 2:
                    pages.append(page)
                    continue
                if options.layout_provider_fallback_only and not _needs_layout_fallback(page):
                    pages.append(replace(page, warnings=page.warnings +
                                         ("layout_provider_not_needed",)))
                    continue
                if calls >= options.max_layout_calls:
                    pages.append(replace(page, warnings=page.warnings +
                                         ("layout_provider_budget_exceeded",)))
                    continue
                calls += 1
                try:
                    pdf_page: fitz.Page = pdf[page.number - 1]
                    source_rect: fitz.Rect = pdf_page.rect * pdf_page.derotation_matrix
                    render, layout_downscaled = _render_layout_page(pdf_page, options)
                    request: LayoutPageRequest = LayoutPageRequest(
                        page.number,
                        float(page.width if page.width is not None else source_rect.width),
                        float(page.height if page.height is not None else source_rect.height),
                        render,
                        tuple(LayoutRegion(r.region_id, r.kind, r.bbox) for r in page.regions))
                    prediction: LayoutOrderPrediction = self._provider.predict_order(request)
                    self._validate(prediction, page)
                    lookup = {r.region_id: r for r in page.regions}
                    pages.append(replace(
                        page,
                        regions=tuple(lookup[region_id] for region_id in prediction.ordered_region_ids),
                        warnings=page.warnings + (
                            ("layout_render_downscaled_review",) if layout_downscaled else ()
                        ) + ("layout_provider_applied_review",),
                        layout_provider_name=prediction.provider_name,
                        layout_confidence=prediction.confidence,
                        layout_model_id=prediction.model_id,
                    ))
                except Exception:
                    # No model output may silently become an authoritative order.
                    pages.append(replace(page, warnings=page.warnings +
                                         ("layout_provider_invalid_or_failed_review",)))
        return replace(document, pages=tuple(pages))

    @staticmethod
    def _validate(prediction: LayoutOrderPrediction, page: PageCandidates) -> None:
        if not isinstance(prediction, LayoutOrderPrediction):
            raise ValueError("Invalid layout prediction type")
        if type(prediction.page_number) is not int or prediction.page_number != page.number:
            raise ValueError("Layout prediction page mismatch")
        if not isinstance(prediction.provider_name, str) or not prediction.provider_name.strip():
            raise ValueError("Missing provider identity")
        if len(prediction.provider_name) > 120:
            raise ValueError("Provider identity too long")
        if not isinstance(prediction.ordered_region_ids, tuple) or not all(
            isinstance(x, str) for x in prediction.ordered_region_ids
        ):
            raise ValueError("Invalid order contract")
        original: set[str] = {r.region_id for r in page.regions}
        supplied: set[str] = set(prediction.ordered_region_ids)
        if (original != supplied or len(supplied) != len(page.regions)
                or len(supplied) != len(prediction.ordered_region_ids)):
            raise ValueError("Layout cannot drop, duplicate, or invent regions")
        conf: float | None = prediction.confidence
        if conf is not None and (type(conf) not in (int, float)
                                or not math.isfinite(conf) or not 0 <= conf <= 1):
            raise ValueError("Invalid layout confidence")
        if prediction.model_id is not None and (not isinstance(prediction.model_id, str)
                                                or len(prediction.model_id) > 200):
            raise ValueError("Invalid model identity")
