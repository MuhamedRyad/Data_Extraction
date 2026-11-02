"""Backward-compatible facade for the native inspect adapter."""

from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest
from data_extraction.inspect_document.service import InspectDocumentService
from data_extraction.shared.models import DocumentInput, FileKind, InspectedDocument


class NativeFileAdapter:
    """Compatibility facade; native dispatch is owned by InspectDocumentService."""

    def __init__(self, service: InspectDocumentService | None = None) -> None:
        self._service = service or InspectDocumentService()

    def inspect(
        self,
        request: DocumentInput,
        kind: FileKind,
        options: InspectDocumentOptions,
    ) -> InspectedDocument:
        contract = InspectDocumentRequest.model_construct(
            file_name=request.file_name,
            content=request.content,
            source_id=request.source_id,
        )
        return self._service._inspect_pages(contract, kind, options)


__all__ = ["NativeFileAdapter"]
