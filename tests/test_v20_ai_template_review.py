from __future__ import annotations

import io
import json
from dataclasses import replace

import httpx
import pytest
from PIL import Image

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    OpenAIResponsesTemplateOptions,
    OpenAIResponsesTemplateReviewer,
    TemplateApprovalRequest,
    TemplateBuildMode,
    TemplateBuildRequest,
    TemplateExtractionError,
    TemplateExtractionOptions,
    TemplateFieldDefinition,
    TemplateGenerationContext,
    TemplateGenerationProposal,
    TemplateReviewContext,
    TemplateReviewIssue,
    TemplateReviewResult,
    TemplateReviewVerdict,
)
from data_extraction.shared.models import ProviderTextResult


def _invoice_png(width: int = 1000, height: int = 1200) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class MoneyOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.width < 500 or image.height < 500:
                return ProviderTextResult("113.50", 0.97, provider_name="money-crop")
        return ProviderTextResult(
            "Invoice\nSubtotal 113.50\nVAT 5.68\nTOTAL 119.18",
            0.97,
            provider_name="money-page",
        )


class SemanticSwapGenerator:
    """Produces a schema-valid but semantically wrong TOTAL crop."""

    def generate_template(
        self, document: DocumentInput, context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        subtotal_bbox = (0.79, 0.64, 0.92, 0.685)
        return TemplateGenerationProposal(
            document_kind="invoice",
            fields=(
                TemplateFieldDefinition(
                    name="subtotal",
                    field_kind="money",
                    required=False,
                    page_number=1,
                    anchors=("Subtotal",),
                    pattern=r"\d+[.,]\d{2}",
                    normalized_value_bbox=subtotal_bbox,
                    normalized_label_bbox=(0.54, 0.575, 0.70, 0.61),
                    minimum_confidence=0.8,
                ),
                TemplateFieldDefinition(
                    name="total",
                    field_kind="money",
                    required=True,
                    page_number=1,
                    anchors=("TOTAL",),
                    pattern=r"\d+[.,]\d{2}",
                    # Deliberate semantic bug: TOTAL points to Subtotal value.
                    normalized_value_bbox=subtotal_bbox,
                    normalized_label_bbox=(0.54, 0.66, 0.70, 0.70),
                    minimum_confidence=0.8,
                ),
            ),
        )

    def refine_template(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context,
    ) -> TemplateGenerationProposal:
        return proposal


class SemanticSwapReviewer:
    def review_template(self, document, candidate, context) -> TemplateReviewResult:
        by_name = {field.name: field for field in candidate.fields}
        subtotal = by_name["subtotal"]
        total = by_name["total"]
        if subtotal.normalized_value_bbox == total.normalized_value_bbox:
            return TemplateReviewResult(
                verdict=TemplateReviewVerdict.REJECT,
                confidence=0.99,
                issues=(TemplateReviewIssue(
                    scope="field",
                    name="total",
                    code="semantic_value_region_mismatch",
                    reason="TOTAL value bbox points to the same value region as Subtotal.",
                    confidence=0.99,
                ),),
                reviewed_candidate_hash=context.candidate_hash,
                reviewer_name="SemanticSwapReviewer",
            )
        return TemplateReviewResult(
            verdict=TemplateReviewVerdict.PASS,
            confidence=0.99,
            issues=(),
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name="SemanticSwapReviewer",
        )


class PassReviewer:
    def __init__(self, confidence: float = 0.98) -> None:
        self.confidence = confidence

    def review_template(self, document, candidate, context) -> TemplateReviewResult:
        return TemplateReviewResult(
            verdict=TemplateReviewVerdict.PASS,
            confidence=self.confidence,
            issues=(),
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name="PassReviewer",
        )


class HumanReviewReviewer:
    def review_template(self, document, candidate, context) -> TemplateReviewResult:
        return TemplateReviewResult(
            verdict=TemplateReviewVerdict.REVIEW_REQUIRED,
            confidence=0.75,
            issues=(TemplateReviewIssue(
                scope="field",
                name="total",
                code="semantic_mapping_uncertain",
                reason="The visible label/value association is ambiguous.",
                confidence=0.75,
            ),),
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name="HumanReviewReviewer",
        )


def _service(reviewer) -> DataExtractionService:
    ocr = MoneyOCR()
    return DataExtractionService(
        ocr_provider=ocr,
        field_ocr_providers=(ocr,),
        template_generation_provider=SemanticSwapGenerator(),
        template_review_provider=reviewer,
        template_options=TemplateExtractionOptions(max_refinement_attempts=1),
    )


def _build(service: DataExtractionService):
    return service.build_template_candidate(TemplateBuildRequest(
        document=DocumentInput("invoice.png", _invoice_png(), "invoice-review"),
        family="nova-invoice",
        required_fields=("total",),
        mode=TemplateBuildMode.AI_REQUIRED,
    ))


def test_independent_reviewer_catches_semantic_swap_that_core_schema_validation_misses() -> None:
    service = _service(SemanticSwapReviewer())
    built = _build(service)

    # The deterministic crop/schema check sees valid money and therefore passes.
    assert "template_geometry_self_validation_passed" in built.diagnostics
    # The independent semantic reviewer still catches the wrong label/value mapping.
    assert built.review is not None
    assert built.review.verdict == TemplateReviewVerdict.REJECT
    assert built.review.issues[0].name == "total"
    assert "template_ai_review_rejected" in built.diagnostics

    with pytest.raises(TemplateExtractionError) as error:
        service.approve_template(TemplateApprovalRequest(built.candidate, "reviewer"))
    assert error.value.code == "TEMPLATE_AI_REVIEW_REJECTED"


def test_passing_independent_review_allows_explicit_human_approval() -> None:
    service = _service(PassReviewer())
    built = _build(service)
    assert built.review is not None
    assert built.review.verdict == TemplateReviewVerdict.PASS
    approved = service.approve_template(TemplateApprovalRequest(built.candidate, "human-reviewer"))
    assert approved.status.value == "approved"
    assert approved.approved_by == "human-reviewer"


def test_review_required_is_triaged_to_human_but_does_not_block_explicit_human_approval() -> None:
    service = _service(HumanReviewReviewer())
    built = _build(service)
    assert built.review is not None
    assert built.review.verdict == TemplateReviewVerdict.REVIEW_REQUIRED
    assert "template_ai_review_requires_human" in built.diagnostics
    approved = service.approve_template(TemplateApprovalRequest(built.candidate, "human-reviewer"))
    assert approved.status.value == "approved"


def test_low_confidence_pass_is_downgraded_to_human_review() -> None:
    service = _service(PassReviewer(confidence=0.50))
    built = _build(service)
    assert built.review is not None
    assert built.review.verdict == TemplateReviewVerdict.REVIEW_REQUIRED
    assert "template_ai_review_low_confidence_downgraded" in built.review.diagnostics


def test_approval_rejects_review_for_different_candidate_hash() -> None:
    service = _service(PassReviewer())
    built = _build(service)
    assert built.review is not None
    stale = replace(built.review, reviewed_candidate_hash="other-hash")
    with pytest.raises(TemplateExtractionError) as error:
        service.approve_template(TemplateApprovalRequest(
            candidate=built.candidate,
            approved_by="human-reviewer",
            review=stale,
        ))
    assert error.value.code == "TEMPLATE_REVIEW_HASH_MISMATCH"


def test_openai_reviewer_uses_separate_visual_semantic_review_contract() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        review_payload = {
            "verdict": "reject",
            "confidence": 0.99,
            "issues": [{
                "scope": "field",
                "name": "total",
                "code": "semantic_value_region_mismatch",
                "reason": "TOTAL bbox is aligned with Subtotal instead of TOTAL.",
                "confidence": 0.99,
            }],
        }
        return httpx.Response(
            200,
            json={"id": "resp-review-1", "status": "completed", "output_text": json.dumps(review_payload)},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    base_service = DataExtractionService(
        ocr_provider=MoneyOCR(),
        field_ocr_providers=(MoneyOCR(),),
        template_generation_provider=SemanticSwapGenerator(),
        template_options=TemplateExtractionOptions(auto_review_template_candidates=False),
    )
    built = _build(base_service)

    reviewer = OpenAIResponsesTemplateReviewer(
        api_key="test-key",
        options=OpenAIResponsesTemplateOptions(model="gpt-review-test"),
        http_client=client,
    )
    context = TemplateReviewContext(
        family=built.candidate.family,
        source_id=built.candidate.source_id,
        source_sha256=built.candidate.source_sha256,
        candidate_hash=built.candidate.content_hash,
        page_text=("Subtotal 113.50 VAT 5.68 TOTAL 119.18",),
        page_sizes=((1000.0, 1200.0),),
    )
    review = reviewer.review_template(
        DocumentInput("invoice.png", _invoice_png(), "invoice-openai-review"),
        built.candidate,
        context,
    )

    assert review.verdict == TemplateReviewVerdict.REJECT
    assert review.issues[0].name == "total"
    assert review.reviewed_candidate_hash == built.candidate.content_hash
    assert len(captured) == 1
    request_payload = captured[0]
    assert request_payload["model"] == "gpt-review-test"
    assert request_payload["store"] is False
    assert request_payload["text"]["format"]["name"] == "document_template_semantic_review"  # type: ignore[index]
    user_content = request_payload["input"][1]["content"]  # type: ignore[index]
    assert any(item.get("type") == "input_image" for item in user_content)
    prompt = next(item["text"] for item in user_content if item.get("type") == "input_text")
    assert "Candidate hash" in prompt
    assert "schema-valid numeric crop is NOT enough" in prompt
    system_prompt = request_payload["input"][0]["content"][0]["text"]  # type: ignore[index]
    assert "independent adversarial reviewer" in system_prompt
    assert "Subtotal, VAT, and TOTAL" in system_prompt
    client.close()
