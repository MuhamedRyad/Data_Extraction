from __future__ import annotations
from data_extraction.analyze_layout.resolve_reading_order import order_pdf_regions
from data_extraction.analyze_layout.contracts import LayoutRegion
from data_extraction.shared.models import CandidateRegion, RegionKind
from data_extraction.actions.resolve_reading_order_action import order_pdf_regions as legacy_order_pdf_regions

def test_legacy_reading_order_points_to_canonical_function() -> None:
    assert order_pdf_regions is legacy_order_pdf_regions

def test_contract_preserves_coordinates() -> None:
    region = LayoutRegion(region_id='r', kind=RegionKind.TEXT, bbox=(1.0, 2.0, 9.0, 10.0))
    assert region.bbox == (1.0, 2.0, 9.0, 10.0)

def test_legacy_candidate_class_is_same_object() -> None:
    from data_extraction.models import CandidateRegion as LegacyCandidateRegion
    assert CandidateRegion is LegacyCandidateRegion
