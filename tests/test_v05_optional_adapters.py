"""Contract tests for model adapter mapping; no model weights were loaded."""
from __future__ import annotations

from dataclasses import dataclass
import io
from typing import Any

from PIL import Image
import pytest

from data_extraction.layout.contracts import LayoutPageRequest, LayoutRegion
from data_extraction.layout.model_adapters import (DetectedBox,match_model_boxes,
     SuryaLayoutOrderProvider,PaddleStructureLayoutOrderProvider)
from data_extraction.models import RegionKind


def page_input() -> LayoutPageRequest:
    image: Image.Image = Image.new('RGB',(400,400),'white')
    buffer: io.BytesIO = io.BytesIO()
    image.save(buffer,'PNG')
    return LayoutPageRequest(1,400,400,buffer.getvalue(),(
        LayoutRegion('a',RegionKind.TEXT,(30,40,100,80)),
        LayoutRegion('b',RegionKind.TEXT,(230,40,320,80)),
    ))


def test_box_mapper_orders_known_region_ids_no_content_mutation() -> None:
    prediction = match_model_boxes(page_input(),(
        DetectedBox((220,30,340,95),0,0.93),
        DetectedBox((20,30,125,95),1,0.91),
    ),'mock-layout','mock-v1')
    assert prediction.ordered_region_ids == ('b','a')
    assert prediction.confidence == .91
    assert prediction.model_id == 'mock-v1'


@pytest.mark.parametrize('boxes',[
    (DetectedBox((20,20,100,90),0,.99),),
    (DetectedBox((float('nan'),20,100,90),0,.9),),
    (DetectedBox((20,20,100,90),-1,.9),),
    (DetectedBox((20,20,100,90),0,float('inf')),),
])
def test_invalid_or_incomplete_layout_cannot_be_mapped(boxes: tuple) -> None:
    with pytest.raises(ValueError):
        match_model_boxes(page_input(),boxes,'mock','model')


@dataclass
class FakeBlock:
    bbox: tuple[int,int,int,int]
    position: int
    confidence: float


@dataclass
class FakeSuryaResult:
    bboxes: list[FakeBlock]
    error: bool = False


class FakeSuryaPredictor:
    def __call__(self,images: list[Image.Image]) -> list[FakeSuryaResult]:
        assert images[0].width == 400
        return [FakeSuryaResult([FakeBlock((220,30,340,95),0,.8),
                                 FakeBlock((20,30,125,95),1,.9)])]


def test_surya_adapter_with_stub_predictor() -> None:
    prediction = SuryaLayoutOrderProvider(FakeSuryaPredictor()).predict_order(page_input())
    assert prediction.ordered_region_ids == ('b','a')
    assert prediction.provider_name == 'surya-layout'


class FakePaddleResult:
    json: dict[str,Any] = {'res':{'parsing_res_list':[
        {'block_bbox':[220,30,340,95],'block_order':0},
        {'block_bbox':[20,30,125,95],'block_order':1}]}}


class FakePaddlePipeline:
    def predict(self,input: Any) -> list[FakePaddleResult]:
        assert input.shape == (400,400,3)
        return [FakePaddleResult()]


def test_paddle_adapter_with_stub_pipeline() -> None:
    prediction = PaddleStructureLayoutOrderProvider(FakePaddlePipeline()).predict_order(page_input())
    assert prediction.ordered_region_ids == ('b','a')
    assert prediction.confidence is None
