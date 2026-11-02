from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from data_extraction import (
    DataExtractionService,
    ExtractionOptions,
    DocumentInput,
    SQLiteTemplateStore,
    TemplateAnchor,
    TemplateApprovalRequest,
    TemplateBuildRequest,
    TemplateExtractionError,
    TemplateExtractionRequest,
    TemplateFieldDefinition,
    TemplateGenerationContext,
    TemplateGenerationProposal,
    TemplateMatchRequest,
    TemplateMatchStatus,
    TemplateStatus,
    TemplateTableDefinition,
)
from data_extraction.providers.contracts import OCRTextRegion
from data_extraction.shared.models import ProviderTextResult


def _png(width: int = 900, height: int = 1400) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _different_png(width: int = 900, height: int = 1400) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    for y in range(height // 3, 2 * height // 3):
        for x in range(width):
            image.putpixel((x, y), (0, 0, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class InvoiceLayoutOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "ACME MARKET\nTax Invoice\nDate 31/10/2026\nTOTAL 112.45",
            0.96,
            provider_name="layout-ocr",
        )

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("ACME MARKET", (80, 60, 300, 100), .97),
            OCRTextRegion("Date", (80, 900, 150, 940), .96),
            OCRTextRegion("31/10/2026", (620, 900, 760, 940), .96),
            OCRTextRegion("TOTAL", (80, 1100, 170, 1140), .97),
            OCRTextRegion("112.45", (650, 1100, 760, 1140), .96),
        )


class ConsensusTotalA:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("112.45", .96, provider_name="total-a")


class ConsensusTotalB:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("112.45", .95, provider_name="total-b")


class FakeInvoiceTemplateAI:
    def __init__(self) -> None:
        self.calls = 0

    def generate_template(
        self, document: DocumentInput, context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        self.calls += 1
        assert context.family == "acme-invoice"
        return TemplateGenerationProposal(
            document_kind="invoice",
            fields=(
                TemplateFieldDefinition(
                    name="total",
                    field_kind="total",
                    required=True,
                    page_number=1,
                    anchors=("TOTAL",),
                    pattern=r"\d+[.,]\d{2}",
                    normalized_label_bbox=(0.0, 0.0, 1.0, 1.0),
                    normalized_value_bbox=(0.70, 0.76, 0.90, 0.86),
                    minimum_confidence=0.90,
                ),
            ),
            anchors=(
                TemplateAnchor("ACME MARKET", 1, (0.0, 0.0, 1.0, 1.0), True),
                TemplateAnchor("TOTAL", 1, (0.0, 0.0, 1.0, 1.0), True),
            ),
            diagnostics=("ai_detected_invoice_layout",),
        )


def _service(store=None) -> tuple[DataExtractionService, FakeInvoiceTemplateAI]:
    generator = FakeInvoiceTemplateAI()
    layout = InvoiceLayoutOCR()
    service = DataExtractionService(
        ocr_provider=layout,
        field_layout_provider=layout,
        field_ocr_providers=(ConsensusTotalA(), ConsensusTotalB()),
        template_generation_provider=generator,
        template_store=store,
    )
    return service, generator


def test_ai_candidate_requires_explicit_approval_before_matching() -> None:
    service, generator = _service()
    document = DocumentInput("invoice.png", _png(), "invoice-1")
    built = service.build_template_candidate(TemplateBuildRequest(document, "acme-invoice"))
    assert generator.calls == 1
    assert built.candidate.status == TemplateStatus.CANDIDATE
    assert built.candidate.content_hash
    match = service.match_template(TemplateMatchRequest(document, (built.candidate,), "acme-invoice"))
    assert match.status == TemplateMatchStatus.REJECTED
    assert "no_approved_template_available" in match.diagnostics


def test_approved_template_matches_similar_document_and_verifies_total_by_consensus() -> None:
    service, _ = _service()
    source = DocumentInput("invoice.png", _png(), "invoice-1")
    candidate = service.build_template_candidate(TemplateBuildRequest(source, "acme-invoice")).candidate
    approved = service.approve_template(TemplateApprovalRequest(candidate, "reviewer@example", save=True))
    assert approved.status == TemplateStatus.APPROVED

    similar = DocumentInput("invoice-copy.png", _png(), "invoice-2")
    result = service.extract_with_template(TemplateExtractionRequest(similar, family="acme-invoice"))
    assert result.match.status == TemplateMatchStatus.ACCEPTED
    total = next(field for field in result.fields if field.name == "total")
    assert total.value == "112.45"
    assert total.verified is True
    assert total.status == "verified_consensus"
    assert result.verified_required_field_ratio == 1.0
    assert result.semantic_complete is True
    assert result.requires_review is False


class DifferentReceiptOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("OTHER STORE\nRECEIPT\nAMOUNT 9.00", .95, provider_name="other")


def test_unrelated_layout_is_rejected_instead_of_forcing_old_template() -> None:
    service, _ = _service()
    source = DocumentInput("invoice.png", _png(), "invoice-1")
    candidate = service.build_template_candidate(TemplateBuildRequest(source, "acme-invoice")).candidate
    approved = service.approve_template(TemplateApprovalRequest(candidate, "human", save=False))
    other_service = DataExtractionService(ocr_provider=DifferentReceiptOCR())
    result = other_service.match_template(
        TemplateMatchRequest(DocumentInput("other.png", _different_png(), "other"), (approved,), "acme-invoice")
    )
    assert result.status == TemplateMatchStatus.REJECTED
    assert result.score < 0.72


def test_template_hash_prevents_approval_after_candidate_tampering() -> None:
    service, _ = _service()
    document = DocumentInput("invoice.png", _png(), "invoice-1")
    candidate = service.build_template_candidate(TemplateBuildRequest(document, "acme-invoice")).candidate
    tampered = replace(candidate, family="different-family")
    with pytest.raises(TemplateExtractionError) as error:
        service.approve_template(TemplateApprovalRequest(tampered, "human"))
    assert error.value.code == "TEMPLATE_HASH_MISMATCH"


def test_sqlite_template_store_persists_approved_version(tmp_path: Path) -> None:
    store = SQLiteTemplateStore(tmp_path / "templates.db")
    service, _ = _service(store)
    document = DocumentInput("invoice.png", _png(), "invoice-1")
    candidate = service.build_template_candidate(
        TemplateBuildRequest(document, "acme-invoice", template_id="acme")
    ).candidate
    approved = service.approve_template(TemplateApprovalRequest(candidate, "human", save=True))
    loaded = store.get("acme", 1)
    assert loaded == approved
    assert store.list_approved("acme-invoice") == (approved,)
    assert store.next_version("acme") == 2


def test_deterministic_bootstrap_never_auto_approves() -> None:
    service = DataExtractionService(ocr_provider=InvoiceLayoutOCR())
    document = DocumentInput("invoice.png", _png(), "invoice-1")
    built = service.build_template_candidate(TemplateBuildRequest(document, "bootstrap"))
    assert built.candidate.status == TemplateStatus.CANDIDATE
    assert built.candidate.generator_name == "deterministic_bootstrap"
    assert "template_bootstrap_without_ai_requires_approval" in built.diagnostics


def test_new_candidate_for_existing_template_id_gets_next_version(tmp_path: Path) -> None:
    store = SQLiteTemplateStore(tmp_path / "templates.db")
    service, _ = _service(store)
    first = DocumentInput("invoice.png", _png(), "invoice-1")
    candidate_v1 = service.build_template_candidate(
        TemplateBuildRequest(first, "acme-invoice", template_id="acme")
    ).candidate
    service.approve_template(TemplateApprovalRequest(candidate_v1, "human", save=True))
    second = DocumentInput("invoice-v2.png", _png(), "invoice-2")
    candidate_v2 = service.build_template_candidate(
        TemplateBuildRequest(second, "acme-invoice", template_id="acme")
    ).candidate
    assert candidate_v2.version == 2
    assert candidate_v2.status == TemplateStatus.CANDIDATE


def test_template_json_is_serializable_without_source_bytes() -> None:
    service, _ = _service()
    document = DocumentInput("invoice.png", _png(), "invoice-1")
    candidate = service.build_template_candidate(TemplateBuildRequest(document, "acme-invoice")).candidate
    payload = DataExtractionService.to_json(candidate)
    assert "invoice-1" in payload
    assert "PNG" not in payload
    assert "document-template.v0.1" in payload


def test_template_candidate_contains_visual_fingerprint_for_image_layout() -> None:
    service, _ = _service()
    document = DocumentInput("invoice.png", _png(), "invoice-visual")
    candidate = service.build_template_candidate(TemplateBuildRequest(document, "acme-invoice")).candidate
    assert candidate.fingerprint.visual_hash is not None
    assert len(candidate.fingerprint.visual_hash) == 64


class ReceiptTableOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "RECEIPT\nACME STORE\nBlue Pen 10.00\nLarge Notebook Pack 20.00\nTOTAL 30.00",
            .96,
            provider_name="receipt-table",
        )

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("TOTAL", (40, 180, 100, 200), .96),
            OCRTextRegion("30.00", (500, 180, 555, 200), .96),
        )

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("Blue", (40, 100, 80, 120), .95),
            OCRTextRegion("Pen", (85, 100, 120, 120), .95),
            OCRTextRegion("10.00", (500, 100, 555, 120), .95),
            OCRTextRegion("Large", (40, 140, 90, 160), .95),
            OCRTextRegion("Notebook", (95, 140, 165, 160), .95),
            OCRTextRegion("Pack", (170, 140, 210, 160), .95),
            OCRTextRegion("20.00", (500, 140, 555, 160), .95),
            OCRTextRegion("TOTAL", (40, 180, 100, 200), .96),
            OCRTextRegion("30.00", (500, 180, 555, 200), .96),
        )


class ReceiptTemplateAI:
    def generate_template(
        self, document: DocumentInput, context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        return TemplateGenerationProposal(
            document_kind="receipt",
            fields=(
                TemplateFieldDefinition(
                    "total", "total", True, 1, ("TOTAL",), r"\d+[.,]\d{2}",
                    normalized_label_bbox=(0.0, 0.0, 1.0, 1.0),
                ),
            ),
            tables=(
                TemplateTableDefinition(
                    "items", 1, None, ("Blue", "Pen"), ("Description", "Amount"), True, True
                ),
            ),
            anchors=(TemplateAnchor("RECEIPT", 1, (0.0, 0.0, 1.0, 1.0), True),),
        )


def test_repeating_table_is_reused_as_structure_but_not_financially_verified() -> None:
    ocr = ReceiptTableOCR()
    service = DataExtractionService(
        ExtractionOptions(enable_semantic_raster_table_recovery=True),
        ocr_provider=ocr,
        field_layout_provider=ocr,
        field_ocr_providers=(ConsensusTotalA(), ConsensusTotalB()),
        template_generation_provider=ReceiptTemplateAI(),
    )
    document = DocumentInput("receipt.png", _png(), "receipt-1")
    candidate = service.build_template_candidate(TemplateBuildRequest(document, "receipt-family")).candidate
    approved = service.approve_template(TemplateApprovalRequest(candidate, "human", save=True))
    result = service.extract_with_template(TemplateExtractionRequest(document, (approved,), "receipt-family"))
    table = result.tables[0]
    assert table.rows == (
        ("Blue Pen", "10.00"),
        ("Large Notebook Pack", "20.00"),
        ("TOTAL", "30.00"),
    )
    assert table.status == "structure_matched_review"
    assert table.verified is False
    assert result.required_table_coverage_ratio == 1.0
    assert result.generic_extraction_performed is True
    assert result.semantic_complete is False
    assert "template_required_table_values_not_verified" in result.diagnostics


class CountingLayoutOCR(InvoiceLayoutOCR):
    def __init__(self) -> None:
        self.recognize_calls = 0
        self.layout_calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.recognize_calls += 1
        return super().recognize(image_bytes, mime_type)

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        self.layout_calls += 1
        return super().recognize_regions(image_bytes, mime_type)


def test_known_template_uses_direct_bbox_fast_path_without_full_generic_or_layout_ocr() -> None:
    generator = FakeInvoiceTemplateAI()
    layout = CountingLayoutOCR()
    service = DataExtractionService(
        ocr_provider=layout,
        field_layout_provider=layout,
        field_ocr_providers=(ConsensusTotalA(), ConsensusTotalB()),
        template_generation_provider=generator,
    )
    source = DocumentInput("invoice.png", _png(), "first")
    candidate = service.build_template_candidate(TemplateBuildRequest(source, "acme-invoice")).candidate
    approved = service.approve_template(TemplateApprovalRequest(candidate, "human", save=True))
    # Candidate generation legitimately used generic OCR. Reset before repeated-layout run.
    layout.recognize_calls = 0
    layout.layout_calls = 0

    repeated = DocumentInput("invoice-2.png", _png(), "repeat")
    result = service.extract_with_template(TemplateExtractionRequest(repeated, (approved,), "acme-invoice"))
    assert result.generic_extraction_performed is False
    assert "template_fast_path_used" in result.diagnostics
    assert layout.recognize_calls == 0
    assert layout.layout_calls == 0
    assert next(field for field in result.fields if field.name == "total").verified is True


class MissingRequiredFieldAI:
    def generate_template(
        self, document: DocumentInput, context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        return TemplateGenerationProposal(document_kind="invoice", fields=())


def test_ai_template_cannot_silently_omit_caller_required_fields() -> None:
    service = DataExtractionService(
        ocr_provider=InvoiceLayoutOCR(), template_generation_provider=MissingRequiredFieldAI()
    )
    with pytest.raises(TemplateExtractionError) as error:
        service.build_template_candidate(TemplateBuildRequest(
            DocumentInput("invoice.png", _png(), "missing"),
            "invoice-family",
            required_fields=("total",),
        ))
    assert error.value.code == "TEMPLATE_REQUIRED_FIELD_MISSING"


def test_visual_layout_drift_downgrades_match_even_when_ocr_words_still_match() -> None:
    service, _ = _service()
    source = DocumentInput("invoice.png", _png(), "invoice-base")
    candidate = service.build_template_candidate(TemplateBuildRequest(source, "acme-invoice")).candidate
    approved = service.approve_template(TemplateApprovalRequest(candidate, "human", save=False))
    changed = DocumentInput("invoice-changed.png", _different_png(), "invoice-changed")
    result = service.match_template(TemplateMatchRequest(changed, (approved,), "acme-invoice"))
    assert result.status != TemplateMatchStatus.ACCEPTED
    assert result.drift_detected is True
