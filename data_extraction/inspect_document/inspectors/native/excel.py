"""Excel worksheets and anchored drawings."""
from __future__ import annotations

import io
from typing import Any

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.workbook.workbook import Workbook
from PIL import Image

from ..raster_table_hint import is_raster_grid_table
from .common import _ensure_regions
from data_extraction.shared.errors import ExtractionError
from data_extraction.shared.models import (
    CandidateRegion,
    CellMerge,
    PageCandidates,
    PageKind,
    RegionKind,
    SourceLocation,
    SourceSurface,
    TableCells,
)
from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest


def _sheet_table_region(
    worksheet: Worksheet,
    sheet_index: int,
    options: InspectDocumentOptions,
) -> CandidateRegion | None:
    if worksheet.max_column * worksheet.max_row > options.max_sheet_cells:
        raise ExtractionError("TOO_MANY_CELLS", "Worksheet exceeds cell limit")
    cells: TableCells = [
        [None if cell.value is None else str(cell.value) for cell in row]
        for row in worksheet.iter_rows()
    ]
    if not cells:
        return None
    merges: tuple[CellMerge, ...] = tuple(
        CellMerge(
            area.min_row - 1,
            area.min_col - 1,
            area.max_row - area.min_row + 1,
            area.max_col - area.min_col + 1,
        )
        for area in worksheet.merged_cells.ranges
    )
    return CandidateRegion(
        f"sheet{sheet_index}:table:1",
        RegionKind.TABLE,
        table=cells,
        source_surface=SourceSurface.WORKSHEET_CELLS,
        source_location=SourceLocation(part="worksheet", sheet_name=worksheet.title),
        table_merges=merges,
    )


def _worksheet_warnings(worksheet: Worksheet) -> list[str]:
    warnings: list[str] = ["sheet_is_not_a_physical_page"]
    if getattr(worksheet, "_charts", []):
        warnings.append("worksheet_charts_not_extracted")
    if any(cell.comment is not None for row in worksheet.iter_rows() for cell in row):
        warnings.append("worksheet_cell_comments_not_extracted")
    return warnings


def _drawing_anchor(drawing: Any) -> str | None:
    from openpyxl.utils.cell import get_column_letter

    anchor: object = drawing.anchor
    if isinstance(anchor, str):
        return anchor
    if not isinstance(anchor, str) and hasattr(anchor, "_from"):
        marker = anchor._from
        return f"{get_column_letter(marker.col + 1)}{marker.row + 1}"
    return None


def _worksheet_drawings(
    worksheet: Worksheet,
    sheet_index: int,
    options: InspectDocumentOptions,
    image_count: int,
    regions: list[CandidateRegion],
) -> tuple[list[str], int]:
    warnings: list[str] = []
    drawings = getattr(worksheet, "_images", [])
    for drawing_index, drawing in enumerate(drawings, start=1):
        image_count += 1
        if image_count > options.max_image_regions:
            raise ExtractionError("TOO_MANY_IMAGES", "Image region count exceeds limit")
        with Image.open(io.BytesIO(drawing._data())) as embedded:
            if embedded.width * embedded.height > options.max_image_pixels:
                raise ExtractionError("IMAGE_TOO_LARGE", "Embedded Excel image exceeds pixel limit")
            buffer: io.BytesIO = io.BytesIO()
            embedded.convert("RGB").save(buffer, format="PNG")
        raster: bytes = buffer.getvalue()
        cell_anchor: str | None = _drawing_anchor(drawing)
        if cell_anchor is None:
            warnings.append("worksheet_image_anchor_unknown")
        else:
            warnings.append("worksheet_drawings_have_cell_anchors_not_linear_order")
        hint: bool = options.detect_raster_table_grids and is_raster_grid_table(raster)
        regions.append(CandidateRegion(
            f"sheet{sheet_index}:image:{drawing_index}",
            RegionKind.IMAGE,
            image_bytes=raster,
            image_mime_type="image/png",
            image_role="raster_grid_table" if hint else "worksheet_drawing",
            content_hint=RegionKind.TABLE if hint else None,
            source_surface=SourceSurface.WORKSHEET_DRAWING,
            source_location=SourceLocation(
                part="worksheet_drawing",
                sheet_name=worksheet.title,
                cell_anchor=cell_anchor,
            ),
        ))
        _ensure_regions(regions, options)
    return warnings, image_count


def _inspect_worksheet(
    worksheet: Worksheet,
    sheet_index: int,
    options: InspectDocumentOptions,
    image_count: int,
) -> tuple[PageCandidates, int]:
    regions: list[CandidateRegion] = []
    table_region: CandidateRegion | None = _sheet_table_region(
        worksheet,
        sheet_index,
        options,
    )
    warnings: list[str] = _worksheet_warnings(worksheet)
    if table_region is not None:
        regions.append(table_region)

    drawing_warnings, image_count = _worksheet_drawings(
        worksheet,
        sheet_index,
        options,
        image_count,
        regions,
    )
    warnings.extend(drawing_warnings)
    return PageCandidates(
        sheet_index,
        PageKind.SHEET,
        str(worksheet.title),
        tuple(regions),
        tuple(dict.fromkeys(warnings)),
    ), image_count


def _sheet_pages(
    request: InspectDocumentRequest,
    options: InspectDocumentOptions,
) -> tuple[PageCandidates, ...]:
    """Extract worksheet matrices and anchored drawings without claiming visual order."""
    try:
        workbook: Workbook = load_workbook(
            io.BytesIO(request.content),
            read_only=False,
            data_only=False,
            keep_links=False,
        )
        if len(workbook.worksheets) > options.max_pages:
            raise ExtractionError("TOO_MANY_PAGES", "Worksheet count exceeds limit")
        pages: list[PageCandidates] = []
        image_count: int = 0
        for sheet_index, worksheet in enumerate(workbook.worksheets, start=1):
            page, image_count = _inspect_worksheet(
                worksheet,
                sheet_index,
                options,
                image_count,
            )
            pages.append(page)
        workbook.close()
        return tuple(pages)
    except ExtractionError:
        raise
    except Exception as error:
        raise ExtractionError("XLSX_PARSE_FAILED", "Unable to inspect spreadsheet") from error
