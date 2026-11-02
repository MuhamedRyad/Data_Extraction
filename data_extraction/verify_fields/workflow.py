from __future__ import annotations

from collections.abc import Callable

from ..shared.errors import ExtractionError, error_from_exception
from ..shared.models import DocumentInput, ExtractionResult
from ..shared.result import Result
from .request import FieldVerificationInput
from .response import FieldVerificationResult
from .service import FieldVerificationService


class FieldVerificationWorkflowService:
    def __init__(
        self,
        extract_document: Callable[[DocumentInput], ExtractionResult],
        service: FieldVerificationService,
    ) -> None:
        self._extract_document = extract_document
        self._service = service

    def execute(self, request: FieldVerificationInput) -> Result[FieldVerificationResult]:
        try:
            domain = request.to_domain()
            extraction = self._extract_document(domain.document)
            return Result.Success(self._service.verify(domain, extraction))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))
