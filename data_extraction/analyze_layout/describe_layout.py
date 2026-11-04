"""Source provenance and metadata for extracted regions."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

from ..reading_order.metadata import describe_region, page_order_quality
from ..shared.models import (
    CandidateRegion, CellMerge, ExtractedRegion, InspectedDocument, PageCandidates,
    RegionPosition, RegionProvenance, RegionRelations, TableDetails,
)


def table_details(table: list[list[str | None]] | None,
                  merged_cells: tuple[CellMerge, ...] | None = None) -> TableDetails | None:
    if table is None:
        return None
    widths: set[int] = {len(row) for row in table}
    return TableDetails(
        row_count=len(table),
        column_count=max(widths, default=0),
        rectangular=len(widths) <= 1,
        empty_cell_count=sum(cell is None or cell == "" for row in table for cell in row),
        merged_cell_geometry_available=merged_cells is not None,
        merged_cells=merged_cells or (),
    )


def enrich_extracted(region: CandidateRegion, extracted: ExtractedRegion,
                     page: PageCandidates, document: InspectedDocument,
                     within_page_index: int, document_index: int,
                     provider_name: str | None) -> ExtractedRegion:
    position: RegionPosition
    relations: RegionRelations
    position, relations = describe_region(page, region, within_page_index, document_index)
    provenance: RegionProvenance = RegionProvenance(
        document.source_id, document.sha256, document.file_kind,
        region.original_source_kind or region.kind, region.source_surface, extracted.method,
        extracted.selected_provider_name or provider_name, extracted.attempted_methods, region.image_role,
        region.native_text_masked, extracted.kind, extracted.selected_model_id,
        sha256(region.original_image_bytes or region.image_bytes).hexdigest()
        if region.original_image_bytes is not None or region.image_bytes is not None else None,
        region.source_location,
    )
    return replace(extracted, content_type=extracted.kind, position=position,
                   relations=relations, provenance=provenance,
                   table_details=(replace(table_details(extracted.table, region.table_merges),
                                          cell_locations=region.table_cell_locations or ())
                                  if extracted.table is not None else None))
