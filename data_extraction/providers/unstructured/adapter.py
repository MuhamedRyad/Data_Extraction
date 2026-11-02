"""Optional local Unstructured PDF table adapter.

Reads text_as_html and coordinate metadata, never an LLM table summary.
Missing geometry fails closed rather than inventing a page location.
"""
from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser
from io import BytesIO
import math
from typing import Any

import fitz

from ...shared.models import BoundingBox, CellMerge, DocumentInput, TableCellLocation
from ...options import ExtractionOptions
from ..structure_contracts import StructuredTablePrediction


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int, bool]]] = []
        self._current_row: list[tuple[str, int, int, bool]] | None = None
        self._cell_parts: list[str] | None = None
        self._span: tuple[int, int] = (1, 1)
        self._header: bool = False
        self._depth: int = 0
        self._seen: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            self._seen += 1
            if self._depth != 1 or self._seen != 1:
                raise ValueError("nested_or_multiple_html_tables")
        elif tag == "tr" and self._depth == 1:
            if self._current_row is not None or self._cell_parts is not None:
                raise ValueError("malformed_html_row")
            self._current_row = []
        elif tag in ("td", "th") and self._depth == 1:
            if self._current_row is None or self._cell_parts is not None:
                raise ValueError("malformed_html_cell")
            fields: dict[str, str | None] = dict(attrs)
            try:
                rowspan: int = int(fields.get("rowspan") or "1")
                colspan: int = int(fields.get("colspan") or "1")
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid_html_span") from exc
            if not 1 <= rowspan <= 1000 or not 1 <= colspan <= 1000:
                raise ValueError("html_span_out_of_bounds")
            self._span = rowspan, colspan
            self._header = tag == "th"
            self._cell_parts = []

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._depth == 1:
            if self._cell_parts is None or self._current_row is None:
                raise ValueError("malformed_html_cell_close")
            value: str = "".join(self._cell_parts).strip()
            self._current_row.append((value, *self._span, self._header))
            self._cell_parts = None
        elif tag == "tr" and self._depth == 1:
            if self._current_row is None or self._cell_parts is not None:
                raise ValueError("malformed_html_row_close")
            self.rows.append(self._current_row)
            self._current_row = None
        elif tag == "table":
            self._depth -= 1
            if self._depth < 0 or self._cell_parts is not None or self._current_row is not None:
                raise ValueError("malformed_html_table_close")


def parse_html_table(html: str, options: ExtractionOptions) -> tuple[
    tuple[tuple[str | None, ...], ...], tuple[CellMerge, ...], tuple[TableCellLocation, ...]
]:
    if not isinstance(html, str) or not html.strip() or len(html) > 2_000_000:
        raise ValueError("invalid_html_table")
    parser: _TableParser = _TableParser()
    parser.feed(html)
    parser.close()
    if parser._depth != 0 or parser._seen != 1 or not parser.rows:
        raise ValueError("html_table_missing_or_incomplete")
    if len(parser.rows) > options.max_table_cells:
        raise ValueError("html_table_budget_exceeded")
    matrix: list[list[str | None]] = []
    occupied: set[tuple[int, int]] = set()
    merges: list[CellMerge] = []
    locations: list[TableCellLocation] = []
    max_columns: int = 0
    for row_index, row_data in enumerate(parser.rows):
        row: list[str | None] = []
        matrix.append(row)
        column: int = 0
        for text, rowspan, colspan, is_header in row_data:
            while (row_index, column) in occupied:
                row.append(None)
                column += 1
            if (row_index + rowspan > len(parser.rows)
                    or (column + colspan) * len(parser.rows) > options.max_table_cells):
                raise ValueError("html_span_exceeds_limits")
            for r in range(row_index, row_index + rowspan):
                for c in range(column, column + colspan):
                    if (r, c) in occupied:
                        raise ValueError("html_overlapping_cells")
                    occupied.add((r, c))
            while len(row) <= column:
                row.append(None)
            row[column] = text
            if rowspan > 1 or colspan > 1:
                merges.append(CellMerge(row_index, column, rowspan, colspan))
            locations.append(TableCellLocation(row_index, column, rowspan, colspan,
                                               None, is_header, False))
            column += colspan
            max_columns = max(max_columns, column)
        while (row_index, column) in occupied:
            row.append(None)
            column += 1
        max_columns = max(max_columns, column)
    if not max_columns or len(matrix) * max_columns > options.max_table_cells:
        raise ValueError("html_cell_budget_exceeded")
    for row in matrix:
        row.extend([None] * (max_columns - len(row)))
    return tuple(tuple(row) for row in matrix), tuple(merges), tuple(locations)


def _coordinates(raw: Any, target_width: float,
                 target_height: float) -> BoundingBox:
    if raw is None:
        raise ValueError("unstructured_missing_coordinates")
    system: Any = getattr(raw, "system", None)
    width: Any = getattr(system, "width", None)
    height: Any = getattr(system, "height", None)
    points: Any = getattr(raw, "points", None)
    if (type(width) not in (int, float) or type(height) not in (int, float)
            or not math.isfinite(width) or not math.isfinite(height)
            or width <= 0 or height <= 0 or not isinstance(points, (tuple, list))
            or len(points) < 2):
        raise ValueError("unstructured_invalid_coordinate_system")
    xs: list[float] = []
    ys: list[float] = []
    for pt in points:
        if not isinstance(pt, (tuple, list)) or len(pt) != 2:
            raise ValueError("unstructured_invalid_coordinate_point")
        x: float = float(pt[0]); y: float = float(pt[1])
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError("unstructured_nonfinite_coordinate")
        xs.append(x * target_width / width)
        ys.append(y * target_height / height)
    bbox: BoundingBox = (min(xs), min(ys), max(xs), max(ys))
    if (bbox[0] < 0 or bbox[1] < 0 or bbox[2] > target_width + 0.1
            or bbox[3] > target_height + 0.1 or bbox[0] >= bbox[2]
            or bbox[1] >= bbox[3]):
        raise ValueError("unstructured_invalid_bbox")
    return bbox


class UnstructuredTableProvider:
    def __init__(self, partition_fn: Callable[..., Any] | None = None,
                 model_id: str | None = None) -> None:
        self._partition_fn: Callable[..., Any] | None = partition_fn
        self._model_id: str | None = model_id

    def predict_tables(self, document: DocumentInput,
                       options: ExtractionOptions) -> tuple[StructuredTablePrediction, ...]:
        if self._partition_fn is None:
            from unstructured.partition.pdf import partition_pdf
            self._partition_fn = partition_pdf
        result: Any = self._partition_fn(file=BytesIO(document.content),
                                         strategy="hi_res", infer_table_structure=True)
        with fitz.open(stream=document.content, filetype="pdf") as pdf:
            dimensions: dict[int, tuple[float, float]] = {
                i + 1: (float(page.rect.width), float(page.rect.height))
                for i, page in enumerate(pdf)
            }
        predictions: list[StructuredTablePrediction] = []
        for element in result:
            if getattr(element, "category", None) != "Table":
                continue
            if len(predictions) >= options.max_structured_tables:
                raise ValueError("unstructured_table_budget_exceeded")
            metadata: Any = getattr(element, "metadata", None)
            page: Any = getattr(metadata, "page_number", None)
            if type(page) is not int or page not in dimensions:
                raise ValueError("unstructured_missing_page_number")
            html: Any = getattr(metadata, "text_as_html", None)
            cells, merges, cell_locations = parse_html_table(html, options)
            width: float; height: float
            width, height = dimensions[page]
            bbox: BoundingBox = _coordinates(getattr(metadata, "coordinates", None),
                                             width, height)
            predictions.append(StructuredTablePrediction(page, bbox, cells, merges,
                              cell_locations, "UnstructuredTableProvider", self._model_id))
        return tuple(predictions)
