"""Typed internal contracts for v0.11 reading-flow analysis.

These contracts are intentionally internal to the extraction engine. They do
not change the public extraction.v0.6 JSON contract. Physical overlap evidence
is preserved separately from whether an object participates in linear reading
flow.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FlowRole(StrEnum):
    PRIMARY_TEXT = "primary_text"
    STRUCTURAL_TABLE = "structural_table"
    SEMANTIC_FIGURE = "semantic_figure"
    NATIVE_TEXT_OVERLAY_SUPPORT = "native_text_overlay_support"
    DECORATIVE_BACKGROUND = "decorative_background"
    DECORATIVE_CANDIDATE = "decorative_candidate"
    TABLE_OF_CONTENTS = "table_of_contents"
    TOC_SOURCE_SUPPORT = "toc_source_text_support"
    REPEATED_NAVIGATION = "repeated_navigation_candidate"


class PageSemanticClass(StrEnum):
    SIMPLE_PROSE = "simple_prose"
    MULTI_COLUMN_PROSE = "multi_column_prose"
    TABLE_HEAVY = "table_heavy"
    TABLE_OF_CONTENTS = "table_of_contents"
    DIAGRAM_HIERARCHY = "diagram_hierarchy"
    MIXED_LAYOUT = "mixed_layout"
    UNKNOWN = "unknown"


class SpatialRelationType(StrEnum):
    ABOVE = "above"
    SAME_COLUMN = "same_column"
    COLUMN_BEFORE = "column_before"
    SPANNER_BEFORE = "spanner_before"
    SPANNER_AFTER = "spanner_after"


@dataclass(frozen=True, slots=True)
class SpatialEdge:
    source_region_id: str
    target_region_id: str
    relation: SpatialRelationType


@dataclass(frozen=True, slots=True)
class ReadingOrderResolution:
    ordered_region_ids: tuple[str, ...]
    warnings: tuple[str, ...]
    semantic_class: PageSemanticClass
    confidence: float
    edges: tuple[SpatialEdge, ...]
    used_spatial_graph: bool
