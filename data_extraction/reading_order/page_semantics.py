"""Deterministic page semantic classification used to gate reading order."""
from __future__ import annotations

from ..shared.models import BoundingBox, CandidateRegion, RegionKind
from .contracts import FlowRole, PageSemanticClass
from .flow_roles import participates_in_linear_flow


def _area(box: BoundingBox | None) -> float:
    if box is None:
        return 0.0
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection_ratio(a: BoundingBox | None, b: BoundingBox | None) -> float:
    if a is None or b is None:
        return 0.0
    width: float = min(a[2], b[2]) - max(a[0], b[0])
    height: float = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    smaller: float = min(_area(a), _area(b))
    return width * height / smaller if smaller > 0 else 0.0


def classify_page_semantics(
    regions: list[CandidateRegion],
    page_width: float,
    page_height: float,
    column_evidence: bool,
) -> PageSemanticClass:
    page_area: float = max(1.0, page_width * page_height)
    text: list[CandidateRegion] = [
        region for region in regions
        if region.kind == RegionKind.TEXT and participates_in_linear_flow(region)
    ]
    toc_tables: list[CandidateRegion] = [
        region for region in regions
        if region.semantic_role == FlowRole.TABLE_OF_CONTENTS.value
    ]
    tables: list[CandidateRegion] = [
        region for region in regions
        if region.semantic_role in {FlowRole.STRUCTURAL_TABLE.value, FlowRole.TABLE_OF_CONTENTS.value}
    ]
    figures: list[CandidateRegion] = [
        region for region in regions
        if region.semantic_role == FlowRole.SEMANTIC_FIGURE.value
    ]

    if toc_tables:
        return PageSemanticClass.TABLE_OF_CONTENTS

    # Large vector/figure containing many text labels is a graph/diagram rather
    # than prose columns. This catches organizational charts without pretending
    # an authoritative linear traversal exists.
    for figure in figures:
        figure_ratio: float = _area(figure.bbox) / page_area
        overlapping_labels: int = sum(
            _intersection_ratio(label.bbox, figure.bbox) >= 0.35 for label in text
        )
        if figure_ratio >= 0.50 and overlapping_labels >= 8:
            return PageSemanticClass.DIAGRAM_HIERARCHY

    table_area_ratio: float = min(1.0, sum(_area(table.bbox) for table in tables) / page_area)
    if tables and (table_area_ratio >= 0.30 or (len(tables) >= 2 and len(figures) == 0)):
        return PageSemanticClass.TABLE_HEAVY
    if figures and text:
        significant: bool = any(_area(figure.bbox) / page_area >= 0.06 for figure in figures)
        if significant:
            return PageSemanticClass.MIXED_LAYOUT
    if column_evidence:
        return PageSemanticClass.MULTI_COLUMN_PROSE
    if text:
        return PageSemanticClass.SIMPLE_PROSE
    if tables or figures:
        return PageSemanticClass.MIXED_LAYOUT
    return PageSemanticClass.UNKNOWN


def semantic_warning(value: PageSemanticClass) -> str:
    return f"page_semantic_{value.value}"
