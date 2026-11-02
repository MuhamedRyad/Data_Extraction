from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    TemplateBuildMode,
    TemplateBuildRequest,
    TemplateExtractionError,
    TemplateExtractionOptions,
)
from data_extraction.integrations.template_generation import (
    OpenAIResponsesTemplateOptions,
    OpenAIResponsesTemplateProvider,
)
from data_extraction.providers.contracts import OCRTextRegion
from data_extraction.shared.models import ProviderTextResult


def _png(width: int = 900, height: int = 1400) -> bytes:
    image: Image.Image = Image.new("RGB", (width, height), "white")
    output: io.BytesIO = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class InvoiceOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "ACME MARKET\nTax Invoice\nInvoice No INV-2026-001\nDate 01/11/2026\nSubtotal 100.00\nVAT 5.00\nTOTAL 105.00",
            0.96,
            provider_name="invoice-ocr",
        )

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("Invoice No", (80, 200, 200, 235), .96),
            OCRTextRegion("INV-2026-001", (620, 200, 790, 235), .96),
            OCRTextRegion("TOTAL", (80, 1100, 170, 1140), .97),
            OCRTextRegion("105.00", (650, 1100, 760, 1140), .96),
        )


def _valid_proposal() -> dict[str, object]:
    return {
        "document_kind": "invoice",
        "fields": [
            {
                "name": "invoice_number",
                "field_kind": "identifier",
                "required": True,
                "page_number": 1,
                "anchors": ["Invoice No"],
                "pattern": "^INV-[0-9]{4}-[0-9]{3}$",
                "mode": "region",
                "normalized_value_bbox": [0.68, 0.13, 0.90, 0.18],
                "normalized_label_bbox": [0.08, 0.13, 0.25, 0.18],
                "template": None,
                "minimum_confidence": 0.90,
            },
            {
                "name": "total",
                "field_kind": "total",
                "required": True,
                "page_number": 1,
                "anchors": ["TOTAL"],
                "pattern": "^[0-9]+[.][0-9]{2}$",
                "mode": "region",
                "normalized_value_bbox": [0.70, 0.76, 0.90, 0.83],
                "normalized_label_bbox": [0.08, 0.76, 0.22, 0.83],
                "template": None,
                "minimum_confidence": 0.90,
            },
        ],
        "tables": [
            {
                "name": "line_items",
                "page_number": 1,
                "normalized_bbox": [0.06, 0.30, 0.94, 0.70],
                "header_anchors": ["Description", "Qty", "Amount"],
                "columns": ["Description", "Qty", "Amount"],
                "repeating_rows": True,
                "required": False,
                "normalized_header_bbox": [0.06, 0.30, 0.94, 0.36],
                "normalized_body_bbox": [0.06, 0.36, 0.94, 0.70],
                "column_definitions": [
                    {"name": "Description", "normalized_x0": 0.0, "normalized_x1": 0.60, "value_kind": "text", "required": True},
                    {"name": "Qty", "normalized_x0": 0.60, "normalized_x1": 0.75, "value_kind": "integer", "required": True},
                    {"name": "Amount", "normalized_x0": 0.75, "normalized_x1": 1.0, "value_kind": "decimal", "required": True},
                ],
                "stop_anchors": ["Subtotal", "TOTAL"],
                "minimum_rows": 1,
            }
        ],
        "anchors": [
            {"text": "Tax Invoice", "page_number": 1, "normalized_bbox": [0.35, 0.04, 0.65, 0.09], "required": True},
            {"text": "TOTAL", "page_number": 1, "normalized_bbox": [0.08, 0.76, 0.22, 0.83], "required": True},
        ],
        "diagnostics": ["vision_model_found_stable_invoice_layout"],
    }


def _mock_client(proposal: dict[str, object]) -> tuple[httpx.Client, list[dict[str, object]]]:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        return httpx.Response(
            200,
            json={"status": "completed", "output_text": json.dumps(proposal)},
            request=request,
        )

    client: httpx.Client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, captured


def test_ai_required_fails_closed_when_provider_is_missing() -> None:
    service: DataExtractionService = DataExtractionService(ocr_provider=InvoiceOCR())
    request: TemplateBuildRequest = TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "invoice-1"),
        "acme-invoice",
        required_fields=("invoice_number", "total"),
        mode=TemplateBuildMode.AI_REQUIRED,
    )
    with pytest.raises(TemplateExtractionError) as error:
        service.build_template_candidate(request)
    assert error.value.code == "TEMPLATE_AI_PROVIDER_REQUIRED"


def test_openai_responses_provider_sends_visual_and_strict_structured_schema() -> None:
    client, captured = _mock_client(_valid_proposal())
    provider: OpenAIResponsesTemplateProvider = OpenAIResponsesTemplateProvider(
        api_key="test-key",
        options=OpenAIResponsesTemplateOptions(model="gpt-test"),
        http_client=client,
    )
    service: DataExtractionService = DataExtractionService(
        ocr_provider=InvoiceOCR(), template_generation_provider=provider,
        template_options=TemplateExtractionOptions(auto_refine_ai_templates=False),
    )
    result = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "invoice-1"),
        "acme-invoice",
        required_fields=("invoice_number", "total"),
        mode=TemplateBuildMode.AI_REQUIRED,
    ))
    assert result.candidate.generator_name == "OpenAIResponsesTemplateProvider"
    assert result.candidate.document_kind == "invoice"
    assert tuple(field.name for field in result.candidate.fields) == ("invoice_number", "total")
    assert result.candidate.tables[0].name == "line_items"
    assert result.candidate.tables[0].normalized_body_bbox == (0.06, 0.36, 0.94, 0.70)
    assert tuple(column.name for column in result.candidate.tables[0].column_definitions) == ("Description", "Qty", "Amount")
    assert result.candidate.tables[0].stop_anchors == ("Subtotal", "TOTAL")
    assert "template_generated_by_ai_provider_requires_approval" in result.diagnostics

    assert len(captured) == 1
    payload: dict[str, object] = captured[0]
    assert payload["model"] == "gpt-test"
    text: dict[str, object] = payload["text"]  # type: ignore[assignment]
    format_payload: dict[str, object] = text["format"]  # type: ignore[assignment]
    assert format_payload["type"] == "json_schema"
    assert format_payload["strict"] is True
    inputs: list[dict[str, object]] = payload["input"]  # type: ignore[assignment]
    user_content: list[dict[str, object]] = inputs[1]["content"]  # type: ignore[assignment]
    assert any(item.get("type") == "input_image" for item in user_content)
    client.close()


def test_ai_required_field_region_collision_is_rejected_by_core() -> None:
    proposal: dict[str, object] = _valid_proposal()
    fields: list[dict[str, object]] = proposal["fields"]  # type: ignore[assignment]
    fields[1]["normalized_value_bbox"] = fields[0]["normalized_value_bbox"]
    client, _ = _mock_client(proposal)
    provider: OpenAIResponsesTemplateProvider = OpenAIResponsesTemplateProvider(
        api_key="test-key", http_client=client
    )
    service: DataExtractionService = DataExtractionService(
        ocr_provider=InvoiceOCR(), template_generation_provider=provider
    )
    with pytest.raises(TemplateExtractionError) as error:
        service.build_template_candidate(TemplateBuildRequest(
            DocumentInput("invoice.png", _png(), "invoice-1"),
            "acme-invoice",
            required_fields=("invoice_number", "total"),
            mode=TemplateBuildMode.AI_REQUIRED,
        ))
    assert error.value.code == "TEMPLATE_REQUIRED_FIELD_REGION_COLLISION"
    client.close()


def test_ai_page_reference_outside_document_is_rejected() -> None:
    proposal: dict[str, object] = _valid_proposal()
    fields: list[dict[str, object]] = proposal["fields"]  # type: ignore[assignment]
    fields[0]["page_number"] = 2
    client, _ = _mock_client(proposal)
    provider: OpenAIResponsesTemplateProvider = OpenAIResponsesTemplateProvider(
        api_key="test-key", http_client=client
    )
    service: DataExtractionService = DataExtractionService(
        ocr_provider=InvoiceOCR(), template_generation_provider=provider
    )
    with pytest.raises(TemplateExtractionError) as error:
        service.build_template_candidate(TemplateBuildRequest(
            DocumentInput("invoice.png", _png(), "invoice-1"),
            "acme-invoice",
            required_fields=("invoice_number", "total"),
            mode=TemplateBuildMode.AI_REQUIRED,
        ))
    assert error.value.code == "TEMPLATE_FIELD_PAGE_OUT_OF_RANGE"
    client.close()


def test_openai_provider_rejects_refusal_instead_of_creating_empty_template() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "cannot comply"}]}],
            },
            request=request,
        )

    client: httpx.Client = httpx.Client(transport=httpx.MockTransport(handler))
    provider: OpenAIResponsesTemplateProvider = OpenAIResponsesTemplateProvider(
        api_key="test-key", http_client=client
    )
    service: DataExtractionService = DataExtractionService(
        ocr_provider=InvoiceOCR(), template_generation_provider=provider
    )
    with pytest.raises(TemplateExtractionError) as error:
        service.build_template_candidate(TemplateBuildRequest(
            DocumentInput("invoice.png", _png(), "invoice-1"),
            "acme-invoice",
            mode=TemplateBuildMode.AI_REQUIRED,
        ))
    assert error.value.code == "TEMPLATE_AI_REFUSED"
    client.close()
