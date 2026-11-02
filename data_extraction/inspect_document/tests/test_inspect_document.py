from __future__ import annotations

from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest
from data_extraction.inspect_document.service import InspectDocumentService
from data_extraction.inspect_document.response import FileManifest
from data_extraction.shared.models import RegionKind

def test_inspect_text_preserves_region_manifest() -> None:
    service = InspectDocumentService()
    doc = service.inspect(
        InspectDocumentRequest(file_name='x.txt', content=b'first line\nsecond line', source_id='src'),
        InspectDocumentOptions(),
    )
    manifest: FileManifest = service.build_manifest(doc)
    assert manifest.file_name == 'x.txt'
    assert manifest.page_count == len(doc.pages)
    assert manifest.document_order_region_ids == tuple(r.region_id for p in doc.pages for r in p.regions)
    assert any(r.kind == RegionKind.TEXT for p in manifest.pages for r in p.regions)

def test_inspect_does_not_need_optional_provider() -> None:
    doc = InspectDocumentService().inspect(
        InspectDocumentRequest(file_name='a.csv', content=b'a,b\n1,2', source_id='src'),
        InspectDocumentOptions(),
    )
    assert doc.pages and doc.file_kind.value == 'csv'
