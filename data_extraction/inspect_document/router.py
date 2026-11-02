from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from ..shared.errors import Error, ErrorType
from ..shared.result import Result
from .request import InspectDocumentRequest
from .response import InspectDocumentResponse
from .service import InspectDocumentService


class InspectDocumentRouter:
    def __init__(self, service: InspectDocumentService | None = None) -> None:
        self._service: InspectDocumentService = service or InspectDocumentService()

    def execute(self, raw_input: Mapping[str, object]) -> Result[InspectDocumentResponse]:
        try:
            request: InspectDocumentRequest = InspectDocumentRequest.model_validate(raw_input)
        except ValidationError as exception:
            fields: tuple[str, ...] = tuple(
                ".".join(str(part) for part in item["loc"])
                for item in exception.errors()
            )
            return Result.Failure(
                Error(
                    ErrorType.VALIDATION,
                    "INVALID_INSPECT_DOCUMENT_REQUEST",
                    "The inspect-document request is invalid",
                    {"fields": fields},
                )
            )
        return self._service.execute(request)
