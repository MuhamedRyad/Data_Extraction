"""Regression checks for preserving physical order, traceability and output format."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass

import fitz
from PIL import Image
from docx import Document
from openpyxl import Workbook

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions, ImageRoute, ProviderTextResult
from data_extraction.models import (ExtractionMethod, ReadingOrderQuality, RegionKind, SourceSurface)


def fixture_image() -> bytes:
    with Image.new("RGB", (200, 140), "white") as rgb:
        result: io.BytesIO = io.BytesIO()
        rgb.save(result, format="PNG")
        return result.getvalue()


def hybrid_pdf() -> bytes:
    pdf: fitz.Document = fitz.open()
    first: fitz.Page = pdf.new_page(width=450, height=550)
    first.insert_text((30, 45), "FIRST-PAGE")
    second: fitz.Page = pdf.new_page(width=450, height=550)
    second.insert_text((30, 40), "PAGE-TWO-HEADER")
    second.insert_image(fitz.Rect(40, 130, 240, 270), stream=fixture_image())
    second.insert_text((30, 370), "PAGE-TWO-FOOTER")
    third: fitz.Page = pdf.new_page(width=450, height=550)
    third.insert_text((30, 45), "THIRD-PAGE")
    output: bytes = pdf.tobytes()
    pdf.close()
    return output


@dataclass
class OCRSpy:
    result: ProviderTextResult

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return self.result


@dataclass
class VisionSpy:
    result: ProviderTextResult

    def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
        return self.result


def test_page_order_stable_in_manifest_output_and_flat_document_sequence() -> None:
    document: DocumentInput = DocumentInput("report.pdf", hybrid_pdf(), "tenant-A:doc-1")
    service: DataExtractionService = DataExtractionService(
        ocr_provider=OCRSpy(ProviderTextResult("Image words", 0.97, provider_name="test-ocr")),
    )
    inspect = service.inspect(document)
    result = service.extract(document)
    assert [page.number for page in inspect.pages] == [1, 2, 3]
    assert [page.number for page in result.pages] == [1, 2, 3]
    assert [page.label for page in inspect.pages] == ["1", "2", "3"]
    assert inspect.document_order_region_ids == result.document_order_region_ids
    assert inspect.schema_version == result.schema_version == "extraction.v0.6"
    flattened_ids: list[str] = [reg.region_id for page in result.pages for reg in page.regions]
    assert flattened_ids == list(result.document_order_region_ids)
    assert len(flattened_ids) == len(set(flattened_ids))
    assert [reg.position.document_order_index for page in result.pages for reg in page.regions] == list(range(1, len(flattened_ids) + 1))
    for page in result.pages:
        assert page.reading_order_region_ids == tuple(reg.region_id for reg in page.regions)
        assert [reg.position.reading_order_index for reg in page.regions] == list(range(1, len(page.regions) + 1))
        assert all(reg.position.page_number == page.number for reg in page.regions)
    assert any("FIRST-PAGE" in (reg.text or "") for reg in result.pages[0].regions)
    assert any("THIRD-PAGE" in (reg.text or "") for reg in result.pages[2].regions)


def test_hybrid_page_native_text_and_raster_are_traceable_separately() -> None:
    service: DataExtractionService = DataExtractionService(
        ocr_provider=OCRSpy(ProviderTextResult("Words found on image", 0.99, provider_name="ocr-mock"))
    )
    output = service.to_dict(service.extract(DocumentInput("hybrid.pdf", hybrid_pdf(), "doc-1")))
    page = output["pages"][1]
    assert page["number"] == 2
    assert [x["position"]["reading_order_index"] for x in page["regions"]] == [1, 2, 3]
    native = [reg for reg in page["regions"] if reg["method"] == "native"]
    from_image = [reg for reg in page["regions"] if reg["source_kind"] == "image"]
    assert len(native) == 2 and len(from_image) == 1
    assert from_image[0]["kind"] == "text"
    assert from_image[0]["content_type"] == "text"
    assert from_image[0]["provenance"]["source_surface"] == "pdf_embedded_image"
    assert from_image[0]["provenance"]["original_region_kind"] == "image"
    assert from_image[0]["provenance"]["extraction_method"] == "ocr"
    assert from_image[0]["provenance"]["provider_name"] == "ocr-mock"
    assert len(from_image[0]["provenance"]["source_asset_sha256"]) == 64
    assert from_image[0]["position"]["coordinate_system"] == "pdf_points_top_left"
    assert len(from_image[0]["position"]["normalized_bbox"]) == 4
    assert all(reg["provenance"]["source_surface"] == "native_pdf_text" for reg in native)
    assert all(reg["provenance"]["source_image_role"] is None for reg in native)
    assert page["reading_order_quality"] == "geometric_estimate"
    serialized: str = json.dumps(output, ensure_ascii=False)
    assert '"document_order_region_ids"' in serialized
    assert '"table_details"' in serialized
    assert "Image words" not in serialized  # no fabricated output from different test


def test_overlay_does_not_falsely_claim_reading_order_is_verified() -> None:
    pdf: fitz.Document = fitz.open()
    page: fitz.Page = pdf.new_page(width=360, height=220)
    page.insert_image(fitz.Rect(0, 0, 360, 220), stream=fixture_image())
    page.insert_text((85, 90), "NATIVE ABOVE RASTER")
    source: DocumentInput = DocumentInput("overlap.pdf", pdf.tobytes(), "tenant:test")
    pdf.close()
    service: DataExtractionService = DataExtractionService(
        ocr_provider=OCRSpy(ProviderTextResult("Other raster content", 0.99))
    )
    manifest = service.inspect(source)
    output = service.extract(source)
    assert manifest.pages[0].reading_order_quality == ReadingOrderQuality.AMBIGUOUS_OVERLAP
    assert output.pages[0].reading_order_quality == ReadingOrderQuality.AMBIGUOUS_OVERLAP
    for item in output.pages[0].regions:
        assert item.position.reading_order_quality == ReadingOrderQuality.AMBIGUOUS_OVERLAP
        assert item.relations.has_native_text_overlap is True
        assert len(item.relations.overlaps_region_ids) > 0
    raster = next(item for item in output.pages[0].regions if item.source_kind == RegionKind.IMAGE)
    assert raster.provenance.native_text_masked is False
    assert raster.requires_review is True
    assert "native_overlay_requires_review" in raster.review_reasons


def test_model_extracted_table_provenance_and_cell_details() -> None:
    from_image: DocumentInput = DocumentInput("table.png", fixture_image(), "doc:table")
    provider: VisionSpy = VisionSpy(ProviderTextResult(
        text="", confidence=0.95, structured_table=[["Code", "Amount"], ["X", None]],
        provider_name="test-gateway", model_id="vision-mock-v3",
    ))
    output = DataExtractionService(
        options=ExtractionOptions(image_route=ImageRoute.VISION_ONLY),
        vision_provider=provider,
    ).extract(from_image)
    region = output.pages[0].regions[0]
    assert region.kind == RegionKind.TABLE and region.content_type == RegionKind.TABLE
    assert region.source_kind == RegionKind.IMAGE
    assert region.method == ExtractionMethod.VISION_MODEL
    assert region.provenance.source_surface == SourceSurface.STANDALONE_IMAGE
    assert region.provenance.model_id == "vision-mock-v3"
    assert region.provenance.provider_name == "test-gateway"
    assert region.table == [["Code", "Amount"], ["X", None]]
    assert region.table_details.row_count == 2
    assert region.table_details.column_count == 2
    assert region.table_details.empty_cell_count == 1
    assert region.table_details.merged_cell_geometry_available is False
    assert region.requires_review is True


def test_docx_preserves_logical_order_without_fake_physical_pages() -> None:
    document = Document()
    document.add_paragraph("First")
    document.add_table(rows=1, cols=2).cell(0, 0).text = "cell"
    document.add_paragraph("Last")
    binary: io.BytesIO = io.BytesIO()
    document.save(binary)
    result = DataExtractionService().extract(DocumentInput("a.docx", binary.getvalue(), "docx-1"))
    regions = result.pages[0].regions
    assert [reg.kind for reg in regions] == [RegionKind.TEXT, RegionKind.TABLE, RegionKind.TEXT]
    assert [reg.position.document_order_index for reg in regions] == [1, 2, 3]
    assert [reg.provenance.source_surface for reg in regions] == [
        SourceSurface.DOCX_PARAGRAPH, SourceSurface.DOCX_TABLE, SourceSurface.DOCX_PARAGRAPH,
    ]
    assert result.pages[0].kind.value == "logical_document"
    assert result.pages[0].reading_order_quality == ReadingOrderQuality.DOCUMENT_FLOW
    assert "docx_physical_pagination_unknown" in result.pages[0].warnings


def test_spreadsheet_sheet_order_and_null_cells() -> None:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "FirstSheet"
    sheet.append(["A", "B", "C"])
    sheet.append(["1", None, "3"])
    other = workbook.create_sheet("SecondSheet")
    other.append(["Z"])
    buffer: io.BytesIO = io.BytesIO()
    workbook.save(buffer)
    result = DataExtractionService().extract(DocumentInput("sheet.xlsx", buffer.getvalue(), "book-1"))
    assert [page.label for page in result.pages] == ["FirstSheet", "SecondSheet"]
    assert result.pages[0].regions[0].table_details.empty_cell_count == 1
    assert result.pages[0].regions[0].table[1][1] is None
    assert result.pages[0].regions[0].provenance.source_surface == SourceSurface.WORKSHEET_CELLS
    assert [page.regions[0].position.document_order_index for page in result.pages] == [1, 2]
