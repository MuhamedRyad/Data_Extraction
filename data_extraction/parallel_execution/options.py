from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ParallelExecutionOptions(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    enabled: bool = True
    max_parallel_page_groups: int = Field(default=4, ge=1, le=64)
    max_parallel_ocr_calls: int = Field(default=4, ge=1, le=64)
    max_parallel_ai_calls: int = Field(default=2, ge=1, le=32)
