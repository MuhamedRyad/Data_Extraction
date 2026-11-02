from __future__ import annotations
from data_extraction.dispatch_extraction.extract_text import ExtractTextAction
from data_extraction.dispatch_extraction.extract_table import ExtractTableAction
from data_extraction.dispatch_extraction.extract_image import ExtractImageAction, VisionBudget
from data_extraction.dispatch_extraction.assemble_document import assemble_page
from data_extraction.dispatch_extraction.service import DispatchRegionsAction
from data_extraction.shared.models import CandidateRegion, ExtractedRegion, ExtractionMethod, PageCandidates, PageKind, RegionKind
from data_extraction.options import ExtractionOptions

def test_text_and_table_handlers_are_isolated() -> None:
    text = ExtractTextAction().execute(CandidateRegion('text',RegionKind.TEXT,text='hello'))
    table = ExtractTableAction().execute(CandidateRegion('table', RegionKind.TABLE, table=[['A','B']]))
    assert text.text == 'hello' and text.table is None
    assert table.table == [['A','B']] and table.text is None

def test_image_handler_is_unresolved_without_provider() -> None:
    region = CandidateRegion('img', RegionKind.IMAGE, image_bytes=b'test', image_mime_type='image/png')
    result = ExtractImageAction(None,None).execute(region, ExtractionOptions(), VisionBudget())
    assert result.requires_review
    assert result.method == ExtractionMethod.UNRESOLVED

def test_assembly_does_not_sort_source_regions() -> None:
    originals = (
      CandidateRegion('z', RegionKind.TEXT, text='second'),
      CandidateRegion('a', RegionKind.TEXT, text='first'),
    )
    page = PageCandidates(1, PageKind.LOGICAL_DOCUMENT, 'doc', originals)
    extracted = [ExtractTextAction().execute(item) for item in originals]
    result = assemble_page(page, extracted, [])
    assert result.reading_order_region_ids == ('z','a')
    assert tuple(item.region_id for item in result.regions) == ('z','a')

def test_legacy_dispatcher_and_extractor_are_same_classes() -> None:
    from data_extraction.router import DispatchRegionsAction as LegacyDispatcher
    from data_extraction.actions.extract_image_action import ExtractImageAction as LegacyImage
    assert DispatchRegionsAction is LegacyDispatcher
    assert ExtractImageAction is LegacyImage

def test_dispatcher_can_be_constructed_without_models() -> None:
    dispatcher = DispatchRegionsAction(ExtractTextAction(),ExtractTableAction(),ExtractImageAction(None,None))
    assert dispatcher is not None
