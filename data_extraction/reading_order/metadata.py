"""Positions, relations, and reading-order quality for document regions."""
from __future__ import annotations

from .flow_roles import participates_in_linear_flow
from ..shared.models import (
    BoundingBox, CandidateRegion, PageCandidates, PageKind, ReadingOrderQuality,
    RegionKind, RegionPosition, RegionRelations,
)


def _intersects(a: BoundingBox | None, b: BoundingBox | None) -> bool:
    if a is None or b is None:
        return False
    overlap_width: float = min(a[2], b[2]) - max(a[0], b[0])
    overlap_height: float = min(a[3], b[3]) - max(a[1], b[1])
    if overlap_width <= 1 or overlap_height <= 1:
        return False
    smaller_area: float = min((a[2] - a[0]) * (a[3] - a[1]),
                              (b[2] - b[0]) * (b[3] - b[1]))
    # Glyph bounding boxes in consecutive Arabic/PDF text lines often overlap
    # by a few points even though the reading flow is unambiguous. Only material
    # intersection counts as a spatial relationship/ordering ambiguity.
    return smaller_area > 0 and overlap_width * overlap_height / smaller_area >= 0.40




def _reading_flow_overlaps(region: CandidateRegion, page: PageCandidates) -> tuple[CandidateRegion, ...]:
    """Material overlaps that can actually make linear reading order ambiguous.

    Physical overlap is still retained in RegionRelations. v0.11 only excludes
    explicitly classified support/background regions from *order ambiguity*.
    """
    if not participates_in_linear_flow(region):
        return ()
    output: list[CandidateRegion] = []
    for other in page.regions:
        if other.region_id == region.region_id or not participates_in_linear_flow(other):
            continue
        if not _intersects(region.bbox, other.bbox):
            continue
        # Stacked/nested raster layers are physical evidence but do not define
        # the order in which text is read. Keep them in RegionRelations only.
        if region.kind == RegionKind.IMAGE and other.kind == RegionKind.IMAGE:
            continue
        output.append(other)
    return tuple(output)

def _normalized(region: CandidateRegion, page: PageCandidates) -> BoundingBox | None:
    if region.bbox is None or not page.width or not page.height:
        return None
    x0: float; y0: float; x1: float; y1: float
    x0, y0, x1, y1 = region.bbox
    return (round(max(0.0, min(1.0, x0 / page.width)), 6),
            round(max(0.0, min(1.0, y0 / page.height)), 6),
            round(max(0.0, min(1.0, x1 / page.width)), 6),
            round(max(0.0, min(1.0, y1 / page.height)), 6))


def _relations(region: CandidateRegion, page: PageCandidates) -> RegionRelations:
    touching: tuple[CandidateRegion, ...] = tuple(
        other for other in page.regions if other.region_id != region.region_id
        and _intersects(region.bbox, other.bbox)
    )
    native_overlap: bool = (region.kind == RegionKind.IMAGE and any(
        other.kind == RegionKind.TEXT for other in touching
    )) or (region.kind == RegionKind.TEXT and any(
        other.kind == RegionKind.IMAGE for other in touching
    ))
    role: str = (region.semantic_role if region.semantic_role is not None else
                 "derived_model_table_overlay" if region.extraction_method_override is not None else
                 "embedded_child" if region.parent_region_id else
                 "spatial_overlap_uncertain" if touching else "primary")
    return RegionRelations(tuple(other.region_id for other in touching), native_overlap, role,
                           region.parent_region_id)


def describe_region(page: PageCandidates, region: CandidateRegion,
                    within_page_index: int, document_index: int) -> tuple[RegionPosition, RegionRelations]:
    relations: RegionRelations = _relations(region, page)
    if "layout_provider_applied_review" in page.warnings:
        quality: ReadingOrderQuality = ReadingOrderQuality.EXTERNAL_ESTIMATE
    elif "reading_order_columns_inferred_review" in page.warnings:
        quality: ReadingOrderQuality = ReadingOrderQuality.COLUMN_HEURISTIC
    elif "docx_inline_image_order_approximate" in page.warnings:
        quality: ReadingOrderQuality = ReadingOrderQuality.GEOMETRIC_ESTIMATE
    elif _reading_flow_overlaps(region, page):
        quality: ReadingOrderQuality = ReadingOrderQuality.AMBIGUOUS_OVERLAP
    elif page.kind == PageKind.PHYSICAL_PAGE:
        quality = ReadingOrderQuality.GEOMETRIC_ESTIMATE
    else:
        quality = ReadingOrderQuality.DOCUMENT_FLOW
    coordinate_system: str | None = (
        "pdf_points_top_left" if page.kind == PageKind.PHYSICAL_PAGE else
        "image_pixels_top_left" if page.kind == PageKind.IMAGE else None
    )
    position: RegionPosition = RegionPosition(
        page.number, page.kind, page.label, within_page_index,
        document_index, region.original_index, quality, coordinate_system,
        region.bbox, _normalized(region, page),
    )
    return position, relations


def page_order_quality(page: PageCandidates) -> ReadingOrderQuality:
    if "layout_provider_applied_review" in page.warnings:
        return ReadingOrderQuality.EXTERNAL_ESTIMATE
    if "reading_order_columns_inferred_review" in page.warnings:
        return ReadingOrderQuality.COLUMN_HEURISTIC
    if "docx_inline_image_order_approximate" in page.warnings:
        return ReadingOrderQuality.GEOMETRIC_ESTIMATE
    if any(_reading_flow_overlaps(region, page) for region in page.regions):
        return ReadingOrderQuality.AMBIGUOUS_OVERLAP
    if page.kind == PageKind.PHYSICAL_PAGE:
        return ReadingOrderQuality.GEOMETRIC_ESTIMATE
    return ReadingOrderQuality.DOCUMENT_FLOW


