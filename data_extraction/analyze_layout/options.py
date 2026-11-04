from __future__ import annotations

from typing import Self

from ..options import ExtractionOptions
from ..shared.contracts import StrictContract


class AnalyzeLayoutOptions(StrictContract):
    """Options used by deterministic layout analysis."""

    analyze_pdf_rtl_geometry: bool = False
    decorative_region_max_area_ratio: float = 0.0015
    max_regions_per_page: int = 500

    @classmethod
    def from_extraction_options(cls, options: ExtractionOptions) -> Self:
        return cls(
            analyze_pdf_rtl_geometry=options.analyze_pdf_rtl_geometry,
            decorative_region_max_area_ratio=options.decorative_region_max_area_ratio,
            max_regions_per_page=options.max_regions_per_page,
        )
