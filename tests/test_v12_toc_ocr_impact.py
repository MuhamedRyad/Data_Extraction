from __future__ import annotations

import io

import fitz
from PIL import Image, ImageDraw

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.analyze_layout.pdf_toc import NativeTextLine, detect_toc_table
from data_extraction.providers.contracts import OCRTextRegion
from data_extraction.shared.models import ProviderTextResult, RegionKind


def _png(width: int, height: int, *, grid: bool = False) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    if grid:
        draw = ImageDraw.Draw(image)
        for x in (10, 110, 210):
            draw.line((x, 10, x, height - 10), fill="black", width=3)
        for y in (10, 60, 110, 160):
            draw.line((10, y, width - 10, y), fill="black", width=3)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_toc_geometry_binds_duplicate_page_numbers_to_distinct_rows() -> None:
    lines = (
        NativeTextLine("حقوق المستهلك", (300, 100, 520, 120), "t1"),
        NativeTextLine("٣٣", (60, 100, 85, 120), "n1"),
        NativeTextLine("سياسة الاسترجاع", (300, 150, 520, 170), "t2"),
        NativeTextLine("٣٣", (60, 150, 85, 170), "n2"),
        NativeTextLine("الشكاوى", (300, 200, 520, 220), "t3"),
        NativeTextLine("٤١", (60, 200, 85, 220), "n3"),
    )
    result = detect_toc_table(lines, 600, 800, 2)
    assert result is not None
    assert result.table_region.table == [
        ["حقوق المستهلك", "33"],
        ["سياسة الاسترجاع", "33"],
        ["الشكاوى", "41"],
    ]
    assert result.table_region.table_cell_locations is not None
    assert len(result.table_region.table_cell_locations) == 6


def test_pdf_toc_becomes_structured_table_and_not_diagram() -> None:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_text((250, 70), "Contents")
    for index, (title, number) in enumerate((
        ("Consumer Rights", "33"),
        ("Refund Policy", "33"),
        ("Complaints", "41"),
        ("Warranty", "55"),
    )):
        y = 150 + index * 55
        page.insert_text((300, y), title)
        page.insert_text((60, y), number)
    content = doc.tobytes()
    doc.close()

    result = DataExtractionService().extract(DocumentInput("toc.pdf", content, "toc"))
    extracted_toc = [region for region in result.pages[0].regions if region.region_id == "p1:toc:1"]
    assert len(extracted_toc) == 1
    assert extracted_toc[0].kind == RegionKind.TABLE
    assert extracted_toc[0].table == [
        ["Consumer Rights", "33"],
        ["Refund Policy", "33"],
        ["Complaints", "41"],
        ["Warranty", "55"],
    ]
    assert "page_semantic_table_of_contents" in result.pages[0].warnings
    assert "page_semantic_diagram_hierarchy" not in result.pages[0].warnings


class PreprocessSensitiveOCR:
    def __init__(self) -> None:
        self.calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        with Image.open(io.BytesIO(image_bytes)) as image:
            if image.mode == "L":
                return ProviderTextResult("وزارة التجارة https://example.gov", 0.94, provider_name="fake")
        return ProviderTextResult("وزاره التجاره htps://examp1e.gov", 0.42, provider_name="fake")


def test_low_confidence_image_ocr_can_recover_with_preprocessing_without_claiming_consensus() -> None:
    provider = PreprocessSensitiveOCR()
    result = DataExtractionService(
        ExtractionOptions(enable_low_confidence_ocr_preprocessing=True, max_ocr_preprocess_attempts=2),
        ocr_provider=provider,
    ).extract(DocumentInput("footer.png", _png(500, 100), "footer"))
    region = result.pages[0].regions[0]
    assert region.text == "وزارة التجارة https://example.gov"
    assert region.confidence == 0.94
    assert region.recovery_applied
    assert "ocr_preprocessing_recovery_review" in region.review_reasons
    assert "ocr_preprocessing_text_changed_review" in region.review_reasons
    assert provider.calls == 2


class GeometryTableOCR:
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult("A 1\nB 2\nC 3", 0.95, provider_name="geometry-fake")

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return (
            OCRTextRegion("A", (30, 25, 55, 45), .95), OCRTextRegion("1", (150, 25, 170, 45), .95),
            OCRTextRegion("B", (30, 75, 55, 95), .95), OCRTextRegion("2", (150, 75, 170, 95), .95),
            OCRTextRegion("C", (30, 125, 55, 145), .95), OCRTextRegion("3", (150, 125, 170, 145), .95),
        )


def test_raster_table_uses_token_geometry_to_preserve_rows_and_columns() -> None:
    result = DataExtractionService(
        ExtractionOptions(detect_raster_table_grids=True, enable_ocr_table_geometry_recovery=True),
        ocr_provider=GeometryTableOCR(),
    ).extract(DocumentInput("table.png", _png(230, 180, grid=True), "table"))
    region = result.pages[0].regions[0]
    assert region.kind == RegionKind.TABLE
    assert region.table == [["A", "1"], ["B", "2"], ["C", "3"]]
    assert "ocr_table_geometry_reconstructed_review" in region.review_reasons
    assert "ocr_table_structure_requires_verification" in region.review_reasons


def test_repeated_navigation_images_are_counted_as_noncritical_unresolved() -> None:
    nav = _png(240, 35)
    doc = fitz.open()
    for page_index in range(3):
        page = doc.new_page(width=600, height=800)
        page.insert_text((60, 100), f"Body {page_index + 1}")
        page.insert_image(fitz.Rect(180, 730, 420, 765), stream=nav)
    content = doc.tobytes()
    doc.close()

    result = DataExtractionService().extract(DocumentInput("navigation.pdf", content, "nav"))
    assert result.unresolved_region_count == 3
    assert result.critical_unresolved_region_count == 0
    assert result.noncritical_unresolved_region_count == 3
    assert result.content_complete
    for page in result.pages:
        unresolved = [region for region in page.regions if not region.content_complete]
        assert len(unresolved) == 1
        assert unresolved[0].critical_for_completeness is False
        assert unresolved[0].relations is not None
        assert unresolved[0].relations.region_role == "repeated_navigation_candidate"
