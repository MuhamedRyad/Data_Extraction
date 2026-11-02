"""Add external table evidence, without deleting/replacing source regions.

Only extract(), never inspect(), invokes optional document AI. All new tables
are flagged as derived overlays so downstream systems can avoid double-counting.
"""
from __future__ import annotations

from dataclasses import replace
import math

from ..shared.models import (BoundingBox, CandidateRegion, DocumentInput, ExtractionMethod,
                      FileKind, InspectedDocument, PageCandidates, RegionKind,
                      SourceSurface)
from ..options import ExtractionOptions
from ..providers.structure_contracts import DocumentStructureProvider, StructuredTablePrediction


def _overlap_fraction(a: BoundingBox, b: BoundingBox) -> float:
    x: float = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    y: float = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    return (x * y) / max(0.000001, (a[2] - a[0]) * (a[3] - a[1]))


def _validate(prediction: StructuredTablePrediction, page: PageCandidates,
              options: ExtractionOptions) -> None:
    if not isinstance(prediction, StructuredTablePrediction):
        raise ValueError("invalid_structure_prediction_type")
    if type(prediction.page_number) is not int or prediction.page_number != page.number:
        raise ValueError("invalid_structure_page")
    if (not isinstance(prediction.provider_name, str) or not prediction.provider_name.strip()
            or len(prediction.provider_name) > 120):
        raise ValueError("invalid_structure_provider")
    if prediction.model_id is not None and (not isinstance(prediction.model_id, str)
                                            or len(prediction.model_id) > 200):
        raise ValueError("invalid_structure_model")
    bbox: BoundingBox = prediction.bbox
    if (not isinstance(bbox, tuple) or len(bbox) != 4
            or any(type(n) not in (float, int) or not math.isfinite(n) for n in bbox)
            or bbox[0] < 0 or bbox[1] < 0 or bbox[2] > (page.width or 0) + 0.1
            or bbox[3] > (page.height or 0) + 0.1
            or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
        raise ValueError("invalid_structure_bbox")
    data: tuple[tuple[str | None, ...], ...] = prediction.cells
    if (not isinstance(data, tuple) or not data or
            not isinstance(data[0], tuple) or not data[0]):
        raise ValueError("invalid_structure_cells")
    columns: int = len(data[0])
    if len(data) * columns > options.max_table_cells:
        raise ValueError("structure_cell_budget_exceeded")
    if any(not isinstance(row, tuple) or len(row) != columns or any(
        cell is not None and not isinstance(cell, str) for cell in row
    ) for row in data):
        raise ValueError("nonrectangular_or_invalid_table")
    covered: set[tuple[int, int]] = set()
    for merge in prediction.merges:
        if (type(merge.row_start) is not int or type(merge.column_start) is not int or
                type(merge.row_span) is not int or type(merge.column_span) is not int or
                merge.row_start < 0 or merge.column_start < 0 or
                merge.row_span < 1 or merge.column_span < 1 or
                merge.row_start + merge.row_span > len(data) or
                merge.column_start + merge.column_span > columns):
            raise ValueError("structure_invalid_merge")
        for r in range(merge.row_start, merge.row_start + merge.row_span):
            for c in range(merge.column_start, merge.column_start + merge.column_span):
                if (r, c) in covered:
                    raise ValueError("structure_overlapping_merges")
                covered.add((r, c))
    observed: set[tuple[int, int]] = set()
    for cell in prediction.cell_locations:
        key: tuple[int, int] = (cell.row, cell.column)
        if (type(cell.row) is not int or type(cell.column) is not int or
                type(cell.row_span) is not int or type(cell.column_span) is not int or
                cell.row < 0 or cell.column < 0 or
                cell.row_span < 1 or cell.column_span < 1 or
                cell.row + cell.row_span > len(data) or
                cell.column + cell.column_span > columns or key in observed):
            raise ValueError("invalid_structure_cell_location")
        observed.add(key)
        if cell.bbox is not None and (not all(isinstance(x, (float, int)) and
                math.isfinite(x) for x in cell.bbox) or
                cell.bbox[0] >= cell.bbox[2] or cell.bbox[1] >= cell.bbox[3]):
            raise ValueError("invalid_structure_cell_bbox")


class ExtractStructuredTablesAction:
    def __init__(self, provider: DocumentStructureProvider) -> None:
        self._provider: DocumentStructureProvider = provider

    def execute(self, request: DocumentInput, document: InspectedDocument,
                options: ExtractionOptions) -> InspectedDocument:
        if document.file_kind != FileKind.PDF or options.max_structured_tables == 0:
            return document
        try:
            predictions = self._provider.predict_tables(request, options)
            if (not isinstance(predictions, tuple) or
                    len(predictions) > options.max_structured_tables):
                raise ValueError("structure_table_count_exceeded")
            pages_by_number: dict[int, PageCandidates] = {p.number: p for p in document.pages}
            for prediction in predictions:
                if not isinstance(prediction, StructuredTablePrediction):
                    raise ValueError("invalid_prediction")
                page = pages_by_number.get(prediction.page_number)
                if page is None:
                    raise ValueError("invalid_page_reference")
                _validate(prediction, page, options)
            new_regions: dict[int, list[CandidateRegion]] = {
                p.number: list(p.regions) for p in document.pages}
            changed: set[int] = set()
            # Stable original region IDs and stable page order, derived IDs are appended.
            for prediction in predictions:
                page = pages_by_number[prediction.page_number]
                # Native table is authoritative if its geometry already matches.
                if any(r.kind == RegionKind.TABLE and r.bbox is not None and
                       _overlap_fraction(prediction.bbox, r.bbox) >= 0.50
                       for r in page.regions):
                    continue
                images: list[CandidateRegion] = [r for r in page.regions if
                    r.kind == RegionKind.IMAGE and r.bbox is not None and
                    _overlap_fraction(prediction.bbox, r.bbox) >= 0.50]
                parent: CandidateRegion | None = min(
                    images, key=lambda x: (x.bbox[2]-x.bbox[0])*(x.bbox[3]-x.bbox[1])) if images else None  # type: ignore[index]
                surface: SourceSurface = (parent.source_surface or SourceSurface.PDF_PAGE_RENDER
                                          if parent is not None else SourceSurface.NATIVE_PDF_TABLE)
                page_regions: list[CandidateRegion] = new_regions[page.number]
                if len(page_regions) >= options.max_regions_per_page:
                    raise ValueError("structure_region_budget_exceeded")
                region_id: str = f"p{page.number}:document_ai_table:{sum(x.region_id.startswith(f'p{page.number}:document_ai_table:') for x in page_regions) + 1}"
                generated: CandidateRegion = CandidateRegion(
                    region_id, RegionKind.TABLE, bbox=prediction.bbox,
                    table=[list(row) for row in prediction.cells],
                    source_surface=surface, parent_region_id=parent.region_id if parent else None,
                    table_merges=prediction.merges, table_cell_locations=prediction.cell_locations,
                    extraction_method_override=ExtractionMethod.DOCUMENT_AI,
                    extraction_provider_name=prediction.provider_name,
                    extraction_model_id=prediction.model_id,
                    original_source_kind=RegionKind.IMAGE if parent else RegionKind.TABLE,
                    image_role=parent.image_role if parent else None,
                    image_bytes=parent.image_bytes if parent else None,
                    image_mime_type=parent.image_mime_type if parent else None,
                )
                # Put structured evidence immediately after its image parent.
                insertion: int = (page_regions.index(parent) + 1 if parent is not None
                                  else next((index for index, x in enumerate(page_regions)
                                             if x.bbox is not None and x.bbox[1] > prediction.bbox[1]),
                                            len(page_regions)))
                page_regions.insert(insertion, generated)
                changed.add(page.number)
            pages: tuple[PageCandidates, ...] = tuple(replace(
                p, regions=tuple(new_regions[p.number]),
                warnings=p.warnings + (("document_ai_structured_tables_review",) if p.number in changed else ())
            ) for p in document.pages)
            return replace(document, pages=pages)
        except (ImportError, ModuleNotFoundError):
            return replace(document, pages=tuple(replace(
                page, warnings=page.warnings +
                ("document_structure_provider_invalid_or_failed_review",
                 "document_structure_provider_dependency_missing"))
                for page in document.pages))
        except Exception:
            # Preserve original document even if a provider crashes or lies.
            return replace(document, pages=tuple(replace(
                page, warnings=page.warnings +
                ("document_structure_provider_invalid_or_failed_review",))
                for page in document.pages))
