from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..shared.models import DocumentInput
from .contracts import DocumentTemplate, TemplateReviewResult


class TemplateBuildMode(StrEnum):
    ADAPTIVE = "adaptive"
    AI_PREFERRED = "ai_preferred"
    AI_REQUIRED = "ai_required"
    DETERMINISTIC = "deterministic"


@dataclass(frozen=True, slots=True)
class TemplateBuildRequest:
    document: DocumentInput
    family: str
    required_fields: tuple[str, ...] = ()
    template_id: str | None = None
    mode: TemplateBuildMode = TemplateBuildMode.AI_PREFERRED


@dataclass(frozen=True, slots=True)
class TemplateApprovalRequest:
    candidate: DocumentTemplate
    approved_by: str
    save: bool = True
    review: TemplateReviewResult | None = None


@dataclass(frozen=True, slots=True)
class TemplateMatchRequest:
    document: DocumentInput
    templates: tuple[DocumentTemplate, ...] = ()
    family: str | None = None


@dataclass(frozen=True, slots=True)
class TemplateExtractionRequest:
    document: DocumentInput
    templates: tuple[DocumentTemplate, ...] = ()
    family: str | None = None

from pydantic import field_validator

from ..shared.contracts import DocumentRequestContract, StrictContract


class TemplateBuildInput(StrictContract):
    document: DocumentRequestContract
    family: str
    required_fields: tuple[str, ...] = ()
    template_id: str | None = None
    mode: TemplateBuildMode = TemplateBuildMode.AI_PREFERRED

    @field_validator("family")
    @classmethod
    def validate_family(cls, value: str) -> str:
        normalized: str = value.strip()
        if not normalized:
            raise ValueError("family must not be blank")
        return normalized

    @field_validator("required_fields")
    @classmethod
    def validate_required_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized: tuple[str, ...] = tuple(item.strip() for item in value)
        if any(not item for item in normalized):
            raise ValueError("required_fields must not contain blank values")
        return normalized

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return TemplateBuildMode(value)
        return value

    def to_domain(self) -> TemplateBuildRequest:
        return TemplateBuildRequest(
            document=self.document.to_domain(),
            family=self.family,
            required_fields=self.required_fields,
            template_id=self.template_id,
            mode=self.mode,
        )


class TemplateApprovalInput(StrictContract):
    candidate: DocumentTemplate
    approved_by: str
    save: bool = True
    review: TemplateReviewResult | None = None

    @field_validator("approved_by")
    @classmethod
    def validate_approved_by(cls, value: str) -> str:
        normalized: str = value.strip()
        if not normalized:
            raise ValueError("approved_by must not be blank")
        return normalized

    def to_domain(self) -> TemplateApprovalRequest:
        return TemplateApprovalRequest(
            candidate=self.candidate,
            approved_by=self.approved_by,
            save=self.save,
            review=self.review,
        )


class TemplateMatchInput(StrictContract):
    document: DocumentRequestContract
    templates: tuple[DocumentTemplate, ...] = ()
    family: str | None = None

    def to_domain(self) -> TemplateMatchRequest:
        return TemplateMatchRequest(
            document=self.document.to_domain(),
            templates=self.templates,
            family=self.family,
        )


class TemplateExtractionInput(StrictContract):
    document: DocumentRequestContract
    templates: tuple[DocumentTemplate, ...] = ()
    family: str | None = None

    def to_domain(self) -> TemplateExtractionRequest:
        return TemplateExtractionRequest(
            document=self.document.to_domain(),
            templates=self.templates,
            family=self.family,
        )
