"""Independent v0.5 layout adapter and ordering regression checks."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

import fitz
import pytest

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.layout.contracts import LayoutOrderPrediction, LayoutPageRequest
from data_extraction.models import CandidateRegion, RegionKind
from data_extraction.actions.resolve_reading_order_action import order_pdf_regions


def make_pdf(pages: int = 1) -> bytes:
    document: fitz.Document = fitz.open()
    for n in range(pages):
        page: fitz.Page = document.new_page(width=520, height=470)
        for x, y, value in ((30, 60, f'LX{n}'), (30, 180, f'LY{n}'),
                            (300, 60, f'RX{n}'), (300, 180, f'RY{n}')):
            page.insert_text((x, y), value)
    output: bytes = document.tobytes()
    document.close()
    return output


@dataclass
class LayoutStub:
    calls: int = 0
    missing: bool = False
    duplicate: bool = False
    incorrect_page: bool = False
    confidence: float | None = 0.92
    fail: bool = False

    def predict_order(self, request: LayoutPageRequest) -> LayoutOrderPrediction:
        self.calls += 1
        assert request.image_png[:8] == b'\x89PNG\r\n\x1a\n'
        assert len(request.regions) == 4
        if self.fail:
            raise RuntimeError('layout backend down')
        ordered: tuple[str, ...] = tuple(reversed([r.region_id for r in request.regions]))
        if self.missing:
            ordered = ordered[:-1]
        if self.duplicate:
            ordered = (ordered[0],) + ordered[:-1]
        return LayoutOrderPrediction(
            request.page_number + (1 if self.incorrect_page else 0), ordered,
            self.confidence, 'layout-stub', 'stub-model-v1')


def extract(stub: LayoutStub, pages: int = 1, **option_changes: int) -> tuple[dict, LayoutStub]:
    service = DataExtractionService(
        ExtractionOptions(**option_changes), layout_provider=stub)
    result = service.extract(DocumentInput('fixture.pdf', make_pdf(pages), 'layout-source'))
    return json.loads(service.to_json(result)), stub


def test_external_layout_reorders_but_cannot_change_text_or_source() -> None:
    original_service = DataExtractionService()
    source = DocumentInput('fixture.pdf',make_pdf(), 'layout-source')
    original = original_service.extract(source)
    stub = LayoutStub()
    service = DataExtractionService(layout_provider=stub)
    inspected = service.inspect(source)
    assert stub.calls == 0
    result = service.extract(source)
    assert stub.calls == 1
    assert [r.text for r in result.pages[0].regions] == [r.text for r in reversed(original.pages[0].regions)]
    assert sorted(r.region_id for r in result.pages[0].regions) == sorted(r.region_id for r in original.pages[0].regions)
    assert result.pages[0].layout_provider_name == 'layout-stub'
    assert result.pages[0].layout_model_id == 'stub-model-v1'
    assert result.pages[0].layout_confidence == 0.92
    assert result.pages[0].reading_order_quality.value == 'external_estimate'
    assert result.ready_for_publication is False
    assert inspected.pages[0].layout_provider_name is None
    assert result.manifest.document_order_region_ids == result.document_order_region_ids
    assert [r.position.document_order_index for r in result.pages[0].regions] == [1,2,3,4]
    assert all(r.provenance.source_surface.value == 'native_pdf_text' for r in result.pages[0].regions)


@pytest.mark.parametrize('changes',[{'missing':True},{'duplicate':True},
                                    {'incorrect_page':True},{'confidence':float('nan')},
                                    {'confidence':float('inf')},{'confidence':1.2},
                                    {'confidence':-0.2},{'fail':True}])
def test_malformed_provider_fails_closed_without_losing_regions(changes: dict) -> None:
    stub = LayoutStub(**changes)
    result, spy = extract(stub)
    assert spy.calls == 1
    page = result['pages'][0]
    assert len(page['regions']) == 4
    assert page['requires_review'] is True
    assert 'layout_provider_invalid_or_failed_review' in page['warnings']
    assert result['ready_for_publication'] is False
    assert page['layout_provider_name'] is None
    assert result['document_order_region_ids'] == result['manifest']['document_order_region_ids']


def test_no_layout_calls_when_vision_budget_is_zero() -> None:
    stub = LayoutStub()
    result, spy = extract(stub,pages=2,max_layout_calls=0)
    assert spy.calls == 0
    assert all('layout_provider_budget_exceeded' in page['warnings'] for page in result['pages'])
    assert not result['ready_for_publication']


def test_page_budget_fails_closed_on_remaining_pages() -> None:
    result,spy = extract(LayoutStub(), pages=3,max_layout_calls=1)
    assert spy.calls == 1
    assert [p['number'] for p in result['pages']] == [1,2,3]
    assert result['pages'][0]['layout_provider_name'] == 'layout-stub'
    assert all('layout_provider_budget_exceeded' in p['warnings'] for p in result['pages'][1:])


def test_pdf_three_column_order_is_correct_and_review_required() -> None:
    items: list[CandidateRegion] = []
    for key,x in (('left',30),('middle',210),('right',380)):
        for index,y in enumerate((50,120,200)):
            items.append(CandidateRegion(f'{key}{index}',RegionKind.TEXT,
                                         (x,y,x+50,y+15),text=f'{key}{index}'))
    result,warnings = order_pdf_regions(items,520,470)
    assert [r.region_id for r in result] == [
        'left0','left1','left2','middle0','middle1','middle2','right0','right1','right2']
    assert 'reading_order_columns_inferred_review' in warnings


def test_spanning_block_preserves_column_sections_without_duplicates() -> None:
    items: list[CandidateRegion] = []
    for y,index in ((40,1),(110,2),(270,3),(340,4)):
        items.append(CandidateRegion(f'L{index}',RegionKind.TEXT,(30,y,90,y+15),text=f'L{index}'))
        items.append(CandidateRegion(f'R{index}',RegionKind.TEXT,(290,y,370,y+15),text=f'R{index}'))
    items.append(CandidateRegion('separator',RegionKind.TEXT,(20,210,500,230),text='separator'))
    result,warnings = order_pdf_regions(items,520,470)
    assert [r.region_id for r in result] == ['L1','L2','R1','R2','separator','L3','L4','R3','R4']
    assert len(set(r.region_id for r in result)) == len(items)
    assert 'reading_order_columns_inferred_review' in warnings


def test_ambiguous_mixed_image_does_not_invent_order() -> None:
    items = [CandidateRegion('l1',RegionKind.TEXT,(20,20,80,35),text='left1'),
             CandidateRegion('r1',RegionKind.TEXT,(240,20,300,35),text='right1'),
             CandidateRegion('l2',RegionKind.TEXT,(20,100,80,115),text='left2'),
             CandidateRegion('r2',RegionKind.TEXT,(240,100,300,115),text='right2'),
             CandidateRegion('image',RegionKind.IMAGE,(100,60,220,90))]
    result,warnings = order_pdf_regions(items,400,300)
    assert set(r.region_id for r in result) == set(r.region_id for r in items)
    assert 'reading_order_complex_columns_review_required' in warnings
