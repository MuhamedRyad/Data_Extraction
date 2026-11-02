from __future__ import annotations

from io import BytesIO
from types import SimpleNamespace
from typing import Any

import pytest

from data_extraction import DataExtractionService
from data_extraction.models import DocumentInput, ExtractionMethod, RegionKind
from data_extraction.options import ExtractionOptions
from data_extraction.structure.unstructured_provider import (
    UnstructuredTableProvider, parse_html_table)
from .test_v06_docling_structure import _pdf


def _obj(**kw: Any) -> Any:
    return SimpleNamespace(**kw)


def _element(html: str = "<table><tr><th colspan='2'>عنوان</th></tr><tr><td>أ</td><td>20</td></tr></table>",
             page: int = 1, geometry: Any | None = None) -> Any:
    coordinate: Any = geometry if geometry is not None else _obj(
        points=((50, 80), (350, 80), (350, 240), (50, 240)),
        system=_obj(width=400, height=300))
    return _obj(category="Table", metadata=_obj(page_number=page,
             text_as_html=html, coordinates=coordinate))


def test_unstructured_html_preserves_span_cells() -> None:
    cells, merges, positions = parse_html_table(
        "<table><tr><th colspan='2'>عنوان</th></tr><tr><td>أ</td><td></td></tr></table>",
        ExtractionOptions())
    assert cells == (("عنوان", None), ("أ", ""))
    assert (merges[0].row_span, merges[0].column_span) == (1, 2)
    assert positions[0].is_column_header


def test_unstructured_rowspan() -> None:
    cells, merges, _ = parse_html_table(
        "<table><tr><td rowspan='2'>A</td><td>B</td></tr><tr><td>C</td></tr></table>",
        ExtractionOptions())
    assert cells == (("A", "B"), (None, "C"))
    assert merges[0].row_span == 2


@pytest.mark.parametrize("html", [
    "", "<p>abc</p>", "<table></table>",
    "<table><tr><td>X</td></tr>",
    "<table><tr><td colspan='0'>X</td></tr></table>",
    "<table><tr><td rowspan='2'>X</td></tr></table>",
    "<table><tr><td colspan='-1'>X</td></tr></table>",
    "<table><tr><td>A<table><tr><td>B</td></tr></table></td></tr></table>",
])
def test_unstructured_invalid_html_rejected(html: str) -> None:
    with pytest.raises(ValueError):
        parse_html_table(html, ExtractionOptions())


def test_unstructured_injected_local_partition_on_real_fixture() -> None:
    seen: dict[str, Any] = {}

    def fake_partition(*, file: BytesIO, strategy: str,
                       infer_table_structure: bool) -> list[Any]:
        seen.update(strategy=strategy, infer=infer_table_structure, head=file.read(4))
        return [_element()]

    provider: UnstructuredTableProvider = UnstructuredTableProvider(
        partition_fn=fake_partition, model_id="local-test")
    source: DocumentInput = _pdf()
    predictions = provider.predict_tables(source, ExtractionOptions())
    assert seen == {"strategy": "hi_res", "infer": True, "head": b"%PDF"}
    assert predictions[0].bbox == (50, 80, 350, 240)
    result = DataExtractionService(structure_provider=provider).extract(source)
    structured = [r for r in result.pages[0].regions if r.method == ExtractionMethod.DOCUMENT_AI]
    assert len(structured) == 1
    assert structured[0].provenance.provider_name == "UnstructuredTableProvider"
    assert structured[0].table == [["عنوان", None], ["أ", "20"]]
    assert structured[0].requires_review


@pytest.mark.parametrize("coordinate", [
    _obj(system=_obj(width=400,height=300), points=((1,1),(float('nan'),3))),
    _obj(system=_obj(width=0,height=300), points=((1,1),(20,20))),
    _obj(system=_obj(width=400,height=300), points=((20,20),(500,220))),
    None,
])
def test_unstructured_invalid_coordinate_failure_propagates_to_review(coordinate: Any) -> None:
    provider = UnstructuredTableProvider(partition_fn=lambda **_: [_element(geometry=coordinate)
                                      if coordinate is not None else _obj(category="Table",
                                         metadata=_obj(page_number=1,
                                             text_as_html="<table><tr><td>A</td></tr></table>",
                                             coordinates=None))])
    result = DataExtractionService(structure_provider=provider).extract(_pdf())
    assert "document_structure_provider_invalid_or_failed_review" in result.pages[0].warnings
    assert not any(r.method == ExtractionMethod.DOCUMENT_AI for r in result.pages[0].regions)


def test_unstructured_unknown_page_rejected() -> None:
    provider = UnstructuredTableProvider(partition_fn=lambda **_: [_element(page=7)])
    result = DataExtractionService(structure_provider=provider).extract(_pdf())
    assert "document_structure_provider_invalid_or_failed_review" in result.pages[0].warnings


def test_unstructured_none_without_table_means_no_generated_content() -> None:
    provider = UnstructuredTableProvider(partition_fn=lambda **_: [_obj(category="Title")])
    assert provider.predict_tables(_pdf(), ExtractionOptions()) == ()
