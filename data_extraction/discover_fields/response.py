from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..shared.models import BoundingBox, ExtractionMethod


class FieldDiscoveryKind(StrEnum):
    EMAIL = "email"
    URL = "url"
    PHONE = "phone"
    DATE = "date"
    TIME = "time"
    MONEY = "money"
    IDENTIFIER = "identifier"
    NUMERIC_CODE = "numeric_code"


@dataclass(frozen=True, slots=True)
class DiscoveredField:
    kind: FieldDiscoveryKind
    value: str
    page_number: int
    region_id: str
    bbox: BoundingBox | None
    extraction_method: ExtractionMethod
    confidence: float | None
    requires_review: bool


@dataclass(frozen=True, slots=True)
class FieldDiscoveryResult:
    fields: tuple[DiscoveredField, ...]
    requires_review: bool
    diagnostics: tuple[str, ...] = ()
    schema_version: str = "field-discovery.v0.1"
