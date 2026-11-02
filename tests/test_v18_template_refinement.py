from __future__ import annotations

import io
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    TemplateBuildMode,
    TemplateBuildRequest,
    TemplateExtractionError,
    TemplateExtractionOptions,
    TemplateFieldDefinition,
    TemplateGenerationContext,
    TemplateGenerationProposal,
)
from data_extraction.shared.models import ProviderTextResult
from data_extraction.template_extraction.contracts import TemplateRefinementContext


def _invoice_png() -> bytes:
    image = Image.new("RGB", (1000, 1000), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 100), "Invoice No", fill="black")
    draw.text((650, 100), "INV-2026-002", fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class FullPageOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("Invoice No INV-2026-002", .95, provider_name="full-page")


class GeometrySensitiveOCR:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        with Image.open(io.BytesIO(image_bytes)) as image:
            width = image.width
        if width < 180:
            return ProviderTextResult("/ INV-2026-002", .96, provider_name=self.name)
        return ProviderTextResult("INV-2026-002", .96, provider_name=self.name)


class RefiningAI:
    def __init__(self) -> None:
        self.generate_calls = 0
        self.refine_calls = 0
        self.feedback: list[TemplateRefinementContext] = []

    def generate_template(
        self, document: DocumentInput, context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        self.generate_calls += 1
        return TemplateGenerationProposal(
            document_kind="invoice",
            fields=(
                TemplateFieldDefinition(
                    name="invoice_number",
                    field_kind="invoice_number",
                    required=True,
                    page_number=1,
                    anchors=("Invoice No",),
                    pattern=r"INV-\d{4}-\d{3}",
                    normalized_value_bbox=(0.64, 0.08, 0.72, 0.14),
                    normalized_label_bbox=(0.06, 0.08, 0.20, 0.14),
                    minimum_confidence=.90,
                ),
            ),
        )

    def refine_template(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context: TemplateRefinementContext,
    ) -> TemplateGenerationProposal:
        self.refine_calls += 1
        self.feedback.append(context)
        field = proposal.fields[0]
        return replace(
            proposal,
            fields=(replace(field, normalized_value_bbox=(0.61, 0.075, 0.88, 0.145)),),
            diagnostics=("refined_invoice_number_geometry",),
        )


class NeverFixedAI(RefiningAI):
    def refine_template(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context: TemplateRefinementContext,
    ) -> TemplateGenerationProposal:
        self.refine_calls += 1
        self.feedback.append(context)
        return replace(proposal, diagnostics=("kept_bad_geometry",))


class SemanticMutationAI(RefiningAI):
    def refine_template(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context: TemplateRefinementContext,
    ) -> TemplateGenerationProposal:
        self.refine_calls += 1
        field = proposal.fields[0]
        return replace(
            proposal,
            fields=(replace(field, pattern=r".*", normalized_value_bbox=(0.61, 0.075, 0.88, 0.145)),),
        )


def _service(generator, *, max_attempts: int = 2) -> DataExtractionService:
    return DataExtractionService(
        ocr_provider=FullPageOCR(),
        field_ocr_providers=(GeometrySensitiveOCR("crop-a"), GeometrySensitiveOCR("crop-b")),
        template_generation_provider=generator,
        template_options=TemplateExtractionOptions(max_refinement_attempts=max_attempts),
    )


def test_ai_candidate_is_crop_validated_and_geometry_refined_before_approval() -> None:
    ai = RefiningAI()
    service = _service(ai)
    built = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _invoice_png(), "invoice-1"),
        "nova-invoice",
        required_fields=("invoice_number",),
        mode=TemplateBuildMode.AI_REQUIRED,
    ))
    field = built.candidate.fields[0]
    assert ai.generate_calls == 1
    assert ai.refine_calls == 1
    assert ai.feedback[0].feedback[0].field_name == "invoice_number"
    assert ai.feedback[0].feedback[0].status == "geometry_review_required"
    assert field.normalized_value_bbox == (0.61, 0.075, 0.88, 0.145)
    assert "template_geometry_refinement_attempt:1" in built.diagnostics
    assert "template_geometry_self_validation_passed" in built.diagnostics
    assert built.candidate.status.value == "candidate"


def test_refinement_is_bounded_and_bad_geometry_remains_unapproved_candidate() -> None:
    ai = NeverFixedAI()
    service = _service(ai, max_attempts=2)
    built = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _invoice_png(), "invoice-2"),
        "nova-invoice",
        required_fields=("invoice_number",),
        mode=TemplateBuildMode.AI_REQUIRED,
    ))
    assert ai.refine_calls == 2
    assert "template_geometry_refinement_attempts_exhausted" in built.diagnostics
    assert "template_geometry_self_validation_incomplete" in built.diagnostics
    assert built.candidate.status.value == "candidate"


def test_refinement_cannot_change_regex_or_semantic_contract() -> None:
    service = _service(SemanticMutationAI())
    with pytest.raises(TemplateExtractionError) as error:
        service.build_template_candidate(TemplateBuildRequest(
            DocumentInput("invoice.png", _invoice_png(), "invoice-3"),
            "nova-invoice",
            required_fields=("invoice_number",),
            mode=TemplateBuildMode.AI_REQUIRED,
        ))
    assert error.value.code == "TEMPLATE_REFINEMENT_SEMANTIC_CHANGE"


def test_deterministic_template_build_skips_ai_refinement() -> None:
    ai = RefiningAI()
    service = _service(ai)
    built = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _invoice_png(), "invoice-4"),
        "bootstrap",
        mode=TemplateBuildMode.DETERMINISTIC,
    ))
    assert ai.generate_calls == 0
    assert ai.refine_calls == 0
    assert "template_bootstrap_without_ai_requires_approval" in built.diagnostics


def test_openai_refinement_request_contains_visual_feedback_and_strict_schema() -> None:
    import json
    import httpx

    from data_extraction.integrations.template_generation import (
        OpenAIResponsesTemplateOptions,
        OpenAIResponsesTemplateProvider,
    )
    from data_extraction.template_extraction.contracts import TemplateRefinementFieldFeedback

    captured: list[dict[str, object]] = []
    refined_payload = {
        "document_kind": "invoice",
        "fields": [
            {
                "name": "invoice_number",
                "field_kind": "invoice_number",
                "required": True,
                "page_number": 1,
                "anchors": ["Invoice No"],
                "pattern": "INV-\\d{4}-\\d{3}",
                "mode": "label_relative",
                "normalized_value_bbox": [0.61, 0.075, 0.88, 0.145],
                "normalized_label_bbox": [0.06, 0.08, 0.20, 0.14],
                "template": None,
                "minimum_confidence": 0.9,
            }
        ],
        "tables": [],
        "anchors": [],
        "diagnostics": ["geometry_refined"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        return httpx.Response(
            200,
            json={"status": "completed", "output_text": json.dumps(refined_payload)},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAIResponsesTemplateProvider(
        api_key="test-key",
        options=OpenAIResponsesTemplateOptions(model="gpt-test"),
        http_client=client,
    )
    proposal = RefiningAI().generate_template(
        DocumentInput("invoice.png", _invoice_png(), "invoice-openai"),
        TemplateGenerationContext(
            family="nova-invoice",
            source_id="invoice-openai",
            source_sha256="abc",
            required_fields=("invoice_number",),
            page_count=1,
            document_kind="invoice",
            page_text=("Invoice No INV-2026-002",),
            page_sizes=((1000.0, 1000.0),),
        ),
    )
    context = TemplateRefinementContext(
        generation=TemplateGenerationContext(
            family="nova-invoice",
            source_id="invoice-openai",
            source_sha256="abc",
            required_fields=("invoice_number",),
            page_count=1,
            document_kind="invoice",
            page_text=("Invoice No INV-2026-002",),
            page_sizes=((1000.0, 1000.0),),
        ),
        attempt=1,
        max_attempts=2,
        feedback=(TemplateRefinementFieldFeedback(
            field_name="invoice_number",
            page_number=1,
            current_value_bbox=(0.64, 0.08, 0.72, 0.14),
            current_label_bbox=(0.06, 0.08, 0.20, 0.14),
            status="geometry_review_required",
            candidate_values=("/ INV-2026-002",),
            schema_valid_values=(),
            review_reasons=("no_schema_valid_candidate",),
        ),),
    )
    refined = provider.refine_template(
        DocumentInput("invoice.png", _invoice_png(), "invoice-openai"), proposal, context
    )
    assert refined.fields[0].normalized_value_bbox == (0.61, 0.075, 0.88, 0.145)
    assert len(captured) == 1
    request_payload = captured[0]
    assert request_payload["model"] == "gpt-test"
    assert request_payload["store"] is False
    assert request_payload["text"]["format"]["name"] == "document_template_geometry_refinement"  # type: ignore[index]
    user_content = request_payload["input"][1]["content"]  # type: ignore[index]
    assert any(item.get("type") == "input_image" for item in user_content)
    prompt = next(item["text"] for item in user_content if item.get("type") == "input_text")
    assert "no_schema_valid_candidate" in prompt
    assert "Change geometry only" in request_payload["input"][0]["content"][0]["text"]  # type: ignore[index]
    client.close()
