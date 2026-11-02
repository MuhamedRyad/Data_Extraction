"""Deterministic assembly of extracted regions into stable page/document contracts.

v0.10 separates extraction completeness from human-review/publication policy.
No OCR, model inference or routing decisions are allowed in this module.
"""
from __future__ import annotations

from dataclasses import replace

from ..shared.models import (
    ExtractedRegion,
    ExtractionResult,
    ExtractionStatus,
    FileManifest,
    ReadingOrderQuality,
    PageCandidates,
    PageResult,
    RegionKind,
)
from ..analyze_layout.describe_layout import page_order_quality
from ..options import ExtractionOptions
from ..validate_extraction.service import ValidateExtractionService
from ..validate_extraction.warning_policy import blocks_publication, classify_warning, WarningSeverity
from ..structured_documents.service import StructuredDocumentAnalysisService
from ..structured_documents.options import StructuredDocumentAnalysisOptions


def _noncritical_unresolved_candidate(
    page: PageCandidates,
    region: ExtractedRegion,
    options: ExtractionOptions,
) -> bool:
    """Conservatively mark only tiny unresolved embedded graphics non-critical.

    This does not discard the region or remove review. It only prevents a tiny,
    likely decorative embedded raster from making *content completeness* false.
    Publication readiness remains strict and still sees the review flag.
    """
    if region.content_complete:
        return False
    semantic_role: str | None = region.relations.region_role if region.relations is not None else None
    if semantic_role in {"repeated_navigation_candidate", "page_header_candidate", "page_footer_candidate"}:
        return True
    if options.decorative_region_max_area_ratio <= 0:
        return False
    if page.width is None or page.height is None or region.bbox is None:
        return False
    if page.width <= 0 or page.height <= 0:
        return False
    role: str | None = region.provenance.source_image_role if region.provenance else None
    if role not in {"embedded_image", "worksheet_drawing"}:
        return False
    width: float = max(0.0, region.bbox[2] - region.bbox[0])
    height: float = max(0.0, region.bbox[3] - region.bbox[1])
    ratio: float = (width * height) / max(1.0, page.width * page.height)
    return ratio <= options.decorative_region_max_area_ratio


def _status(content_complete: bool, requires_review: bool, any_complete: bool) -> ExtractionStatus:
    if content_complete:
        return ExtractionStatus.COMPLETE_WITH_REVIEW if requires_review else ExtractionStatus.COMPLETE
    return ExtractionStatus.PARTIAL_SUCCESS if any_complete else ExtractionStatus.FAILED


def assemble_page(page: PageCandidates, extracted: list[ExtractedRegion],
                  diagnostics: list[str], options: ExtractionOptions | None = None) -> PageResult:
    """Build page metadata, preserving strict review while measuring completeness."""
    options = options or ExtractionOptions()
    normalized: list[ExtractedRegion] = []
    for item in extracted:
        if _noncritical_unresolved_candidate(page, item, options):
            item = replace(
                item,
                critical_for_completeness=False,
                recovery_notes=item.recovery_notes + (("repeated_or_furniture_noncritical",) if item.relations and item.relations.region_role in {"repeated_navigation_candidate", "page_header_candidate", "page_footer_candidate"} else ("tiny_decorative_candidate_noncritical",)),
            )
            diagnostics.append(f"{item.region_id}: unresolved noncritical repeated/decorative region retained for review")
        normalized.append(item)

    kinds: tuple[RegionKind, ...] = tuple(sorted({region.kind for region in normalized},
                                                key=lambda value: value.value))
    order_quality: ReadingOrderQuality = page_order_quality(page)
    if order_quality == ReadingOrderQuality.EXTERNAL_ESTIMATE:
        diagnostics.append(f"page{page.number}: layout_provider_applied_review")
    if "layout_provider_invalid_or_failed_review" in page.warnings:
        diagnostics.append(f"page{page.number}: layout_provider_invalid_or_failed_review")
    if "layout_provider_budget_exceeded" in page.warnings:
        diagnostics.append(f"page{page.number}: layout_provider_budget_exceeded")
    if order_quality == ReadingOrderQuality.COLUMN_HEURISTIC:
        diagnostics.append(f"page{page.number}: reading_order_columns_inferred_review")
    if order_quality == ReadingOrderQuality.AMBIGUOUS_OVERLAP:
        diagnostics.append(f"page{page.number}: reading_order_ambiguous_overlap")
    order_warnings: tuple[str, ...] = page.warnings + (
        ("reading_order_ambiguous_overlap",) if
        order_quality == ReadingOrderQuality.AMBIGUOUS_OVERLAP else ()
    )
    for warning in page.warnings:
        severity: WarningSeverity = classify_warning(warning)
        if severity != WarningSeverity.INFORMATIONAL:
            diagnostics.append(f"page{page.number}: {warning} [{severity.value}]")

    requires_review: bool = (
        any(item.requires_review for item in normalized)
        or order_quality in (
            ReadingOrderQuality.AMBIGUOUS_OVERLAP,
            ReadingOrderQuality.COLUMN_HEURISTIC,
            ReadingOrderQuality.EXTERNAL_ESTIMATE,
        )
        or blocks_publication(page.warnings)
    )
    critical: list[ExtractedRegion] = [item for item in normalized if item.critical_for_completeness]
    completed_critical: int = sum(1 for item in critical if item.content_complete)
    incomplete_warning: bool = any(
        classify_warning(warning) == WarningSeverity.INCOMPLETE for warning in page.warnings
    )
    content_complete: bool = (
        completed_critical == len(critical) and not incomplete_warning and bool(normalized)
    )
    completeness_ratio: float = (
        completed_critical / len(critical) if critical else (1.0 if normalized else 0.0)
    )
    unresolved_count: int = sum(1 for item in normalized if not item.content_complete)
    critical_unresolved_count: int = sum(
        1 for item in normalized if not item.content_complete and item.critical_for_completeness
    )
    noncritical_unresolved_count: int = unresolved_count - critical_unresolved_count
    review_count: int = sum(1 for item in normalized if item.requires_review)
    status: ExtractionStatus = _status(
        content_complete, requires_review, any(item.content_complete for item in normalized)
    )

    base: PageResult = PageResult(
        page.number, page.kind, page.label, kinds, tuple(normalized), requires_review,
        order_warnings, page.width, page.height, order_quality,
        tuple(region.region_id for region in page.regions),
        page.layout_provider_name, page.layout_confidence, page.layout_model_id,
        content_complete=content_complete,
        completeness_ratio=completeness_ratio,
        unresolved_region_count=unresolved_count,
        critical_unresolved_region_count=critical_unresolved_count,
        noncritical_unresolved_region_count=noncritical_unresolved_count,
        review_region_count=review_count,
        extraction_status=status,
        text_coverage_complete=content_complete,
        text_coverage_ratio=completeness_ratio,
    )
    if not options.enable_structured_document_analysis:
        return base
    analysis = StructuredDocumentAnalysisService(
        StructuredDocumentAnalysisOptions(
            minimum_kind_confidence=options.structured_document_min_confidence,
            minimum_suspicion_confidence=options.structured_document_suspicion_confidence,
        )
    ).analyze_page(base)
    if analysis.semantic_complete is None:
        return replace(
            base,
            structured_document_kind=analysis.kind.value,
            structured_document_confidence=analysis.kind_confidence,
            semantic_complete=None,
            semantic_status=analysis.semantic_status.value,
            field_coverage_ratio=analysis.field_coverage_ratio,
            verified_field_coverage_ratio=analysis.verified_field_coverage_ratio,
            semantic_gap_count=analysis.semantic_gap_count,
            structured_fields=analysis.fields,
        )

    # A raster receipt/invoice/form can have complete OCR text coverage while
    # still missing or misreading business-critical fields. Do not let the old
    # text-coverage meaning of `content_complete` leak into semantic automation.
    semantic_content_complete: bool = content_complete and analysis.semantic_complete
    semantic_ratio: float = min(completeness_ratio, analysis.verified_field_coverage_ratio)
    semantic_review: bool = requires_review or analysis.requires_review
    for reason in analysis.review_reasons:
        diagnostics.append(f"page{page.number}: {reason}")
    semantic_status = _status(
        semantic_content_complete, semantic_review,
        any(item.content_complete for item in normalized) or analysis.field_coverage_ratio > 0,
    )
    return replace(
        base,
        requires_review=semantic_review,
        content_complete=semantic_content_complete,
        completeness_ratio=semantic_ratio,
        extraction_status=semantic_status,
        structured_document_kind=analysis.kind.value,
        structured_document_confidence=analysis.kind_confidence,
        semantic_complete=analysis.semantic_complete,
        semantic_status=analysis.semantic_status.value,
        field_coverage_ratio=analysis.field_coverage_ratio,
        verified_field_coverage_ratio=analysis.verified_field_coverage_ratio,
        semantic_gap_count=analysis.semantic_gap_count,
        structured_fields=analysis.fields,
    )


def assemble_document(manifest: FileManifest, pages: list[PageResult],
                      vision_calls: int, diagnostics: list[str],
                      document_order: list[str]) -> ExtractionResult:
    """Finalize immutable result without changing the source reading order."""
    region_count: int = sum(len(page.regions) for page in pages)
    completed_count: int = sum(
        1 for page in pages for region in page.regions
        if region.content_complete or not region.critical_for_completeness
    )
    text_coverage_complete: bool = bool(pages) and all(page.text_coverage_complete for page in pages)
    text_coverage_ratio: float = completed_count / region_count if region_count else 0.0
    content_complete: bool = bool(pages) and all(page.content_complete for page in pages)
    semantic_pages: list[PageResult] = [page for page in pages if page.semantic_complete is not None]
    semantic_complete: bool | None = (
        all(page.semantic_complete is True for page in semantic_pages) if semantic_pages else None
    )
    field_coverage_ratio: float = (
        sum(page.field_coverage_ratio for page in semantic_pages) / len(semantic_pages)
        if semantic_pages else 1.0
    )
    verified_field_coverage_ratio: float = (
        sum(page.verified_field_coverage_ratio for page in semantic_pages) / len(semantic_pages)
        if semantic_pages else 1.0
    )
    semantic_gap_count: int = sum(page.semantic_gap_count for page in semantic_pages)
    completeness_ratio: float = min(text_coverage_ratio, verified_field_coverage_ratio) if semantic_pages else text_coverage_ratio
    unresolved_count: int = sum(page.unresolved_region_count for page in pages)
    critical_unresolved_count: int = sum(page.critical_unresolved_region_count for page in pages)
    noncritical_unresolved_count: int = sum(page.noncritical_unresolved_region_count for page in pages)
    review_count: int = sum(page.review_region_count for page in pages)
    requires_review: bool = any(page.requires_review for page in pages)
    any_complete: bool = any(region.content_complete for page in pages for region in page.regions)
    status: ExtractionStatus = _status(content_complete, requires_review, any_complete)
    return ExtractionResult(
        manifest, tuple(pages),
        ValidateExtractionService.is_ready_for_publication(pages),
        vision_calls, tuple(diagnostics), "extraction.v0.6",
        tuple(document_order),
        content_complete=content_complete,
        completeness_ratio=completeness_ratio,
        unresolved_region_count=unresolved_count,
        critical_unresolved_region_count=critical_unresolved_count,
        noncritical_unresolved_region_count=noncritical_unresolved_count,
        review_region_count=review_count,
        extraction_status=status,
        text_coverage_complete=text_coverage_complete,
        text_coverage_ratio=text_coverage_ratio,
        semantic_complete=semantic_complete,
        semantic_status=(
            "not_applicable" if not semantic_pages else
            "complete" if semantic_complete else
            "partial" if field_coverage_ratio > 0 else "unknown"
        ),
        field_coverage_ratio=field_coverage_ratio,
        verified_field_coverage_ratio=verified_field_coverage_ratio,
        semantic_gap_count=semantic_gap_count,
    )
