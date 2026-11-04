import io
from types import SimpleNamespace

import fitz
import pytest
from PIL import Image, ImageDraw
from reportlab.pdfgen import canvas

from data_extraction.dispatch_extraction.extract_table import ExtractTableAction
from data_extraction.inspect_document.inspectors.native.pdf import _pdf_pages
from data_extraction.inspect_document.inspectors.native.pdf_vectors import vector_candidate_bbox
from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest
from data_extraction.inspect_document.models import CandidateRegion
from data_extraction.service import DataExtractionService
from data_extraction.shared.models import DocumentInput, ProviderTextResult, RegionKind, TableTextEvidence, TableCellLocation
from data_extraction.shared.errors import ExtractionError


def _request(content: bytes) -> InspectDocumentRequest:
    return InspectDocumentRequest(file_name="sample.pdf", source_id="test", content=content)


def _image(color=(80, 120, 160)) -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (100, 100), color).save(stream, format="PNG")
    return stream.getvalue()


def test_table_keeps_native_text_evidence_when_table_omits_value(monkeypatch):
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((40, 60), "Mouse 50")
    content = doc.tobytes()
    doc.close()

    table = SimpleNamespace(bbox=(20, 20, 200, 100), extract=lambda: [["Mouse", None]])
    monkeypatch.setattr(fitz.Page, "find_tables", lambda self: SimpleNamespace(tables=[table]))
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    candidate = next(r for r in inspected.regions if r.kind == RegionKind.TABLE)
    assert any("50" in item.text for item in candidate.table_text_evidence)
    assert not any(r.kind == RegionKind.TEXT and "50" in (r.text or "") for r in inspected.regions)
    extracted = ExtractTableAction().execute(candidate)
    assert extracted.table == [["Mouse", None]]
    assert any("50" in item.text for item in extracted.table_text_evidence)
    assert extracted.requires_review
    assert not extracted.content_complete
    result = DataExtractionService().extract(DocumentInput("sample.pdf", content, "test"))
    extracted_table = next(r for r in result.pages[0].regions if r.kind == RegionKind.TABLE)
    assert "50" in extracted_table.table_text_evidence[0].text
    assert "native_table_text_mismatch_review" in extracted_table.review_reasons


def test_matching_arabic_table_text_does_not_create_a_mismatch():
    region = CandidateRegion(
        region_id="table", kind=RegionKind.TABLE, table=[["منتج", "٥٠"]],
        table_text_evidence=(TableTextEvidence("منتج ٥٠", (0, 0, 50, 10), 1),),
    )
    extracted = ExtractTableAction().execute(region)
    assert extracted.content_complete
    assert "native_table_text_mismatch_review" not in extracted.review_reasons


def test_table_evidence_detects_values_swapped_between_cells():
    candidate = CandidateRegion(
        "table", RegionKind.TABLE, table=[["50", "Mouse"]],
        table_text_evidence=(
            TableTextEvidence("Mouse", (10, 10, 40, 20), 1),
            TableTextEvidence("50", (60, 10, 80, 20), 2),
        ),
        table_cell_locations=(
            TableCellLocation(0, 0, 1, 1, (0, 0, 50, 30)),
            TableCellLocation(0, 1, 1, 1, (50, 0, 100, 30)),
        ),
    )
    extracted = ExtractTableAction().execute(candidate)
    assert extracted.requires_review
    assert not extracted.content_complete
    assert "native_table_cell_text_mismatch_review" in extracted.review_reasons


def test_table_evidence_with_no_cell_geometry_requires_review():
    candidate = CandidateRegion(
        "table", RegionKind.TABLE, table=[["50", "Mouse"]],
        table_text_evidence=(TableTextEvidence("Mouse 50", (0, 0, 90, 20), 1),),
    )
    extracted = ExtractTableAction().execute(candidate)
    assert extracted.requires_review
    assert "native_table_cell_geometry_unavailable_review" in extracted.review_reasons


def test_swapped_table_cells_are_reviewed_in_full_extraction(monkeypatch):
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((30, 50), "Mouse")
    page.insert_text((130, 50), "50")
    content = doc.tobytes()
    doc.close()
    row = SimpleNamespace(cells=[(20, 20, 100, 80), (100, 20, 200, 80)])
    table = SimpleNamespace(bbox=(20, 20, 200, 80), rows=[row], extract=lambda: [["50", "Mouse"]])
    monkeypatch.setattr(fitz.Page, "find_tables", lambda self: SimpleNamespace(tables=[table]))
    result = DataExtractionService().extract(DocumentInput("sample.pdf", content, "test"))
    extracted = next(r for r in result.pages[0].regions if r.kind == RegionKind.TABLE)
    assert extracted.table == [["50", "Mouse"]]
    assert "native_table_cell_text_mismatch_review" in extracted.review_reasons
    assert not extracted.content_complete


def test_hybrid_image_is_unmasked_during_inspection():
    source_image = Image.new("RGB", (300, 200), (80, 120, 160))
    ImageDraw.Draw(source_image).rectangle((100, 60, 200, 120), fill="black")
    image_stream = io.BytesIO()
    source_image.save(image_stream, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_image(page.rect, stream=image_stream.getvalue())
    page.draw_rect(fitz.Rect(100, 60, 200, 120), color=None, fill=(1, 1, 1))
    page.insert_text((105, 100), "Native overlay", fontsize=18)
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    image = next(r for r in inspected.regions if r.kind == RegionKind.IMAGE)
    assert image.image_role == "native_text_overlay"
    assert image.native_text_masked is False
    assert image.original_image_ocr_safe is True
    with Image.open(io.BytesIO(image.original_image_bytes)) as original:
        assert original.convert("RGB").getpixel((110, 80)) == (0, 0, 0)
    with Image.open(io.BytesIO(image.image_bytes)) as raster:
        assert raster.convert("RGB").getpixel((round(110 * 150 / 72), round(80 * 150 / 72))) == (255, 255, 255)

    class OCR:
        def __init__(self):
            self.images = []

        def recognize(self, image_bytes, mime_type):
            self.images.append(image_bytes)
            return ProviderTextResult("Some raster content", 0.99)

    ocr = OCR()
    result = DataExtractionService(ocr_provider=ocr).extract(DocumentInput("sample.pdf", content, "test"))
    extracted = next(r for r in result.pages[0].regions if r.source_kind == RegionKind.IMAGE)
    source_images = []
    for payload in ocr.images:
        with Image.open(io.BytesIO(payload)) as source_for_ocr:
            if source_for_ocr.size == (300, 200):
                source_images.append(source_for_ocr.convert("RGB").copy())
    assert source_images
    assert source_images[0].getpixel((110, 80)) == (0, 0, 0)
    assert any(payload != image.image_bytes for payload in ocr.images)
    assert extracted.provenance.native_text_masked is False
    assert "native_overlay_requires_review" in extracted.review_reasons


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_keeps_image_in_unrotated_coordinates(rotation):
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    page.insert_image(fitz.Rect(300, 40, 380, 120), stream=_image())
    page.set_rotation(rotation)
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    image = next(r for r in inspected.regions if r.source_surface and r.source_surface.value == "pdf_embedded_image")
    assert image.bbox == pytest.approx((300, 40, 380, 120))
    assert (inspected.width, inspected.height) == pytest.approx((400, 200))
    assert image.original_image_ocr_safe is False
    with Image.open(io.BytesIO(image.image_bytes)) as raster:
        assert raster.convert("RGB").getpixel((raster.width // 2, raster.height // 2)) == (80, 120, 160)


def test_text_dict_does_not_include_image_bytes(monkeypatch):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(20, 20, 120, 120), stream=_image())
    content = doc.tobytes()
    doc.close()
    calls = []
    original = fitz.Page.get_text

    def observed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        if args and args[0] == "dict":
            calls.append(result)
        return result

    monkeypatch.setattr(fitz.Page, "get_text", observed)
    page_result = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    assert any(r.kind == RegionKind.IMAGE for r in page_result.regions)
    assert calls and all("image" not in block for block in calls[0]["blocks"])


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropped_rotated_page_keeps_image(rotation):
    doc = fitz.open()
    page = doc.new_page(width=500, height=300)
    page.insert_image(fitz.Rect(300, 100, 380, 180), stream=_image())
    page.set_cropbox(fitz.Rect(50, 30, 450, 230))
    page.set_rotation(rotation)
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    image = next(r for r in inspected.regions if r.source_surface and r.source_surface.value == "pdf_embedded_image")
    assert image.bbox == pytest.approx((250, 70, 330, 150))
    assert (inspected.width, inspected.height) == pytest.approx((400, 200))
    with Image.open(io.BytesIO(image.image_bytes)) as raster:
        assert raster.convert("RGB").getpixel((raster.width // 2, raster.height // 2)) == (80, 120, 160)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_cropped_rotated_table_uses_same_coordinates_as_text(rotation):
    doc = fitz.open()
    page = doc.new_page(width=500, height=300)
    for x in (100, 200, 300):
        page.draw_line((x, 70), (x, 190))
    for y in (70, 130, 190):
        page.draw_line((100, y), (300, y))
    for x, y, text in ((110, 100, "Mouse"), (210, 100, "50"),
                       (110, 160, "Keyboard"), (210, 160, "100")):
        page.insert_text((x, y), text)
    page.set_cropbox(fitz.Rect(50, 30, 450, 230))
    page.set_rotation(rotation)
    content = doc.tobytes()
    doc.close()

    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    table = next(r for r in inspected.regions if r.kind == RegionKind.TABLE)
    assert table.bbox == pytest.approx((50, 40, 250, 160))
    assert table.table == [["Mouse", "50"], ["Keyboard", "100"]]
    assert len(table.table_cell_locations) == 4
    assert {item.text for item in table.table_text_evidence} == {"Mouse", "50", "Keyboard", "100"}
    assert not any(r.kind == RegionKind.TEXT for r in inspected.regions)


def test_inline_and_repeated_images_are_detected_without_image_bytes_in_text_dict():
    stream = io.BytesIO()
    pdf = canvas.Canvas(stream, pagesize=(400, 300))
    image = Image.new("RGB", (20, 20), (80, 120, 160))
    pdf.drawInlineImage(image, 30, 200, width=60, height=60)
    pdf.drawInlineImage(image, 150, 200, width=60, height=60)
    pdf.save()
    page = _pdf_pages(_request(stream.getvalue()), InspectDocumentOptions())[0]
    images = [r for r in page.regions if r.kind == RegionKind.IMAGE]
    assert len(images) == 2
    assert images[0].bbox == pytest.approx((30, 40, 90, 100))
    assert images[1].bbox == pytest.approx((150, 40, 210, 100))
    assert all(image.original_image_bytes for image in images)


def test_failed_vector_render_does_not_consume_image_region_limit(monkeypatch):
    from data_extraction.inspect_document.inspectors.native import pdf

    doc = fitz.open()
    page = doc.new_page()
    monkeypatch.setattr(pdf, "vector_candidate_bbox", lambda *args: (10, 10, 50, 50))
    monkeypatch.setattr(pdf, "_render_pdf_image", lambda *args: (_ for _ in ()).throw(
        ExtractionError("IMAGE_TOO_LARGE", "too large")))
    regions = [CandidateRegion("text", RegionKind.TEXT, text="text")]
    warnings = []
    region, count = pdf._extract_vector_candidate(
        page, 1, regions, [], [], InspectDocumentOptions(max_image_regions=1), warnings, 0
    )
    assert region is None
    assert count == 0
    assert "unresolved_vector_graphics" in warnings
    monkeypatch.setattr(pdf, "_render_pdf_image", lambda *args: (b"image", False))
    region, count = pdf._extract_vector_candidate(
        page, 2, regions, [], [], InspectDocumentOptions(max_image_regions=1), [], count
    )
    assert region is not None
    assert count == 1
    doc.close()


def test_repeated_filled_table_row_bands_are_not_vector_graphics():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.draw_rect(fitz.Rect(0, 0, 595, 64), color=None, fill=(0.2, 0.3, 0.5))
    for top in (216, 251, 286, 321, 356, 391):
        page.draw_rect(
            fitz.Rect(48, top, 547, top + 17.5),
            color=None,
            fill=(0.92, 0.92, 0.92),
        )
    assert vector_candidate_bbox(page, []) is None
    doc.close()


def test_original_index_tracks_source_blocks_not_geometry_order():
    doc = fitz.open()
    page = doc.new_page(width=400, height=500)
    page.insert_image(fitz.Rect(20, 20, 100, 320), stream=_image())
    page.insert_text((160, 100), "second")
    page.insert_text((160, 400), "third")
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    image = next(r for r in inspected.regions if r.kind == RegionKind.IMAGE)
    text = [r for r in inspected.regions if r.kind == RegionKind.TEXT]
    assert image.original_index == 1
    assert [r.original_index for r in text] == [2, 3]


def test_text_region_ids_follow_source_order_not_page_geometry():
    def inspect_texts(first_y: int, second_y: int):
        doc = fitz.open()
        page = doc.new_page(width=300, height=400)
        page.insert_text((40, first_y), "First source block")
        page.insert_text((40, second_y), "Second source block")
        content = doc.tobytes()
        doc.close()
        inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
        return [
            (region.text, region.region_id, region.original_index)
            for region in inspected.regions
            if region.kind == RegionKind.TEXT
        ]

    expected = [
        ("First source block", "p1:text:1", 1),
        ("Second source block", "p1:text:2", 2),
    ]
    assert inspect_texts(350, 50) == expected
    assert inspect_texts(50, 350) == expected


def test_pdf_image_soft_mask_is_preserved_separately():
    stream = io.BytesIO()
    Image.new("RGBA", (80, 80), (100, 150, 200, 80)).save(stream, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_image(fitz.Rect(20, 20, 180, 180), stream=stream.getvalue())
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    image = next(r for r in inspected.regions if r.kind == RegionKind.IMAGE)
    assert image.original_image_bytes
    assert image.original_image_mask_bytes
    with Image.open(io.BytesIO(image.original_image_mask_bytes)) as mask:
        assert mask.convert("L").getpixel((10, 10)) == 80

    class OCR:
        def __init__(self):
            self.images = []

        def recognize(self, image_bytes, mime_type):
            self.images.append(image_bytes)
            return ProviderTextResult("image text", 0.99)

    ocr = OCR()
    DataExtractionService(ocr_provider=ocr).extract(
        DocumentInput("sample.pdf", content, "test")
    )
    embedded_payload = None
    for payload in ocr.images:
        with Image.open(io.BytesIO(payload)) as candidate:
            if candidate.size == (80, 80):
                embedded_payload = payload
                break
    assert embedded_payload is not None
    with Image.open(io.BytesIO(embedded_payload)) as normalized:
        assert normalized.mode == "RGB"
        red, green, blue = normalized.getpixel((10, 10))
        assert red == pytest.approx(255 - (255 - 100) * 80 / 255, abs=2)
        assert green == pytest.approx(255 - (255 - 150) * 80 / 255, abs=2)
        assert blue == pytest.approx(255 - (255 - 200) * 80 / 255, abs=2)


def test_rotated_pdf_image_uses_page_render_for_ocr():
    doc = fitz.open()
    page = doc.new_page(width=400, height=200)
    page.insert_image(fitz.Rect(300, 40, 380, 120), stream=_image())
    page.set_rotation(90)
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions())[0]
    image = next(r for r in inspected.regions if r.kind == RegionKind.IMAGE)
    assert image.original_image_ocr_safe is False

    class OCR:
        def __init__(self):
            self.images = []

        def recognize(self, image_bytes, mime_type):
            self.images.append(image_bytes)
            return ProviderTextResult("image text", 0.99)

    ocr = OCR()
    DataExtractionService(ocr_provider=ocr).extract(
        DocumentInput("sample.pdf", content, "test")
    )
    assert ocr.images
    assert ocr.images[0] == image.image_bytes


def test_original_image_budget_marks_unavailable_without_losing_render():
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_image(fitz.Rect(20, 20, 180, 180), stream=_image())
    content = doc.tobytes()
    doc.close()
    inspected = _pdf_pages(_request(content), InspectDocumentOptions(max_original_image_bytes=1000))[0]
    image = next(r for r in inspected.regions if r.kind == RegionKind.IMAGE)
    assert image.image_bytes
    assert image.original_image_bytes is None
    assert "pdf_original_image_unavailable" in inspected.warnings
