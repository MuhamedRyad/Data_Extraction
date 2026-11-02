from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class TemplateExtractionOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    accepted_match_threshold: float = 0.90
    review_match_threshold: float = 0.72
    drift_threshold: float = 0.84
    anchor_weight: float = 0.30
    geometry_weight: float = 0.20
    page_shape_weight: float = 0.10
    document_kind_weight: float = 0.10
    table_structure_weight: float = 0.10
    page_count_weight: float = 0.10
    visual_fingerprint_weight: float = 0.10
    geometry_tolerance: float = 0.12
    max_templates_to_match: int = 100
    max_fields_per_template: int = 100
    max_tables_per_template: int = 50
    minimum_preflight_evidence_weight: float = 0.25
    preflight_visual_minimum: float = 0.90
    preflight_anchor_visual_minimum: float = 0.96
    max_pattern_chars: int = 512
    max_anchor_chars: int = 200
    ambiguity_margin: float = 0.03
    auto_refine_ai_templates: bool = True
    max_refinement_attempts: int = 2
    refinement_require_schema_valid_crop: bool = True
    max_template_table_ocr_calls: int = 120
    table_row_vertical_tolerance_ratio: float = 0.70
    table_cell_inset_pixels: int = 4
    table_cell_fallback_enabled: bool = True
    table_consensus_count: int = 2
    auto_review_template_candidates: bool = True
    minimum_ai_review_confidence: float = 0.80
    require_review_when_reviewer_configured: bool = True
    adaptive_high_quality_threshold: float = 0.82
    adaptive_medium_quality_threshold: float = 0.50
    adaptive_minimum_schema_valid_ratio: float = 0.75
    adaptive_minimum_geometry_precision: float = 0.65
    adaptive_required_field_weight: float = 0.30
    adaptive_geometry_weight: float = 0.20
    adaptive_anchor_weight: float = 0.10
    adaptive_schema_weight: float = 0.30
    adaptive_table_weight: float = 0.10
    adaptive_refine_after_reviewer_issue: bool = True
    # v0.22: multi-page safety + bounded AI batching.
    page_visual_weight: float = 0.55
    page_anchor_weight: float = 0.25
    page_geometry_weight: float = 0.15
    page_shape_weight: float = 0.05
    page_match_accepted_threshold: float = 0.90
    page_match_review_threshold: float = 0.72
    selective_page_fallback_enabled: bool = True
    max_pages_per_ai_batch: int = 4
    max_ai_batches_per_document: int = 25
    # v0.23: independent AI page batches may run concurrently, but never
    # beyond this bound. Merge order remains deterministic.
    enable_parallel_ai_batches: bool = True
    max_parallel_ai_batches: int = 2

