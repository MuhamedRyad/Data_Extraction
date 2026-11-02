"""CSV, UTF-8 text and standalone image extraction."""
from __future__ import annotations

import csv
import io
from PIL import Image
from data_extraction.providers.native.image_layout import is_raster_grid_table
from data_extraction.shared.models import (CandidateRegion, FileKind,
    InspectedDocument, PageCandidates, PageKind, RegionKind, BoundingBox,
    TableCells, SourceSurface, SourceLocation, CellMerge)
from data_extraction.shared.errors import ExtractionError
from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest

def _csv_pages(request: InspectDocumentRequest, options: InspectDocumentOptions) -> tuple[PageCandidates, ...]:
    content: str = request.content.decode("utf-8-sig")
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(content[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows: TableCells = [[str(value) for value in row] for row in csv.reader(io.StringIO(content), dialect)]
    if sum(len(row) for row in rows) > options.max_sheet_cells:
        raise ExtractionError("TOO_MANY_CELLS", "CSV exceeds cell limit")
    regions: tuple[CandidateRegion, ...] = (
        CandidateRegion("csv:table:1", RegionKind.TABLE, table=rows,
                        source_surface=SourceSurface.CSV_CELLS),
    ) if rows else ()
    return (PageCandidates(1, PageKind.LOGICAL_DOCUMENT, "csv", regions,
                           ("csv_has_no_physical_pages",)),)


def _text_pages(request: InspectDocumentRequest, options: InspectDocumentOptions) -> tuple[PageCandidates, ...]:
    content: str = request.content.decode("utf-8-sig")
    region: CandidateRegion = CandidateRegion("d1:text:1", RegionKind.TEXT, text=content,
                                             source_surface=SourceSurface.PLAIN_TEXT)
    return (PageCandidates(1, PageKind.LOGICAL_DOCUMENT, "document", (region,),
                           ("plain_text_has_no_physical_pages",)),)


def _image_pages(request: InspectDocumentRequest, options: InspectDocumentOptions) -> tuple[PageCandidates, ...]:
    warnings: list[str] = []
    try:
        with Image.open(io.BytesIO(request.content)) as image:
            mime: str = Image.MIME.get(image.format or "", "image/png")
            original_width: int = image.width
            original_height: int = image.height
            rgb: Image.Image = image.convert("RGB")
            pixels: int = rgb.width * rgb.height
            if pixels > options.max_image_pixels:
                if not options.adaptive_image_recovery:
                    raise ExtractionError("IMAGE_TOO_LARGE", "Image exceeds pixel budget")
                ratio: float = (options.max_image_pixels / float(pixels)) ** 0.5
                target: tuple[int, int] = (
                    max(1, int(rgb.width * ratio)), max(1, int(rgb.height * ratio))
                )
                rgb = rgb.resize(target, Image.Resampling.LANCZOS)
                warnings.append("image_downscaled_for_processing_review")
            buffer: io.BytesIO = io.BytesIO()
            rgb.save(buffer, format="PNG")
            content: bytes = buffer.getvalue()
    except ExtractionError:
        raise
    except Exception as error:
        raise ExtractionError("IMAGE_PARSE_FAILED", "Unable to inspect image") from error
    table_hint: bool = options.detect_raster_table_grids and is_raster_grid_table(content)
    region: CandidateRegion = CandidateRegion(
        "image:1", RegionKind.IMAGE,
        (0.0, 0.0, float(original_width), float(original_height)),
        image_bytes=content, image_mime_type="image/png",
        image_role="raster_grid_table" if table_hint else "standalone_image",
        content_hint=RegionKind.TABLE if table_hint else None,
        source_surface=SourceSurface.STANDALONE_IMAGE,
    )
    return (PageCandidates(1, PageKind.IMAGE, "image", (region,), tuple(warnings),
                           float(original_width), float(original_height)),)


