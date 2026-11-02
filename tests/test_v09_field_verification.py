from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image
from reportlab.pdfgen import canvas

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    FieldDefinition,
    FieldVerificationRequest,
    FieldVerificationStatus,
    OCRTextRegion,
    ProviderTextResult,
)


def _pdf_with_line(line: str) -> bytes:
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(420, 220))
    pdf.drawString(40, 150, line)
    pdf.save()
    return buffer.getvalue()


@dataclass(frozen=True, slots=True)
class FakeCropProvider:
    value: str
    name: str
    confidence: float = 0.96

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        assert image_bytes
        return ProviderTextResult(
            text=self.value,
            confidence=self.confidence,
            provider_name=self.name,
        )


@dataclass(frozen=True, slots=True)
class FakeLayoutProvider:
    regions: tuple[OCRTextRegion, ...]

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        assert image_bytes
        return self.regions

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        return ProviderTextResult(
            text="\n".join(region.text for region in self.regions),
            confidence=0.9,
            provider_name="fake-layout",
        )


def _invoice_definition() -> FieldDefinition:
    return FieldDefinition(
        name="invoice_number",
        labels=("Invoice Number", "رقم الفاتورة"),
        pattern=r"^INV-\d{4}-[A-Z]\d{3}$",
        template="INV-9999-A999",
        minimum_confidence=0.80,
    )


def test_native_valid_source_is_verified_without_mutating_extraction() -> None:
    document = DocumentInput(
        "invoice.pdf",
        _pdf_with_line("Invoice Number: INV-2026-A123"),
        "source-1",
    )
    service = DataExtractionService()
    extraction = service.extract(document)
    before = tuple(region.text for page in extraction.pages for region in page.regions)

    result = service.verify_fields(FieldVerificationRequest(document, (_invoice_definition(),)))

    assert len(result.fields) == 1
    field = result.fields[0]
    assert field.status == FieldVerificationStatus.VERIFIED_SOURCE
    assert field.original_value == "INV-2026-A123"
    assert field.suggested_value == "INV-2026-A123"
    assert result.requires_review is False
    after = tuple(region.text for page in service.extract(document).pages for region in page.regions)
    assert after == before


def test_two_independent_field_ocr_attempts_can_verify_consensus() -> None:
    document = DocumentInput(
        "invoice.pdf",
        _pdf_with_line("Invoice Number: INV-2026-AI23"),
        "source-2",
    )
    providers = (
        FakeCropProvider("INV-2026-A123", "engine-a", 0.95),
        FakeCropProvider("INV-2026-A123", "engine-b", 0.94),
    )
    service = DataExtractionService(field_ocr_providers=providers)

    result = service.verify_fields(FieldVerificationRequest(document, (_invoice_definition(),)))

    field = result.fields[0]
    assert field.status == FieldVerificationStatus.VERIFIED_CONSENSUS
    assert field.original_value == "INV-2026-AI23"
    assert field.suggested_value == "INV-2026-A123"
    assert {candidate.provider_name for candidate in field.candidates if candidate.schema_valid} >= {
        "engine-a", "engine-b"
    }
    assert result.requires_review is False


def test_single_template_transform_remains_review_only() -> None:
    document = DocumentInput(
        "invoice.pdf",
        _pdf_with_line("Invoice Number: INV-2O26-AI23"),
        "source-3",
    )
    service = DataExtractionService(
        field_ocr_providers=(FakeCropProvider("INV-2O26-AI23", "engine-a", 0.99),)
    )

    result = service.verify_fields(FieldVerificationRequest(document, (_invoice_definition(),)))

    field = result.fields[0]
    assert field.status == FieldVerificationStatus.REVIEW_REQUIRED
    assert field.suggested_value == "INV-2026-A123"
    assert "template_transform_requires_independent_evidence" in field.review_reasons
    assert result.requires_review is True


def test_scanned_image_uses_layout_geometry_then_field_crop_consensus() -> None:
    image = Image.new("RGB", (500, 220), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    document = DocumentInput("scan.png", buffer.getvalue(), "source-4")
    layout = FakeLayoutProvider((
        OCRTextRegion("Invoice Number", (40, 70, 185, 100), 0.91),
        OCRTextRegion("INV-2026-A123", (205, 70, 365, 100), 0.89),
    ))
    service = DataExtractionService(
        field_ocr_providers=(
            FakeCropProvider("INV-2026-A123", "engine-a", 0.95),
            FakeCropProvider("INV-2026-A123", "engine-b", 0.96),
        ),
        field_layout_provider=layout,
    )

    result = service.verify_fields(FieldVerificationRequest(document, (_invoice_definition(),)))

    field = result.fields[0]
    assert field.page_number == 1
    assert field.value_bbox == (205, 70, 365, 100)
    assert field.status == FieldVerificationStatus.VERIFIED_CONSENSUS
    # Layout OCR is evidence, not authoritative source text; independent crop
    # attempts provide the consensus needed for verification.
    assert len(field.candidates) >= 3


def test_malformed_email_is_not_silently_accepted() -> None:
    document = DocumentInput(
        "mail.pdf",
        _pdf_with_line("Email: user@@example.com"),
        "source-5",
    )
    definition = FieldDefinition(
        name="email",
        labels=("Email",),
        pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
        minimum_confidence=0.80,
    )
    service = DataExtractionService()

    result = service.verify_fields(FieldVerificationRequest(document, (definition,)))

    field = result.fields[0]
    assert field.status == FieldVerificationStatus.PROVIDER_UNAVAILABLE
    assert field.original_value == "user@@example.com"
    assert "field_ocr_provider_not_configured" in field.review_reasons
    assert result.requires_review is True


def test_missing_label_is_explicit() -> None:
    document = DocumentInput("plain.pdf", _pdf_with_line("Nothing relevant"), "source-6")
    service = DataExtractionService()

    result = service.verify_fields(FieldVerificationRequest(document, (_invoice_definition(),)))

    assert result.fields[0].status == FieldVerificationStatus.LABEL_NOT_FOUND
    assert result.requires_review is True


def test_arabic_indic_digits_are_normalized_as_evidence_not_source_mutation() -> None:
    document = DocumentInput(
        "file.txt",
        "File Number: ١٢٣٤٥٦".encode("utf-8"),
        "source-7",
    )
    definition = FieldDefinition(
        name="file_number",
        labels=("File Number",),
        pattern=r"^\d{6}$",
    )
    service = DataExtractionService()

    before = service.extract(document).pages[0].regions[0].text
    result = service.verify_fields(FieldVerificationRequest(document, (definition,)))
    after = service.extract(document).pages[0].regions[0].text

    assert result.fields[0].status == FieldVerificationStatus.VERIFIED_SOURCE
    assert result.fields[0].suggested_value == "123456"
    assert before == after


def test_punctuation_spacing_is_compacted_for_schema_verification() -> None:
    document = DocumentInput(
        "mail.txt",
        b"Email: user @ example . com",
        "source-8",
    )
    definition = FieldDefinition(
        name="email",
        labels=("Email",),
        pattern=r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$",
    )
    result = DataExtractionService().verify_fields(
        FieldVerificationRequest(document, (definition,))
    )

    assert result.fields[0].status == FieldVerificationStatus.VERIFIED_SOURCE
    assert result.fields[0].suggested_value == "user@example.com"


def test_rtl_label_prefers_value_on_expected_left_side() -> None:
    image = Image.new("RGB", (600, 220), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    document = DocumentInput("rtl-scan.png", buffer.getvalue(), "source-9")
    layout = FakeLayoutProvider((
        OCRTextRegion("رقم الفاتورة", (330, 70, 470, 100), 0.94),
        OCRTextRegion("INV-2026-A123", (140, 70, 310, 100), 0.92),
        OCRTextRegion("INV-9999-Z999", (480, 70, 590, 100), 0.99),
    ))
    service = DataExtractionService(field_layout_provider=layout)

    result = service.verify_fields(
        FieldVerificationRequest(document, (_invoice_definition(),))
    )

    field = result.fields[0]
    assert field.original_value == "INV-2026-A123"
    assert field.value_bbox == (140, 70, 310, 100)
    assert field.status == FieldVerificationStatus.PROVIDER_UNAVAILABLE


@dataclass(slots=True)
class SizeSensitiveDateProvider:
    calls: int = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        with Image.open(io.BytesIO(image_bytes)) as image:
            text = "22/11/2026" if image.width >= 300 else "206"
        return ProviderTextResult(text=text, confidence=0.95, provider_name="size-sensitive")


def test_direct_template_crop_retries_upscaled_only_after_schema_invalid_ocr() -> None:
    image = Image.new("RGB", (900, 1200), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    document = DocumentInput("date.png", buffer.getvalue(), "schema-retry")
    definition = FieldDefinition(
        name="date",
        labels=("Date",),
        pattern=r"\d{2}/\d{2}/\d{4}",
        page_number=1,
        normalized_value_bbox=(0.10, 0.10, 0.20, 0.14),
        minimum_confidence=0.80,
    )
    provider = SizeSensitiveDateProvider()
    service = DataExtractionService(field_ocr_providers=(provider,))

    result = service.verify_fields(FieldVerificationRequest(document, (definition,)))

    field = result.fields[0]
    assert provider.calls == 2
    assert field.suggested_value == "22/11/2026"
    assert any(candidate.source == "template_field_crop+schema_retry_upscale" for candidate in field.candidates)
    assert "template_field_schema_retry_upscaled:date:p1" in result.diagnostics
    assert field.status == FieldVerificationStatus.REVIEW_REQUIRED
