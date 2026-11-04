"""Coordinate inspection and layout analysis for application entry points."""
from __future__ import annotations

from .analyze_layout.native_pdf import analyze_pdf_layout
from .analyze_layout.options import AnalyzeLayoutOptions
from .inspect_document.models import InspectedDocument
from .inspect_document.options import InspectDocumentOptions
from .inspect_document.request import InspectDocumentRequest
from .inspect_document.response import InspectDocumentResponse
from .inspect_document.service import InspectDocumentService
from .shared.errors import ExtractionError, error_from_exception
from .shared.result import Result


def inspect_and_analyze(
    request: InspectDocumentRequest,
    service: InspectDocumentService,
    inspect_options: InspectDocumentOptions | None = None,
    analysis_options: AnalyzeLayoutOptions | None = None,
) -> InspectedDocument:
    effective_inspect_options = inspect_options or service.options
    effective_analysis_options = analysis_options or AnalyzeLayoutOptions(
        max_regions_per_page=effective_inspect_options.max_regions_per_page
    )
    document = service.inspect(request, effective_inspect_options)
    return analyze_pdf_layout(document, request.content, effective_analysis_options)


def inspect_document_result(
    request: InspectDocumentRequest,
    service: InspectDocumentService,
    inspect_options: InspectDocumentOptions | None = None,
    analysis_options: AnalyzeLayoutOptions | None = None,
) -> Result[InspectDocumentResponse]:
    try:
        document = inspect_and_analyze(request, service, inspect_options, analysis_options)
        return Result.Success(InspectDocumentResponse(manifest=service.build_manifest(document)))
    except ExtractionError as exception:
        return Result.Failure(error_from_exception(exception))
