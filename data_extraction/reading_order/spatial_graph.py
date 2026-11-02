"""Small deterministic spatial graph for auditable page ordering.

The graph does not infer document meaning. It only records precedence supported
by stable geometry: vertical order within a column, column traversal, and
spanning separators.
"""
from __future__ import annotations

import heapq
from collections import defaultdict

from ..shared.models import CandidateRegion
from .contracts import SpatialEdge, SpatialRelationType


def build_linear_edges(
    groups: list[list[CandidateRegion]],
    spanners: list[CandidateRegion],
    rtl: bool,
) -> tuple[SpatialEdge, ...]:
    edges: list[SpatialEdge] = []
    ordered_groups: list[list[CandidateRegion]] = list(reversed(groups)) if rtl else groups
    for group in ordered_groups:
        for first, second in zip(group, group[1:]):
            edges.append(SpatialEdge(first.region_id, second.region_id, SpatialRelationType.ABOVE))
    for first_group, second_group in zip(ordered_groups, ordered_groups[1:]):
        if first_group and second_group:
            edges.append(SpatialEdge(
                first_group[-1].region_id,
                second_group[0].region_id,
                SpatialRelationType.COLUMN_BEFORE,
            ))
    for first, second in zip(spanners, spanners[1:]):
        edges.append(SpatialEdge(first.region_id, second.region_id, SpatialRelationType.SPANNER_BEFORE))
    return tuple(edges)


def topological_order(
    regions: list[CandidateRegion],
    edges: tuple[SpatialEdge, ...],
) -> list[CandidateRegion] | None:
    lookup: dict[str, CandidateRegion] = {region.region_id: region for region in regions}
    incoming: dict[str, int] = {region.region_id: 0 for region in regions}
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        if edge.source_region_id not in lookup or edge.target_region_id not in lookup:
            continue
        outgoing[edge.source_region_id].append(edge.target_region_id)
        incoming[edge.target_region_id] += 1
    queue: list[tuple[float, float, str]] = []
    for region in regions:
        if incoming[region.region_id] == 0:
            box = region.bbox or (0.0, 0.0, 0.0, 0.0)
            heapq.heappush(queue, (box[1], box[0], region.region_id))
    result: list[CandidateRegion] = []
    while queue:
        _, _, region_id = heapq.heappop(queue)
        result.append(lookup[region_id])
        for target in outgoing.get(region_id, []):
            incoming[target] -= 1
            if incoming[target] == 0:
                box = lookup[target].bbox or (0.0, 0.0, 0.0, 0.0)
                heapq.heappush(queue, (box[1], box[0], target))
    return result if len(result) == len(regions) else None
