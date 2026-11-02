"""Strict structure engine tests; no installed Docling model needed for contract checks."""
from __future__ import annotations

from dataclasses import replace
from io import BytesIO
from types import SimpleNamespace
from typing import Any
import json
import math

import fitz
from PIL import Image, ImageDraw
import pytest

from data_extraction import DataExtractionService
from data_extraction.models import (BoundingBox, CellMerge, DocumentInput,
                                    ExtractionMethod, RegionKind, TableCellLocation)
from data_extraction.options import ExtractionOptions
from data_extraction.structure.contracts import StructuredTablePrediction
from data_extraction.structure.docling_provider import DoclingTableProvider, decode_docling_table


def _object(**kwargs: Any) -> Any:
    return SimpleNamespace(**kwargs)


def _docling_cell(row: int, col: int, *, row_span: int = 1,
                  col_span: int = 1, text: str = "value") -> Any:
    return _object(start_row_offset_idx=row, end_row_offset_idx=row + row_span,
                   start_col_offset_idx=col, end_col_offset_idx=col + col_span,
                   row_span=row_span, col_span=col_span, text=text,
                   column_header=(row == 0), row_header=False, bbox=None)


def _item(cells: list[Any] | None = None, *, rows: int = 2, cols: int = 2,
          page: int = 1, box: Any | None = None) -> Any:
    bbox: Any = box or _object(l=30.0, t=230.0, r=370.0, b=70.0,
                               coord_origin="CoordOrigin.BOTTOMLEFT")
    return _object(prov=[_object(page_no=page, bbox=bbox)],
                   data=_object(num_rows=rows, num_cols=cols,
                                table_cells=cells if cells is not None else [
                                    _docling_cell(0, 0, col_span=2, text="عنوان"),
                                    _docling_cell(1, 0, text="اسم"),
                                    _docling_cell(1, 1, text="قيمة")]))


def _pdf() -> DocumentInput:
    image: Image.Image = Image.new("RGB", (320, 180), "white")
    painter: ImageDraw.ImageDraw = ImageDraw.Draw(image)
    painter.rectangle((5, 5, 312, 175), outline="black", width=3)
    painter.line((5, 95, 312, 95), fill="black", width=3)
    painter.line((160, 95, 160, 175), fill="black", width=3)
    encoded: BytesIO = BytesIO()
    image.save(encoded, format="PNG")
    pdf: fitz.Document = fitz.open()
    page: fitz.Page = pdf.new_page(width=400, height=300)
    page.insert_text((35, 40), "Native header")
    page.insert_image(fitz.Rect(40, 70, 360, 250), stream=encoded.getvalue())
    content: bytes = pdf.tobytes()
    pdf.close()
    return DocumentInput("document.pdf", content, "source:test-v06")


def _prediction(*, bbox: BoundingBox = (50.0, 80.0, 350.0, 240.0),
                rows: tuple[tuple[str | None, ...], ...] = (("عنوان", None), ("اسم", "قيمة")),
                page: int = 1, merges: tuple[CellMerge, ...] = (CellMerge(0, 0, 1, 2),),
                provider: str = "DoclingTableProvider") -> StructuredTablePrediction:
    return StructuredTablePrediction(page, bbox, rows, merges,
                                     (TableCellLocation(0, 0, 1, 2, None, True, False),),
                                     provider, "docling-v-test")


class FakeStructureProvider:
    def __init__(self, response: Any = None, throw: bool = False) -> None:
        self.response: Any = (_prediction(),) if response is None else response
        self.throw: bool = throw
        self.calls: int = 0

    def predict_tables(self, document: DocumentInput,
                       options: ExtractionOptions) -> tuple[StructuredTablePrediction, ...]:
        self.calls += 1
        if self.throw:
            raise RuntimeError("blocked")
        return self.response


def test_docling_coordinates_and_merged_cells() -> None:
    output: StructuredTablePrediction = decode_docling_table(
        _item(), {1: (400.0, 300.0)}, ExtractionOptions())
    assert output.bbox == (30.0, 70.0, 370.0, 230.0)
    assert output.cells == (("عنوان", None), ("اسم", "قيمة"))
    assert output.merges == (CellMerge(0, 0, 1, 2),)
    assert output.cell_locations[0].is_column_header


def test_docling_top_left_coordinates_preserved() -> None:
    box: Any = _object(l=40, t=50, r=120, b=80, coord_origin="TOPLEFT")
    output: StructuredTablePrediction = decode_docling_table(
        _item(box=box), {1: (400.0, 300.0)}, ExtractionOptions())
    assert output.bbox == (40.0, 50.0, 120.0, 80.0)


@pytest.mark.parametrize("cell_list,rows,cols", [
    ([_docling_cell(0, 0, col_span=2), _docling_cell(0, 1)], 2, 2),
    ([_docling_cell(0, 0, row_span=3)], 2, 2),
    ([_docling_cell(0, 0)], 0, 2),
    ([_docling_cell(0, 0)], 2, 0),
    ([], 2, 2),
    ([_docling_cell(0, 0)], 200, 200),
])
def test_docling_rejects_invalid_table_geometry(
        cell_list: list[Any], rows: int, cols: int) -> None:
    with pytest.raises(ValueError):
        decode_docling_table(_item(cell_list, rows=rows, cols=cols),
                             {1: (400.0, 300.0)}, ExtractionOptions())


@pytest.mark.parametrize("box", [
    _object(l=0, t=240, r=100, b=200, coord_origin="TOPLEFT"),
    _object(l=-20, t=20, r=100, b=80, coord_origin="TOPLEFT"),
    _object(l=math.nan, t=20, r=100, b=80, coord_origin="TOPLEFT"),
    _object(l=10, t=20, r=100, b=80, coord_origin="UNKNOWN"),
])
def test_docling_bad_bbox_rejected(box: Any) -> None:
    with pytest.raises(ValueError):
        decode_docling_table(_item(box=box), {1: (400.0, 300.0)}, ExtractionOptions())


def test_docling_ambiguous_page_provenance_rejected() -> None:
    item: Any = _item()
    item.prov.append(item.prov[0])
    with pytest.raises(ValueError):
        decode_docling_table(item, {1: (400.0, 300.0)}, ExtractionOptions())


def test_docling_converter_reading_without_markdown() -> None:
    doc: Any = _object(tables=[_item()],
                       pages={1: _object(size=_object(width=400.0, height=300.0))})
    fake_converter: Any = _object(convert=lambda stream: _object(document=doc))
    provider: DoclingTableProvider = DoclingTableProvider(
        converter=fake_converter, stream_factory=lambda request: request,
        model_id="docling-pinned")
    predictions: tuple[StructuredTablePrediction, ...] = provider.predict_tables(
        _pdf(), ExtractionOptions())
    assert predictions[0].model_id == "docling-pinned"
    assert predictions[0].cells[0][0] == "عنوان"


def test_inspect_never_calls_external_structure_provider() -> None:
    provider: FakeStructureProvider = FakeStructureProvider()
    service: DataExtractionService = DataExtractionService(structure_provider=provider)
    manifest = service.inspect(_pdf())
    assert provider.calls == 0
    assert manifest.page_count == 1
    assert all("document_ai_table" not in region.region_id for page in manifest.pages
               for region in page.regions)


def test_derived_table_preserves_original_image_and_page_order() -> None:
    provider: FakeStructureProvider = FakeStructureProvider()
    service: DataExtractionService = DataExtractionService(structure_provider=provider)
    result = service.extract(_pdf())
    page = result.pages[0]
    images = [r for r in page.regions if r.source_kind == RegionKind.IMAGE
              and r.kind != RegionKind.TABLE]
    tables = [r for r in page.regions if r.kind == RegionKind.TABLE]
    assert provider.calls == 1
    assert len(images) == 1 and len(tables) == 1
    table = tables[0]
    assert table.method == ExtractionMethod.DOCUMENT_AI
    assert table.provenance.original_region_kind == RegionKind.IMAGE
    assert table.provenance.source_surface.value.startswith("pdf_")
    assert table.provenance.provider_name == "DoclingTableProvider"
    assert table.provenance.model_id == "docling-v-test"
    assert table.table_details.merged_cells == (CellMerge(0, 0, 1, 2),)
    assert table.table_details.cell_locations[0].is_column_header
    assert table.relations.region_role == "derived_model_table_overlay"
    assert table.relations.parent_region_id == images[0].region_id
    assert table.position.page_number == 1
    assert result.ready_for_publication is False
    assert len(set(result.document_order_region_ids)) == len(result.document_order_region_ids)
    assert result.manifest.document_order_region_ids == result.document_order_region_ids
    # Valid finite JSON, full provenance and read order.
    payload: dict[str, Any] = json.loads(service.to_json(result))
    assert payload["pages"][0]["regions"][-1]["table_details"]["merged_cells"]


def test_original_regions_are_stable_when_model_adds_table() -> None:
    source: DocumentInput = _pdf()
    baseline: DataExtractionService = DataExtractionService()
    enhanced: DataExtractionService = DataExtractionService(structure_provider=FakeStructureProvider())
    before: list[str] = list(baseline.inspect(source).document_order_region_ids)
    after: list[str] = list(enhanced.extract(source).document_order_region_ids)
    assert all(identifier in after for identifier in before)
    assert len(after) == len(before) + 1


@pytest.mark.parametrize("prediction", [
    _prediction(page=999),
    _prediction(provider=" "),
    _prediction(bbox=(0.0, 0.0, 700.0, 800.0)),
    _prediction(bbox=(0.0, math.nan, 200.0, 250.0)),
    _prediction(rows=(("A", "B"), ("C",))),
    _prediction(merges=(CellMerge(0, 0, 1, 2), CellMerge(0, 1, 1, 1))),
    _prediction(merges=(CellMerge(-1, 0, 1, 1),)),
    replace(_prediction(), cell_locations=(TableCellLocation(5, 0, 1, 1),)),
])
def test_invalid_structure_prediction_fails_closed(prediction: StructuredTablePrediction) -> None:
    source: DocumentInput = _pdf()
    provider: FakeStructureProvider = FakeStructureProvider((prediction,))
    service: DataExtractionService = DataExtractionService(structure_provider=provider)
    original: tuple[str, ...] = DataExtractionService().inspect(source).document_order_region_ids
    result = service.extract(source)
    assert result.document_order_region_ids == original
    assert result.ready_for_publication is False
    assert "document_structure_provider_invalid_or_failed_review" in result.pages[0].warnings


def test_failed_provider_fails_closed_without_losing_text() -> None:
    result = DataExtractionService(structure_provider=FakeStructureProvider(throw=True)).extract(_pdf())
    assert result.ready_for_publication is False
    assert any(r.kind == RegionKind.TEXT for r in result.pages[0].regions)
    assert "document_structure_provider_invalid_or_failed_review" in result.pages[0].warnings


def test_external_table_count_budget() -> None:
    result = DataExtractionService(
        options=ExtractionOptions(max_structured_tables=1),
        structure_provider=FakeStructureProvider((_prediction(), _prediction()))).extract(_pdf())
    assert result.ready_for_publication is False
    assert "document_structure_provider_invalid_or_failed_review" in result.pages[0].warnings


def test_empty_structure_provider_no_changes() -> None:
    result = DataExtractionService(structure_provider=FakeStructureProvider(())).extract(_pdf())
    assert not any(x.region_id.startswith("p1:document_ai_table:")
                   for x in result.pages[0].regions)
    assert "document_structure_provider_invalid_or_failed_review" not in result.pages[0].warnings


def test_multi_page_document_preserves_physical_page_order() -> None:
    source_one: DocumentInput = _pdf()
    combined: fitz.Document = fitz.open()
    with fitz.open(stream=source_one.content, filetype="pdf") as one:
        combined.insert_pdf(one)
        combined.insert_pdf(one)
    source: DocumentInput = DocumentInput("multi.pdf", combined.tobytes(), "pages:2")
    combined.close()
    provider: FakeStructureProvider = FakeStructureProvider((_prediction(page=2),))
    svc: DataExtractionService = DataExtractionService(structure_provider=provider)
    original = svc.inspect(source)
    result = svc.extract(source)
    assert [p.number for p in result.pages] == [1, 2]
    assert result.pages[0].reading_order_region_ids == original.pages[0].reading_order_region_ids
    assert any(r.kind == RegionKind.TABLE and r.method == ExtractionMethod.DOCUMENT_AI
               for r in result.pages[1].regions)
    assert result.structure_provider_invocations == 1
    assert all(r.position.document_order_index == index
               for index, r in enumerate((r for page in result.pages for r in page.regions), start=1))


def test_invalid_later_page_fails_atomically_without_additions() -> None:
    request: DocumentInput = _pdf()
    provider: FakeStructureProvider = FakeStructureProvider((_prediction(), _prediction(page=123)))
    result = DataExtractionService(structure_provider=provider).extract(request)
    assert result.structure_provider_invocations == 1
    assert not any(r.method == ExtractionMethod.DOCUMENT_AI
                   for page in result.pages for r in page.regions)


def test_document_ai_respects_disabled_budget_without_provider_call() -> None:
    provider: FakeStructureProvider = FakeStructureProvider()
    result = DataExtractionService(options=ExtractionOptions(max_structured_tables=0),
                                   structure_provider=provider).extract(_pdf())
    assert provider.calls == 0
    assert result.structure_provider_invocations == 0


def test_missing_provider_dependency_is_reported_safely() -> None:
    class Unavailable:
        def predict_tables(self, document: DocumentInput,
                           options: ExtractionOptions) -> tuple[StructuredTablePrediction, ...]:
            raise ModuleNotFoundError("private provider module path")

    result = DataExtractionService(structure_provider=Unavailable()).extract(_pdf())
    assert "document_structure_provider_dependency_missing" in result.pages[0].warnings
    assert "private provider module path" not in DataExtractionService.to_json(result)
    assert not result.ready_for_publication
