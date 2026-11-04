"""Conservative flow-role classification for PDF regions.

A region can remain physically present and extraction-relevant while being
excluded from *linear reading-order ambiguity*. This is essential for native
text overlays and decorative/background graphics.
"""
from __future__ import annotations

from dataclasses import replace

from ..shared.models import BoundingBox, CandidateRegion, RegionKind
from .contracts import FlowRole


def _area(box: BoundingBox | None) -> float:
    if box is None:
        return 0.0
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def _intersection_ratio(inner: BoundingBox | None, outer: BoundingBox | None) -> float:
    """Fraction of the first box covered by the second box."""
    if inner is None or outer is None:
        return 0.0
    width: float = min(inner[2], outer[2]) - max(inner[0], outer[0])
    height: float = min(inner[3], outer[3]) - max(inner[1], outer[1])
    if width <= 0 or height <= 0:
        return 0.0
    denominator: float = _area(inner)
    return (width * height) / denominator if denominator > 0 else 0.0


def classify_flow_roles(
    regions: list[CandidateRegion],
    page_width: float,
    page_height: float,
    decorative_area_ratio: float = 0.0015,
) -> list[CandidateRegion]:
    """Return immutable copies annotated with semantic roles.

    The strongest rule is evidence-based: an image produced as a
    ``native_text_overlay`` has overlapping native text. A small support image
    must not make the same native text ambiguous merely due to that overlap;
    the image remains available for extraction and review.
    """
    page_area: float = max(1.0, page_width * page_height)
    text_regions: tuple[CandidateRegion, ...] = tuple(
        region for region in regions if region.kind == RegionKind.TEXT and region.bbox is not None
    )
    output: list[CandidateRegion] = []
    for region in regions:
        role: str | None = region.semantic_role
        if region.kind == RegionKind.TABLE:
            role = (FlowRole.TABLE_OF_CONTENTS.value
                    if region.semantic_role == FlowRole.TABLE_OF_CONTENTS.value
                    else FlowRole.STRUCTURAL_TABLE.value)
        elif region.kind == RegionKind.IMAGE:
            area_ratio: float = _area(region.bbox) / page_area
            covered_text_count: int = sum(
                _intersection_ratio(text.bbox, region.bbox) >= 0.80 for text in text_regions
            )
            if region.image_role == "native_text_overlay":
                # Full-page scans with only a small native text layer remain
                # reading-order relevant: they may contain unrepresented content.
                # Exclude only strong support/background cases.
                if area_ratio >= 0.55 and covered_text_count >= 3:
                    role = FlowRole.DECORATIVE_BACKGROUND.value
                elif area_ratio <= 0.18 and covered_text_count:
                    role = FlowRole.NATIVE_TEXT_OVERLAY_SUPPORT.value
                else:
                    role = FlowRole.SEMANTIC_FIGURE.value
            elif region.image_role == "vector_graphic_candidate":
                # Medium vector decoration behind many native text blocks is a
                # support layer (e.g. contents-page rules/cards). Small vectors
                # with labels remain semantic charts; near-full-page vectors can
                # still classify as diagrams/hierarchies.
                if (region.native_text_masked and 0.20 <= area_ratio < 0.50
                        and covered_text_count >= 5):
                    role = FlowRole.NATIVE_TEXT_OVERLAY_SUPPORT.value
                else:
                    role = FlowRole.SEMANTIC_FIGURE.value
            elif region.content_hint == RegionKind.TABLE or region.image_role == "raster_grid_table":
                role = FlowRole.STRUCTURAL_TABLE.value
            elif region.image_role == "embedded_image" and area_ratio <= decorative_area_ratio:
                role = FlowRole.DECORATIVE_CANDIDATE.value
            else:
                role = role or FlowRole.SEMANTIC_FIGURE.value
        output.append(replace(region, semantic_role=role) if role is not None else region)
    return output


def participates_in_linear_flow(region: CandidateRegion) -> bool:
    """Whether a region should affect linear reading-order ambiguity."""
    return region.semantic_role not in {
        FlowRole.NATIVE_TEXT_OVERLAY_SUPPORT.value,
        FlowRole.DECORATIVE_BACKGROUND.value,
        FlowRole.DECORATIVE_CANDIDATE.value,
        FlowRole.TOC_SOURCE_SUPPORT.value,
        FlowRole.REPEATED_NAVIGATION.value,
        "page_header_candidate",
        "page_footer_candidate",
    }
