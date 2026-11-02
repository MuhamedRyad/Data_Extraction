from __future__ import annotations

import io
from dataclasses import dataclass

import fitz
from PIL import Image

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions, LayoutOrderPrediction
from data_extraction.analyze_layout.resolve_reading_order import resolve_pdf_regions
from data_extraction.reading_order.flow_roles import classify_flow_roles
from data_extraction.reading_order.page_semantics import classify_page_semantics
from data_extraction.reading_order.contracts import PageSemanticClass
from data_extraction.shared.models import CandidateRegion, RegionKind, ReadingOrderQuality


def _png(width: int = 300, height: int = 40) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _overlay_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    for index, y in enumerate((100, 180, 260, 340), start=1):
        page.insert_image(fitz.Rect(60, y - 25, 360, y + 15), stream=_png())
        page.insert_text((80, y), f"Native text block {index}")
    content = document.tobytes()
    document.close()
    return content


def _simple_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_text((60, 80), "Heading")
    page.insert_text((60, 140), "First paragraph")
    page.insert_text((60, 200), "Second paragraph")
    page.insert_text((60, 260), "Third paragraph")
    content = document.tobytes()
    document.close()
    return content


def _mixed_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=700, height=500)
    page.insert_image(fitz.Rect(40, 100, 300, 360), stream=_png(260, 260))
    page.insert_text((390, 120), "Mixed layout heading")
    page.insert_text((390, 180), "Paragraph one")
    page.insert_text((390, 240), "Paragraph two")
    content = document.tobytes()
    document.close()
    return content


def _diagram_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=700, height=500)
    # A large connected vector hierarchy with many native text labels. Circles
    # avoid being mistaken for a native PDF table by find_tables().
    centers: list[tuple[float, float]] = []
    for row in range(3):
        for column in range(4):
            cx = 95 + column * 165
            cy = 80 + row * 145
            page.draw_circle(fitz.Point(cx, cy), 36, color=(0, 0, 0))
            page.insert_text((cx - 24, cy + 4), f"N{row}{column}")
            centers.append((cx, cy))
    for index in range(4):
        page.draw_line(centers[index], centers[4 + index], color=(0, 0, 0))
        page.draw_line(centers[4 + index], centers[8 + index], color=(0, 0, 0))
    content = document.tobytes()
    document.close()
    return content


def test_native_text_overlay_support_does_not_create_false_ambiguity() -> None:
    manifest = DataExtractionService().inspect(
        DocumentInput("overlay-support.pdf", _overlay_pdf(), "overlay-support")
    )
    page = manifest.pages[0]
    assert page.reading_order_quality != ReadingOrderQuality.AMBIGUOUS_OVERLAP
    overlay_roles = [
        region.relations.region_role for region in page.regions
        if region.image_role == "native_text_overlay" and region.relations is not None
    ]
    assert overlay_roles
    assert set(overlay_roles) == {"native_text_overlay_support"}
    assert "page_semantic_simple_prose" in page.warnings


def test_full_page_scan_with_small_text_layer_stays_ambiguous() -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, stream=_png(1200, 1600))
    page.insert_text((200, 300), "Only one native text block")
    content = document.tobytes()
    document.close()
    manifest = DataExtractionService().inspect(DocumentInput("scan.pdf", content, "scan"))
    assert manifest.pages[0].reading_order_quality == ReadingOrderQuality.AMBIGUOUS_OVERLAP


def test_large_vector_hierarchy_is_classified_as_diagram_and_abstains() -> None:
    regions = [
        CandidateRegion(
            "V", RegionKind.IMAGE, (5, 5, 695, 495),
            image_role="vector_graphic_candidate", native_text_masked=True,
        )
    ]
    for row in range(3):
        for column in range(4):
            x = 50 + column * 160
            y = 50 + row * 140
            regions.append(CandidateRegion(
                f"T{row}{column}", RegionKind.TEXT, (x, y, x + 100, y + 35),
                text=f"Node {row}-{column}",
            ))
    classified = classify_flow_roles(regions, 700, 500)
    semantic = classify_page_semantics(classified, 700, 500, column_evidence=False)
    assert semantic == PageSemanticClass.DIAGRAM_HIERARCHY
    _, evidence = resolve_pdf_regions(classified, 700, 500)
    assert evidence.semantic_class == PageSemanticClass.DIAGRAM_HIERARCHY
    assert "reading_order_diagram_hierarchy_review_required" in evidence.warnings


def test_spatial_graph_preserves_ltr_column_order_with_evidence() -> None:
    regions = [
        CandidateRegion("H", RegionKind.TEXT, (40, 20, 400, 45), text="Heading"),
        CandidateRegion("L1", RegionKind.TEXT, (40, 100, 180, 125), text="Left 1"),
        CandidateRegion("R1", RegionKind.TEXT, (320, 100, 470, 125), text="Right 1"),
        CandidateRegion("L2", RegionKind.TEXT, (40, 160, 180, 185), text="Left 2"),
        CandidateRegion("R2", RegionKind.TEXT, (320, 160, 470, 185), text="Right 2"),
        CandidateRegion("F", RegionKind.TEXT, (40, 700, 400, 725), text="Footer"),
    ]
    classified = classify_flow_roles(regions, 600, 800)
    ordered, evidence = resolve_pdf_regions(classified, 600, 800)
    assert [region.region_id for region in ordered] == ["H", "L1", "L2", "R1", "R2", "F"]
    assert evidence.used_spatial_graph
    assert evidence.confidence >= 0.90
    assert evidence.edges
    assert "reading_order_columns_inferred_review" in evidence.warnings


@dataclass
class CountingLayoutProvider:
    calls: int = 0

    def predict_order(self, request):
        self.calls += 1
        return LayoutOrderPrediction(
            page_number=request.page_number,
            ordered_region_ids=tuple(region.region_id for region in request.regions),
            confidence=0.90,
            provider_name="counting-layout",
        )


def test_layout_provider_can_be_reserved_for_complex_fallback_only() -> None:
    provider = CountingLayoutProvider()
    service = DataExtractionService(
        ExtractionOptions(layout_provider_fallback_only=True),
        layout_provider=provider,
    )
    simple = service.extract(DocumentInput("simple.pdf", _simple_pdf(), "simple"))
    assert provider.calls == 0
    assert "layout_provider_not_needed" in simple.pages[0].warnings

    complex_result = service.extract(DocumentInput("mixed.pdf", _mixed_pdf(), "mixed"))
    assert provider.calls == 1
    assert complex_result.pages[0].layout_provider_name == "counting-layout"
