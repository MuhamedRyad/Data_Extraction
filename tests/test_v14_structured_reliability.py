from __future__ import annotations

import io

from PIL import Image

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.analyze_layout.pdf_toc import NativeTextLine, detect_toc_table
from data_extraction.providers.contracts import OCRTextRegion
from data_extraction.providers.ocr.table_geometry import OCRWord, reconstruct_financial_candidate_table
from data_extraction.shared.models import ProviderTextResult, RegionKind


def _png(width: int = 900, height: int = 1400) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class LowConfidenceFormOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "Name: Mohamed\nAddress: Kuwait\nSignature",
            0.91,
            provider_name="form-fake",
        )


def test_suspected_form_below_kind_threshold_does_not_escape_semantic_gate() -> None:
    result = DataExtractionService(ocr_provider=LowConfidenceFormOCR()).extract(
        DocumentInput("form.png", _png(), "form")
    )
    page = result.pages[0]
    assert 0.40 <= page.structured_document_confidence < 0.72
    assert page.structured_document_kind == "form"
    assert page.semantic_complete is False
    assert page.content_complete is False
    assert "page1: structured_kind_suspected_below_acceptance_threshold" in result.diagnostics


class SellerLabelOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "Tax Invoice\nSeller's Name\nZHENG HUI TRADING\nIssuance\n"
            "Invoice Number: INV-100\nDate: 31/10/2026\nTOTAL 112.45",
            0.95,
            provider_name="seller-label-fake",
        )


def test_merchant_candidate_prefers_value_after_label_not_label_or_generic_word() -> None:
    result = DataExtractionService(ocr_provider=SellerLabelOCR()).extract(
        DocumentInput("invoice.png", _png(), "invoice")
    )
    merchant = next(field for field in result.pages[0].structured_fields if field.field_kind == "merchant_name")
    assert merchant.value == "ZHENG HUI TRADING"
    assert merchant.value not in {"Seller's Name", "Issuance"}


class GarbageHeaderOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "RECEIPT\nAd>odige\nNO.89 SOME ROAD\nDate: 31/10/2026\nTOTAL 9.00",
            0.94,
            provider_name="garbage-header-fake",
        )


def test_ocr_garbage_header_is_not_promoted_to_merchant_name() -> None:
    result = DataExtractionService(ocr_provider=GarbageHeaderOCR()).extract(
        DocumentInput("receipt.png", _png(), "receipt")
    )
    merchant_values = [
        field.value for field in result.pages[0].structured_fields
        if field.field_kind == "merchant_name"
    ]
    assert "Ad>odige" not in merchant_values


def test_toc_prefers_outer_page_reference_over_section_number_on_same_row() -> None:
    lines = (
        NativeTextLine("Contents", (230, 40, 360, 65), "h"),
        NativeTextLine("1", (70, 100, 85, 120), "s1"),
        NativeTextLine("Introduction", (120, 100, 360, 120), "t1"),
        NativeTextLine("11", (550, 100, 575, 120), "p1"),
        NativeTextLine("2", (70, 150, 85, 170), "s2"),
        NativeTextLine("Registration", (120, 150, 360, 170), "t2"),
        NativeTextLine("13", (550, 150, 575, 170), "p2"),
        NativeTextLine("3", (70, 200, 85, 220), "s3"),
        NativeTextLine("Payments", (120, 200, 360, 220), "t3"),
        NativeTextLine("15", (550, 200, 575, 220), "p3"),
    )
    detection = detect_toc_table(lines, 600, 800, 3, 30)
    assert detection is not None
    assert detection.table_region.table == [
        ["Introduction", "11"],
        ["Registration", "13"],
        ["Payments", "15"],
    ]


def test_legal_article_rows_are_not_toc_without_explicit_contents_heading() -> None:
    lines = (
        NativeTextLine("Article One", (120, 100, 360, 120), "t1"),
        NativeTextLine("5", (550, 100, 570, 120), "n1"),
        NativeTextLine("Article Two", (120, 150, 360, 170), "t2"),
        NativeTextLine("9", (550, 150, 570, 170), "n2"),
        NativeTextLine("Article Three", (120, 200, 360, 220), "t3"),
        NativeTextLine("12", (550, 200, 570, 220), "n3"),
        NativeTextLine("Article Four", (120, 250, 360, 270), "t4"),
        NativeTextLine("14", (550, 250, 570, 270), "n4"),
        NativeTextLine("Article Five", (120, 300, 360, 320), "t5"),
        NativeTextLine("15", (550, 300, 570, 320), "n5"),
    )
    assert detect_toc_table(lines, 600, 800, 5, 20) is None


def test_invoice_line_items_are_not_reconstructed_as_toc() -> None:
    lines = (
        NativeTextLine("# Description Qty Unit price Amount", (40, 80, 560, 100), "header"),
        NativeTextLine("1", (45, 300, 55, 320), "n1"),
        NativeTextLine("A4 copier paper, 75 gsm, ream of 500", (70, 300, 350, 320), "t1"),
        NativeTextLine("2", (45, 350, 55, 370), "n2"),
        NativeTextLine("Ballpoint pens, blue, box of 50", (70, 350, 350, 370), "t2"),
        NativeTextLine("3", (45, 400, 55, 420), "n3"),
        NativeTextLine("Lever-arch files, A4, pack of 10", (70, 400, 350, 420), "t3"),
        NativeTextLine("4", (45, 450, 55, 470), "n4"),
        NativeTextLine("Whiteboard markers, assorted, pack of 4", (70, 450, 350, 470), "t4"),
        NativeTextLine("5", (45, 500, 55, 520), "n5"),
        NativeTextLine("Desk organiser, mesh, black", (70, 500, 350, 520), "t5"),
    )
    assert detect_toc_table(lines, 600, 800, 1, 1) is None


def test_flexible_financial_table_handles_variable_description_token_counts() -> None:
    words = (
        OCRWord("Product", 40, 100, 100, 120, .95),
        OCRWord("A", 105, 100, 120, 120, .95),
        OCRWord("10.00", 500, 100, 555, 120, .95),
        OCRWord("Another", 40, 140, 105, 160, .95),
        OCRWord("Long", 110, 140, 145, 160, .95),
        OCRWord("Product", 150, 140, 210, 160, .95),
        OCRWord("20.00", 500, 140, 555, 160, .95),
        OCRWord("TOTAL", 40, 180, 100, 200, .95),
        OCRWord("30.00", 500, 180, 555, 200, .95),
    )
    assert reconstruct_financial_candidate_table(words) == [
        ["Product A", "10.00"],
        ["Another Long Product", "20.00"],
        ["TOTAL", "30.00"],
    ]


class FlexibleReceiptOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            "RECEIPT\nSTORE\nItem list 10.00\nTOTAL",
            0.94,
            provider_name="flex-receipt-fake",
        )

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("Product", (40, 100, 100, 120), .95),
            OCRTextRegion("A", (105, 100, 120, 120), .95),
            OCRTextRegion("10.00", (500, 100, 555, 120), .95),
            OCRTextRegion("Another", (40, 140, 105, 160), .95),
            OCRTextRegion("Long", (110, 140, 145, 160), .95),
            OCRTextRegion("Product", (150, 140, 210, 160), .95),
            OCRTextRegion("20.00", (500, 140, 555, 160), .95),
            OCRTextRegion("TOTAL", (40, 180, 100, 200), .95),
            OCRTextRegion("30.00", (500, 180, 555, 200), .95),
        )


def test_financial_token_geometry_can_recover_table_when_strict_grid_fails() -> None:
    result = DataExtractionService(
        ExtractionOptions(enable_semantic_raster_table_recovery=True),
        ocr_provider=FlexibleReceiptOCR(),
    ).extract(DocumentInput("receipt.png", _png(), "receipt"))
    region = result.pages[0].regions[0]
    assert region.kind == RegionKind.TABLE
    assert region.table == [
        ["Product A", "10.00"],
        ["Another Long Product", "20.00"],
        ["TOTAL", "30.00"],
    ]
    assert any(field.field_kind == "total" and field.value == "30.00" for field in result.pages[0].structured_fields)
