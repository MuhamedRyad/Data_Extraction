from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..shared.models import BoundingBox


class FieldVerificationStatus(StrEnum):
    VERIFIED_SOURCE = "verified_source"
    VERIFIED_CONSENSUS = "verified_consensus"
    REVIEW_REQUIRED = "review_required"
    LABEL_NOT_FOUND = "label_not_found"
    VALUE_REGION_NOT_FOUND = "value_region_not_found"
    PROVIDER_UNAVAILABLE = "provider_unavailable"


@dataclass(frozen=True, slots=True)
class FieldCandidate:
    value: str
    confidence: float | None
    source: str
    provider_name: str | None
    schema_valid: bool
    transformed: bool = False


@dataclass(frozen=True, slots=True)
class VerifiedField:
    field_name: str
    page_number: int | None
    status: FieldVerificationStatus
    label_text: str | None
    original_value: str | None
    suggested_value: str | None
    confidence: float | None
    label_bbox: BoundingBox | None
    value_bbox: BoundingBox | None
    candidates: tuple[FieldCandidate, ...]
    review_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FieldVerificationResult:
    fields: tuple[VerifiedField, ...]
    requires_review: bool
    diagnostics: tuple[str, ...]
    schema_version: str = "field-verification.v0.1"
