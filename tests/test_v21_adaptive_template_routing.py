from __future__ import annotations

import io
from dataclasses import replace

import fitz
from PIL import Image

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    ProviderTextResult,
    TemplateAdaptiveRoute,
    TemplateAnchor,
    TemplateBuildMode,
    TemplateBuildRequest,
    TemplateExtractionOptions,
    TemplateFieldDefinition,
    TemplateFieldMode,
    TemplateGenerationProposal,
    TemplateReviewResult,
    TemplateReviewVerdict,
)


class FullDocumentOCR:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        return ProviderTextResult(self.text, 0.98, provider_name="full-document-ocr")


class AdaptiveTemplateAI:
    def __init__(self) -> None:
        self.generate_calls = 0
        self.refine_calls = 0

    def generate_template(self, document, context) -> TemplateGenerationProposal:
        self.generate_calls += 1
        return TemplateGenerationProposal(
            document_kind="invoice",
            fields=(
                TemplateFieldDefinition(
                    "invoice_number", "invoice_number", True, 1, ("Invoice No",),
                    r"INV-\d{4}-\d{3}", TemplateFieldMode.LABEL_RELATIVE,
                    (0.24, 0.12, 0.47, 0.19), (0.05, 0.12, 0.22, 0.19), minimum_confidence=0.8,
                ),
                TemplateFieldDefinition(
                    "date", "date", True, 1, ("Date",),
                    r"\d{2}/\d{2}/\d{4}", TemplateFieldMode.LABEL_RELATIVE,
                    (0.70, 0.12, 0.90, 0.19), (0.55, 0.12, 0.68, 0.19), minimum_confidence=0.8,
                ),
                TemplateFieldDefinition(
                    "total", "total", True, 1, ("TOTAL",),
                    r"\d+[.,]\d{2}", TemplateFieldMode.LABEL_RELATIVE,
                    (0.78, 0.74, 0.93, 0.81), (0.55, 0.74, 0.72, 0.81), minimum_confidence=0.8,
                ),
            ),
            anchors=(TemplateAnchor("Invoice", 1, None, False),),
            diagnostics=("adaptive_fake_full_generation",),
        )

    def refine_template(self, document, proposal, context) -> TemplateGenerationProposal:
        self.refine_calls += 1
        refined = []
        for index, field in enumerate(proposal.fields):
            if field.name == "invoice_number":
                value_bbox = (0.24, 0.12, 0.47, 0.19)
                label_bbox = (0.05, 0.12, 0.22, 0.19)
            elif field.name == "date":
                value_bbox = (0.70, 0.12, 0.90, 0.19)
                label_bbox = (0.55, 0.12, 0.68, 0.19)
            elif field.name == "total":
                value_bbox = (0.78, 0.74, 0.93, 0.81)
                label_bbox = (0.55, 0.74, 0.72, 0.81)
            else:
                y0 = 0.22 + index * 0.06
                value_bbox = (0.20, y0, 0.45, min(0.98, y0 + 0.04))
                label_bbox = field.normalized_label_bbox
            refined.append(replace(
                field,
                normalized_value_bbox=value_bbox,
                normalized_label_bbox=label_bbox,
            ))
        return replace(
            proposal,
            fields=tuple(refined),
            diagnostics=proposal.diagnostics + ("adaptive_fake_geometry_refined",),
        )


class PassReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review_template(self, document, candidate, context) -> TemplateReviewResult:
        self.calls += 1
        return TemplateReviewResult(
            verdict=TemplateReviewVerdict.PASS,
            confidence=0.99,
            issues=(),
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name="PassReviewer",
            diagnostics=("adaptive_fake_review_pass",),
        )


def _png() -> bytes:
    image = Image.new("RGB", (900, 1200), "white")
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _invoice_text() -> str:
    return (
        "NOVA SUPPLIES\n"
        "Invoice No: INV-2026-101\n"
        "Date: 01/11/2026\n"
        "Subtotal 100.00\n"
        "VAT 5.00\n"
        "TOTAL 105.00\n"
    )


def _service(
    text: str, options: TemplateExtractionOptions | None = None,
) -> tuple[DataExtractionService, AdaptiveTemplateAI, PassReviewer]:
    ocr = FullDocumentOCR(text)
    ai = AdaptiveTemplateAI()
    reviewer = PassReviewer()
    service = DataExtractionService(
        ocr_provider=ocr,
        field_ocr_providers=(ocr,),
        template_generation_provider=ai,
        template_review_provider=reviewer,
        template_options=options or TemplateExtractionOptions(max_refinement_attempts=1),
    )
    return service, ai, reviewer


def test_adaptive_medium_ocr_candidate_refines_without_full_ai_generation() -> None:
    service, ai, reviewer = _service(_invoice_text())
    request = TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "adaptive-medium"),
        "nova-invoice",
        required_fields=("invoice_number", "date", "total"),
        mode=TemplateBuildMode.ADAPTIVE,
    )
    result = service.build_template_candidate(request)

    assert result.route == TemplateAdaptiveRoute.DETERMINISTIC_REFINE_REVIEW
    assert result.quality is not None
    assert ai.generate_calls == 0
    assert ai.refine_calls == 1
    assert reviewer.calls == 1
    assert result.review is not None
    assert result.review.verdict == TemplateReviewVerdict.PASS
    assert "adaptive_route:deterministic_refine_review" in result.diagnostics


def test_adaptive_missing_required_field_escalates_to_full_ai_generator() -> None:
    service, ai, reviewer = _service("NOVA SUPPLIES\nTOTAL 105.00\n")
    request = TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "adaptive-low"),
        "nova-invoice",
        required_fields=("invoice_number", "date", "total"),
        mode=TemplateBuildMode.ADAPTIVE,
    )
    result = service.build_template_candidate(request)

    assert result.route == TemplateAdaptiveRoute.AI_GENERATE_REVIEW
    assert result.quality is not None
    assert any(item.startswith("missing_required_field:") for item in result.quality.hard_failures)
    assert ai.generate_calls == 1
    assert reviewer.calls == 1
    assert result.review is not None
    assert result.review.verdict == TemplateReviewVerdict.PASS
    assert "adaptive_route:ai_generate_review" in result.diagnostics


def test_adaptive_high_quality_candidate_uses_reviewer_only() -> None:
    options = TemplateExtractionOptions(
        max_refinement_attempts=1,
        adaptive_high_quality_threshold=0.40,
        adaptive_medium_quality_threshold=0.20,
        adaptive_minimum_geometry_precision=0.0,
        adaptive_minimum_schema_valid_ratio=0.15,
    )
    service, ai, reviewer = _service(_invoice_text(), options)
    request = TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "adaptive-high"),
        "nova-high",
        required_fields=("invoice_number", "date", "total"),
        mode=TemplateBuildMode.ADAPTIVE,
    )
    result = service.build_template_candidate(request)

    assert result.route == TemplateAdaptiveRoute.DETERMINISTIC_REVIEW
    assert result.quality is not None
    assert ai.generate_calls == 0
    assert ai.refine_calls == 0
    assert reviewer.calls == 1
    assert result.review is not None
    assert result.review.verdict == TemplateReviewVerdict.PASS


class SemanticMappingReviewer:
    def __init__(self) -> None:
        self.calls = 0

    def review_template(self, document, candidate, context) -> TemplateReviewResult:
        self.calls += 1
        by_name = {field.name: field for field in candidate.fields}
        subtotal = by_name.get("subtotal")
        total = by_name.get("total")
        if subtotal is not None and total is not None \
                and subtotal.normalized_value_bbox == total.normalized_value_bbox:
            from data_extraction import TemplateReviewIssue
            return TemplateReviewResult(
                verdict=TemplateReviewVerdict.REJECT,
                confidence=0.99,
                issues=(TemplateReviewIssue(
                    scope="field",
                    code="semantic_value_region_mismatch",
                    reason="TOTAL points to the same value region as Subtotal.",
                    confidence=0.99,
                    name="total",
                ),),
                reviewed_candidate_hash=context.candidate_hash,
                reviewer_name="SemanticMappingReviewer",
            )
        return TemplateReviewResult(
            verdict=TemplateReviewVerdict.PASS,
            confidence=0.99,
            issues=(),
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name="SemanticMappingReviewer",
        )


class SemanticRepairAI(AdaptiveTemplateAI):
    def refine_template(self, document, proposal, context) -> TemplateGenerationProposal:
        self.refine_calls += 1
        fields = []
        for field in proposal.fields:
            if field.name == "total":
                fields.append(replace(
                    field,
                    normalized_value_bbox=(0.78, 0.74, 0.93, 0.81),
                    normalized_label_bbox=(0.55, 0.74, 0.72, 0.81),
                ))
            else:
                fields.append(field)
        return replace(
            proposal,
            fields=tuple(fields),
            diagnostics=proposal.diagnostics + ("semantic_bbox_repaired",),
        )


def test_adaptive_reviewer_can_trigger_geometry_repair_without_full_generation() -> None:
    ocr = FullDocumentOCR(_invoice_text())
    ai = SemanticRepairAI()
    reviewer = SemanticMappingReviewer()
    options = TemplateExtractionOptions(
        max_refinement_attempts=1,
        adaptive_high_quality_threshold=0.40,
        adaptive_medium_quality_threshold=0.20,
        adaptive_minimum_geometry_precision=0.0,
        adaptive_minimum_schema_valid_ratio=0.15,
        adaptive_refine_after_reviewer_issue=True,
    )
    service = DataExtractionService(
        ocr_provider=ocr,
        field_ocr_providers=(ocr,),
        template_generation_provider=ai,
        template_review_provider=reviewer,
        template_options=options,
    )
    request = TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "adaptive-semantic-repair"),
        "nova-semantic-repair",
        required_fields=("invoice_number", "date", "total"),
        mode=TemplateBuildMode.ADAPTIVE,
    )
    result = service.build_template_candidate(request)

    assert result.route == TemplateAdaptiveRoute.DETERMINISTIC_REFINE_REVIEW
    assert ai.generate_calls == 0
    assert ai.refine_calls == 1
    assert reviewer.calls == 2
    assert result.review is not None
    assert result.review.verdict == TemplateReviewVerdict.PASS
    by_name = {field.name: field for field in result.candidate.fields}
    assert by_name["total"].normalized_value_bbox != by_name["subtotal"].normalized_value_bbox
    assert "semantic_bbox_repaired" in result.diagnostics


def test_adaptive_medium_candidate_retries_once_when_reviewer_rejects_refined_semantics() -> None:
    class MediumSemanticRepairAI(AdaptiveTemplateAI):
        def refine_template(self, document, proposal, context) -> TemplateGenerationProposal:
            self.refine_calls += 1
            fields = []
            for field in proposal.fields:
                if field.name == "subtotal":
                    value_bbox = (0.70, 0.60, 0.90, 0.65)
                    label_bbox = (0.50, 0.60, 0.68, 0.65)
                elif field.name == "total":
                    value_bbox = (
                        (0.70, 0.60, 0.90, 0.65)
                        if self.refine_calls == 1
                        else (0.70, 0.78, 0.90, 0.83)
                    )
                    label_bbox = (0.50, 0.78, 0.68, 0.83)
                elif field.name == "invoice_number":
                    value_bbox = (0.25, 0.15, 0.50, 0.20)
                    label_bbox = (0.05, 0.15, 0.22, 0.20)
                elif field.name == "date":
                    value_bbox = (0.70, 0.15, 0.90, 0.20)
                    label_bbox = (0.55, 0.15, 0.68, 0.20)
                elif field.name == "tax":
                    value_bbox = (0.70, 0.69, 0.90, 0.74)
                    label_bbox = (0.50, 0.69, 0.68, 0.74)
                else:
                    value_bbox = field.normalized_value_bbox
                    label_bbox = field.normalized_label_bbox
                fields.append(replace(
                    field,
                    normalized_value_bbox=value_bbox,
                    normalized_label_bbox=label_bbox,
                ))
            return replace(proposal, fields=tuple(fields))

    ocr = FullDocumentOCR(_invoice_text())
    ai = MediumSemanticRepairAI()
    reviewer = SemanticMappingReviewer()
    service = DataExtractionService(
        ocr_provider=ocr,
        field_ocr_providers=(ocr,),
        template_generation_provider=ai,
        template_review_provider=reviewer,
        template_options=TemplateExtractionOptions(max_refinement_attempts=1),
    )
    result = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "adaptive-medium-review-repair"),
        "nova-medium-review-repair",
        required_fields=("invoice_number", "date", "total"),
        mode=TemplateBuildMode.ADAPTIVE,
    ))

    assert result.route == TemplateAdaptiveRoute.DETERMINISTIC_REFINE_REVIEW
    assert ai.generate_calls == 0
    assert ai.refine_calls == 2
    assert reviewer.calls == 2
    assert result.review is not None
    assert result.review.verdict == TemplateReviewVerdict.PASS
    assert "adaptive_review_issue_refinement_retry" in result.diagnostics
