"""v0.2 routing and raster-layout regression tests (no real paid models)."""
from __future__ import annotations

import io
from dataclasses import dataclass

import fitz
from PIL import Image, ImageDraw

from data_extraction import (DataExtractionService, DocumentInput, ExtractionOptions,
                             ImageRoute, ProviderTextResult, RegionKind)
from data_extraction.adapters.image_layout import is_raster_grid_table
from data_extraction.models import ExtractionMethod


def make_input(blob: bytes, file_name: str = "page.png") -> DocumentInput:
    return DocumentInput(file_name, blob, "tenant:test-01")


def grid_png() -> bytes:
    image: Image.Image = Image.new("RGB", (400, 300), "white")
    draw: ImageDraw.ImageDraw = ImageDraw.Draw(image)
    for y in (45, 105, 165, 225, 275):
        draw.line((30, y, 370, y), fill="black", width=2)
    for x in (30, 140, 260, 370):
        draw.line((x, 45, x, 275), fill="black", width=2)
    stream: io.BytesIO = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def text_png() -> bytes:
    image: Image.Image = Image.new("RGB", (400, 150), "white")
    draw: ImageDraw.ImageDraw = ImageDraw.Draw(image)
    draw.text((45, 55), "Hello text", fill="black")
    stream: io.BytesIO = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


@dataclass
class OCRSpy:
    result: ProviderTextResult
    calls: int = 0
    last_image: bytes | None = None

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        self.last_image = image_bytes
        return self.result


@dataclass
class VisionSpy:
    result: ProviderTextResult
    calls: int = 0
    last_role: str | None = None

    def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
        self.calls += 1
        self.last_role = role
        return self.result


def test_inspect_can_hint_raster_table_but_does_not_invent_cells() -> None:
    ocr: OCRSpy = OCRSpy(ProviderTextResult("ABC 5.000", 0.99))
    vision: VisionSpy = VisionSpy(ProviderTextResult("Table", 0.7, [["Code", "Amount"], ["ABC", "5.000"]]))
    service: DataExtractionService = DataExtractionService(ocr_provider=ocr, vision_provider=vision)
    manifest = service.inspect(make_input(grid_png()))
    assert manifest.page_count == 1
    assert manifest.pages[0].content_types == (RegionKind.IMAGE,)
    assert manifest.pages[0].regions[0].content_hint == RegionKind.TABLE
    assert manifest.pages[0].regions[0].image_role == "raster_grid_table"
    assert ocr.calls == vision.calls == 0


def test_grid_goes_to_vision_first_and_returns_verified_shape_not_authority() -> None:
    ocr: OCRSpy = OCRSpy(ProviderTextResult("unused", 0.99))
    vision: VisionSpy = VisionSpy(ProviderTextResult("", 0.8, [["Ref", "Cost"], ["A", "5.000"]]))
    result = DataExtractionService(ocr_provider=ocr, vision_provider=vision).extract(make_input(grid_png()))
    extracted = result.pages[0].regions[0]
    assert extracted.kind == RegionKind.TABLE
    assert extracted.table == [["Ref", "Cost"], ["A", "5.000"]]
    assert extracted.method == ExtractionMethod.VISION_MODEL
    assert extracted.route_reason == "raster_table_vision_first"
    assert extracted.attempted_methods == (ExtractionMethod.VISION_MODEL,)
    assert vision.calls == 1 and ocr.calls == 0 and vision.last_role == "raster_grid_table"
    assert not result.ready_for_publication


def test_raster_grid_with_ocr_only_cannot_claim_structured_table() -> None:
    ocr: OCRSpy = OCRSpy(ProviderTextResult("Code Amount 5.000", 0.99))
    result = DataExtractionService(ExtractionOptions(image_route=ImageRoute.OCR_ONLY), ocr).extract(make_input(grid_png()))
    item = result.pages[0].regions[0]
    assert item.method == ExtractionMethod.OCR and item.kind == RegionKind.TEXT
    assert item.source_kind == RegionKind.IMAGE
    assert "raster_table_structure_not_reconstructed" in item.review_reasons
    assert item.requires_review


def test_raster_grid_vision_only_uses_vision() -> None:
    vision: VisionSpy = VisionSpy(ProviderTextResult("", 0.9, [["A", "B"], ["1", "2"]]))
    result = DataExtractionService(ExtractionOptions(image_route=ImageRoute.VISION_ONLY),
                                   vision_provider=vision).extract(make_input(grid_png()))
    assert result.pages[0].regions[0].kind == RegionKind.TABLE
    assert vision.calls == 1


def test_raster_grid_model_missing_structure_falls_back_to_ocr_as_review() -> None:
    vision: VisionSpy = VisionSpy(ProviderTextResult("It has columns", 0.9))
    ocr: OCRSpy = OCRSpy(ProviderTextResult("A 1", 0.99))
    result = DataExtractionService(ocr_provider=ocr, vision_provider=vision).extract(make_input(grid_png()))
    item = result.pages[0].regions[0]
    assert vision.calls == ocr.calls == 1
    assert item.method == ExtractionMethod.OCR
    assert item.attempted_methods == (ExtractionMethod.VISION_MODEL, ExtractionMethod.OCR)
    assert "raster_table_structure_not_reconstructed" in item.review_reasons


def test_invalid_ocr_confidence_triggers_vision_instead_of_auto_accept() -> None:
    ocr: OCRSpy = OCRSpy(ProviderTextResult("confidence wrong", 3.5))
    vision: VisionSpy = VisionSpy(ProviderTextResult("Vision fallback", 0.7))
    item = DataExtractionService(ocr_provider=ocr, vision_provider=vision).extract(make_input(text_png())).pages[0].regions[0]
    assert ocr.calls == vision.calls == 1
    assert item.method == ExtractionMethod.VISION_MODEL
    assert item.attempted_methods == (ExtractionMethod.OCR, ExtractionMethod.VISION_MODEL)
    assert item.requires_review


def test_native_text_overlay_is_sent_to_ocr_without_masking() -> None:
    doc: fitz.Document = fitz.open()
    page: fitz.Page = doc.new_page(width=400, height=200)
    page.insert_image(fitz.Rect(0, 0, 400, 200), stream=text_png())
    page.insert_text((100, 90), "NATIVE LABEL", fontsize=18)
    binary: bytes = doc.tobytes()
    doc.close()
    spy: OCRSpy = OCRSpy(ProviderTextResult("Some other raster information", 0.99))
    result = DataExtractionService(ocr_provider=spy).extract(make_input(binary, "hybrid.pdf"))
    assert spy.calls == 1 and spy.last_image is not None
    native = [r for r in result.pages[0].regions if r.kind == RegionKind.TEXT]
    raster = [r for r in result.pages[0].regions if r.source_kind == RegionKind.IMAGE]
    assert any("NATIVE LABEL" in (r.text or "") for r in native)
    assert len(raster) == 1 and raster[0].requires_review
    assert raster[0].route_reason == "ocr_confidence_gate"
    assert "native_overlay_requires_review" in raster[0].review_reasons
    with Image.open(io.BytesIO(spy.last_image)) as ocr_source:
        assert ocr_source.size == (400, 150)
        assert ocr_source.mode == "RGB"


def test_text_image_not_mistaken_for_grid() -> None:
    assert is_raster_grid_table(text_png()) is False
    service: DataExtractionService = DataExtractionService()
    manifest = service.inspect(make_input(text_png()))
    assert manifest.pages[0].regions[0].content_hint is None


def test_irregular_model_table_requires_review() -> None:
    vision: VisionSpy = VisionSpy(ProviderTextResult("", 0.99, [["A", "B"], ["X"]]))
    result = DataExtractionService(ExtractionOptions(image_route=ImageRoute.VISION_ONLY),
                                   vision_provider=vision).extract(make_input(grid_png()))
    item = result.pages[0].regions[0]
    assert "table_shape_requires_review" in item.review_reasons
    assert not result.ready_for_publication


def test_disable_grid_detection_routes_ocr_first() -> None:
    ocr: OCRSpy = OCRSpy(ProviderTextResult("HELLO", 0.99))
    vision: VisionSpy = VisionSpy(ProviderTextResult("model", 0.9))
    options = ExtractionOptions(detect_raster_table_grids=False)
    result = DataExtractionService(options, ocr, vision).extract(make_input(grid_png()))
    assert result.manifest.pages[0].regions[0].content_hint is None
    assert ocr.calls == 1 and vision.calls == 0


def test_grid_hint_not_assumed_without_borders() -> None:
    # Grid detector is conservative: it reports unknown rather than guessing cells.
    blank: Image.Image = Image.new("RGB", (250, 250), "white")
    output: io.BytesIO = io.BytesIO()
    blank.save(output, format="PNG")
    manifest = DataExtractionService().inspect(make_input(output.getvalue()))
    assert manifest.pages[0].regions[0].content_hint is None


def test_docx_large_image_is_rejected_by_pixel_budget() -> None:
    from docx import Document
    from data_extraction.errors import ExtractionError
    from pytest import raises

    word = Document()
    word.add_picture(io.BytesIO(text_png()))
    output: io.BytesIO = io.BytesIO()
    word.save(output)
    options = ExtractionOptions(max_image_pixels=100)
    with raises(ExtractionError) as failure:
        DataExtractionService(options).inspect(make_input(output.getvalue(), "report.docx"))
    assert failure.value.code == "IMAGE_TOO_LARGE"
