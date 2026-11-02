from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class FieldVerificationOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    render_dpi: int = 180
    crop_padding_x: int = 18
    crop_padding_y: int = 8
    max_vertical_distance: float = 90.0
    minimum_vertical_overlap: float = 0.25
    consensus_count: int = 2
    max_fields: int = 100
    max_field_occurrences: int = 200
    max_candidates_per_field: int = 24
    max_candidate_chars: int = 512
    max_page_pixels: int = 8_000_000
    max_crop_pixels: int = 2_000_000
    max_field_ocr_calls: int = 300
    enable_schema_retry_upscale: bool = True
    schema_retry_upscale_factor: int = 3
