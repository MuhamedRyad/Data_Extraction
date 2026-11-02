from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..shared.models import BoundingBox


class TemplateStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROVED = "approved"
    RETIRED = "retired"


class TemplateMatchStatus(StrEnum):
    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


class TemplateFieldMode(StrEnum):
    LABEL_RELATIVE = "label_relative"
    REGION = "region"
    FIXED_ANCHOR = "fixed_anchor"


class TemplateAdaptiveRoute(StrEnum):
    DETERMINISTIC_REVIEW = "deterministic_review"
    DETERMINISTIC_REFINE_REVIEW = "deterministic_refine_review"
    AI_GENERATE_REVIEW = "ai_generate_review"
    DETERMINISTIC_FALLBACK_REVIEW = "deterministic_fallback_review"


@dataclass(frozen=True, slots=True)
class TemplateCandidateQuality:
    score: float
    required_field_coverage: float
    geometry_precision: float
    anchor_coverage: float
    schema_valid_ratio: float
    table_structure_ratio: float
    hard_failures: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplatePageBatch:
    batch_index: int
    page_numbers: tuple[int, ...]
    reason: str


@dataclass(frozen=True, slots=True)
class TemplateAnchor:
    text: str
    page_number: int
    normalized_bbox: BoundingBox | None = None
    required: bool = True
    visual_hash: str | None = None


@dataclass(frozen=True, slots=True)
class TemplateFieldDefinition:
    name: str
    field_kind: str
    required: bool
    page_number: int
    anchors: tuple[str, ...]
    pattern: str
    mode: TemplateFieldMode = TemplateFieldMode.LABEL_RELATIVE
    normalized_value_bbox: BoundingBox | None = None
    normalized_label_bbox: BoundingBox | None = None
    template: str | None = None
    minimum_confidence: float = 0.88


@dataclass(frozen=True, slots=True)
class TemplateTableColumnDefinition:
    """Reusable horizontal column geometry inside the repeating-row body crop.

    x coordinates are normalized to the table *body crop*, not the full page.
    The value kind is a generic OCR hint (text/integer/decimal/identifier/date).
    """

    name: str
    normalized_x0: float
    normalized_x1: float
    value_kind: str = "text"
    required: bool = True


@dataclass(frozen=True, slots=True)
class TemplateTableDefinition:
    name: str
    page_number: int
    normalized_bbox: BoundingBox | None
    header_anchors: tuple[str, ...]
    columns: tuple[str, ...]
    repeating_rows: bool = True
    required: bool = False
    normalized_header_bbox: BoundingBox | None = None
    normalized_body_bbox: BoundingBox | None = None
    column_definitions: tuple[TemplateTableColumnDefinition, ...] = ()
    stop_anchors: tuple[str, ...] = ()
    minimum_rows: int = 1


@dataclass(frozen=True, slots=True)
class TemplateLayoutFingerprint:
    page_count: int
    page_aspect_ratios: tuple[float | None, ...]
    page_region_counts: tuple[int, ...]
    page_table_counts: tuple[int, ...]
    anchors: tuple[TemplateAnchor, ...]
    layout_signature: str
    visual_hash: str | None = None
    # v0.22: one cheap perceptual fingerprint per physical PDF/image page.
    # ``visual_hash`` is retained as a backward-compatible alias for page 1.
    page_visual_hashes: tuple[str | None, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentTemplate:
    template_id: str
    family: str
    version: int
    status: TemplateStatus
    document_kind: str
    fields: tuple[TemplateFieldDefinition, ...]
    tables: tuple[TemplateTableDefinition, ...]
    fingerprint: TemplateLayoutFingerprint
    source_sha256: str
    source_id: str
    generator_name: str
    content_hash: str
    approved_by: str | None = None
    schema_version: str = "document-template.v0.1"


@dataclass(frozen=True, slots=True)
class TemplateGenerationProposal:
    document_kind: str
    fields: tuple[TemplateFieldDefinition, ...]
    tables: tuple[TemplateTableDefinition, ...] = ()
    anchors: tuple[TemplateAnchor, ...] = ()
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateGenerationContext:
    family: str
    source_id: str
    source_sha256: str
    required_fields: tuple[str, ...]
    page_count: int
    document_kind: str
    page_text: tuple[str, ...]
    page_sizes: tuple[tuple[float | None, float | None], ...]


class TemplateReviewVerdict(StrEnum):
    PASS = "pass"
    REVIEW_REQUIRED = "review_required"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class TemplateReviewIssue:
    scope: str
    code: str
    reason: str
    confidence: float
    name: str | None = None


@dataclass(frozen=True, slots=True)
class TemplateReviewContext:
    family: str
    source_id: str
    source_sha256: str
    candidate_hash: str
    page_text: tuple[str, ...]
    page_sizes: tuple[tuple[float | None, float | None], ...]


@dataclass(frozen=True, slots=True)
class TemplateReviewResult:
    verdict: TemplateReviewVerdict
    confidence: float
    issues: tuple[TemplateReviewIssue, ...]
    reviewed_candidate_hash: str
    reviewer_name: str
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateRefinementFieldFeedback:
    field_name: str
    page_number: int
    current_value_bbox: BoundingBox | None
    current_label_bbox: BoundingBox | None
    status: str
    candidate_values: tuple[str, ...] = ()
    schema_valid_values: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateRefinementContext:
    generation: TemplateGenerationContext
    attempt: int
    max_attempts: int
    feedback: tuple[TemplateRefinementFieldFeedback, ...]
