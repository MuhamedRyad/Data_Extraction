from __future__ import annotations

import io
from dataclasses import dataclass

import fitz
from PIL import Image

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.shared.models import (
    ExtractionMethod,
    ExtractionStatus,
    ImageRoute,
    ProviderTextResult,
)


@dataclass
class SizeAwareOCR:
    calls: int = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        with Image.open(io.BytesIO(image_bytes)) as image:
            # The full 4MP frame is intentionally weak; bounded tiles are strong.
            if image.width * image.height >= 3_000_000:
                return ProviderTextResult("weak full page", 0.30, provider_name="fake")
            return ProviderTextResult(f"tile-{self.calls}", 0.95, provider_name="fake")


@dataclass
class ConstantOCR:
    text: str = "Recovered chart text"
    confidence: float = 0.96
    calls: int = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        return ProviderTextResult(self.text, self.confidence, provider_name="fake-ocr")


def _png(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _vector_pdf() -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=600)
    page.insert_text((55, 55), "Revenue Summary")
    page.draw_rect(fitz.Rect(80, 220, 510, 460), color=(0, 0, 0))
    page.draw_rect(fitz.Rect(160, 295, 210, 415), fill=(.1, .3, .8))
    page.draw_rect(fitz.Rect(260, 252, 310, 415), fill=(.4, .8, .2))
    content = document.tobytes()
    document.close()
    return content


def _pdf_with_tiny_image() -> bytes:
    document = fitz.open()
    page = document.new_page(width=600, height=600)
    page.insert_text((50, 50), "Main content is fully native")
    page.insert_image(fitz.Rect(570, 10, 580, 20), stream=_png(20, 20))
    content = document.tobytes()
    document.close()
    return content


def test_large_standalone_image_is_adaptively_downscaled() -> None:
    content = _png(4000, 3000)
    service = DataExtractionService(ExtractionOptions(image_route=ImageRoute.DISABLED))
    manifest = service.inspect(DocumentInput("large.png", content, "large"))
    assert "image_downscaled_for_processing_review" in manifest.pages[0].warnings
    assert manifest.pages[0].width == 4000
    assert manifest.pages[0].height == 3000


def test_large_standalone_image_can_still_fail_when_adaptive_recovery_disabled() -> None:
    content = _png(4000, 3000)
    service = DataExtractionService(ExtractionOptions(
        image_route=ImageRoute.DISABLED,
        adaptive_image_recovery=False,
    ))
    try:
        service.inspect(DocumentInput("large.png", content, "large"))
    except Exception as error:
        assert getattr(error, "code", None) == "IMAGE_TOO_LARGE"
    else:
        raise AssertionError("expected IMAGE_TOO_LARGE when adaptive recovery is disabled")


def test_vector_candidate_is_actively_recovered_by_ocr() -> None:
    service = DataExtractionService(
        ExtractionOptions(image_route=ImageRoute.AUTO), ocr_provider=ConstantOCR()
    )
    result = service.extract(DocumentInput("chart.pdf", _vector_pdf(), "chart"))
    vectors = [
        region for region in result.pages[0].regions
        if region.provenance and region.provenance.source_image_role == "vector_graphic_candidate"
    ]
    assert len(vectors) == 1
    vector = vectors[0]
    assert vector.method == ExtractionMethod.OCR
    assert vector.content_complete
    assert vector.recovery_applied
    assert "vector_raster_recovery" in vector.recovery_notes
    assert result.content_complete
    assert result.extraction_status == ExtractionStatus.COMPLETE_WITH_REVIEW
    assert not result.ready_for_publication  # review is still explicit and strict


def test_unresolved_meaningful_image_produces_partial_success() -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=600)
    page.insert_text((50, 50), "Native text survives")
    page.insert_image(fitz.Rect(100, 100, 500, 500), stream=_png(300, 300))
    content = document.tobytes()
    document.close()
    service = DataExtractionService(ExtractionOptions(image_route=ImageRoute.AUTO))
    result = service.extract(DocumentInput("mixed.pdf", content, "mixed"))
    assert result.extraction_status == ExtractionStatus.PARTIAL_SUCCESS
    assert not result.content_complete
    assert result.unresolved_region_count >= 1
    assert 0 < result.completeness_ratio < 1


def test_tiny_unresolved_embedded_image_does_not_make_content_incomplete() -> None:
    service = DataExtractionService(ExtractionOptions(image_route=ImageRoute.AUTO))
    result = service.extract(DocumentInput("tiny.pdf", _pdf_with_tiny_image(), "tiny"))
    assert result.content_complete
    assert result.extraction_status == ExtractionStatus.COMPLETE_WITH_REVIEW
    assert result.unresolved_region_count == 1
    unresolved = [r for r in result.pages[0].regions if not r.content_complete]
    assert len(unresolved) == 1
    assert not unresolved[0].critical_for_completeness
    assert "tiny_decorative_candidate_noncritical" in unresolved[0].recovery_notes


def test_low_confidence_large_image_uses_bounded_tiled_ocr_recovery() -> None:
    provider = SizeAwareOCR()
    options = ExtractionOptions(
        image_route=ImageRoute.AUTO,
        max_image_pixels=8_000_000,
        ocr_tile_trigger_pixels=3_000_000,
        ocr_tile_max_pixels=1_500_000,
        max_ocr_tile_calls_per_region=8,
    )
    service = DataExtractionService(options, ocr_provider=provider)
    result = service.extract(DocumentInput("scan.png", _png(2000, 2000), "scan"))
    region = result.pages[0].regions[0]
    assert region.method == ExtractionMethod.OCR
    assert region.recovery_applied
    assert "tiled_ocr" in region.recovery_notes
    assert region.route_reason == "ocr_tiled_recovery"
    assert provider.calls > 1
    assert provider.calls <= 1 + options.max_ocr_tile_calls_per_region
    assert result.content_complete


def test_generic_field_discovery_finds_shapes_without_domain_guessing() -> None:
    from data_extraction import FieldDiscoveryKind, FieldDiscoveryRequest

    content = (
        "Contact: billing@example.com\n"
        "Portal: https://example.com/orders/42\n"
        "Reference: AB-12345-Z\n"
        "Date: 2026-10-31\n"
        "Code: 987654\n"
    ).encode("utf-8")
    service = DataExtractionService()
    result = service.discover_fields(FieldDiscoveryRequest(
        DocumentInput("values.txt", content, "values"),
        kinds=(
            FieldDiscoveryKind.EMAIL,
            FieldDiscoveryKind.URL,
            FieldDiscoveryKind.IDENTIFIER,
            FieldDiscoveryKind.DATE,
            FieldDiscoveryKind.NUMERIC_CODE,
        ),
    ))
    pairs = {(field.kind, field.value) for field in result.fields}
    assert (FieldDiscoveryKind.EMAIL, "billing@example.com") in pairs
    assert (FieldDiscoveryKind.URL, "https://example.com/orders/42") in pairs
    assert (FieldDiscoveryKind.IDENTIFIER, "AB-12345-Z") in pairs
    assert (FieldDiscoveryKind.DATE, "2026-10-31") in pairs
    assert (FieldDiscoveryKind.NUMERIC_CODE, "987654") in pairs
    # It discovers a shape, not business semantics like "invoice_number".
    assert all("invoice" not in field.kind.value for field in result.fields)


def test_large_pdf_vector_region_downscales_instead_of_image_too_large() -> None:
    document = fitz.open()
    page = document.new_page(width=1800, height=1800)
    page.insert_text((50, 50), "Large engineering chart")
    page.draw_rect(fitz.Rect(100, 200, 900, 1600), fill=(.2, .2, .8))
    page.draw_rect(fitz.Rect(950, 300, 1700, 1600), fill=(.2, .8, .2))
    content = document.tobytes()
    document.close()
    service = DataExtractionService(ExtractionOptions(
        image_route=ImageRoute.DISABLED,
        image_dpi=150,
        max_image_pixels=8_000_000,
        adaptive_image_recovery=True,
        min_image_dpi=72,
    ))
    manifest = service.inspect(DocumentInput("large-vector.pdf", content, "large-vector"))
    assert "image_downscaled_for_processing_review" in manifest.pages[0].warnings
    assert any(region.image_role == "vector_graphic_candidate" for region in manifest.pages[0].regions)


def test_layout_provider_render_uses_adaptive_budget() -> None:
    from data_extraction import LayoutOrderPrediction

    class LayoutProvider:
        def predict_order(self, request):
            return LayoutOrderPrediction(
                page_number=request.page_number,
                ordered_region_ids=tuple(region.region_id for region in request.regions),
                provider_name="fake-layout",
                confidence=0.99,
            )

    document = fitz.open()
    page = document.new_page(width=1800, height=1800)
    page.insert_text((50, 80), "First region", fontsize=18)
    page.insert_text((950, 800), "Second region", fontsize=18)
    content = document.tobytes()
    document.close()
    service = DataExtractionService(
        ExtractionOptions(
            image_route=ImageRoute.DISABLED,
            layout_render_dpi=150,
            max_image_pixels=8_000_000,
            adaptive_image_recovery=True,
            min_image_dpi=72,
        ),
        layout_provider=LayoutProvider(),
    )
    result = service.extract(DocumentInput("layout-large.pdf", content, "layout-large"))
    assert "layout_render_downscaled_review" in result.pages[0].warnings
    assert result.pages[0].layout_provider_name == "fake-layout"


def test_layout_provider_receives_unrotated_page_geometry_and_render() -> None:
    from data_extraction import LayoutOrderPrediction

    captured = []

    class LayoutProvider:
        def predict_order(self, request):
            captured.append(request)
            return LayoutOrderPrediction(
                page_number=request.page_number,
                ordered_region_ids=tuple(region.region_id for region in request.regions),
                confidence=0.90,
                provider_name="rotation-layout",
            )

    document = fitz.open()
    page = document.new_page(width=400, height=200)
    page.insert_text((40, 60), "first", fontsize=14)
    page.insert_text((240, 150), "second", fontsize=14)
    page.set_rotation(90)
    content = document.tobytes()
    document.close()

    service = DataExtractionService(
        ExtractionOptions(layout_render_dpi=72, image_route=ImageRoute.DISABLED),
        layout_provider=LayoutProvider(),
    )
    result = service.extract(DocumentInput("rotated-layout.pdf", content, "rotated-layout"))

    assert result.pages[0].layout_provider_name == "rotation-layout"
    assert len(captured) == 1
    request = captured[0]
    assert (request.page_width, request.page_height) == (400.0, 200.0)
    with Image.open(io.BytesIO(request.image_png)) as rendered:
        assert rendered.size == (400, 200)
