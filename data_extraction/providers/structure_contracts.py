"""Typed, normalized outputs of document structure engines.

Provider model output is not trusted: validation lives in the extraction action.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..shared.models import BoundingBox, CellMerge, DocumentInput, TableCellLocation
from ..options import ExtractionOptions


@dataclass(frozen=True, slots=True)
class StructuredTablePrediction:
    page_number: int
    bbox: BoundingBox
    cells: tuple[tuple[str | None, ...], ...]
    merges: tuple[CellMerge, ...]
    cell_locations: tuple[TableCellLocation, ...]
    provider_name: str
    model_id: str | None = None


class DocumentStructureProvider(Protocol):
    def predict_tables(self, document: DocumentInput,
                       options: ExtractionOptions) -> tuple[StructuredTablePrediction, ...]: ...
