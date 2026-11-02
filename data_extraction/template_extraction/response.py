from __future__ import annotations

from dataclasses import dataclass

from ..shared.models import BoundingBox, ExtractionResult
from .contracts import (
    DocumentTemplate, TemplateAdaptiveRoute, TemplateCandidateQuality,
    TemplateMatchStatus, TemplateReviewResult,
)


@dataclass(frozen=True, slots=True)
class TemplateBuildResult:
    candidate: DocumentTemplate
    diagnostics: tuple[str, ...]
    review: TemplateReviewResult | None = None
    route: TemplateAdaptiveRoute | None = None
    quality: TemplateCandidateQuality | None = None


@dataclass(frozen=True, slots=True)
class TemplateMatchEvidence:
    component: str
    score: float
    details: str


@dataclass(frozen=True, slots=True)
class TemplatePageMatch:
    page_number: int
    score: float
    status: TemplateMatchStatus
    drift_detected: bool
    visual_score: float | None
    anchor_score: float | None
    geometry_score: float | None
    shape_score: float | None
    targeted: bool
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateMatchResult:
    template: DocumentTemplate | None
    score: float
    status: TemplateMatchStatus
    drift_detected: bool
    evidence: tuple[TemplateMatchEvidence, ...]
    diagnostics: tuple[str, ...]
    page_matches: tuple[TemplatePageMatch, ...] = ()
    changed_pages: tuple[int, ...] = ()
    stable_pages: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class TemplateFieldResult:
    name: str
    value: str | None
    page_number: int | None
    confidence: float | None
    verified: bool
    status: str
    source: str
    value_bbox: BoundingBox | None
    review_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemplateTableResult:
    name: str
    page_number: int
    rows: tuple[tuple[str | None, ...], ...]
    status: str
    verified: bool
    source_region_id: str | None
    review_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemplateExtractionResult:
    extraction: ExtractionResult
    match: TemplateMatchResult
    fields: tuple[TemplateFieldResult, ...]
    tables: tuple[TemplateTableResult, ...]
    semantic_complete: bool
    verified_required_field_ratio: float
    required_table_coverage_ratio: float
    requires_review: bool
    drift_detected: bool
    diagnostics: tuple[str, ...]
    generic_extraction_performed: bool = True
    generic_extraction_pages: tuple[int, ...] = ()
    template_fast_path_pages: tuple[int, ...] = ()
    schema_version: str = "template-extraction.v0.1"
