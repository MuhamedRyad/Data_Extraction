from __future__ import annotations

from dataclasses import replace
import io
import json

import fitz
import httpx
import pytest
from PIL import Image, ImageDraw, ImageFont

from data_extraction import DataExtractionService, DocumentInput
from data_extraction.integrations.template_generation import (
    OpenAIResponsesTemplateOptions,
    OpenAIResponsesTemplateProvider,
)
from data_extraction.integrations.template_store import SQLiteTemplateStore
from data_extraction.shared.models import ProviderTextResult
from data_extraction.template_extraction import plan_page_batches
from data_extraction.template_extraction.contracts import (
    DocumentTemplate,
    TemplateAnchor,
    TemplateFieldDefinition,
    TemplateFieldMode,
    TemplateGenerationContext,
    TemplateLayoutFingerprint,
    TemplateReviewContext,
    TemplateStatus,
)
from data_extraction.template_extraction.request import TemplateExtractionRequest
from data_extraction.template_extraction.validation import template_content_hash


_FONT = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 34)
_SMALL = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)


def _page(items: tuple[tuple[str, tuple[int, int], ImageFont.FreeTypeFont], ...]) -> bytes:
    image = Image.new("RGB", (700, 1000), "white")
    painter = ImageDraw.Draw(image)
    for text, xy, font in items:
        painter.text(xy, text, fill="black", font=font)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _pdf(invoice_number: str, amount: str, label: str = "TOTAL", pages: int = 3) -> bytes:
    images = [
        _page((("MULTI PAGE INVOICE", (120, 100), _FONT), ("Invoice No", (70, 240), _SMALL), (invoice_number, (330, 235), _FONT))),
        _page((("PAGE 2 - FINANCIAL", (90, 80), _SMALL), (label, (420, 760), _FONT), (amount, (420, 830), _FONT))),
        _page((("PAGE 3 - DELIVERY", (90, 80), _SMALL), ("DELIVERY NOTE", (160, 420), _FONT))),
    ][:pages]
    pdf = fitz.open()
    for image in images:
        page = pdf.new_page(width=700, height=1000)
        page.insert_image(page.rect, stream=image)
    content = pdf.tobytes()
    pdf.close()
    return content


class _SequentialFieldOCR:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        value = "INV-2026-002" if self.calls % 2 == 1 else "999.99"
        return ProviderTextResult(value, 0.99, provider_name=self.name)


class _CountingGenericOCR:
    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        return ProviderTextResult("SALARY 999.99", 0.99, provider_name="generic-counting")


def _approved_template(service: DataExtractionService, source: DocumentInput) -> DocumentTemplate:
    extraction = service.extract(source)
    invoice_label = (0.09, 0.21, 0.30, 0.30)
    total_label = (0.56, 0.73, 0.84, 0.82)
    anchors = (
        TemplateAnchor("Invoice No", 1, invoice_label, True),
        TemplateAnchor("TOTAL", 2, total_label, True),
    )
    fingerprint = service._template_feature._fingerprint(extraction, anchors, source)
    fields = (
        TemplateFieldDefinition(
            "invoice_number", "invoice_number", True, 1, ("Invoice No",), r"INV-\d{4}-\d{3}",
            TemplateFieldMode.LABEL_RELATIVE, (0.43, 0.20, 0.85, 0.31), invoice_label, None, 0.80,
        ),
        TemplateFieldDefinition(
            "total", "money", True, 2, ("TOTAL",), r"\d+[.]\d{2}",
            TemplateFieldMode.LABEL_RELATIVE, (0.56, 0.80, 0.88, 0.92), total_label, None, 0.80,
        ),
    )
    template = DocumentTemplate(
        "multi-v22", "multi-v22", 1, TemplateStatus.APPROVED, "general", fields, (), fingerprint,
        extraction.manifest.sha256, source.source_id, "test", "",
    )
    return replace(template, content_hash=template_content_hash(template))


def test_same_structure_new_values_uses_fast_path_without_stale_values() -> None:
    generic = _CountingGenericOCR()
    field_a = _SequentialFieldOCR("field-a")
    field_b = _SequentialFieldOCR("field-b")
    service = DataExtractionService(
        ocr_provider=generic,
        field_ocr_providers=(field_a, field_b),
    )
    first = DocumentInput("first.pdf", _pdf("INV-2026-001", "123.45"), "first")
    template = _approved_template(service, first)
    generic.calls = 0
    field_a.calls = 0
    field_b.calls = 0

    repeated = DocumentInput("second.pdf", _pdf("INV-2026-002", "999.99"), "second")
    result = service.extract_with_template(TemplateExtractionRequest(repeated, (template,), "multi-v22"))

    assert result.match.status.value == "accepted"
    assert result.generic_extraction_performed is False
    assert result.generic_extraction_pages == ()
    assert set(result.template_fast_path_pages) == {1, 2}
    values = {field.name: field.value for field in result.fields}
    assert values == {"invoice_number": "INV-2026-002", "total": "999.99"}
    assert generic.calls == 0


def test_changed_later_page_is_blocked_and_only_that_page_is_generically_extracted() -> None:
    generic = _CountingGenericOCR()
    field_a = _SequentialFieldOCR("field-a")
    field_b = _SequentialFieldOCR("field-b")
    service = DataExtractionService(
        ocr_provider=generic,
        field_ocr_providers=(field_a, field_b),
    )
    first = DocumentInput("first.pdf", _pdf("INV-2026-001", "123.45"), "first")
    template = _approved_template(service, first)
    generic.calls = 0
    field_a.calls = 0
    field_b.calls = 0

    changed = DocumentInput("changed.pdf", _pdf("INV-2026-002", "999.99", label="SALARY"), "changed")
    preflight = service._template_feature.preflight_match(service.inspect(changed), changed, (template,), "multi-v22")
    assert preflight.status.value == "review_required"
    assert preflight.changed_pages == (2,)
    assert preflight.stable_pages == (1,)

    result = service.extract_with_template(TemplateExtractionRequest(changed, (template,), "multi-v22"))
    fields = {field.name: field for field in result.fields}
    assert result.generic_extraction_pages == (2,)
    assert result.template_fast_path_pages == (1,)
    assert fields["invoice_number"].value == "INV-2026-002"
    assert fields["invoice_number"].verified is True
    assert fields["total"].value is None
    assert fields["total"].status == "template_page_drift"
    assert result.semantic_complete is False
    assert result.requires_review is True
    # Only the changed raster page enters generic OCR; page 1 and page 3 are placeholders.
    assert generic.calls == 1
    assert result.extraction.pages[0].regions == ()
    assert result.extraction.pages[1].regions
    assert result.extraction.pages[2].regions == ()


def test_page_batch_planner_keeps_consecutive_context_and_bounds_calls() -> None:
    batches = plan_page_batches(
        tuple(range(1, 10)), max_pages_per_batch=4, max_batches=3, reason="test"
    )
    assert tuple(item.page_numbers for item in batches) == ((1, 2, 3, 4), (5, 6, 7, 8), (9,))


def test_legacy_multi_page_template_without_page_visual_evidence_fails_closed() -> None:
    service = DataExtractionService(
        ocr_provider=_CountingGenericOCR(),
        field_ocr_providers=(_SequentialFieldOCR("a"), _SequentialFieldOCR("b")),
    )
    first = DocumentInput("first.pdf", _pdf("INV-2026-001", "123.45"), "first")
    current = DocumentInput("second.pdf", _pdf("INV-2026-002", "999.99"), "second")
    template = _approved_template(service, first)
    legacy_fingerprint = replace(
        template.fingerprint,
        page_visual_hashes=(),
        anchors=tuple(replace(anchor, visual_hash=None) for anchor in template.fingerprint.anchors),
    )
    legacy = replace(template, fingerprint=legacy_fingerprint, content_hash="")
    legacy = replace(legacy, content_hash=template_content_hash(legacy))
    preflight = service._template_feature.preflight_match(
        service.inspect(current), current, (legacy,), "multi-v22"
    )
    assert preflight.status.value != "accepted"
    assert 2 in preflight.changed_pages
    assert any(
        "page_required_anchor_visual_evidence_missing" in item.diagnostics
        or "page_visual_fingerprint_missing" in item.diagnostics
        for item in preflight.page_matches if item.page_number == 2
    )


def test_openai_template_generation_batches_all_nine_pages() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        proposal = {
            "document_kind": "general",
            "fields": [], "tables": [], "anchors": [], "diagnostics": [],
        }
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(proposal)}, request=request)

    provider = OpenAIResponsesTemplateProvider(
        api_key="test",
        options=OpenAIResponsesTemplateOptions(max_pages_per_batch=4, max_ai_batches_per_document=3),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    document = DocumentInput("nine.pdf", _pdf("INV-2026-001", "1.00", pages=3) + b"", "nine")
    # Build a real 9-page PDF separately to keep the helper focused on semantic fixtures.
    pdf = fitz.open()
    for index in range(9):
        page = pdf.new_page(width=300, height=400)
        page.insert_text((40, 80), f"PAGE {index + 1}")
    document = replace(document, content=pdf.tobytes())
    pdf.close()
    context = TemplateGenerationContext(
        "nine", "nine", "sha", (), 9, "general", tuple("" for _ in range(9)), tuple((300.0, 400.0) for _ in range(9))
    )
    proposal = provider.generate_template(document, context)
    assert proposal.document_kind == "general"
    assert len(captured) == 3
    texts: list[str] = []
    for payload in captured:
        user_content = payload["input"][1]["content"]  # type: ignore[index]
        texts.append("\n".join(str(item.get("text", "")) for item in user_content if isinstance(item, dict)))
    assert "VISUAL PAGE 1" in texts[0] and "VISUAL PAGE 4" in texts[0]
    assert "VISUAL PAGE 5" in texts[1] and "VISUAL PAGE 8" in texts[1]
    assert "VISUAL PAGE 9" in texts[2]


def test_openai_reviewer_batches_target_pages_and_aggregates_reject() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured.append(payload)
        text = json.dumps(payload)
        reject = "VISUAL PAGE 5" in text
        review = {
            "verdict": "reject" if reject else "pass",
            "confidence": 0.97,
            "issues": ([{
                "scope": "field", "name": "total", "code": "wrong_label",
                "reason": "TOTAL label mismatch", "confidence": 0.98,
            }] if reject else []),
        }
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(review)}, request=request)

    provider = OpenAIResponsesTemplateProvider(
        api_key="test",
        options=OpenAIResponsesTemplateOptions(max_pages_per_batch=4, max_ai_batches_per_document=4),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    pdf = fitz.open()
    for index in range(6):
        page = pdf.new_page(width=300, height=400)
        page.insert_text((40, 80), f"PAGE {index + 1}")
    document = DocumentInput("six.pdf", pdf.tobytes(), "six")
    pdf.close()
    fields = (
        TemplateFieldDefinition("invoice_number", "identifier", True, 1, ("Invoice No",), r".+"),
        TemplateFieldDefinition("total", "money", True, 5, ("TOTAL",), r"\d+[.]\d{2}"),
    )
    fingerprint = TemplateLayoutFingerprint(
        6, (0.75,) * 6, (1,) * 6, (0,) * 6,
        (TemplateAnchor("Invoice No", 1, None, True), TemplateAnchor("TOTAL", 5, None, True)),
        "sig",
    )
    candidate = DocumentTemplate(
        "six", "six", 1, TemplateStatus.CANDIDATE, "general", fields, (), fingerprint,
        "sha", "six", "test", "candidate-hash",
    )
    context = TemplateReviewContext(
        "six", "six", "sha", "candidate-hash", tuple("" for _ in range(6)),
        tuple((300.0, 400.0) for _ in range(6)),
    )
    review = provider.review_template(document, candidate, context)
    assert review.verdict.value == "reject"
    assert len(captured) == 2
    assert review.issues and review.issues[0].name == "total"


def test_sqlite_round_trip_preserves_per_page_and_anchor_visual_hashes(tmp_path) -> None:
    service = DataExtractionService(
        ocr_provider=_CountingGenericOCR(),
        field_ocr_providers=(_SequentialFieldOCR("a"), _SequentialFieldOCR("b")),
    )
    source = DocumentInput("first.pdf", _pdf("INV-2026-001", "123.45"), "first")
    template = _approved_template(service, source)
    store = SQLiteTemplateStore(tmp_path / "templates.db")
    store.save(template)
    restored = store.get(template.template_id, template.version)
    assert restored == template
    assert restored is not None
    assert len(restored.fingerprint.page_visual_hashes) == 3
    assert all(anchor.visual_hash for anchor in restored.fingerprint.anchors)
