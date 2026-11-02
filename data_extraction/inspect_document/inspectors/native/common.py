"""Native parser safety limits used across file formats."""
from __future__ import annotations

from data_extraction.shared.models import (CandidateRegion, FileKind,
    InspectedDocument, PageCandidates, PageKind, RegionKind, BoundingBox,
    TableCells, SourceSurface, SourceLocation, CellMerge)
from data_extraction.shared.errors import ExtractionError
from data_extraction.inspect_document.options import InspectDocumentOptions

def _ensure_regions(regions: list[CandidateRegion], options: InspectDocumentOptions) -> None:
    if len(regions) > options.max_regions_per_page:
        raise ExtractionError("TOO_MANY_REGIONS", "Too many content regions in a page")


