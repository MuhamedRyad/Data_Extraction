from __future__ import annotations

import io
import json
from dataclasses import dataclass

import fitz
import pytest
from docx import Document
from openpyxl import Workbook
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from data_extraction import (DataExtractionService, DocumentInput,
                             ExtractionError, ExtractionOptions, ImageRoute,
                             PageKind, ProviderTextResult, RegionKind)
from data_extraction.models import ExtractionMethod


def input_file(content: bytes, name: str = "test.pdf") -> DocumentInput:
    return DocumentInput(name, content, "test-tenant-01:doc-1")


def pdf_with_text() -> bytes:
    page: fitz.Document = fitz.open()
    p: fitz.Page = page.new_page()
    p.insert_text((60, 70), "Hello original text 123")
    output: bytes = page.tobytes()
    page.close()
    return output


def png_with_content() -> bytes:
    img: Image.Image = Image.new("RGB", (420, 110), "white")
    draw: ImageDraw.ImageDraw = ImageDraw.Draw(img)
    draw.text((15, 30), "OCR sample text", fill="black")
    output: io.BytesIO = io.BytesIO()
    img.save(output, format="PNG")
    return output.getvalue()


def pdf_with_table_and_image() -> bytes:
    result: io.BytesIO = io.BytesIO()
    doc: canvas.Canvas = canvas.Canvas(result, pagesize=(600, 650))
    doc.drawString(50, 610, "Invoice header")
    for x in (50, 200, 350):
        doc.line(x, 430, x, 540)
    for y in (430, 465, 500, 540):
        doc.line(50, y, 350, y)
    doc.drawString(60, 513, "Item")
    doc.drawString(210, 513, "Cost")
    doc.drawString(60, 480, "Paper")
    doc.drawString(210, 480, "5.000")
    image: Image.Image = Image.new("RGB", (200, 120), "white")
    paint: ImageDraw.ImageDraw = ImageDraw.Draw(image)
    paint.text((15, 20), "Scanned", fill="black")
    doc.drawImage(ImageReader(image), 60, 200, width=250, height=150)
    doc.save()
    return result.getvalue()


@dataclass
class MockOcr:
    result: ProviderTextResult
    calls: int = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        return self.result


@dataclass
class MockVision:
    result: ProviderTextResult
    calls: int = 0

    def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
        self.calls += 1
        return self.result


def test_detect_pdf_signature_and_native_text() -> None:
    document: DocumentInput = input_file(pdf_with_text(), "pretend.jpg")
    service: DataExtractionService = DataExtractionService()
    inspected = service.inspect(document)
    assert inspected.file_kind == "pdf"
    assert inspected.page_count == 1
    assert inspected.pages[0].kind == PageKind.PHYSICAL_PAGE
    assert inspected.pages[0].content_types == (RegionKind.TEXT,)
    output = service.extract(document)
    assert output.ready_for_publication
    assert output.total_vision_calls == 0
    assert "Hello original text" in output.pages[0].regions[0].text


def test_pdf_manifest_contains_text_table_image_regions() -> None:
    service: DataExtractionService = DataExtractionService()
    inspected = service.inspect(input_file(pdf_with_table_and_image()))
    assert set(inspected.pages[0].content_types) == {RegionKind.TEXT, RegionKind.TABLE, RegionKind.IMAGE}
    assert inspected.pages[0].regions[0].bbox is not None
    assert len([reg for reg in inspected.pages[0].regions if reg.kind == RegionKind.TABLE]) == 1
    extracted = service.extract(input_file(pdf_with_table_and_image()))
    assert not extracted.ready_for_publication  # missing OCR/vision leaves image unresolved
    assert any(region.method == ExtractionMethod.NATIVE and region.kind == RegionKind.TABLE
               for region in extracted.pages[0].regions)
    assert any(region.method == ExtractionMethod.UNRESOLVED for region in extracted.pages[0].regions)


def test_inspect_never_calls_ocr_or_vision() -> None:
    ocr: MockOcr = MockOcr(ProviderTextResult("some text", 0.99))
    vision: MockVision = MockVision(ProviderTextResult("picture", 0.9))
    service: DataExtractionService = DataExtractionService(ocr_provider=ocr, vision_provider=vision)
    manifest = service.inspect(input_file(png_with_content(), "photo.png"))
    assert manifest.page_count == 1
    assert manifest.pages[0].kind == PageKind.IMAGE
    assert manifest.pages[0].content_types == (RegionKind.IMAGE,)
    assert ocr.calls == vision.calls == 0


def test_ocr_high_confidence_normal_text_ready() -> None:
    ocr: MockOcr = MockOcr(ProviderTextResult("Text without numbers", 0.98))
    result = DataExtractionService(ocr_provider=ocr).extract(input_file(png_with_content(), "ocr.png"))
    assert ocr.calls == 1
    assert result.ready_for_publication
    assert result.pages[0].regions[0].method == ExtractionMethod.OCR


def test_numeric_ocr_still_requires_review() -> None:
    ocr: MockOcr = MockOcr(ProviderTextResult("REF-O01I8B-2026", 0.99))
    result = DataExtractionService(ocr_provider=ocr).extract(input_file(png_with_content(), "ocr.png"))
    assert not result.ready_for_publication
    assert "numeric_content_requires_review" in result.pages[0].regions[0].review_reasons


def test_low_confidence_ocr_falls_back_to_vision() -> None:
    ocr: MockOcr = MockOcr(ProviderTextResult("uncertain", 0.3))
    vision: MockVision = MockVision(ProviderTextResult("A diagram", 0.92))
    result = DataExtractionService(ocr_provider=ocr, vision_provider=vision).extract(
        input_file(png_with_content(), "ocr.png"))
    assert ocr.calls == vision.calls == result.total_vision_calls == 1
    assert result.pages[0].regions[0].method == ExtractionMethod.VISION_MODEL
    assert not result.ready_for_publication  # unverified model output


def test_model_budget_blocks_vision_and_keeps_ocr() -> None:
    ocr: MockOcr = MockOcr(ProviderTextResult("uncertain", 0.3))
    vision: MockVision = MockVision(ProviderTextResult("Model", 0.92))
    options = ExtractionOptions(max_vision_calls=0)
    result = DataExtractionService(options, ocr, vision).extract(input_file(png_with_content(), "ocr.png"))
    assert vision.calls == result.total_vision_calls == 0
    assert result.pages[0].regions[0].method == ExtractionMethod.OCR
    assert not result.ready_for_publication


def test_vision_table_is_structured_and_review_required() -> None:
    vision = MockVision(ProviderTextResult("2-row image table", 0.9,
                                         [["Code", "Amount"], ["ABC-1", "5.000"]]))
    result = DataExtractionService(ExtractionOptions(image_route=ImageRoute.VISION_ONLY),
                                   vision_provider=vision).extract(input_file(png_with_content(), "table.png"))
    region = result.pages[0].regions[0]
    assert region.kind == RegionKind.TABLE
    assert region.method == ExtractionMethod.VISION_MODEL
    assert region.table is not None and region.table[1][1] == "5.000"
    assert region.requires_review


def test_docx_is_logical_document_not_fake_page() -> None:
    document: Document = Document()
    document.add_paragraph("Welcome Arabic DOCX")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Customer"
    table.cell(0, 1).text = "Amount"
    buffer = io.BytesIO()
    document.save(buffer)
    manifest = DataExtractionService().inspect(input_file(buffer.getvalue(), "note.docx"))
    assert manifest.pages[0].kind == PageKind.LOGICAL_DOCUMENT
    assert set(manifest.pages[0].content_types) == {RegionKind.TEXT, RegionKind.TABLE}
    assert "docx_physical_pagination_unknown" in manifest.pages[0].warnings


def test_xlsx_sheets_are_not_physical_pages_and_formulas_are_preserved() -> None:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Invoices"
    sheet.append(["Item", "Total"])
    sheet.append(["Book", "=2+3"])
    book.create_sheet("Other")
    buffer = io.BytesIO()
    book.save(buffer)
    result = DataExtractionService().extract(input_file(buffer.getvalue(), "a.xlsx"))
    assert result.manifest.page_count == 2
    assert result.pages[0].kind == PageKind.SHEET
    assert result.pages[0].label == "Invoices"
    assert result.pages[0].regions[0].table[1][1] == "=2+3"
    assert result.pages[1].kind == PageKind.SHEET


def test_csv_rows_and_txt() -> None:
    service = DataExtractionService()
    csv_result = service.extract(input_file(b"name,price\nPen,10\n", "data.csv"))
    assert csv_result.pages[0].regions[0].table[1][1] == "10"
    text_result = service.extract(input_file("مرحبا\nHello".encode(), "notes.txt"))
    assert "مرحبا" in text_result.pages[0].regions[0].text
    assert text_result.pages[0].kind == PageKind.LOGICAL_DOCUMENT


def test_empty_unsupported_encrypted_or_corrupt_files_are_rejected() -> None:
    service = DataExtractionService()
    with pytest.raises(ExtractionError) as empty:
        service.inspect(input_file(b"", "empty.txt"))
    assert empty.value.code == "EMPTY_FILE"
    with pytest.raises(ExtractionError) as unsupported:
        service.inspect(input_file(b"\x00\x01\x02", "file.bin"))
    assert unsupported.value.code == "UNSUPPORTED_BINARY"
    with pytest.raises(ExtractionError) as corrupt:
        service.inspect(input_file(b"%PDF-invalid", "file.pdf"))
    assert corrupt.value.code == "PDF_PARSE_FAILED"


def test_reject_size_limit_and_spreadsheet_cell_limit() -> None:
    service = DataExtractionService(ExtractionOptions(max_input_bytes=2))
    with pytest.raises(ExtractionError) as size_error:
        service.inspect(input_file(b"123456", "x.txt"))
    assert size_error.value.code == "FILE_TOO_LARGE"
    book = Workbook()
    book.active["Z100"] = 1
    buffer = io.BytesIO()
    book.save(buffer)
    sheet_service = DataExtractionService(ExtractionOptions(max_sheet_cells=100))
    with pytest.raises(ExtractionError) as cell_error:
        sheet_service.inspect(input_file(buffer.getvalue(), "x.xlsx"))
    assert cell_error.value.code == "TOO_MANY_CELLS"


def test_json_output_does_not_expose_image_bytes() -> None:
    output = DataExtractionService().extract(input_file(png_with_content(), "photo.png"))
    as_dict = DataExtractionService.to_dict(output)
    encoded: str = json.dumps(as_dict, ensure_ascii=False)
    assert "image_bytes" not in encoded
    assert "unresolved" in encoded
    assert "ready_for_publication" in encoded


def test_no_auto_model_if_high_confidence_ocr() -> None:
    ocr = MockOcr(ProviderTextResult("Text", 0.99))
    vision = MockVision(ProviderTextResult("Should not get called", 0.9))
    service = DataExtractionService(ocr_provider=ocr, vision_provider=vision)
    result = service.extract(input_file(png_with_content(), "photo.png"))
    assert result.total_vision_calls == vision.calls == 0


def test_ocr_only_route_never_uses_model() -> None:
    ocr = MockOcr(ProviderTextResult("Low", 0.01))
    vision = MockVision(ProviderTextResult("Should never happen", 0.9))
    service = DataExtractionService(ExtractionOptions(image_route=ImageRoute.OCR_ONLY),
                                    ocr_provider=ocr, vision_provider=vision)
    result = service.extract(input_file(png_with_content(), "photo.png"))
    assert result.pages[0].regions[0].method == ExtractionMethod.OCR
    assert vision.calls == 0
    assert not result.ready_for_publication


def test_native_text_overlay_and_ocr_remain_reviewable() -> None:
    document: fitz.Document = fitz.open()
    page: fitz.Page = document.new_page(width=420, height=320)
    page.insert_image(fitz.Rect(0, 0, 420, 320), stream=png_with_content())
    page.insert_text((80, 150), "SEARCHABLE NATIVE TEXT")
    data: bytes = document.tobytes()
    document.close()
    ocr = MockOcr(ProviderTextResult("SHOULD NOT DUPLICATE", 0.99))
    service = DataExtractionService(ocr_provider=ocr)
    manifest = service.inspect(input_file(data, "overlay.pdf"))
    assert any(r.image_role == "native_text_overlay" for r in manifest.pages[0].regions)
    result = service.extract(input_file(data, "overlay.pdf"))
    assert ocr.calls == 1
    assert "SEARCHABLE NATIVE TEXT" in " ".join(r.text or "" for r in result.pages[0].regions)
    image_regions = [region for region in result.pages[0].regions if region.source_kind == RegionKind.IMAGE]
    assert len(image_regions) == 1
    assert image_regions[0].requires_review
    assert "native_overlay_requires_review" in image_regions[0].review_reasons


def test_docx_embedded_image_is_detected() -> None:
    doc: Document = Document()
    doc.add_paragraph("Paragraph before embedded image")
    doc.add_picture(io.BytesIO(png_with_content()))
    buf = io.BytesIO()
    doc.save(buf)
    manifest = DataExtractionService().inspect(input_file(buf.getvalue(), "doc.docx"))
    assert RegionKind.IMAGE in manifest.pages[0].content_types
    assert RegionKind.TEXT in manifest.pages[0].content_types


def test_csv_preserves_empty_cells() -> None:
    rows = DataExtractionService().extract(input_file(b"code,amount\nABC,\nXYZ,0\n", "list.csv"))
    cells = rows.pages[0].regions[0].table
    assert cells[1][1] == ""
    assert cells[2][1] == "0"
