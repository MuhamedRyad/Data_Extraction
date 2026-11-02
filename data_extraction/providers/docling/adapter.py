"""Docling PDF table adapter. Converts DoclingDocument tables, never Markdown.

Docling is an optional dependency and is imported only on actual execution.
A converter can be injected for offline contract tests.
"""
from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Any
import math

from ...shared.models import BoundingBox, CellMerge, DocumentInput, TableCellLocation
from ...options import ExtractionOptions
from ..structure_contracts import StructuredTablePrediction


def _positive_index(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"docling_invalid_{name}")
    return value


def _box(raw: Any, height: float, width: float) -> BoundingBox:
    if raw is None:
        raise ValueError("docling_missing_bbox")
    coords: tuple[float, float, float, float] = (
        float(raw.l), float(raw.t), float(raw.r), float(raw.b))
    if not all(math.isfinite(num) for num in coords):
        raise ValueError("docling_nonfinite_bbox")
    origin: str = str(getattr(raw, "coord_origin", "UNKNOWN")).upper()
    # Docling PDFs usually report BOTTOMLEFT; normalize once to PyMuPDF top-left.
    if "BOTTOMLEFT" in origin:
        box: BoundingBox = (coords[0], height - coords[1], coords[2], height - coords[3])
    elif "TOPLEFT" in origin:
        box = coords
    else:
        raise ValueError("docling_unknown_coordinate_origin")
    if (box[0] < -2 or box[1] < -2 or box[2] > width + 2 or box[3] > height + 2
            or box[0] >= box[2] or box[1] >= box[3]):
        raise ValueError("docling_bbox_outside_page")
    return tuple(max(0.0, min((width if i % 2 == 0 else height), value))
                 for i, value in enumerate(box))  # type: ignore[return-value]


def decode_docling_table(item: Any, dimensions: dict[int, tuple[float, float]],
                         options: ExtractionOptions,
                         model_id: str | None = None) -> StructuredTablePrediction:
    """Reads native Docling TableData cells incl. spans, cell boxes, provenance."""
    provenance: Any = getattr(item, "prov", ())
    if len(provenance) != 1:
        raise ValueError("docling_table_ambiguous_page_provenance")
    page_number: int = _positive_index(provenance[0].page_no, "page_number")
    if page_number not in dimensions or page_number < 1:
        raise ValueError("docling_unknown_page")
    width: float; height: float
    width, height = dimensions[page_number]
    bbox: BoundingBox = _box(provenance[0].bbox, height, width)
    table_data: Any = item.data
    rows: int = _positive_index(table_data.num_rows, "num_rows")
    columns: int = _positive_index(table_data.num_cols, "num_cols")
    if not rows or not columns or rows * columns > options.max_table_cells:
        raise ValueError("docling_table_cell_budget_exceeded")
    matrix: list[list[str | None]] = [[None] * columns for _ in range(rows)]
    occupied: set[tuple[int, int]] = set()
    merges: list[CellMerge] = []
    locations: list[TableCellLocation] = []
    cells: Any = getattr(table_data, "table_cells", None)
    if not isinstance(cells, list) or not cells:
        raise ValueError("docling_empty_table")
    for cell in cells:
        row: int = _positive_index(cell.start_row_offset_idx, "row_index")
        column: int = _positive_index(cell.start_col_offset_idx, "column_index")
        end_row: int = _positive_index(cell.end_row_offset_idx, "end_row_index")
        end_col: int = _positive_index(cell.end_col_offset_idx, "end_col_index")
        if not (row < end_row <= rows and column < end_col <= columns):
            raise ValueError("docling_invalid_cell_span")
        row_span: int = end_row - row
        col_span: int = end_col - column
        for r in range(row, end_row):
            for c in range(column, end_col):
                if (r, c) in occupied:
                    raise ValueError("docling_overlapping_cells")
                occupied.add((r, c))
        value: Any = cell.text
        if not isinstance(value, str):
            raise ValueError("docling_invalid_cell_text")
        matrix[row][column] = value
        if row_span > 1 or col_span > 1:
            merges.append(CellMerge(row, column, row_span, col_span))
        cbbox: BoundingBox | None = None
        if getattr(cell, "bbox", None) is not None:
            cbbox = _box(cell.bbox, height, width)
        locations.append(TableCellLocation(row, column, row_span, col_span, cbbox,
                                           bool(getattr(cell, "column_header", False)),
                                           bool(getattr(cell, "row_header", False))))
    return StructuredTablePrediction(page_number, bbox,
                                     tuple(tuple(line) for line in matrix),
                                     tuple(merges), tuple(locations),
                                     "DoclingTableProvider", model_id)


class DoclingTableProvider:
    def __init__(self, converter: Any | None = None,
                 stream_factory: Callable[[DocumentInput], Any] | None = None,
                 model_id: str | None = None) -> None:
        self._converter: Any | None = converter
        self._stream_factory: Callable[[DocumentInput], Any] | None = stream_factory
        self._model_id: str | None = model_id

    def predict_tables(self, document: DocumentInput,
                       options: ExtractionOptions) -> tuple[StructuredTablePrediction, ...]:
        if self._converter is None:
            from docling.document_converter import DocumentConverter
            from docling.datamodel.base_models import InputFormat
            self._converter = DocumentConverter(allowed_formats=[InputFormat.PDF])
        if self._stream_factory is None:
            from docling.datamodel.base_models import DocumentStream
            stream: Any = DocumentStream(name=document.file_name, stream=BytesIO(document.content))
        else:
            stream = self._stream_factory(document)
        converted: Any = self._converter.convert(stream)
        tables: Any = getattr(converted.document, "tables", None)
        pages: Any = getattr(converted.document, "pages", None)
        if tables is None or pages is None:
            raise ValueError("docling_missing_document_structure")
        if len(tables) > options.max_structured_tables:
            raise ValueError("docling_table_count_exceeded")
        dimensions: dict[int, tuple[float, float]] = {
            int(page_no): (float(page.size.width), float(page.size.height))
            for page_no, page in pages.items()
        }
        return tuple(decode_docling_table(table, dimensions, options, self._model_id)
                     for table in tables)
