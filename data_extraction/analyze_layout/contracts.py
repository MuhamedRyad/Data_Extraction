"""Model-agnostic layout order contract; content is never replaced by a model."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..shared.models import BoundingBox, RegionKind


@dataclass(frozen=True, slots=True)
class LayoutRegion:
    region_id: str
    kind: RegionKind
    bbox: BoundingBox | None


@dataclass(frozen=True, slots=True)
class LayoutPageRequest:
    page_number: int
    page_width: float
    page_height: float
    image_png: bytes
    regions: tuple[LayoutRegion, ...]


@dataclass(frozen=True, slots=True)
class LayoutOrderPrediction:
    """Only candidate order; a model cannot create/delete/change extracted regions."""
    page_number: int
    ordered_region_ids: tuple[str, ...]
    confidence: float | None
    provider_name: str
    model_id: str | None = None


class LayoutOrderProvider(Protocol):
    def predict_order(self, request: LayoutPageRequest) -> LayoutOrderPrediction: ...
