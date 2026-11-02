"""Deliberately strict adversarial acceptance checks; failures indicate real feature gaps."""
from __future__ import annotations
import io
import fitz
from PIL import Image
from docx import Document
from openpyxl import Workbook
from openpyxl.drawing.image import Image as ExcelImage
from data_extraction import DataExtractionService, DocumentInput
from data_extraction.models import RegionKind


def run(blob: bytes, name: str):
    return DataExtractionService().extract(DocumentInput(name, blob, 'audit:gaps'))


def picture():
    image = Image.new('RGB', (120, 80), 'white')
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def test_two_column_pdf_respects_column_reading_order() -> None:
    doc = fitz.open(); page=doc.new_page(width=500,height=400)
    for x,y,t in [(30,40,'LEFT_ONE'),(30,140,'LEFT_TWO'),(270,40,'RIGHT_ONE'),(270,140,'RIGHT_TWO')]:
        page.insert_text((x,y),t)
    blob=doc.tobytes(); doc.close()
    texts=[r.text for r in run(blob,'col.pdf').pages[0].regions]
    assert texts == ['LEFT_ONE','LEFT_TWO','RIGHT_ONE','RIGHT_TWO'], 'Reading order must follow columns, not horizontal scan lines'


def test_docx_inline_image_is_in_exact_run_position() -> None:
    doc=Document(); p=doc.add_paragraph(); p.add_run('BEFORE '); p.add_run().add_picture(io.BytesIO(picture())); p.add_run(' AFTER')
    stream=io.BytesIO(); doc.save(stream)
    regions=run(stream.getvalue(),'inline.docx').pages[0].regions
    assert [(r.kind.value, r.text) for r in regions] == [('text','BEFORE '),('image',None),('text',' AFTER')]


def test_docx_embedded_image_in_table_cell_not_lost() -> None:
    doc=Document(); table=doc.add_table(rows=1,cols=1); table.cell(0,0).paragraphs[0].add_run().add_picture(io.BytesIO(picture()))
    stream=io.BytesIO(); doc.save(stream)
    result=run(stream.getvalue(),'table-image.docx')
    assert any(r.source_kind==RegionKind.IMAGE for p in result.pages for r in p.regions), 'Embedded cell image disappeared'


def test_xlsx_embedded_image_not_lost() -> None:
    book=Workbook(); sheet=book.active; sheet.add_image(ExcelImage(io.BytesIO(picture())),'D4')
    stream=io.BytesIO(); book.save(stream)
    result=run(stream.getvalue(),'drawing.xlsx')
    assert any(r.source_kind==RegionKind.IMAGE for p in result.pages for r in p.regions), 'Worksheet image disappeared'


def test_docx_header_and_footer_text_extracted() -> None:
    doc=Document(); section=doc.sections[0]; section.header.paragraphs[0].text='HEADER_TICKET'; section.footer.paragraphs[0].text='FOOTER_TICKET'; doc.add_paragraph('BODY_TICKET')
    stream=io.BytesIO(); doc.save(stream)
    result=run(stream.getvalue(),'hdr.docx')
    texts=' '.join(r.text or '' for p in result.pages for r in p.regions)
    assert 'HEADER_TICKET' in texts and 'FOOTER_TICKET' in texts, 'Headers/footers omitted'


def test_uncertain_two_column_order_must_not_auto_approve() -> None:
    doc=fitz.open(); page=doc.new_page(width=500,height=400)
    for x,y,t in [(30,40,'L1'),(30,140,'L2'),(270,40,'R1'),(270,140,'R2')]:
        page.insert_text((x,y),t)
    result=run(doc.tobytes(),'two-columns.pdf');doc.close()
    assert result.ready_for_publication is False, 'Geometric estimate is being auto-approved although the columns are out of reading order'


def test_external_vision_table_must_have_2d_cell_contract() -> None:
    from data_extraction import ExtractionOptions, ImageRoute, ProviderTextResult
    class BadVision:
        def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
            return ProviderTextResult('', 0.98, structured_table=['not-a-matrix'])  # type: ignore[arg-type]
    # Accepting malformed untrusted vendor output as table is a contract violation.
    result=DataExtractionService(ExtractionOptions(image_route=ImageRoute.VISION_ONLY),vision_provider=BadVision()).extract(DocumentInput('photo.png',picture(),'audit:bad-model'))
    region=result.pages[0].regions[0]
    assert region.kind != RegionKind.TABLE or isinstance(region.table[0],list) and all(isinstance(row,list) for row in region.table)


def test_invalid_provider_nan_confidence_not_emitted_as_json_nan() -> None:
    import json
    from data_extraction import ExtractionOptions, ImageRoute, ProviderTextResult
    class BadVision:
        def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
            return ProviderTextResult('text', float('nan'))
    service=DataExtractionService(ExtractionOptions(image_route=ImageRoute.VISION_ONLY), vision_provider=BadVision())
    obj=service.to_dict(service.extract(DocumentInput('photo.png',picture(),'audit:nan')))
    json.dumps(obj, allow_nan=False)  # strict JSON schema prohibits NaN
