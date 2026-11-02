from __future__ import annotations

import io

from PIL import Image

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.analyze_layout.pdf_toc import NativeTextLine, detect_toc_table
from data_extraction.providers.contracts import OCRTextRegion
from data_extraction.shared.models import ProviderTextResult, RegionKind


def _png(width: int = 900, height: int = 1400) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class ReceiptOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "ACME STORE\n123 MAIN STREET\nDate: 25/12/2018\nItem 10.00\nTax 1.50\nTOTAL 11.50",
            0.96,
            provider_name="receipt-fake",
        )


def test_ocr_text_success_does_not_claim_receipt_semantic_completeness() -> None:
    result = DataExtractionService(ocr_provider=ReceiptOCR()).extract(
        DocumentInput("receipt.png", _png(), "receipt")
    )
    page = result.pages[0]
    assert page.text_coverage_complete is True
    assert page.text_coverage_ratio == 1.0
    assert page.structured_document_kind == "receipt"
    assert page.field_coverage_ratio == 1.0
    assert page.verified_field_coverage_ratio == 0.0
    assert page.semantic_complete is False
    assert page.content_complete is False
    assert page.completeness_ratio == 0.0
    assert result.text_coverage_complete is True
    assert result.content_complete is False
    assert result.completeness_ratio == 0.0
    assert result.ready_for_publication is False
    assert any(field.field_kind == "date" for field in page.structured_fields)
    assert any(field.field_kind == "total" for field in page.structured_fields)
    assert "high_ocr_confidence_does_not_prove_field_coverage" not in result.diagnostics
    assert any("structured_fields_not_independently_verified" in item for item in result.diagnostics)


def test_native_invoice_can_be_semantically_complete_when_core_fields_are_source_text() -> None:
    text = (
        "Tax Invoice\n"
        "Invoice No: INV-2026-001\n"
        "Date: 31/10/2026\n"
        "Subtotal 220.00\n"
        "VAT 33.00\n"
        "Total 253.00\n"
    ).encode("utf-8")
    result = DataExtractionService().extract(DocumentInput("invoice.txt", text, "invoice"))
    page = result.pages[0]
    assert page.structured_document_kind == "invoice"
    assert page.field_coverage_ratio == 1.0
    assert page.verified_field_coverage_ratio == 1.0
    assert page.semantic_complete is True
    assert page.content_complete is True


class BorderlessInvoiceOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        # The flattened OCR deliberately misses the financial amounts.
        # Token geometry below is the only evidence that can recover them.
        return ProviderTextResult(
            "Tax Invoice\nSeller Information\nBuyer Information\nInvoice No: INV-100\nDate: 31/10/2026",
            0.95,
            provider_name="invoice-fake",
        )

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("Item", (50, 300, 120, 330), .95), OCRTextRegion("10.00", (600, 300, 680, 330), .95),
            OCRTextRegion("Tax", (50, 350, 120, 380), .95), OCRTextRegion("1.50", (600, 350, 680, 380), .95),
            OCRTextRegion("Total", (50, 400, 130, 430), .95), OCRTextRegion("11.50", (600, 400, 690, 430), .95),
        )


def test_borderless_invoice_can_recover_candidate_table_from_token_geometry() -> None:
    result = DataExtractionService(
        ExtractionOptions(enable_semantic_raster_table_recovery=True),
        ocr_provider=BorderlessInvoiceOCR(),
    ).extract(DocumentInput("invoice.png", _png(), "invoice-raster"))
    region = result.pages[0].regions[0]
    assert region.kind == RegionKind.TABLE
    assert region.table == [["Item", "10.00"], ["Tax", "1.50"], ["Total", "11.50"]]
    assert "ocr_table_geometry_reconstructed_review" in region.review_reasons
    page = result.pages[0]
    assert page.structured_document_kind == "invoice"
    assert page.field_coverage_ratio == 1.0
    assert any(field.field_kind == "total" and field.value == "11.50" for field in page.structured_fields)
    assert any(field.field_kind == "tax" and field.value == "1.50" for field in page.structured_fields)
    assert result.content_complete is False  # table candidate + single OCR fields are not authoritative


def test_statistics_table_is_not_misclassified_as_toc_without_toc_evidence() -> None:
    lines = (
        NativeTextLine("Employees", (80, 100, 220, 120), "t1"),
        NativeTextLine("55", (300, 100, 330, 120), "n1"),
        NativeTextLine("Branches", (80, 150, 220, 170), "t2"),
        NativeTextLine("12", (300, 150, 330, 170), "n2"),
        NativeTextLine("Licenses", (80, 200, 220, 220), "t3"),
        NativeTextLine("78", (300, 200, 330, 220), "n3"),
        NativeTextLine("Inspections", (80, 250, 220, 270), "t4"),
        NativeTextLine("31", (300, 250, 330, 270), "n4"),
    )
    assert detect_toc_table(lines, 600, 800, 8, 25) is None


def test_adaptive_tesseract_numeric_disagreement_is_explicit(monkeypatch) -> None:
    from data_extraction.providers.ocr.adaptive_tesseract import AdaptiveTesseractOCRProvider
    from data_extraction.providers.ocr.tesseract import TesseractOCRProvider

    def fake_recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        if self.language == "eng":
            return ProviderTextResult("TOTAL 112.45", .96, provider_name="tesseract")
        return ProviderTextResult("TOTAL 112.46", .94, provider_name="tesseract")

    monkeypatch.setattr(TesseractOCRProvider, "recognize", fake_recognize)
    result = AdaptiveTesseractOCRProvider(("ara+eng", "eng")).recognize(_png(), "image/png")
    assert result.text == "TOTAL 112.45"
    assert result.provider_name == "adaptive_tesseract_numeric_conflict"
    assert result.confidence == .94


class HighConfidenceIncompleteInvoiceOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "Simplified Tax Invoice\nSeller Information\nBuyer Information\nInvoice Number: INV-14",
            0.925,
            provider_name="zatca-like-fake",
        )


def test_high_ocr_confidence_cannot_hide_missing_invoice_fields() -> None:
    result = DataExtractionService(ocr_provider=HighConfidenceIncompleteInvoiceOCR()).extract(
        DocumentInput("invoice-example.png", _png(), "zatca-like")
    )
    page = result.pages[0]
    assert page.text_coverage_complete is True
    assert page.structured_document_kind == "invoice"
    assert page.field_coverage_ratio < 1.0
    assert page.verified_field_coverage_ratio == 0.0
    assert page.semantic_gap_count == 3
    assert page.semantic_complete is False
    assert page.content_complete is False
    assert result.semantic_gap_count == 3
    assert result.completeness_ratio == 0.0
    assert "page1: high_ocr_confidence_does_not_prove_field_coverage" in result.diagnostics
    assert "page1: structured_required_fields_missing" in result.diagnostics



def _structured_test_page(text: str):
    from data_extraction.shared.models import ExtractedRegion, ExtractionMethod, PageKind, PageResult, RegionKind
    region = ExtractedRegion(
        region_id="p1:text:1", kind=RegionKind.TEXT, bbox=(0.0, 0.0, 1000.0, 1000.0),
        method=ExtractionMethod.OCR, text=text, table=None, confidence=0.95,
        requires_review=True, review_reasons=("test",), source_kind=RegionKind.IMAGE,
        selected_provider_name="test",
    )
    return PageResult(
        number=1, kind=PageKind.IMAGE, label="page-1", content_types=(RegionKind.TEXT,),
        regions=(region,), requires_review=True, warnings=(), width=1000.0, height=1000.0,
    )


def test_arabic_subtotal_is_not_promoted_to_total_when_total_label_is_missing() -> None:
    from data_extraction.structured_documents.service import StructuredDocumentAnalysisService
    page = _structured_test_page(
        "فاتورة ضريبية\nرقم الفاتورة INV-2026-900\nالتاريخ 01/11/2026\n"
        "المجموع الفرعي 100.00\nضريبة 5.00"
    )
    result = StructuredDocumentAnalysisService().analyze_page(page)
    values = {field.field_kind: field.value for field in result.fields}
    assert values.get("subtotal") == "100.00"
    assert "total" not in values


def test_invoice_number_line_is_not_promoted_to_address() -> None:
    from data_extraction.structured_documents.service import StructuredDocumentAnalysisService
    page = _structured_test_page(
        "NOVA SUPPLIES\nInvoice No: INV-2026-901\nDate: 01/11/2026\n"
        "Subtotal 100.00\nVAT 5.00\nTOTAL 105.00"
    )
    result = StructuredDocumentAnalysisService().analyze_page(page)
    values = {field.field_kind: field.value for field in result.fields}
    assert values.get("invoice_number") == "INV-2026-901"
    assert "address" not in values
