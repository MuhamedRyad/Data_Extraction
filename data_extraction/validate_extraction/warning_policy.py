"""Stable, auditable warning severity for extraction completeness.

Unknown provider warnings do not become blocking by accident; add known
content-loss cases here, covered by regression tests. No domain validation.
"""
from __future__ import annotations

from enum import StrEnum


class WarningSeverity(StrEnum):
    INFORMATIONAL = "informational"
    REVIEW_REQUIRED = "review_required"
    INCOMPLETE = "incomplete"


INCOMPLETE_WARNINGS: frozenset[str] = frozenset({
    "table_detection_failed",
    "docx_customxml_unrepresented",
    "docx_unrecognized_body_element",
    "docx_non_raster_drawing_unsupported",
    "docx_chart_not_extracted",
    "worksheet_charts_not_extracted",
    "worksheet_cell_comments_not_extracted",
    "unresolved_vector_graphics",
    "docx_legacy_or_embedded_objects_require_review",
    "docx_content_controls_require_review",
    "docx_nested_table_structure_requires_review",
    "docx_textbox_layout_requires_review",
})

REVIEW_WARNINGS: frozenset[str] = frozenset({
    "reading_order_complex_columns_review_required",
    "reading_order_diagram_hierarchy_review_required",
    "layout_provider_invalid_or_failed_review",
    "layout_provider_budget_exceeded",
    "worksheet_image_anchor_unknown",
    "docx_image_relationship_unresolved",
    "docx_external_image_reference_unresolved",
    "docx_floating_image_layout_requires_review",
    "document_structure_provider_invalid_or_failed_review",
    "document_ai_structured_tables_review",
    "pdf_heading_inferred_review",
    "pdf_page_furniture_inferred_review",
    "pdf_rtl_geometry_mismatch_review",
    "pdf_toc_rows_reconstructed_review",
    "pdf_repeated_navigation_inferred_review",
    "vector_graphics_detected_review",
    "vector_detection_failed_review",
    "image_downscaled_for_processing_review",
    "layout_render_downscaled_review",
})


def classify_warning(warning: str) -> WarningSeverity:
    if warning in INCOMPLETE_WARNINGS:
        return WarningSeverity.INCOMPLETE
    if warning in REVIEW_WARNINGS:
        return WarningSeverity.REVIEW_REQUIRED
    return WarningSeverity.INFORMATIONAL


def blocks_publication(warnings: tuple[str, ...]) -> bool:
    return any(classify_warning(w) != WarningSeverity.INFORMATIONAL for w in warnings)
