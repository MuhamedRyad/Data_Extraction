from __future__ import annotations
import pytest
from data_extraction.validate_extraction.service import ValidateExtractionService
from data_extraction.inspect_document.validate_upload import validate_upload
from data_extraction.shared.models import DocumentInput, PageKind, PageResult, RegionKind, ExtractedRegion, ExtractionMethod
from data_extraction.options import ExtractionOptions
from data_extraction.shared.errors import ExtractionError

def page(review: bool, regions: bool=True) -> PageResult:
    r = ExtractedRegion('a',RegionKind.TEXT,None,ExtractionMethod.NATIVE,'value',None,None,review,())
    return PageResult(1,PageKind.LOGICAL_DOCUMENT,'test',(RegionKind.TEXT,),(r,) if regions else (),review,())

@pytest.mark.parametrize('items,ready', [
    ([], True),
    ([page(False)], True),
    ([page(False),page(False)], True),
    ([page(True)], False),
    ([page(False),page(True)], False),
    ([page(False,False)], False),
])
def test_quality_gate_matches_v06_behavior(items: list[PageResult], ready: bool) -> None:
    assert ValidateExtractionService.is_ready_for_publication(items) is ready

def test_reject_empty_upload() -> None:
    with pytest.raises(ExtractionError) as exc:
        validate_upload(DocumentInput('a.txt', b'', 'src'), ExtractionOptions())
    assert exc.value.code == 'EMPTY_FILE'
