from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .parallel_execution.options import ParallelExecutionOptions
from .shared.models import ImageRoute


class ExtractionOptions(BaseModel):
    """Validated immutable configuration for the extraction capability."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    max_input_bytes: int = 15_000_000
    max_uncompressed_zip_bytes: int = 40_000_000
    max_pages: int = 100
    max_regions_per_page: int = 500
    max_image_pixels: int = 8_000_000
    max_sheet_cells: int = 30_000
    max_image_regions: int = 200
    max_vision_calls: int = 10
    image_dpi: int = 150
    ocr_confidence_threshold: float = 0.83
    image_route: ImageRoute = ImageRoute.AUTO
    detect_raster_table_grids: bool = True
    max_structured_tables: int = 80
    max_table_cells: int = 4000
    max_layout_calls: int = 20
    layout_render_dpi: int = 96
    analyze_pdf_rtl_geometry: bool = False

    adaptive_image_recovery: bool = True
    auto_enable_local_ocr: bool = False
    tesseract_language: str = "ara+eng"
    adaptive_tesseract_profiles: bool = True
    tesseract_language_candidates: tuple[str, ...] = ("ara+eng", "eng")
    min_image_dpi: int = 72
    enable_ocr_tiling: bool = True
    ocr_tile_trigger_pixels: int = 3_000_000
    ocr_tile_max_pixels: int = 1_500_000
    max_ocr_tile_calls_per_region: int = 8
    enable_low_confidence_ocr_preprocessing: bool = True
    max_ocr_preprocess_attempts: int = 2
    enable_ocr_table_geometry_recovery: bool = True
    enable_semantic_raster_table_recovery: bool = True
    decorative_region_max_area_ratio: float = 0.0015
    layout_provider_fallback_only: bool = False

    auto_enable_ai: bool = True
    auto_ai_for_structured_images: bool = True
    ai_model: str = "gpt-6.1-sol"
    ai_base_url: str = "https://api.openai.com/v1"
    ai_timeout_seconds: float = 90.0
    ai_image_detail: str = "high"
    ai_max_visual_dimension: int = 1800
    ai_max_output_tokens: int = 5000
    ai_store_response: bool = False

    enable_structured_document_analysis: bool = True
    structured_document_min_confidence: float = 0.72
    structured_document_suspicion_confidence: float = 0.40

    # Compatibility-flat settings remain public in v0.24. New code should use
    # the ``parallel`` property, which returns the feature-owned options model.
    enable_parallel_execution: bool = True
    max_parallel_page_groups: int = 4
    max_parallel_ocr_calls: int = 4
    max_parallel_ai_calls: int = 2

    selected_pdf_pages: tuple[int, ...] = ()

    @property
    def parallel(self) -> ParallelExecutionOptions:
        return ParallelExecutionOptions(
            enabled=self.enable_parallel_execution,
            max_parallel_page_groups=self.max_parallel_page_groups,
            max_parallel_ocr_calls=self.max_parallel_ocr_calls,
            max_parallel_ai_calls=self.max_parallel_ai_calls,
        )
