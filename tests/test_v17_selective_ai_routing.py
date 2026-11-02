from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    ExtractionOptions,
    TemplateBuildMode,
    TemplateBuildRequest,
)
from data_extraction.shared.models import ExtractionMethod, ProviderTextResult


def _png() -> bytes:
    image: Image.Image = Image.new("RGB", (800, 1000), "white")
    stream: io.BytesIO = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


class StrongPlainOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("This is ordinary readable paragraph text.", 0.98, provider_name="strong-plain")


class LowOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("blurry txt", 0.31, provider_name="low-ocr")


class StrongInvoiceOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "ACME MARKET\nTax Invoice\nInvoice No INV-2026-001\nDate 01/11/2026\nSubtotal 100.00\nVAT 5.00\nTOTAL 105.00",
            0.97,
            provider_name="strong-invoice",
        )


def _valid_template_proposal() -> dict[str, object]:
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
            }
        ],
        "tables": [],
        "anchors": [
            {"text": "Tax Invoice", "page_number": 1, "normalized_bbox": [0.35, 0.04, 0.65, 0.09], "required": True}
        ],
        "diagnostics": ["ai_template"],
    }


def _client(captured: list[dict[str, object]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        payload: dict[str, object] = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        text_section: dict[str, object] = payload["text"]  # type: ignore[assignment]
        format_section: dict[str, object] = text_section["format"]  # type: ignore[assignment]
        schema_name: str = str(format_section["name"])
        if schema_name == "document_region_extraction":
            output = {
                "text": "AI recovered exact visible content",
                "confidence": 0.93,
                "table": None,
            }
        elif schema_name == "document_template_candidate":
            output = _valid_template_proposal()
        else:
            raise AssertionError(f"unexpected schema {schema_name}")
        return httpx.Response(
            200,
            json={"status": "completed", "id": "resp-test", "output_text": json.dumps(output)},
            request=request,
        )
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_default_selective_ai_does_not_call_model_for_strong_plain_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[dict[str, object]] = []
    client = _client(captured)
    service = DataExtractionService(ocr_provider=StrongPlainOCR(), ai_http_client=client)
    result = service.extract(DocumentInput("page.png", _png(), "plain-1"))
    assert captured == []
    assert result.pages[0].regions[0].method == ExtractionMethod.OCR
    assert "selective_ai_routing_enabled" in result.diagnostics
    client.close()


def test_default_selective_ai_calls_model_after_low_confidence_ocr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[dict[str, object]] = []
    client = _client(captured)
    service = DataExtractionService(ocr_provider=LowOCR(), ai_http_client=client)
    result = service.extract(DocumentInput("page.png", _png(), "low-1"))
    assert len(captured) == 1
    assert result.pages[0].regions[0].method == ExtractionMethod.VISION_MODEL
    assert result.total_vision_calls == 1
    client.close()


def test_strong_flat_invoice_uses_ai_for_missing_structure_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[dict[str, object]] = []
    client = _client(captured)
    service = DataExtractionService(ocr_provider=StrongInvoiceOCR(), ai_http_client=client)
    result = service.extract(DocumentInput("invoice.png", _png(), "invoice-1"))
    assert len(captured) == 1
    assert result.pages[0].regions[0].method == ExtractionMethod.VISION_MODEL
    client.close()


def test_auto_ai_can_be_disabled_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[dict[str, object]] = []
    client = _client(captured)
    service = DataExtractionService(
        ExtractionOptions(auto_enable_ai=False),
        ocr_provider=LowOCR(),
        ai_http_client=client,
    )
    result = service.extract(DocumentInput("page.png", _png(), "disabled-1"))
    assert captured == []
    assert result.pages[0].regions[0].method == ExtractionMethod.OCR
    assert result.pages[0].regions[0].requires_review is True
    client.close()


def test_template_ai_provider_is_auto_configured_from_same_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    captured: list[dict[str, object]] = []
    client = _client(captured)
    service = DataExtractionService(
        ExtractionOptions(auto_ai_for_structured_images=False),
        ocr_provider=StrongInvoiceOCR(),
        ai_http_client=client,
    )
    result = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "invoice-template-1"),
        family="acme-invoice",
        required_fields=("invoice_number",),
        mode=TemplateBuildMode.AI_REQUIRED,
    ))
    assert result.candidate.generator_name == "OpenAIResponsesTemplateProvider"
    assert tuple(field.name for field in result.candidate.fields) == ("invoice_number",)
    assert any(
        payload["text"]["format"]["name"] == "document_template_candidate"  # type: ignore[index]
        for payload in captured
    )
    client.close()


def test_native_text_pdf_does_not_call_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    import fitz

    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((60, 100), "Native text is already reliable and should stay local.", fontsize=14)
    content: bytes = document.tobytes()
    document.close()
    captured: list[dict[str, object]] = []
    client = _client(captured)
    service = DataExtractionService(ai_http_client=client)
    result = service.extract(DocumentInput("native.pdf", content, "native-pdf-1"))
    assert captured == []
    assert any(region.method == ExtractionMethod.NATIVE for region in result.pages[0].regions)
    client.close()
