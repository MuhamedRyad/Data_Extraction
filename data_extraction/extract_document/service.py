from __future__ import annotations

from collections.abc import Callable

from ..shared.errors import ExtractionError, error_from_exception
from ..shared.models import DocumentInput, ExtractionResult
from ..shared.result import Result
from .request import ExtractDocumentRequest
from .response import ExtractDocumentResponse


class ExtractDocumentService:
    """Canonical application boundary for document extraction."""

    def __init__(self, extract_document: Callable[[DocumentInput], ExtractionResult]) -> None:
        self._extract_document = extract_document

    def execute(self, request: ExtractDocumentRequest) -> Result[ExtractDocumentResponse]:
        try:
            extraction: ExtractionResult = self._extract_document(request.to_domain())
            return Result.Success(ExtractDocumentResponse(extraction=extraction))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))
