"""Independent stress tests of v0.3.0. No production implementation changes."""
from __future__ import annotations

import io
import json
import hashlib
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import fitz
import pytest
from PIL import Image, ImageDraw
from docx import Document
from openpyxl import Workbook

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions, ImageRoute, ProviderTextResult
from data_extraction.models import RegionKind, ExtractionMethod, PageKind
from data_extraction.errors import ExtractionError


def inp(blob: bytes, name: str = "input.pdf", source: str = "audit:tenant:A") -> DocumentInput:
    return DocumentInput(name, blob, source)


def create_pdf(pages: int, with_hybrid: bool = False) -> bytes:
    pdf = fitz.open()
    for i in range(1, pages + 1):
        page = pdf.new_page(width=500, height=600)
        page.insert_text((25, 45), f"DOC_PAGE_{i:04d}_START")
        if with_hybrid and i % 2 == 0:
            page.insert_image(fitz.Rect(50, 100, 240, 180), stream=png(i))
        page.insert_text((25, 500), f"DOC_PAGE_{i:04d}_END")
    data = pdf.tobytes()
    pdf.close()
    return data


def png(seed: int = 1) -> bytes:
    image = Image.new("RGB", (400, 190), "white")
    draw = ImageDraw.Draw(image)
    draw.text((12, 12), f"IMAGE_{seed:03d}", fill="black")
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def make_docx(n: int) -> bytes:
    doc = Document()
    for i in range(n):
        doc.add_paragraph(f"PARAGRAPH_{i:04d}")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def make_xlsx(n: int) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Alpha"
    sheet.append(["First", "Second"])
    for i in range(n):
        sheet.append([f"REC_{i:04d}", None if i % 2 else str(i)])
    second = book.create_sheet("Beta")
    second.append(["SAFE", "END"])
    buf = io.BytesIO()
    book.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("count", [1,2,3,4,5,6,7,8,9,10,12,15,18,20,25,30,35,40])
@pytest.mark.parametrize("hybrid", [False, True])
def test_pdf_page_region_provenance_order_and_coverage(count: int, hybrid: bool) -> None:
    blob = create_pdf(count, hybrid)
    src = inp(blob)
    svc = DataExtractionService()
    m = svc.inspect(src)
    output = svc.extract(src)
    assert m.page_count == count
    assert [p.number for p in output.pages] == list(range(1, count + 1))
    assert m.sha256 == hashlib.sha256(blob).hexdigest()
    assert m.schema_version == output.schema_version == 'extraction.v0.6'
    assert m.document_order_region_ids == output.document_order_region_ids
    seq = [r for p in output.pages for r in p.regions]
    assert len(seq) == len(set(r.region_id for r in seq))
    assert [r.position.document_order_index for r in seq] == list(range(1,len(seq)+1))
    assert [r.region_id for r in seq] == list(output.document_order_region_ids)
    for page_number,page in enumerate(output.pages, start=1):
        native = ' '.join(r.text or '' for r in page.regions if r.method == ExtractionMethod.NATIVE)
        assert f'DOC_PAGE_{page_number:04d}_START' in native
        assert f'DOC_PAGE_{page_number:04d}_END' in native
        assert page.kind == PageKind.PHYSICAL_PAGE
        assert [r.position.reading_order_index for r in page.regions] == list(range(1,len(page.regions)+1))
        assert page.reading_order_region_ids == tuple(r.region_id for r in page.regions)
        for r in page.regions:
            assert r.provenance.sha256 == m.sha256
            assert r.position.page_number == page_number
            assert r.position.coordinate_system == 'pdf_points_top_left'
            assert all(0 <= val <= 1 for val in r.position.normalized_bbox)
        if hybrid and page_number % 2 == 0:
            assert any(r.source_kind == RegionKind.IMAGE for r in page.regions)
    assert 'image_bytes' not in json.dumps(svc.to_dict(output))


@pytest.mark.parametrize('n', [1,2,4,8,12,20,32,50,75,100,150])
def test_docx_flow_and_no_fake_physical_page(n: int) -> None:
    result = DataExtractionService().extract(inp(make_docx(n), 'file.docx'))
    assert len(result.pages) == 1
    assert result.pages[0].kind == PageKind.LOGICAL_DOCUMENT
    assert len(result.pages[0].regions) == n
    assert [r.text for r in result.pages[0].regions] == [f'PARAGRAPH_{i:04d}' for i in range(n)]
    assert [r.position.document_order_index for r in result.pages[0].regions] == list(range(1,n+1))


@pytest.mark.parametrize('n', [1,2,3,4,6,10,20,40,80,120,250])
def test_xlsx_sheet_data_and_missing_cells(n: int) -> None:
    result = DataExtractionService().extract(inp(make_xlsx(n), 'file.xlsx'))
    assert [page.label for page in result.pages] == ['Alpha','Beta']
    a = result.pages[0].regions[0]
    assert a.kind == RegionKind.TABLE
    assert len(a.table) == n+1
    assert a.table[0] == ['First', 'Second']
    assert a.table[1][0] == 'REC_0000'
    if n > 1:
        assert a.table[2][1] is None
    assert result.pages[0].kind == PageKind.SHEET


@pytest.mark.parametrize('n', [1,2,3,4,7,11,20,50,100,200])
def test_csv_source_and_rows(n: int) -> None:
    rows = ['id,description,flag'] + [f'{i},Item_{i},ok' for i in range(n)]
    output = DataExtractionService().extract(inp('\n'.join(rows).encode(), 'test.csv'))
    table = output.pages[0].regions[0].table
    assert len(table) == n+1
    assert table[-1] == [str(n-1), f'Item_{n-1}', 'ok']
    assert output.pages[0].regions[0].provenance.source_surface.value == 'csv_cells'


@pytest.mark.parametrize('langtext', [
    'مرحبا بالعالم', 'English text', 'العربية + English 2026',
    'سعر 125.500 د.ك', 'ID O01I8B vs 00118B',
    'نص جديد\nثاني سطر', 'A\tB\tC', 'إدارةُ المواردِ البشرية',
    'مرحبا 😊', '12345678901234567890', 'https://example.test/a?x=5&y=7',
])
def test_exact_native_unicode_text_preserved(langtext: str) -> None:
    output = DataExtractionService().extract(inp(langtext.encode('utf-8'), 'text.txt'))
    region = output.pages[0].regions[0]
    assert region.text == langtext
    assert region.provenance.extraction_method == ExtractionMethod.NATIVE


@pytest.mark.parametrize('count', [1,2,5,10,15,20])
def test_vision_budget_enforced_even_across_many_regions(count: int) -> None:
    @dataclass
    class Model:
        calls: int = 0
        def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
            self.calls += 1
            return ProviderTextResult('description', 0.99, provider_name='mock', model_id='m1')
    model = Model()
    pdf = fitz.open()
    page = pdf.new_page(width=500, height=760)
    for i in range(count):
        x = (i % 4) * 120 + 10
        y = (i // 4) * 132 + 10
        if y + 95 < 760:
            page.insert_image(fitz.Rect(x,y,x+100,y+90),stream=png(i))
    source = inp(pdf.tobytes()); pdf.close()
    service = DataExtractionService(ExtractionOptions(image_route=ImageRoute.VISION_ONLY,max_vision_calls=3), vision_provider=model)
    output = service.extract(source)
    assert model.calls <= 3
    assert model.calls == output.total_vision_calls
    assert model.calls == min(3, len([r for r in output.pages[0].regions if r.source_kind == RegionKind.IMAGE]))


@pytest.mark.parametrize('option,code', [
    (ExtractionOptions(max_input_bytes=1), 'FILE_TOO_LARGE'),
    (ExtractionOptions(max_pages=0), 'INVALID_OPTIONS'),
    (ExtractionOptions(max_vision_calls=-1), 'INVALID_OPTIONS'),
    (ExtractionOptions(image_dpi=30), 'INVALID_OPTIONS'),
    (ExtractionOptions(ocr_confidence_threshold=2), 'INVALID_OPTIONS'),
])
def test_invalid_options_fail_closed(option: ExtractionOptions, code: str) -> None:
    with pytest.raises(ExtractionError) as captured:
        DataExtractionService(option).inspect(inp(create_pdf(1)))
    assert captured.value.code == code


def test_parallel_extract_same_service_is_deterministic() -> None:
    blob = create_pdf(4, True)
    svc = DataExtractionService()
    with ThreadPoolExecutor(max_workers=6) as pool:
        outputs = list(pool.map(lambda i: svc.extract(inp(blob, source=f'tenant:doc{i}')), range(18)))
    assert len(outputs) == 18
    for i, o in enumerate(outputs):
        assert o.manifest.source_id == f'tenant:doc{i}'
        assert [p.number for p in o.pages] == [1,2,3,4]
        assert all(r.provenance.source_id == f'tenant:doc{i}' for p in o.pages for r in p.regions)
