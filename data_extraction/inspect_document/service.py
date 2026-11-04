from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Callable

from ..reading_order.metadata import describe_region, page_order_quality
from .inspectors.native.excel import _sheet_pages
from .inspectors.native.flat_files import _csv_pages, _image_pages, _text_pages
from .inspectors.native.pdf import _pdf_pages
from .inspectors.native.word import _word_pages
from ..shared.errors import ExtractionError, error_from_exception
from ..shared.models import FileKind, PageCandidates, RegionKind
from ..shared.result import Result
from .manifest import FileManifest, PageManifest, RegionManifest
from .models import InspectedDocument
from .options import InspectDocumentOptions
from .request import InspectDocumentRequest
from .response import InspectDocumentResponse
from .validation import validate_options, validate_upload


_INSPECTORS: dict[
    FileKind,
    Callable[[InspectDocumentRequest, InspectDocumentOptions], tuple[PageCandidates, ...]],
] = {
    FileKind.PDF: _pdf_pages,
    FileKind.DOCX: _word_pages,
    FileKind.XLSX: _sheet_pages,
    FileKind.CSV: _csv_pages,
    FileKind.TXT: _text_pages,
    FileKind.MD: _text_pages,
    FileKind.JSON: _text_pages,
    FileKind.XML: _text_pages,
    FileKind.HTML: _text_pages,
    FileKind.HTM: _text_pages,
    FileKind.LOG: _text_pages,
    FileKind.PNG: _image_pages,
    FileKind.JPG: _image_pages,
    FileKind.JPEG: _image_pages,
    FileKind.WEBP: _image_pages,
}
'''
dict =FileKindو ActionName
Dictionary<FileKind, Func<
    InspectDocumentRequest,
    InspectDocumentOptions,
    IReadOnlyList<PageCandidates>

     handler = _INSPECTORS[kind]  => get actionName
     pages: tuple[PageCandidates, ...] = handler(request, options)


>>'''



class InspectDocumentService:
    """Canonical inspect feature service with explicit Result semantics."""

    def __init__(
        self,
        options: InspectDocumentOptions | None = None,
    ) -> None:
        self._options: InspectDocumentOptions = options or InspectDocumentOptions()

    @property
    def options(self) -> InspectDocumentOptions:
        return self._options

    def execute(self, request: InspectDocumentRequest) -> Result[InspectDocumentResponse]:
        try:
            document: InspectedDocument = self.inspect(request)
            return Result.Success(
                InspectDocumentResponse(manifest=self.build_manifest(document))
            )
        except ExtractionError as exception:
            return Result.Failure(error_from_exception(exception))

    def inspect(
        self,
        request: InspectDocumentRequest,
        options: InspectDocumentOptions | None = None,
    ) -> InspectedDocument:
        effective_options: InspectDocumentOptions = options or self._options
        validate_options(effective_options)
        validate_upload(request, effective_options)

        kind = self._detect_file(request)
        return self._inspect_pages(request, kind, effective_options)

    @staticmethod
    def _detect_file(request: InspectDocumentRequest) -> FileKind:
        extension: str = Path(request.file_name).suffix.lower().lstrip(".")
        try:
            return FileKind(extension)
        except ValueError as exception:
            raise ExtractionError(
                "UNSUPPORTED_FILE_TYPE",
                "Unsupported file extension",
            ) from exception

    def _inspect_pages(
        self,
        request: InspectDocumentRequest,
        kind: FileKind,
        options: InspectDocumentOptions,
    ) -> InspectedDocument:
        handler = _INSPECTORS[kind]
        pages: tuple[PageCandidates, ...] = handler(request, options)
        return InspectedDocument(
            request.source_id,
            request.file_name,
            sha256(request.content).hexdigest(),
            kind,
            pages,
        )

    @staticmethod
    def build_manifest(document: InspectedDocument) -> FileManifest:
        page_manifests: list[PageManifest] = []
        document_order: list[str] = []
        for page in document.pages:
            kinds: tuple[RegionKind, ...] = tuple(
                sorted({region.kind for region in page.regions}, key=lambda kind: kind.value)
            )
            manifest_regions: list[RegionManifest] = []
            for index, region in enumerate(page.regions, start=1):
                document_order.append(region.region_id)
                position, relations = describe_region(
                    page, region, index, len(document_order)
                )
                manifest_regions.append(
                    RegionManifest(
                        region.region_id,
                        region.kind,
                        region.bbox,
                        (
                            region.text[:100]
                            if region.text is not None
                            else f"{len(region.table or [])} rows"
                            if region.table is not None
                            else None
                        ),
                        region.image_role,
                        region.content_hint,
                        region.source_surface,
                        position,
                        relations,
                        region.source_location,
                    )
                )
            page_manifests.append(
                PageManifest(
                    page.number,
                    page.kind,
                    page.label,
                    kinds,
                    tuple(manifest_regions),
                    page.warnings
                    + (
                        ("reading_order_ambiguous_overlap",)
                        if page_order_quality(page).value == "ambiguous_overlap"
                        else ()
                    ),
                    page.width,
                    page.height,
                    page_order_quality(page),
                    tuple(region.region_id for region in page.regions),
                    page.layout_provider_name,
                    page.layout_confidence,
                    page.layout_model_id,
                )
            )
        return FileManifest(
            document.source_id,
            document.file_name,
            document.sha256,
            document.file_kind,
            len(document.pages),
            tuple(page_manifests),
            document.warnings,
            "extraction.v0.6",
            tuple(document_order),
        )


 
