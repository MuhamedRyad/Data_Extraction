from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from ..shared.errors import Error, ErrorType
from ..shared.result import Result
from .request import FieldVerificationInput
from .response import FieldVerificationResult
from .workflow import FieldVerificationWorkflowService


class FieldVerificationRouter:
    def __init__(self, workflow: FieldVerificationWorkflowService) -> None:
        self._workflow = workflow

    def execute(self, raw_input: Mapping[str, object]) -> Result[FieldVerificationResult]:
        try:
            request = FieldVerificationInput.model_validate(raw_input)
        except ValidationError as exception:
            fields = tuple(
                ".".join(str(part) for part in item["loc"])
                for item in exception.errors()
            )
            return Result.Failure(Error(
                ErrorType.VALIDATION,
                "INVALID_FIELD_VERIFICATION_REQUEST",
                "The field-verification request is invalid",
                {"fields": fields},
            ))
        return self._workflow.execute(request)
