from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import io
import json

import fitz
from PIL import Image, ImageDraw, ImageFont

from data_extraction import DataExtractionService, DocumentInput
from data_extraction.providers.ocr.tesseract import TesseractOCRProvider
from data_extraction.shared.models import ProviderTextResult
from data_extraction.template_extraction.contracts import (
    DocumentTemplate, TemplateAnchor, TemplateFieldDefinition, TemplateFieldMode, TemplateStatus,
)
from data_extraction.template_extraction.request import TemplateExtractionRequest
from data_extraction.template_extraction.validation import template_content_hash

FONT = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 34)
SMALL = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 24)


def page(items):
    image = Image.new('RGB', (700, 1000), 'white')
    draw = ImageDraw.Draw(image)
    for text, xy, font in items:
        draw.text(xy, text, fill='black', font=font)
    out = io.BytesIO(); image.save(out, format='PNG'); return out.getvalue()


def pdf_bytes(amount: str, label: str = 'TOTAL') -> bytes:
    pages = (
        page((('MULTI PAGE INVOICE', (120, 100), FONT), ('Supplier A', (120, 180), SMALL))),
        page((('PAGE 2 - FINANCIAL', (90, 80), SMALL), (label, (420, 760), FONT), (amount, (420, 830), FONT))),
        page((('PAGE 3 - DELIVERY', (90, 80), SMALL), ('DELIVERY NOTE', (160, 420), FONT))),
    )
    pdf = fitz.open()
    for content in pages:
        p = pdf.new_page(width=700, height=1000); p.insert_image(p.rect, stream=content)
    content = pdf.tobytes(); pdf.close(); return content


class NamedTesseract:
    def __init__(self, name: str):
        self.name = name; self.calls = 0; self.inner = TesseractOCRProvider(language='eng')
    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        r = self.inner.recognize(image_bytes, mime_type)
        return ProviderTextResult(r.text, r.confidence, r.structured_table, self.name, r.model_id)


primary = NamedTesseract('generic-tesseract')
field_a = NamedTesseract('field-tesseract-a')
field_b = NamedTesseract('field-tesseract-b')
service = DataExtractionService(ocr_provider=primary, field_ocr_providers=(field_a, field_b))
original = DocumentInput('original.pdf', pdf_bytes('123.45'), 'original')
same_layout = DocumentInput('same-layout.pdf', pdf_bytes('999.99'), 'same-layout')
semantic_drift = DocumentInput('semantic-drift.pdf', pdf_bytes('999.99', 'SALARY'), 'semantic-drift')

extraction = service.extract(original)
label_bbox = (0.57, 0.73, 0.82, 0.81)
value_bbox = (0.57, 0.80, 0.86, 0.92)
fingerprint = service._template_feature._fingerprint(
    extraction, (TemplateAnchor('TOTAL', 2, label_bbox, True),), original
)
field = TemplateFieldDefinition(
    'total', 'money', True, 2, ('TOTAL',), r'\d+[.]\d{2}', TemplateFieldMode.LABEL_RELATIVE,
    value_bbox, label_bbox, None, 0.50,
)
template = DocumentTemplate(
    'v22-real', 'v22-real', 1, TemplateStatus.APPROVED, 'general', (field,), (), fingerprint,
    extraction.manifest.sha256, original.source_id, 'benchmark', '',
)
template = replace(template, content_hash=template_content_hash(template))

# Reset counters after onboarding.
primary.calls = field_a.calls = field_b.calls = 0
same_result = service.extract_with_template(TemplateExtractionRequest(same_layout, (template,), 'v22-real'))
same_counts = {'generic_ocr_calls': primary.calls, 'field_a_calls': field_a.calls, 'field_b_calls': field_b.calls}

primary.calls = field_a.calls = field_b.calls = 0
drift_result = service.extract_with_template(TemplateExtractionRequest(semantic_drift, (template,), 'v22-real'))
drift_counts = {'generic_ocr_calls': primary.calls, 'field_a_calls': field_a.calls, 'field_b_calls': field_b.calls}

payload = {
    'version': '0.22.0',
    'scenario': 'three_page_same_layout_vs_semantic_drift',
    'same_layout_new_value': {
        'match_score': same_result.match.score,
        'match_status': same_result.match.status.value,
        'changed_pages': list(same_result.match.changed_pages),
        'generic_extraction_performed': same_result.generic_extraction_performed,
        'generic_extraction_pages': list(same_result.generic_extraction_pages),
        'template_fast_path_pages': list(same_result.template_fast_path_pages),
        'field': [
            {'name': x.name, 'value': x.value, 'verified': x.verified, 'status': x.status}
            for x in same_result.fields
        ],
        'calls': same_counts,
    },
    'semantic_drift_page_2': {
        'match_score': drift_result.match.score,
        'match_status': drift_result.match.status.value,
        'changed_pages': list(drift_result.match.changed_pages),
        'generic_extraction_performed': drift_result.generic_extraction_performed,
        'generic_extraction_pages': list(drift_result.generic_extraction_pages),
        'template_fast_path_pages': list(drift_result.template_fast_path_pages),
        'field': [
            {'name': x.name, 'value': x.value, 'verified': x.verified, 'status': x.status, 'review_reasons': list(x.review_reasons)}
            for x in drift_result.fields
        ],
        'calls': drift_counts,
        'page_region_counts': [len(page.regions) for page in drift_result.extraction.pages],
    },
}
Path('benchmarks/V22_REAL_MULTIPAGE_BENCHMARK.json').write_text(json.dumps(payload, indent=2, ensure_ascii=False))
print(json.dumps(payload, indent=2, ensure_ascii=False))
