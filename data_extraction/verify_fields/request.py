from __future__ import annotations

from dataclasses import dataclass

from ..shared.models import BoundingBox, DocumentInput


@dataclass(frozen=True, slots=True)
class FieldDefinition:
    """Generic definition for a value that should be verified independently.

    ``pattern`` is a full-match regular expression. ``template`` is optional and
    enables deterministic OCR-confusion proposals. Template symbols:
      A = uppercase Latin letter, a = lowercase Latin letter,
      9 = decimal digit, X = alphanumeric; every other character is literal.

    Template transforms are evidence only. They never mutate extracted source
    text and cannot become verified without independent supporting evidence.
    """

    name: str
    labels: tuple[str, ...]
    pattern: str
    minimum_confidence: float = 0.88
    template: str | None = None
    normalize_arabic_digits: bool = True
    compact_punctuation: bool = True
    # v0.15 template fast path: optional page-relative crop. When present,
    # field verification can skip whole-page layout OCR and re-read only the
    # expected value region. Coordinates are normalized to 0..1.
    page_number: int | None = None
    normalized_value_bbox: BoundingBox | None = None
    normalized_label_bbox: BoundingBox | None = None


@dataclass(frozen=True, slots=True)
class FieldVerificationRequest:
    document: DocumentInput
    fields: tuple[FieldDefinition, ...]

from pydantic import Field
from ..shared.contracts import DocumentRequestContract, StrictContract


class FieldDefinitionInput(StrictContract):
    name: str = Field(min_length=1, max_length=100)
    labels: tuple[str, ...] = ()
    pattern: str = Field(min_length=1, max_length=512)
    minimum_confidence: float = Field(default=0.88, ge=0.0, le=1.0)
    template: str | None = Field(default=None, max_length=512)
    normalize_arabic_digits: bool = True
    compact_punctuation: bool = True
    page_number: int | None = Field(default=None, ge=1)
    normalized_value_bbox: BoundingBox | None = None
    normalized_label_bbox: BoundingBox | None = None

    def to_domain(self) -> FieldDefinition:
        return FieldDefinition(
            name=self.name,
            labels=self.labels,
            pattern=self.pattern,
            minimum_confidence=self.minimum_confidence,
            template=self.template,
            normalize_arabic_digits=self.normalize_arabic_digits,
            compact_punctuation=self.compact_punctuation,
            page_number=self.page_number,
            normalized_value_bbox=self.normalized_value_bbox,
            normalized_label_bbox=self.normalized_label_bbox,
        )


class FieldVerificationInput(StrictContract):
    document: DocumentRequestContract
    fields: tuple[FieldDefinitionInput, ...] = Field(min_length=1)

    def to_domain(self) -> FieldVerificationRequest:
        return FieldVerificationRequest(
            document=self.document.to_domain(),
            fields=tuple(item.to_domain() for item in self.fields),
        )
