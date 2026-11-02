from __future__ import annotations

import pytest
from pydantic import ValidationError

from data_extraction import Error, ErrorType, ExtractionOptions, ParallelExecutionOptions, Result
from data_extraction.template_extraction.options import TemplateExtractionOptions
from data_extraction.verify_fields.options import FieldVerificationOptions


def test_result_success_and_failure_are_explicit() -> None:
    success: Result[int] = Result.Success(7)
    failure: Result[int] = Result.Failure(
        Error(ErrorType.VALIDATION, "BAD_INPUT", "Bad input")
    )
    assert success.is_success and success.unwrap() == 7
    assert failure.is_failure and failure.error is not None
    assert failure.error.code == "BAD_INPUT"


def test_extraction_options_are_strict_frozen_and_expose_parallel_feature_options() -> None:
    options = ExtractionOptions(max_parallel_ocr_calls=3)
    assert options.parallel == ParallelExecutionOptions(
        enabled=True,
        max_parallel_page_groups=4,
        max_parallel_ocr_calls=3,
        max_parallel_ai_calls=2,
    )
    with pytest.raises(ValidationError):
        ExtractionOptions(max_pages="100")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ExtractionOptions(unknown_option=True)  # type: ignore[call-arg]


def test_feature_options_are_strict_contracts() -> None:
    assert TemplateExtractionOptions().max_pages_per_ai_batch == 4
    assert FieldVerificationOptions().consensus_count == 2
    with pytest.raises(ValidationError):
        TemplateExtractionOptions(max_refinement_attempts="2")  # type: ignore[arg-type]


