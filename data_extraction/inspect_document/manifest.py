from __future__ import annotations

from dataclasses import dataclass

from ..shared.models import (
    BoundingBox,
    FileKind,
    PageKind,
    ReadingOrderQuality,
    RegionKind,
    RegionPosition,
    RegionRelations,
    SourceLocation,
    SourceSurface,
)


@dataclass(frozen=True, slots=True)
class RegionManifest:
    region_id: str
    kind: RegionKind
    bbox: BoundingBox | None
    preview: str | None
    image_role: str | None
    content_hint: RegionKind | None = None
    source_surface: SourceSurface | None = None
    position: RegionPosition | None = None
    relations: RegionRelations | None = None
    source_location: SourceLocation | None = None


@dataclass(frozen=True, slots=True)
class PageManifest:
    number: int
    kind: PageKind
    label: str
    content_types: tuple[RegionKind, ...]
    regions: tuple[RegionManifest, ...]
    warnings: tuple[str, ...]
    width: float | None = None
    height: float | None = None
    reading_order_quality: ReadingOrderQuality = ReadingOrderQuality.GEOMETRIC_ESTIMATE
    reading_order_region_ids: tuple[str, ...] = ()
    layout_provider_name: str | None = None
    layout_confidence: float | None = None
    layout_model_id: str | None = None


@dataclass(frozen=True, slots=True)
class FileManifest:
    source_id: str
    file_name: str
    sha256: str
    file_kind: FileKind
    page_count: int
    pages: tuple[PageManifest, ...]
    warnings: tuple[str, ...]
    schema_version: str = "extraction.v0.6"
    document_order_region_ids: tuple[str, ...] = ()


__all__ = ["RegionManifest", "PageManifest", "FileManifest"]
