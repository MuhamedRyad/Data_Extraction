from __future__ import annotations

from typing import Protocol

from ..shared.models import DocumentInput
from .contracts import (
    DocumentTemplate,
    TemplateGenerationContext,
    TemplateGenerationProposal,
    TemplateRefinementContext,
    TemplateReviewContext,
    TemplateReviewResult,
)


class TemplateGenerationProvider(Protocol):
    """AI/Vision adapter used only for unknown layouts.

    The provider proposes a template. Core validation + explicit approval are
    still required before the template can be used for automatic extraction.
    """

    def generate_template(
        self,
        document: DocumentInput,
        context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal: ...


class TemplateRefinementProvider(Protocol):
    """Optional AI geometry-refinement capability.

    Refinement is deliberately narrower than generation: the provider may
    adjust only normalized label/value geometry. Core code rejects semantic,
    regex, required-field, anchor, table, or page-number changes.
    """

    def refine_template(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context: TemplateRefinementContext,
    ) -> TemplateGenerationProposal: ...


class TemplateReviewProvider(Protocol):
    """Independent semantic critic for an already-built template candidate.

    The reviewer does not generate or mutate the template. It compares the
    candidate against the original visual document and returns a bounded
    verdict plus issues. Core code still controls approval and persistence.
    """

    def review_template(
        self,
        document: DocumentInput,
        candidate: "DocumentTemplate",
        context: TemplateReviewContext,
    ) -> TemplateReviewResult: ...
