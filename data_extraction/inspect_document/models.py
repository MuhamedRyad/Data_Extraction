from __future__ import annotations

from dataclasses import dataclass, field

from ..shared.models import (
    BoundingBox,
    CellMerge,
    ExtractionMethod,
    FileKind,
    PageKind,
    RegionKind,
    SourceLocation,
    SourceSurface,
    TableCellLocation,
    TableCells,
    TableTextEvidence,
)


@dataclass(frozen=True, slots=True)
class CandidateRegion:
    region_id: str
    kind: RegionKind
    bbox: BoundingBox | None = None
    text: str | None = None
    table: TableCells | None = None
    image_bytes: bytes | None = field(default=None, repr=False)
    image_mime_type: str | None = None
    image_role: str | None = None
    content_hint: RegionKind | None = None
    source_surface: SourceSurface | None = None
    original_index: int | None = None
    native_text_masked: bool = False
    source_location: SourceLocation | None = None
    parent_region_id: str | None = None
    table_merges: tuple[CellMerge, ...] | None = None
    extraction_method_override: ExtractionMethod | None = None
    extraction_provider_name: str | None = None
    extraction_model_id: str | None = None
    original_source_kind: RegionKind | None = None
    table_cell_locations: tuple[TableCellLocation, ...] | None = None
    semantic_role: str | None = None
    reading_order_proposal: str | None = None
    table_text_evidence: tuple[TableTextEvidence, ...] = ()
    original_image_bytes: bytes | None = field(default=None, repr=False)
    original_image_mime_type: str | None = None
    original_image_mask_bytes: bytes | None = field(default=None, repr=False)
    original_image_ocr_safe: bool = False


@dataclass(frozen=True, slots=True)
class PDFTextLineEvidence:
    text: str
    bbox: BoundingBox
    source_region_id: str


@dataclass(frozen=True, slots=True)
class PDFSpanStyleEvidence:
    font_size: float
    character_count: int
    font_flags: int | None
    font_name: str


@dataclass(frozen=True, slots=True)
class PDFBlockStyleEvidence:
    citation_anchor: str
    text: str
    page_number: int
    bbox: BoundingBox
    page_height: float
    line_count: int
    spans: tuple[PDFSpanStyleEvidence, ...]


@dataclass(frozen=True, slots=True)
class PDFPageAnalysisEvidence:
    """Native text observations retained for subsequent layout analysis."""

    text_lines: tuple[PDFTextLineEvidence, ...] = ()
    block_styles: tuple[PDFBlockStyleEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class PageCandidates:
    number: int
    kind: PageKind
    label: str
    regions: tuple[CandidateRegion, ...]
    warnings: tuple[str, ...] = ()
    width: float | None = None
    height: float | None = None
    layout_provider_name: str | None = None
    layout_confidence: float | None = None
    layout_model_id: str | None = None
    pdf_analysis_evidence: PDFPageAnalysisEvidence | None = None


@dataclass(frozen=True, slots=True)
class InspectedDocument:
    source_id: str
    file_name: str
    sha256: str
    file_kind: FileKind
    pages: tuple[PageCandidates, ...]
    warnings: tuple[str, ...] = ()


__all__ = [
    "CandidateRegion", "PageCandidates", "InspectedDocument",
    "PDFTextLineEvidence", "PDFSpanStyleEvidence", "PDFBlockStyleEvidence",
    "PDFPageAnalysisEvidence",
]
