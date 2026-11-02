from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Callable

from pydantic import ValidationError

from ..shared.errors import Error, ErrorType
from ..shared.models import DocumentInput, ExtractionResult
from ..shared.result import Result
from .request import FieldDiscoveryInput
from .response import FieldDiscoveryResult
from .workflow import FieldDiscoveryWorkflowService


class FieldDiscoveryRouter:
    def __init__(self, workflow: FieldDiscoveryWorkflowService) -> None:
        self._workflow = workflow

    def execute(self, raw_input: Mapping[str, object]) -> Result[FieldDiscoveryResult]:
        try:
            request = FieldDiscoveryInput.model_validate(raw_input)
        except ValidationError as exception:
            fields = tuple(
                ".".join(str(part) for part in item["loc"])
                for item in exception.errors()
            )
            return Result.Failure(Error(
                ErrorType.VALIDATION,
                "INVALID_FIELD_DISCOVERY_REQUEST",
                "The field-discovery request is invalid",
                {"fields": fields},
            ))
        return self._workflow.execute(request)
