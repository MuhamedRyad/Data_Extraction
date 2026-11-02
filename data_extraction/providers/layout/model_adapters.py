"""Opt-in layout ordering adapters for Surya 2 and PaddleOCR PP-StructureV3.

Adapters do not extract/replace content; they map discovered native regions to
ordered detected layout boxes. If matching is incomplete the caller fails closed.
Imports and model weight loading are deliberately lazy and optional.

External library versions/models must be tested separately for quality and
license compliance. Passing stub tests is NOT a live model benchmark.
"""
from __future__ import annotations

from dataclasses import dataclass
import io
import math
from typing import Any

from PIL import Image

from ...analyze_layout.contracts import LayoutOrderPrediction, LayoutPageRequest
from ...shared.models import BoundingBox


@dataclass(frozen=True, slots=True)
class DetectedBox:
    bbox_px: BoundingBox
    order_index: int
    confidence: float | None


def match_model_boxes(request: LayoutPageRequest,
                      boxes: tuple[DetectedBox, ...], provider_name: str,
                      model_id: str | None) -> LayoutOrderPrediction:
    """Map model page boxes into native region ordering (never drops regions)."""
    if not boxes:
        raise ValueError('No layout boxes')
    if request.page_width <= 0 or request.page_height <= 0:
        raise ValueError('Unknown page dimensions')
    with Image.open(io.BytesIO(request.image_png)) as image:
        pixel_width: int = image.width
        pixel_height: int = image.height
    if pixel_width <= 0 or pixel_height <= 0:
        raise ValueError('Invalid render')
    for box in boxes:
        coords: BoundingBox = box.bbox_px
        if (len(coords) != 4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in coords)
                or not (0 <= coords[0] < coords[2] <= pixel_width)
                or not (0 <= coords[1] < coords[3] <= pixel_height)):
            raise ValueError('Invalid model layout box')
        if type(box.order_index) is not int or box.order_index < 0:
            raise ValueError('Invalid layout order index')
        if box.confidence is not None and (
            type(box.confidence) not in (int,float) or
            not math.isfinite(box.confidence) or not 0 <= box.confidence <= 1
        ):
            raise ValueError('Invalid model confidence')
    sortable: list[tuple[int, float, float, str]] = []
    confidences: list[float] = []
    matched_orders: set[int] = set()
    for region in request.regions:
        if region.bbox is None:
            raise ValueError('Unpositioned region: cannot map')
        x0,y0,x1,y1 = region.bbox
        if x1 <= x0 or y1 <= y0:
            raise ValueError('Invalid native region bbox')
        rx0: float = x0 * pixel_width / request.page_width
        rx1: float = x1 * pixel_width / request.page_width
        ry0: float = y0 * pixel_height / request.page_height
        ry1: float = y1 * pixel_height / request.page_height
        area: float = max((rx1-rx0)*(ry1-ry0),0.000001)
        matches: list[tuple[float, DetectedBox]] = []
        for box in boxes:
            bx0,by0,bx1,by1 = box.bbox_px
            overlap: float = (max(0.0,min(rx1,bx1)-max(rx0,bx0)) *
                              max(0.0,min(ry1,by1)-max(ry0,by0)))
            fraction: float = overlap/area
            if fraction >= 0.30:
                matches.append((fraction,box))
        if not matches:
            raise ValueError('Layout region unmatched; cannot force an order')
        # Prefer the box covering the most of the source region; tie-break by order.
        best: DetectedBox = sorted(matches,key=lambda pair:(-pair[0],pair[1].order_index))[0][1]
        matched_orders.add(best.order_index)
        if best.confidence is not None:
            confidences.append(best.confidence)
        sortable.append((best.order_index,ry0,rx0,region.region_id))
    if not sortable:
        raise ValueError('No native regions')
    # Confidence is absent if even one selected box lacks a score.
    score: float | None = min(confidences) if len(confidences)==len(sortable) else None
    ordered: tuple[str,...] = tuple(x[3] for x in sorted(sortable))
    return LayoutOrderPrediction(request.page_number,ordered,score,provider_name,model_id)


class SuryaLayoutOrderProvider:
    """Surya 2 layout prediction; requires optional surya + model backend."""
    def __init__(self, predictor: Any | None = None) -> None:
        self._predictor: Any | None = predictor

    def predict_order(self, request: LayoutPageRequest) -> LayoutOrderPrediction:
        if self._predictor is None:
            from surya.inference import SuryaInferenceManager
            from surya.layout import LayoutPredictor
            self._predictor = LayoutPredictor(SuryaInferenceManager())
        with Image.open(io.BytesIO(request.image_png)) as source:
            image: Image.Image = source.convert('RGB')
        predictions: Any = self._predictor([image])
        if len(predictions) != 1 or getattr(predictions[0],'error',False):
            raise ValueError('Surya layout model failed')
        raw: Any = getattr(predictions[0],'bboxes',None)
        if raw is None:
            raise ValueError('Surya missing layout bboxes')
        boxes: tuple[DetectedBox,...] = tuple(
            DetectedBox(tuple(float(v) for v in block.bbox),
                        int(getattr(block,'position',index)),
                        getattr(block,'confidence',None))
            for index,block in enumerate(raw)
        )
        return match_model_boxes(request, boxes,'surya-layout','surya-layout-optional')


class PaddleStructureLayoutOrderProvider:
    """PP-StructureV3 layout parsing results; optional paddleocr dependency."""
    def __init__(self, pipeline: Any | None = None) -> None:
        self._pipeline: Any | None = pipeline

    def predict_order(self, request: LayoutPageRequest) -> LayoutOrderPrediction:
        if self._pipeline is None:
            from paddleocr import PPStructureV3
            self._pipeline = PPStructureV3()
        with Image.open(io.BytesIO(request.image_png)) as source:
            import numpy as np
            image_array: np.ndarray = np.asarray(source.convert('RGB'))
        predictions: Any = list(self._pipeline.predict(input=image_array))
        if len(predictions) != 1:
            raise ValueError('PaddleOCR expected exactly one page')
        data: Any = predictions[0].json
        if not isinstance(data,dict):
            raise ValueError('PaddleOCR missing JSON payload')
        data = data.get('res',data)
        raw: Any = data.get('parsing_res_list',None) if isinstance(data,dict) else None
        if not isinstance(raw,list):
            raise ValueError('PaddleOCR no ordered layout blocks')
        boxes: tuple[DetectedBox,...] = tuple(
            DetectedBox(tuple(float(v) for v in block['block_bbox']),
                        int(block['block_order']),
                        None)
            for block in raw if block.get('block_order') is not None
        )
        return match_model_boxes(request, boxes,'paddleocr-ppstructurev3','ppstructurev3-optional')
