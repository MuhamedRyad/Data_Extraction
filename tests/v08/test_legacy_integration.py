"""Independent regression: v0.7 false-ready bugs and safe legacy capabilities."""
from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import fitz
from docx import Document
from docx.oxml import OxmlElement
import pytest

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.shared.models import ImageRoute, ProviderTextResult, RegionKind
from data_extraction.providers.ocr.cache import CachedOCRProvider
from data_extraction.providers.ocr.multi_pass import MultiPassOCRProvider
from data_extraction.providers.ocr.quality import assess_ocr_quality
from data_extraction.validate_extraction.warning_policy import classify_warning, WarningSeverity


def _service() -> DataExtractionService:
    return DataExtractionService(ExtractionOptions(image_route=ImageRoute.DISABLED))


def _pdf_table() -> bytes:
    doc: fitz.Document = fitz.open()
    page: fitz.Page = doc.new_page(width=500, height=500)
    page.insert_text((45, 40), "Invoice", fontsize=16)
    for y in (100, 140, 180, 220):
        page.draw_line((50, y), (350, y), width=1)
    for x in (50, 190, 350):
        page.draw_line((x, 100), (x, 220), width=1)
    for value, x, y in (("Item", 60, 126), ("Total", 200, 126),
                        ("Paper", 60, 166), ("12.00", 200, 166),
                        ("Ink", 60, 206), ("45.00", 200, 206)):
        page.insert_text((x, y), value, fontsize=12)
    content: bytes = doc.tobytes()
    doc.close()
    return content


def test_pdf_table_failure_is_not_false_ready() -> None:
    content: bytes = _pdf_table()
    normal = _service().extract(DocumentInput('table.pdf', content, 't0'))
    assert RegionKind.TABLE in {r.kind for r in normal.pages[0].regions}
    with patch.object(fitz.Page, 'find_tables', side_effect=RuntimeError('unavailable')):
        incomplete = _service().extract(DocumentInput('table.pdf', content, 't0'))
    assert 'table_detection_failed' in incomplete.pages[0].warnings
    assert incomplete.pages[0].requires_review
    assert not incomplete.ready_for_publication
    assert 'table_detection_failed' in ' '.join(incomplete.diagnostics)


def test_pdf_table_graphics_do_not_generate_spurious_vector_candidates() -> None:
    result = _service().extract(DocumentInput('table.pdf', _pdf_table(), 'tables'))
    assert 'vector_graphics_detected_review' not in result.pages[0].warnings


def test_vector_chart_with_native_text_is_not_silently_lost() -> None:
    doc: fitz.Document = fitz.open()
    page: fitz.Page = doc.new_page(width=600, height=600)
    page.insert_text((55, 55), 'Revenue Summary')
    page.draw_rect(fitz.Rect(80, 220, 510, 460), color=(0, 0, 0))
    page.draw_rect(fitz.Rect(160, 295, 210, 415), fill=(.1, .3, .8))
    page.draw_rect(fitz.Rect(260, 252, 310, 415), fill=(.4, .8, .2))
    content: bytes = doc.tobytes()
    doc.close()
    result = _service().extract(DocumentInput('plot.pdf', content, 'plot'))
    candidates = [item for item in result.pages[0].regions if item.provenance.source_image_role == 'vector_graphic_candidate']
    assert len(candidates) == 1
    assert candidates[0].method.value == 'unresolved'
    assert 'vector_graphics_detected_review' in result.pages[0].warnings
    assert not result.ready_for_publication
    assert any(item.text == 'Revenue Summary' for item in result.pages[0].regions)


def test_single_page_bordered_rectangle_not_assumed_chart() -> None:
    doc: fitz.Document = fitz.open()
    page: fitz.Page = doc.new_page()
    page.insert_text((50, 40), 'Simple text')
    page.draw_rect(fitz.Rect(40, 80, 340, 290))
    content: bytes = doc.tobytes(); doc.close()
    result = _service().extract(DocumentInput('border.pdf', content, 'border'))
    assert not any(item.provenance.source_image_role == 'vector_graphic_candidate' for item in result.pages[0].regions)


def test_word_heading_style_preserved_and_original_text_unchanged() -> None:
    doc: Document = Document()
    doc.add_heading('Financial Results', level=2)
    doc.add_paragraph('Body content')
    content: io.BytesIO = io.BytesIO(); doc.save(content)
    result = _service().extract(DocumentInput('headings.docx', content.getvalue(), 'h'))
    rows = [r for page in result.pages for r in page.regions]
    assert rows[0].text == 'Financial Results'
    assert rows[0].relations.region_role == 'heading_2'
    assert rows[1].relations.region_role == 'primary'
    assert rows[0].provenance.source_location.paragraph_index == 1


def test_docx_customxml_reports_incomplete_without_silent_approval() -> None:
    doc = Document(); doc.add_paragraph('Known text')
    wrapper = OxmlElement('w:customXml')
    para = OxmlElement('w:p'); run = OxmlElement('w:r'); node = OxmlElement('w:t')
    node.text = 'Unseen meaningful text'
    run.append(node); para.append(run); wrapper.append(para)
    doc.element.body.insert(len(doc.element.body) - 1, wrapper)
    output = io.BytesIO(); doc.save(output)
    result = _service().extract(DocumentInput('complex.docx', output.getvalue(), 'c'))
    assert 'docx_customxml_unrepresented' in result.pages[0].warnings
    assert not result.ready_for_publication
    assert not any(region.text == 'Unseen meaningful text' for region in result.pages[0].regions)


def test_unknown_warning_kept_informational() -> None:
    assert classify_warning('docx_physical_pagination_unknown') == WarningSeverity.INFORMATIONAL
    assert classify_warning('table_detection_failed') == WarningSeverity.INCOMPLETE
    assert classify_warning('pdf_heading_inferred_review') == WarningSeverity.REVIEW_REQUIRED


@dataclass
class FakeOCR:
    output: ProviderTextResult
    calls: int = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        return self.output


def test_multipass_does_not_delete_unselected_text() -> None:
    first: FakeOCR = FakeOCR(ProviderTextResult('Revenue 120', .98))
    second: FakeOCR = FakeOCR(ProviderTextResult('Revenue 120\nAccount 98765', .78))
    ensemble: MultiPassOCRProvider = MultiPassOCRProvider((first, second))
    evidence = ensemble.compare(b'image-bytes', 'image/png')
    assert evidence.conflict
    assert evidence.consensus is None
    assert len(evidence.attempts) == 2
    assert 'Account 98765' in evidence.attempts[1].result.text
    result: ProviderTextResult = ensemble.recognize(b'image-bytes', 'image/png')
    assert result.text == '' and result.provider_name == 'multi_pass_conflict'


def test_multipass_approves_only_complete_agreement() -> None:
    ensemble: MultiPassOCRProvider = MultiPassOCRProvider((
        FakeOCR(ProviderTextResult('Same', .98)), FakeOCR(ProviderTextResult('Same', .80))
    ))
    result: ProviderTextResult = ensemble.recognize(b'image-bytes', 'image/png')
    assert result.text == 'Same' and result.confidence == .80
    assert result.provider_name == 'multi_pass_consensus'


def test_cache_is_scoped_and_content_addressed(tmp_path: Path) -> None:
    fake: FakeOCR = FakeOCR(ProviderTextResult('A', .91, provider_name='fake'))
    a = CachedOCRProvider(fake, tmp_path, 'v1:ara+eng', 'tenant-a')
    b = CachedOCRProvider(fake, tmp_path, 'v1:ara+eng', 'tenant-b')
    assert a.recognize(b'img', 'image/png').text == 'A'
    fake.output = ProviderTextResult('B', .88)
    assert a.recognize(b'img', 'image/png').text == 'A'
    assert fake.calls == 1
    assert a.recognize(b'changed', 'image/png').text == 'B'
    assert b.recognize(b'img', 'image/png').text == 'B'
    assert fake.calls == 3


def test_invalid_cache_triggers_reextraction(tmp_path: Path) -> None:
    fake: FakeOCR = FakeOCR(ProviderTextResult('First', .91))
    cache = CachedOCRProvider(fake, tmp_path, 's', 'tenant')
    cache.recognize(b'i', 'image/png')
    cache._entry_path(b'i', 'image/png').write_text('{invalid JSON', encoding='utf-8')
    fake.output = ProviderTextResult('Recovered', .92)
    assert cache.recognize(b'i', 'image/png').text == 'Recovered'
    assert fake.calls == 2


def test_short_valid_ocr_page_not_rejected_for_length() -> None:
    evidence = assess_ocr_quality(ProviderTextResult('نعم', .99))
    assert evidence.non_whitespace_characters == 3
    assert not evidence.review_reasons


def test_geometry_table_is_candidate_and_keeps_blank_cells() -> None:
    from data_extraction.providers.ocr.table_geometry import OCRWord, reconstruct_candidate_table
    words = tuple(
        OCRWord(value, x, y, x + 16, y + 12)
        for y, values in ((10, ('Item', 'Qty', 'Price')),
                          (40, ('Paper', '2', '30')),
                          (70, ('Ink', '3', '20')))
        for x, value in zip((10, 150, 300), values)
    )
    result = reconstruct_candidate_table(words)
    assert result == [['Item', 'Qty', 'Price'], ['Paper', '2', '30'], ['Ink', '3', '20']]
    assert reconstruct_candidate_table(words[:5]) is None


def test_pdf_repeated_header_classified_without_dropping_text() -> None:
    doc = fitz.open()
    for index in range(3):
        page = doc.new_page(width=500, height=700)
        page.insert_text((50, 45), f'Annual report - Page {index + 1}', fontsize=10)
        page.insert_text((50, 150), f'Text on page {index + 1}', fontsize=10)
    raw = doc.tobytes(); doc.close()
    result = _service().extract(DocumentInput('repeat.pdf', raw, 'repeat'))
    assert len(result.pages) == 3
    for page in result.pages:
        assert any(region.relations.region_role == 'page_header_candidate' for region in page.regions)
        assert any('Annual report' in (r.text or '') for r in page.regions)
        assert page.requires_review


def test_rtl_proposal_is_not_a_raw_text_replacement(monkeypatch) -> None:
    from data_extraction.analyze_layout.pdf_native_reading_order import (
        PdfNativeReadingOrderResolver, propose_mixed_direction_text,
    )
    monkeypatch.setattr(PdfNativeReadingOrderResolver, 'resolve_region_text',
                        lambda page, bbox: 'ID-22 رقم')
    proposed = propose_mixed_direction_text(object(), (0, 0, 100, 20), 'رقم ID-22')
    assert proposed.original_text == 'رقم ID-22'
    assert proposed.proposed_text == 'ID-22 رقم'
    assert proposed.same_token_multiset
    assert propose_mixed_direction_text(object(), (0, 0, 100, 20), 'Only English') is None


def test_ocr_disagreement_blocks_end_to_end_publication() -> None:
    from PIL import Image
    image = Image.new('RGB', (80, 50), 'white')
    output = io.BytesIO(); image.save(output, format='PNG')
    providers = MultiPassOCRProvider((
        FakeOCR(ProviderTextResult('Revenue 120', .98)),
        FakeOCR(ProviderTextResult('Revenue 120\nAccount 98765', .78)),
    ))
    svc = DataExtractionService(ExtractionOptions(image_route=ImageRoute.OCR_ONLY),
                                ocr_provider=providers)
    result = svc.extract(DocumentInput('image.png', output.getvalue(), 'img'))
    assert not result.ready_for_publication
    assert 'ocr_pass_conflict_or_incomplete_requires_review' in result.pages[0].regions[0].review_reasons


def test_ocr_cache_preserves_success_when_directory_is_not_writable(tmp_path: Path) -> None:
    fake = FakeOCR(ProviderTextResult('Success', .9))
    unwritable = tmp_path / 'file_not_directory'
    unwritable.write_text('exists')
    wrapped = CachedOCRProvider(fake, unwritable, 'sig', 'scope')
    assert wrapped.recognize(b'i', 'image/png').text == 'Success'


def test_ocr_quality_flags_arabic_english_mix() -> None:
    evidence = assess_ocr_quality(ProviderTextResult('رقم INV-44', .97))
    assert evidence.mixes_rtl_and_ltr
    assert 'ocr_mixed_direction_review' in evidence.review_reasons


def test_preprocess_profiles_preserve_source_and_call_ocr_with_png() -> None:
    from data_extraction.providers.ocr.preprocessing import PreprocessedOCRProvider, PreprocessMode
    from PIL import Image

    @dataclass
    class CaptureProvider:
        images: list[bytes]

        def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
            assert mime_type == 'image/png'
            self.images.append(image_bytes)
            return ProviderTextResult('Text', .95)

    image = Image.new('RGB', (90, 70), (120, 160, 200))
    source = io.BytesIO(); image.save(source, format='PNG')
    original: bytes = source.getvalue()
    provider = CaptureProvider([])
    for mode in PreprocessMode:
        assert PreprocessedOCRProvider(provider, mode).recognize(original, 'image/png').text == 'Text'
    assert source.getvalue() == original
    assert len(provider.images) == 3
    assert all(value != original for value in provider.images)


def test_easyocr_lazy_adapter_with_fake_engine(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace
    from PIL import Image
    from data_extraction.providers.ocr.easyocr import EasyOCRProvider
    calls: list[dict] = []

    class Reader:
        def __init__(self, languages, gpu, download_enabled):
            calls.append(dict(languages=languages, gpu=gpu, download_enabled=download_enabled))

        def readtext(self, image, detail, paragraph):
            assert detail == 1 and not paragraph
            return [
                ([[10, 10], [50, 10], [50, 22], [10, 22]], 'Hello', .96),
                ([[10, 35], [50, 35], [50, 47], [10, 47]], 'World', .91),
            ]

    monkeypatch.setitem(sys.modules, 'easyocr', SimpleNamespace(Reader=Reader))
    src = io.BytesIO(); Image.new('RGB', (80, 80), 'white').save(src, format='PNG')
    provider = EasyOCRProvider()
    assert not calls
    response = provider.recognize(src.getvalue(), 'image/png')
    assert response.text == 'Hello\nWorld'
    assert response.confidence is not None and .90 < response.confidence < .97
    assert response.provider_name == 'easyocr'
    assert len(calls) == 1 and calls[0]['download_enabled'] is False
    provider.recognize(src.getvalue(), 'image/png')
    assert len(calls) == 1


def test_multi_pass_audit_sink_receives_every_attempt() -> None:
    stored = []
    ensemble = MultiPassOCRProvider((
        FakeOCR(ProviderTextResult('one', .99)),
        FakeOCR(ProviderTextResult('one plus two', .88)),
    ), comparison_sink=stored.append)
    assert ensemble.recognize(b'image', 'image/png').provider_name == 'multi_pass_conflict'
    assert len(stored) == 1 and len(stored[0].attempts) == 2
    assert stored[0].attempts[0].result.text == 'one'
    assert stored[0].attempts[1].result.text == 'one plus two'
