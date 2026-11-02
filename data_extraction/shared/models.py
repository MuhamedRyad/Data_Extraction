"""Typed, vendor-independent contracts for document discovery and extraction."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

BoundingBox: TypeAlias = tuple[float, float, float, float]
TableCells: TypeAlias = list[list[str | None]]


class FileKind(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    CSV = "csv"

    TXT = "txt"
    MD = "md"
    JSON = "json"
    XML = "xml"
    HTML = "html"
    HTM = "htm"
    LOG = "log"

    PNG = "png"
    JPG = "jpg"
    JPEG = "jpeg"
    WEBP = "webp"


class RegionKind(StrEnum):
    TEXT = "text"
    TABLE = "table"
    IMAGE = "image"


class PageKind(StrEnum):
    PHYSICAL_PAGE = "physical_page"
    LOGICAL_DOCUMENT = "logical_document"
    SHEET = "sheet"
    IMAGE = "image"


class ExtractionStatus(StrEnum):
    COMPLETE = "complete"
    COMPLETE_WITH_REVIEW = "complete_with_review"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


class ExtractionMethod(StrEnum):
    NATIVE = "native"
    OCR = "ocr"
    VISION_MODEL = "vision_model"
    DOCUMENT_AI = "document_ai"
    UNRESOLVED = "unresolved"


class SourceSurface(StrEnum):
    """Physical source; distinct from the extracted semantic content type."""
    NATIVE_PDF_TEXT = "native_pdf_text"
    NATIVE_PDF_TABLE = "native_pdf_table"
    PDF_EMBEDDED_IMAGE = "pdf_embedded_image"
    PDF_PAGE_RENDER = "pdf_page_render"
    DOCX_PARAGRAPH = "docx_paragraph"
    DOCX_TABLE = "docx_table"
    DOCX_EMBEDDED_IMAGE = "docx_embedded_image"
    DOCX_HEADER = "docx_header"
    DOCX_FOOTER = "docx_footer"
    WORKSHEET_DRAWING = "worksheet_drawing"
    WORKSHEET_CELLS = "worksheet_cells"
    CSV_CELLS = "csv_cells"
    PLAIN_TEXT = "plain_text"
    STANDALONE_IMAGE = "standalone_image"


class ReadingOrderQuality(StrEnum):
    DOCUMENT_FLOW = "document_flow"
    GEOMETRIC_ESTIMATE = "geometric_estimate"
    AMBIGUOUS_OVERLAP = "ambiguous_overlap"
    COLUMN_HEURISTIC = "column_heuristic"
    EXTERNAL_ESTIMATE = "external_estimate"


class ImageRoute(StrEnum):
    AUTO = "auto"
    OCR_ONLY = "ocr_only"
    VISION_ONLY = "vision_only"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class DocumentInput:
    file_name: str
    content: bytes
    source_id: str


@dataclass(frozen=True, slots=True)
class SourceLocation:
    """Logical source anchor, not a fabricated physical DOCX/Excel page."""
    part: str | None = None
    paragraph_index: int | None = None
    run_index: int | None = None
    table_index: int | None = None
    row_index: int | None = None
    column_index: int | None = None
    sheet_name: str | None = None
    cell_anchor: str | None = None
    section_index: int | None = None


@dataclass(frozen=True, slots=True)
class CellMerge:
    """Zero-based logical table coordinates and spanning dimensions."""
    row_start: int
    column_start: int
    row_span: int
    column_span: int


@dataclass(frozen=True, slots=True)
class RegionPosition:
    """Explicit one-based physical/logical page + reading order positions.

    A deterministic index is NOT proof the inferred PDF reading order is correct.
    """
    page_number: int
    page_kind: PageKind
    page_label: str
    reading_order_index: int
    document_order_index: int
    source_stream_index: int | None
    reading_order_quality: ReadingOrderQuality
    coordinate_system: str | None
    bbox: BoundingBox | None
    normalized_bbox: BoundingBox | None


@dataclass(frozen=True, slots=True)
class RegionRelations:
    """Spatial overlap is evidence, not an invented semantic parent/child edge."""
    overlaps_region_ids: tuple[str, ...] = ()
    has_native_text_overlap: bool = False
    region_role: str = "primary"
    parent_region_id: str | None = None


@dataclass(frozen=True, slots=True)
class RegionProvenance:
    source_id: str
    sha256: str
    source_file_kind: FileKind
    original_region_kind: RegionKind
    source_surface: SourceSurface | None
    extraction_method: ExtractionMethod
    provider_name: str | None
    attempted_methods: tuple[ExtractionMethod, ...]
    source_image_role: str | None
    native_text_masked: bool
    output_content_kind: RegionKind
    model_id: str | None = None
    source_asset_sha256: str | None = None
    source_location: SourceLocation | None = None


@dataclass(frozen=True, slots=True)
class TableDetails:
    row_count: int
    column_count: int
    rectangular: bool
    empty_cell_count: int
    merged_cell_geometry_available: bool = False
    header_row_verified: bool = False
    merged_cells: tuple[CellMerge, ...] = ()
    cell_locations: tuple["TableCellLocation", ...] = ()


@dataclass(frozen=True, slots=True)
class TableCellLocation:
    row: int
    column: int
    row_span: int
    column_span: int
    bbox: BoundingBox | None = None
    is_column_header: bool = False
    is_row_header: bool = False


@dataclass(frozen=True, slots=True)
class ProviderTextResult:
    text: str
    confidence: float | None
    structured_table: TableCells | None = None
    provider_name: str | None = None
    model_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedRegion:
    region_id: str
    kind: RegionKind
    bbox: BoundingBox | None
    method: ExtractionMethod
    text: str | None
    table: TableCells | None
    confidence: float | None
    requires_review: bool
    review_reasons: tuple[str, ...]
    route_reason: str = ""
    attempted_methods: tuple[ExtractionMethod, ...] = ()
    source_kind: RegionKind | None = None
    content_type: RegionKind | None = None
    position: RegionPosition | None = None
    relations: RegionRelations | None = None
    provenance: RegionProvenance | None = None
    table_details: TableDetails | None = None
    selected_provider_name: str | None = None
    selected_model_id: str | None = None
    # v0.10 completeness is separate from review/publication policy.
    content_complete: bool = True
    recovery_applied: bool = False
    recovery_notes: tuple[str, ...] = ()
    critical_for_completeness: bool = True


@dataclass(frozen=True, slots=True)
class PageResult:
    number: int
    kind: PageKind
    label: str
    content_types: tuple[RegionKind, ...]
    regions: tuple[ExtractedRegion, ...]
    requires_review: bool
    warnings: tuple[str, ...]
    width: float | None = None
    height: float | None = None
    reading_order_quality: ReadingOrderQuality = ReadingOrderQuality.GEOMETRIC_ESTIMATE
    reading_order_region_ids: tuple[str, ...] = ()
    layout_provider_name: str | None = None
    layout_confidence: float | None = None
    layout_model_id: str | None = None
    content_complete: bool = True
    completeness_ratio: float = 1.0
    unresolved_region_count: int = 0
    critical_unresolved_region_count: int = 0
    noncritical_unresolved_region_count: int = 0
    review_region_count: int = 0
    extraction_status: ExtractionStatus = ExtractionStatus.COMPLETE
    # v0.13: old region/text coverage is preserved separately from semantic completeness.
    text_coverage_complete: bool = True
    text_coverage_ratio: float = 1.0
    structured_document_kind: str = "general"
    structured_document_confidence: float = 0.0
    semantic_complete: bool | None = None
    semantic_status: str = "not_applicable"
    field_coverage_ratio: float = 1.0
    verified_field_coverage_ratio: float = 1.0
    semantic_gap_count: int = 0
    structured_fields: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    manifest: FileManifest
    pages: tuple[PageResult, ...]
    ready_for_publication: bool
    total_vision_calls: int
    diagnostics: tuple[str, ...]
    schema_version: str = "extraction.v0.6"
    document_order_region_ids: tuple[str, ...] = ()
    structure_provider_invocations: int = 0
    content_complete: bool = True
    completeness_ratio: float = 1.0
    unresolved_region_count: int = 0
    critical_unresolved_region_count: int = 0
    noncritical_unresolved_region_count: int = 0
    review_region_count: int = 0
    extraction_status: ExtractionStatus = ExtractionStatus.COMPLETE
    text_coverage_complete: bool = True
    text_coverage_ratio: float = 1.0
    semantic_complete: bool | None = None
    semantic_status: str = "not_applicable"
    field_coverage_ratio: float = 1.0
    verified_field_coverage_ratio: float = 1.0
    semantic_gap_count: int = 0


# Compatibility re-exports for callers that still import inspect contracts
# from shared.models. The canonical definitions live in inspect_document.
_INSPECT_MODEL_EXPORTS = {
    "CandidateRegion",
    "PageCandidates",
    "InspectedDocument",
    "RegionManifest",
    "PageManifest",
    "FileManifest",
}


def __getattr__(name: str) -> object:
    if name not in _INSPECT_MODEL_EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from ..inspect_document.manifest import FileManifest, PageManifest, RegionManifest
    from ..inspect_document.models import CandidateRegion, InspectedDocument, PageCandidates

    exports: dict[str, object] = {
        "CandidateRegion": CandidateRegion,
        "PageCandidates": PageCandidates,
        "InspectedDocument": InspectedDocument,
        "RegionManifest": RegionManifest,
        "PageManifest": PageManifest,
        "FileManifest": FileManifest,
    }
    globals().update(exports)
    return exports[name]
