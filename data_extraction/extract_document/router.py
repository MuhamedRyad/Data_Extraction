from __future__ import annotations

from collections.abc import Mapping

from pydantic import ValidationError

from ..shared.errors import Error, ErrorType
from ..shared.result import Result
from .request import ExtractDocumentRequest
from .response import ExtractDocumentResponse
from .service import ExtractDocumentService


class ExtractDocumentRouter:
    def __init__(self, service: ExtractDocumentService) -> None:
        self._service = service

    def execute(self, raw_input: Mapping[str, object]) -> Result[ExtractDocumentResponse]:
        try:
            request = ExtractDocumentRequest.model_validate(raw_input)
        except ValidationError as exception:
            fields: tuple[str, ...] = tuple(
                ".".join(str(part) for part in item["loc"])
                for item in exception.errors()
            )
            return Result.Failure(Error(
                ErrorType.VALIDATION,
                "INVALID_EXTRACT_DOCUMENT_REQUEST",
                "The extract-document request is invalid",
                {"fields": fields},
            ))
        return self._service.execute(request)
