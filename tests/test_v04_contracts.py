"""Focused v0.4 regression checks beyond the historical eight audit gaps."""
from __future__ import annotations

import io
import json
from dataclasses import asdict
from typing import Any

import fitz
import pytest
from docx import Document
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image

from data_extraction import (DataExtractionService, DocumentInput, ExtractionOptions,
                             ImageRoute, ProviderTextResult)
from data_extraction.actions.resolve_reading_order_action import order_pdf_regions
from data_extraction.models import CandidateRegion, RegionKind, ReadingOrderQuality


def picture() -> bytes:
    image: Image.Image = Image.new("RGB", (110, 80), "white")
    stream: io.BytesIO = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def run(content: bytes, name: str, service: DataExtractionService | None = None) -> Any:
    svc: DataExtractionService = service or DataExtractionService()
    return svc.extract(DocumentInput(name, content, "tenant:test"))


def test_docx_run_order_and_source_location_json() -> None:
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("ONE ")
    paragraph.add_run().add_picture(io.BytesIO(picture()))
    paragraph.add_run(" TWO")
    stream: io.BytesIO = io.BytesIO()
    document.save(stream)
    result: Any = run(stream.getvalue(), "test.docx")
    assert [r.kind.value for r in result.pages[0].regions] == ["text", "image", "text"]
    location: Any = result.pages[0].regions[1].provenance.source_location
    assert location.part == "body"
    assert location.paragraph_index == 1
    assert location.run_index == 2
    assert [r.position.document_order_index for r in result.pages[0].regions] == [1, 2, 3]
    json.dumps(asdict(result), allow_nan=False)


def test_docx_table_image_parent_and_cell_location() -> None:
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(1, 1).paragraphs[0].add_run().add_picture(io.BytesIO(picture()))
    buffer: io.BytesIO = io.BytesIO()
    document.save(buffer)
    result: Any = run(buffer.getvalue(), "table.docx")
    parent: Any = next(r for r in result.pages[0].regions if r.kind == RegionKind.TABLE)
    child: Any = next(r for r in result.pages[0].regions if r.source_kind == RegionKind.IMAGE)
    assert child.relations.parent_region_id == parent.region_id
    assert child.provenance.source_location.row_index == 1
    assert child.provenance.source_location.column_index == 1


def test_excel_drawing_anchor_in_inspect_and_extract() -> None:
    book: Workbook = Workbook()
    sheet: Any = book.active
    sheet.append(["A", "B"])
    sheet.add_image(ExcelImage(io.BytesIO(picture())), "D4")
    buffer: io.BytesIO = io.BytesIO()
    book.save(buffer)
    req: DocumentInput = DocumentInput("data.xlsx", buffer.getvalue(), "sheet-id")
    service: DataExtractionService = DataExtractionService()
    manifest: Any = service.inspect(req)
    result: Any = service.extract(req)
    image_manifest: Any = next(r for r in manifest.pages[0].regions if r.kind == RegionKind.IMAGE)
    image_result: Any = next(r for r in result.pages[0].regions if r.source_kind == RegionKind.IMAGE)
    assert image_manifest.source_location.cell_anchor == "D4"
    assert image_result.provenance.source_location.cell_anchor == "D4"
    assert image_result.provenance.source_surface == "worksheet_drawing"
    assert not result.ready_for_publication


def test_pdf_columns_with_full_width_header() -> None:
    pdf: fitz.Document = fitz.open()
    page: fitz.Page = pdf.new_page(width=500, height=400)
    for x, y, value in [(20, 20, "LONG FULL WIDTH HEADING MORE CHARACTERS TO SPAN THE WIDTH"),
                        (30, 65, "LEFT-1"), (30, 165, "LEFT-2"),
                        (270, 65, "RIGHT-1"), (270, 165, "RIGHT-2")]:
        page.insert_text((x, y), value)
    result: Any = run(pdf.tobytes(), "cols.pdf")
    pdf.close()
    text: list[str] = [r.text for r in result.pages[0].regions]
    assert text.index("LEFT-2") < text.index("RIGHT-1")
    assert result.pages[0].reading_order_quality == ReadingOrderQuality.COLUMN_HEURISTIC
    assert not result.ready_for_publication


def test_heuristic_orders_arabic_columns_right_first() -> None:
    items: list[CandidateRegion] = [
        CandidateRegion("l1", RegionKind.TEXT, (10, 20, 80, 30), text="يسار أول"),
        CandidateRegion("r1", RegionKind.TEXT, (230, 20, 310, 30), text="يمين أول"),
        CandidateRegion("l2", RegionKind.TEXT, (10, 120, 80, 130), text="يسار ثاني"),
        CandidateRegion("r2", RegionKind.TEXT, (230, 120, 310, 130), text="يمين ثاني"),
    ]
    ordered, warnings = order_pdf_regions(items, 350, 400)
    assert [r.region_id for r in ordered] == ["r1", "r2", "l1", "l2"]
    assert warnings == ("reading_order_columns_inferred_review",)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), -0.1, 1.1, True])
def test_vision_confidence_invalid_is_unresolved(bad: float | bool) -> None:
    class InvalidVision:
        def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
            return ProviderTextResult("text", bad)
    service: DataExtractionService = DataExtractionService(
        ExtractionOptions(image_route=ImageRoute.VISION_ONLY), vision_provider=InvalidVision())
    result: Any = run(picture(), "image.png", service)
    assert result.pages[0].regions[0].method.value == "unresolved"
    assert "provider_invalid_output" in result.pages[0].regions[0].review_reasons
    json.dumps(service.to_dict(result), allow_nan=False)


@pytest.mark.parametrize("bad_table", [
    ["row 1", "row 2"], [["okay"], [2]], [], [["a"], {"bad": "row"}],
])
def test_malformed_vision_matrix_fails_closed(bad_table: Any) -> None:
    class InvalidVision:
        def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
            return ProviderTextResult("", 0.95, structured_table=bad_table)
    service: DataExtractionService = DataExtractionService(
        ExtractionOptions(image_route=ImageRoute.VISION_ONLY), vision_provider=InvalidVision())
    result: Any = run(picture(), "image.png", service)
    assert result.pages[0].regions[0].kind != RegionKind.TABLE
    assert not result.ready_for_publication


def test_docx_header_footer_logical_parts_not_page_claims() -> None:
    document = Document()
    section: Any = document.sections[0]
    section.header.paragraphs[0].text = "HEAD"
    section.footer.paragraphs[0].text = "FOOT"
    document.add_paragraph("BODY")
    buf: io.BytesIO = io.BytesIO()
    document.save(buf)
    result: Any = run(buf.getvalue(), "office.docx")
    assert [r.text for r in result.pages[0].regions] == ["HEAD", "BODY", "FOOT"]
    assert [r.provenance.source_location.part for r in result.pages[0].regions] == ["header", "body", "footer"]
    assert result.pages[0].kind.value == "logical_document"


def test_table_figure_hybrid_never_certifies_arbitrary_column_merge() -> None:
    items: list[CandidateRegion] = [
        CandidateRegion("l1", RegionKind.TEXT, (20, 20, 80, 35), text="LEFT1"),
        CandidateRegion("r1", RegionKind.TEXT, (230, 20, 300, 35), text="RIGHT1"),
        CandidateRegion("l2", RegionKind.TEXT, (20, 120, 80, 135), text="LEFT2"),
        CandidateRegion("r2", RegionKind.TEXT, (230, 120, 300, 135), text="RIGHT2"),
        CandidateRegion("image", RegionKind.IMAGE, (110, 60, 220, 80)),
    ]
    ordered, warnings = order_pdf_regions(items, 400, 300)
    assert len(ordered) == 5
    assert "reading_order_complex_columns_review_required" in warnings


def test_worksheet_merged_cells_are_explicit_in_json() -> None:
    workbook: Workbook = Workbook()
    worksheet: Any = workbook.active
    worksheet["A1"] = "Merged title"
    worksheet.merge_cells("A1:C2")
    workbook_stream: io.BytesIO = io.BytesIO()
    workbook.save(workbook_stream)
    result: Any = run(workbook_stream.getvalue(), "merged.xlsx")
    details: Any = result.pages[0].regions[0].table_details
    assert details.merged_cell_geometry_available is True
    assert [(m.row_start, m.column_start, m.row_span, m.column_span)
            for m in details.merged_cells] == [(0, 0, 2, 3)]
    json.dumps(asdict(result), allow_nan=False)


def test_excel_chart_is_disclosed_as_coverage_gap() -> None:
    from openpyxl.chart import BarChart, Reference
    workbook: Workbook = Workbook()
    worksheet: Any = workbook.active
    worksheet.append(["Count"])
    worksheet.append([5])
    chart: BarChart = BarChart()
    chart.add_data(Reference(worksheet, min_col=1, min_row=1, max_row=2))
    worksheet.add_chart(chart, "D3")
    workbook_stream: io.BytesIO = io.BytesIO()
    workbook.save(workbook_stream)
    result: Any = run(workbook_stream.getvalue(), "chart.xlsx")
    assert "worksheet_charts_not_extracted" in result.pages[0].warnings
    assert not result.ready_for_publication


def test_excel_comment_is_disclosed_as_coverage_gap() -> None:
    from openpyxl.comments import Comment
    workbook: Workbook = Workbook()
    worksheet: Any = workbook.active
    worksheet["A1"] = "Visible data"
    worksheet["A1"].comment = Comment("Important hidden note", "analyst")
    workbook_stream: io.BytesIO = io.BytesIO()
    workbook.save(workbook_stream)
    result: Any = run(workbook_stream.getvalue(), "comment.xlsx")
    assert "worksheet_cell_comments_not_extracted" in result.pages[0].warnings
    assert not result.ready_for_publication


def test_docx_merged_cell_image_is_not_duplicated() -> None:
    document: Any = Document()
    table: Any = document.add_table(rows=1, cols=2)
    left = table.cell(0, 0)
    left.merge(table.cell(0, 1))
    left.paragraphs[0].add_run().add_picture(io.BytesIO(picture()))
    document_stream: io.BytesIO = io.BytesIO()
    document.save(document_stream)
    result: Any = run(document_stream.getvalue(), "merged-cell.docx")
    image_regions: list[Any] = [r for r in result.pages[0].regions if r.source_kind == RegionKind.IMAGE]
    assert len(image_regions) == 1


def test_to_json_has_strict_serialization() -> None:
    result: Any = run(picture(), "picture.png")
    assert json.loads(DataExtractionService.to_json(result))["schema_version"] == "extraction.v0.6"
