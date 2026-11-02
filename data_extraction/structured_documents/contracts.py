from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..shared.models import BoundingBox


class StructuredDocumentKind(StrEnum):
    GENERAL = "general"
    RECEIPT = "receipt"
    INVOICE = "invoice"
    FORM = "form"


class StructuredSemanticStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StructuredFieldEvidence:
    field_kind: str
    value: str
    page_number: int
    source_region_id: str | None
    bbox: BoundingBox | None
    confidence: float | None
    source: str
    verified: bool
    review_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructuredPageAnalysis:
    kind: StructuredDocumentKind
    kind_confidence: float
    semantic_status: StructuredSemanticStatus
    semantic_complete: bool | None
    field_coverage_ratio: float
    verified_field_coverage_ratio: float
    semantic_gap_count: int
    required_fields: tuple[str, ...]
    found_fields: tuple[str, ...]
    fields: tuple[StructuredFieldEvidence, ...]
    requires_review: bool
    review_reasons: tuple[str, ...]
