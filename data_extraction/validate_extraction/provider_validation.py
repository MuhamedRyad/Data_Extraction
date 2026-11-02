"""Fail-closed validation of untrusted OCR/Vision provider output."""
from __future__ import annotations

import math

from ..shared.models import ProviderTextResult
from ..options import ExtractionOptions


class InvalidProviderResult(ValueError):
    pass


def validate_provider_result(result: object, options: ExtractionOptions) -> ProviderTextResult:
    if not isinstance(result, ProviderTextResult):
        raise InvalidProviderResult("unexpected_provider_response_type")
    if not isinstance(result.text, str) or len(result.text) > 1_000_000:
        raise InvalidProviderResult("invalid_provider_text")
    if result.confidence is not None:
        value: object = result.confidence
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise InvalidProviderResult("invalid_provider_confidence")
    for label, value in (("provider_name", result.provider_name), ("model_id", result.model_id)):
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise InvalidProviderResult("invalid_" + label)
    matrix: object = result.structured_table
    if matrix is not None:
        if not isinstance(matrix, list) or not matrix:
            raise InvalidProviderResult("invalid_provider_table")
        total: int = 0
        for row in matrix:
            if not isinstance(row, list):
                raise InvalidProviderResult("table_row_is_not_a_list")
            total += len(row)
            if total > options.max_sheet_cells:
                raise InvalidProviderResult("provider_table_exceeds_cell_limit")
            if any(not (isinstance(cell, str) or cell is None) for cell in row):
                raise InvalidProviderResult("invalid_provider_cell_type")
            if any(isinstance(cell, str) and len(cell) > 100_000 for cell in row):
                raise InvalidProviderResult("provider_cell_too_large")
    return result
