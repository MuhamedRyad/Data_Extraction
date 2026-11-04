from __future__ import annotations

from typing import Self

from ..options import ExtractionOptions
from ..shared.contracts import StrictContract


class InspectDocumentOptions(StrictContract):
    """Options required to validate and inspect native document structure."""

    max_input_bytes: int = 15_000_000
    max_original_image_bytes: int = 15_000_000
    max_uncompressed_zip_bytes: int = 40_000_000
    max_pages: int = 100
    max_regions_per_page: int = 500
    max_sheet_cells: int = 30_000
    max_image_pixels: int = 8_000_000
    max_image_regions: int = 200
    image_dpi: int = 150
    min_image_dpi: int = 72
    adaptive_image_recovery: bool = True
    detect_raster_table_grids: bool = True
    selected_pdf_pages: tuple[int, ...] = ()

    @classmethod
    def from_extraction_options(cls, options: ExtractionOptions) -> Self:
        """Project shared capability options into the inspect-owned subset."""
        return cls(
            max_input_bytes=options.max_input_bytes,
            max_uncompressed_zip_bytes=options.max_uncompressed_zip_bytes,
            max_pages=options.max_pages,
            max_regions_per_page=options.max_regions_per_page,
            max_sheet_cells=options.max_sheet_cells,
            max_image_pixels=options.max_image_pixels,
            max_image_regions=options.max_image_regions,
            image_dpi=options.image_dpi,
            min_image_dpi=options.min_image_dpi,
            adaptive_image_recovery=options.adaptive_image_recovery,
            detect_raster_table_grids=options.detect_raster_table_grids,
            selected_pdf_pages=options.selected_pdf_pages,
        )


__all__ = ["InspectDocumentOptions"]
