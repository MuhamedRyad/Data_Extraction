"""No spurious column ambiguity for continuous right-aligned Arabic prose."""
from __future__ import annotations

from data_extraction.actions.resolve_reading_order_action import order_pdf_regions
from data_extraction.models import CandidateRegion,RegionKind


def test_right_aligned_native_prose_remains_in_geometric_order() -> None:
    texts: list[CandidateRegion] = [
        CandidateRegion(f'line{i}',RegionKind.TEXT,(60+i%3*10,i*27,550,i*27+23),
                        text='بيانات عربية ورقم مرجعي قد يحتوي على إنجليزي')
        for i in range(1,12)
    ]
    ordered, warnings = order_pdf_regions(texts,595,842)
    assert [r.region_id for r in ordered] == [r.region_id for r in texts]
    assert not warnings


def test_single_column_page_with_table_needs_no_column_warning() -> None:
    entries: list[CandidateRegion] = [
        CandidateRegion('head',RegionKind.TEXT,(70,40,520,65),text='عنوان التقرير'),
        CandidateRegion('sub',RegionKind.TEXT,(70,90,520,115),text='تفاصيل التقرير'),
        CandidateRegion('table',RegionKind.TABLE,(50,170,540,430),table=[['a']]),
        CandidateRegion('foot1',RegionKind.TEXT,(65,480,525,503),text='خلاصة التقرير'),
        CandidateRegion('foot2',RegionKind.TEXT,(65,510,525,533),text='ملاحظة إضافية'),
    ]
    ordered,warnings = order_pdf_regions(entries,595,842)
    assert [r.region_id for r in ordered] == ['head','sub','table','foot1','foot2']
    assert not warnings
