"""Hybrid deterministic reading order for native PDF regions.

v0.11 keeps the proven v0.10 column behavior, but separates physical overlays
from linear flow, classifies semantic page type, and records a spatial graph for
supported column layouts. It remains fail-closed for diagrams and unsupported
mixed layouts.
"""
from __future__ import annotations

import re
from typing import Sequence

from ..reading_order.contracts import PageSemanticClass, ReadingOrderResolution, SpatialEdge
from ..reading_order.flow_roles import participates_in_linear_flow
from ..reading_order.page_semantics import classify_page_semantics
from ..reading_order.spatial_graph import build_linear_edges, topological_order
from ..shared.models import BoundingBox, CandidateRegion, RegionKind


def _by_geometry(items: Sequence[CandidateRegion]) -> list[CandidateRegion]:
    return sorted(items, key=lambda item: (item.bbox[1], item.bbox[0]) if item.bbox
                  else (0.0, 0.0))


def _band(items: Sequence[CandidateRegion]) -> tuple[float, float]:
    boxes: list[BoundingBox] = [region.bbox for region in items if region.bbox is not None]
    return min(box[1] for box in boxes), max(box[3] for box in boxes)


def _column_groups(items: list[CandidateRegion], page_width: float) -> list[list[CandidateRegion]] | None:
    """Detect repeated x-overlap-connected vertical bands."""
    if len(items) < 4 or any(region.bbox is None for region in items):
        return None
    sorted_x: list[CandidateRegion] = sorted(items, key=lambda r: (r.bbox[0], r.bbox[1]))  # type: ignore[index]
    groups: list[list[CandidateRegion]] = []
    end_x: float = -1e100
    gap_threshold: float = max(7.0, page_width * 0.035)
    for region in sorted_x:
        box: BoundingBox = region.bbox  # type: ignore[assignment]
        if not groups or box[0] > end_x + gap_threshold:
            groups.append([region])
            end_x = box[2]
        else:
            groups[-1].append(region)
            end_x = max(end_x, box[2])
    if not 2 <= len(groups) <= 4 or any(len(group) < 2 for group in groups):
        return None
    spans: list[tuple[float, float]] = [_band(group) for group in groups]
    top_shared: float = max(span[0] for span in spans)
    bottom_shared: float = min(span[1] for span in spans)
    if top_shared >= bottom_shared:
        return None
    if bottom_shared - top_shared < max(14.0, 0.08 * max(span[1] for span in spans)):
        return None
    return groups


def _columns_with_evidence(
    items: list[CandidateRegion], page_width: float, rtl: bool,
) -> tuple[list[CandidateRegion], bool, tuple[SpatialEdge, ...]]:
    groups: list[list[CandidateRegion]] | None = _column_groups(items, page_width)
    if groups is None:
        return _by_geometry(items), False, ()
    spans: list[tuple[float, float]] = [_band(group) for group in groups]
    top_shared: float = max(span[0] for span in spans)
    bottom_shared: float = min(span[1] for span in spans)

    prefixes: list[CandidateRegion] = []
    suffixes: list[CandidateRegion] = []
    active: list[list[CandidateRegion]] = []
    for group in groups:
        kept: list[CandidateRegion] = []
        for region in group:
            bbox: BoundingBox = region.bbox  # type: ignore[assignment]
            height: float = bbox[3] - bbox[1]
            if bbox[3] < top_shared - max(8.0, height * 1.5):
                prefixes.append(region)
            elif bbox[1] > bottom_shared + max(8.0, height * 1.5):
                suffixes.append(region)
            else:
                kept.append(region)
        if len(kept) < 2:
            return _by_geometry(items), False, ()
        active.append(_by_geometry(kept))

    edges: tuple[SpatialEdge, ...] = build_linear_edges(active, [], rtl)
    active_regions: list[CandidateRegion] = [region for group in active for region in group]
    graph_order: list[CandidateRegion] | None = topological_order(active_regions, edges)
    if graph_order is None:
        return _by_geometry(items), False, ()
    return _by_geometry(prefixes) + graph_order + _by_geometry(suffixes), True, edges


def _columns(items: list[CandidateRegion], page_width: float,
             rtl: bool) -> tuple[list[CandidateRegion], bool]:
    """Backward-compatible helper retained for existing tests/imports."""
    ordered, did_split, _ = _columns_with_evidence(items, page_width, rtl)
    return ordered, did_split


def _merge_excluded(
    active_order: list[CandidateRegion], excluded: list[CandidateRegion],
) -> list[CandidateRegion]:
    """Keep excluded overlays visible without allowing them to constrain flow."""
    if not excluded:
        return active_order
    output: list[CandidateRegion] = list(active_order)
    for region in _by_geometry(excluded):
        y0: float = region.bbox[1] if region.bbox else 0.0
        insertion: int = len(output)
        for index, current in enumerate(output):
            current_y: float = current.bbox[1] if current.bbox else 0.0
            if current_y >= y0:
                insertion = index
                break
        output.insert(insertion, region)
    return output


def resolve_pdf_regions(
    regions: list[CandidateRegion], width: float, height: float,
) -> tuple[list[CandidateRegion], ReadingOrderResolution]:
    geometric: list[CandidateRegion] = _by_geometry(regions)
    if width <= 0 or height <= 0:
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in geometric), (), PageSemanticClass.UNKNOWN,
            0.25, (), False,
        )
        return geometric, resolution

    active: list[CandidateRegion] = [region for region in geometric if participates_in_linear_flow(region)]
    excluded: list[CandidateRegion] = [region for region in geometric if not participates_in_linear_flow(region)]
    native: list[CandidateRegion] = [
        region for region in active if region.kind == RegionKind.TEXT and region.bbox is not None
    ]
    rtl: bool = (
        sum(len(re.findall(r'[\u0600-\u06ff]', region.text or '')) for region in native)
        > sum(len(re.findall(r'[A-Za-z]', region.text or '')) for region in native)
    )
    narrow: list[CandidateRegion] = [
        region for region in native if region.bbox and region.bbox[2] - region.bbox[0] < width * 0.55
    ]
    _, parallel_evidence, preliminary_edges = _columns_with_evidence(narrow, width, rtl)
    semantic_class: PageSemanticClass = classify_page_semantics(
        active, width, height, parallel_evidence
    )

    if len(active) < 4 or len(native) < 4:
        ordered: list[CandidateRegion] = _merge_excluded(geometric if not active else _by_geometry(active), excluded)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), (), semantic_class,
            0.92 if semantic_class == PageSemanticClass.SIMPLE_PROSE else 0.65,
            preliminary_edges, False,
        )
        return ordered, resolution

    if semantic_class == PageSemanticClass.DIAGRAM_HIERARCHY:
        ordered = _merge_excluded(_by_geometry(active), excluded)
        warnings: tuple[str, ...] = ("reading_order_diagram_hierarchy_review_required",)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), warnings, semantic_class,
            0.20, preliminary_edges, False,
        )
        return ordered, resolution

    if not parallel_evidence:
        ordered = _merge_excluded(_by_geometry(active), excluded)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), (), semantic_class,
            0.90 if semantic_class == PageSemanticClass.SIMPLE_PROSE else 0.70,
            (), False,
        )
        return ordered, resolution

    # Wide spanning regions (headings, tables, figures) split column sections.
    spanners: list[CandidateRegion] = [
        region for region in active
        if region.bbox and region.bbox[2] - region.bbox[0] >= width * 0.55
    ]
    if len(spanners) > 8:
        ordered = _merge_excluded(_by_geometry(active), excluded)
        warnings = ("reading_order_complex_columns_review_required",)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), warnings, semantic_class,
            0.35, preliminary_edges, False,
        )
        return ordered, resolution
    sorted_spanners: list[CandidateRegion] = _by_geometry(spanners)
    if any(a.bbox[3] > b.bbox[1] for a, b in zip(sorted_spanners, sorted_spanners[1:])):  # type: ignore[index]
        ordered = _merge_excluded(_by_geometry(active), excluded)
        warnings = ("reading_order_complex_columns_review_required",)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), warnings, semantic_class,
            0.35, preliminary_edges, False,
        )
        return ordered, resolution

    # A narrow semantic object inside a prose column needs semantic evidence;
    # the graph intentionally abstains instead of guessing caption/figure order.
    non_text_narrow: list[CandidateRegion] = [
        region for region in active
        if region.kind != RegionKind.TEXT and region not in spanners
    ]
    if non_text_narrow:
        ordered = _merge_excluded(_by_geometry(active), excluded)
        warnings = ("reading_order_complex_columns_review_required",)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), warnings, semantic_class,
            0.40, preliminary_edges, False,
        )
        return ordered, resolution

    remaining: list[CandidateRegion] = [region for region in native if region not in spanners]
    completed: list[CandidateRegion] = []
    all_edges: list[SpatialEdge] = []
    previous_y: float = float('-inf')
    split_found: bool = False
    for separator in sorted_spanners + [None]:
        ceiling: float = separator.bbox[1] if separator and separator.bbox else float('inf')
        segment: list[CandidateRegion] = [
            region for region in remaining if region.bbox
            and region.bbox[1] >= previous_y and region.bbox[3] <= ceiling
        ]
        ambiguous: list[CandidateRegion] = [
            region for region in remaining if region.bbox
            and region.bbox[1] < ceiling < region.bbox[3]
        ]
        if ambiguous:
            ordered = _merge_excluded(_by_geometry(active), excluded)
            warnings = ("reading_order_complex_columns_review_required",)
            resolution = ReadingOrderResolution(
                tuple(region.region_id for region in ordered), warnings, semantic_class,
                0.35, tuple(all_edges), False,
            )
            return ordered, resolution
        ordered_segment, did_split, edges = _columns_with_evidence(segment, width, rtl)
        split_found = split_found or did_split
        completed.extend(ordered_segment)
        all_edges.extend(edges)
        if separator is not None:
            completed.append(separator)
            previous_y = separator.bbox[3]  # type: ignore[index]

    if len(completed) != len(active) or len({region.region_id for region in completed}) != len(active):
        ordered = _merge_excluded(_by_geometry(active), excluded)
        warnings = ("reading_order_complex_columns_review_required",)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), warnings, semantic_class,
            0.35, tuple(all_edges), False,
        )
        return ordered, resolution
    if not split_found:
        ordered = _merge_excluded(_by_geometry(active), excluded)
        resolution = ReadingOrderResolution(
            tuple(region.region_id for region in ordered), (), semantic_class,
            0.80, tuple(all_edges), bool(all_edges),
        )
        return ordered, resolution

    ordered = _merge_excluded(completed, excluded)
    warnings = ("reading_order_columns_inferred_review",)
    resolution = ReadingOrderResolution(
        tuple(region.region_id for region in ordered), warnings, semantic_class,
        0.95 if not excluded else 0.93, tuple(all_edges), True,
    )
    return ordered, resolution


def order_pdf_regions(regions: list[CandidateRegion], width: float,
                      height: float) -> tuple[list[CandidateRegion], tuple[str, ...]]:
    """Backward-compatible public helper returning only order and warnings."""
    ordered, resolution = resolve_pdf_regions(regions, width, height)
    return ordered, resolution.warnings
