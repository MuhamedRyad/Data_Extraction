from __future__ import annotations

from collections.abc import Callable

from ..shared.errors import ExtractionError, error_from_exception
from ..shared.models import DocumentInput, ExtractionResult
from ..shared.result import Result
from .request import FieldDiscoveryInput
from .response import FieldDiscoveryResult
from .service import FieldDiscoveryService


class FieldDiscoveryWorkflowService:
    def __init__(
        self,
        extract_document: Callable[[DocumentInput], ExtractionResult],
        service: FieldDiscoveryService | None = None,
    ) -> None:
        self._extract_document = extract_document
        self._service = service or FieldDiscoveryService()

    def execute(self, request: FieldDiscoveryInput) -> Result[FieldDiscoveryResult]:
        try:
            domain = request.to_domain()
            extraction: ExtractionResult = self._extract_document(domain.document)
            return Result.Success(self._service.discover(extraction, domain.kinds))
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))
