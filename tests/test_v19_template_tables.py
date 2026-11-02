from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    ExtractionOptions,
    OCRTextRegion,
    ProviderTextResult,
    SQLiteTemplateStore,
    TemplateApprovalRequest,
    TemplateBuildMode,
    TemplateBuildRequest,
    TemplateExtractionRequest,
    TemplateGenerationContext,
    TemplateGenerationProposal,
    TemplateTableColumnDefinition,
    TemplateTableDefinition,
)
from data_extraction.template_extraction.errors import TemplateExtractionError
from data_extraction.template_extraction.options import TemplateExtractionOptions
from data_extraction.template_extraction.validation import validate_table


def _png() -> bytes:
    image = Image.new("RGB", (1000, 1000), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class TableOCR:
    def __init__(self) -> None:
        self.cell_calls = 0
        self.full_calls = 0
        self.token_calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.full_calls += 1
        return ProviderTextResult("TABLE DOCUMENT", 0.95, provider_name="table-ocr")

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        return ()

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        self.token_calls += 1
        # Qty is deliberately absent to exercise the cell-level fallback.
        return (
            OCRTextRegion("Blue", (20, 30, 70, 50), .95),
            OCRTextRegion("Pen", (75, 30, 110, 50), .95),
            OCRTextRegion("12.50", (520, 30, 580, 50), .95),
            OCRTextRegion("25.00", (690, 30, 750, 50), .95),
            OCRTextRegion("Notebook", (20, 110, 115, 130), .95),
            OCRTextRegion("Pack", (120, 110, 170, 130), .95),
            OCRTextRegion("40.00", (520, 110, 580, 130), .95),
            OCRTextRegion("40.00", (690, 110, 750, 130), .95),
            OCRTextRegion("Desk", (20, 190, 70, 210), .95),
            OCRTextRegion("Lamp", (75, 190, 130, 210), .95),
            OCRTextRegion("48.50", (520, 190, 580, 210), .95),
            OCRTextRegion("48.50", (690, 190, 750, 210), .95),
            OCRTextRegion("Subtotal", (500, 270, 590, 290), .95),
            OCRTextRegion("113.50", (690, 270, 750, 290), .95),
        )

    def recognize_cell(self, image_bytes: bytes, mime_type: str, value_kind: str) -> ProviderTextResult:
        self.cell_calls += 1
        values = ("2", "1", "1")
        index = min(self.cell_calls - 1, len(values) - 1)
        return ProviderTextResult(values[index], 0.95, provider_name="table-cell")


class TableOCR2(TableOCR):
    pass


class TableAI:
    def generate_template(
        self, document: DocumentInput, context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        return TemplateGenerationProposal(
            document_kind="invoice",
            fields=(),
            tables=(
                TemplateTableDefinition(
                    name="line_items",
                    page_number=1,
                    normalized_bbox=(0.10, 0.20, 0.90, 0.65),
                    header_anchors=("Description", "Qty", "Unit Price", "Amount"),
                    columns=("Description", "Qty", "Unit Price", "Amount"),
                    repeating_rows=True,
                    required=True,
                    normalized_header_bbox=(0.10, 0.20, 0.90, 0.25),
                    # Deliberately includes a summary row; stop_anchors must prevent it becoming a line item.
                    normalized_body_bbox=(0.10, 0.25, 0.90, 0.65),
                    column_definitions=(
                        TemplateTableColumnDefinition("Description", 0.00, 0.50, "text"),
                        TemplateTableColumnDefinition("Qty", 0.50, 0.62, "integer"),
                        TemplateTableColumnDefinition("Unit Price", 0.62, 0.82, "decimal"),
                        TemplateTableColumnDefinition("Amount", 0.82, 1.00, "decimal"),
                    ),
                    stop_anchors=("Subtotal", "VAT", "TOTAL"),
                    minimum_rows=3,
                ),
            ),
            anchors=(),
        )


def test_table_column_geometry_validation_rejects_overlap() -> None:
    table = TableAI().generate_template(
        DocumentInput("x.png", _png(), "x"),
        TemplateGenerationContext("x", "x", "h", (), 1, "invoice", ("",), ((1000, 1000),)),
    ).tables[0]
    bad = replace(
        table,
        column_definitions=(
            TemplateTableColumnDefinition("Description", 0.00, 0.60),
            TemplateTableColumnDefinition("Qty", 0.50, 0.62, "integer"),
            TemplateTableColumnDefinition("Unit Price", 0.62, 0.82, "decimal"),
            TemplateTableColumnDefinition("Amount", 0.82, 1.00, "decimal"),
        ),
    )
    with pytest.raises(TemplateExtractionError) as error:
        validate_table(bad)
    assert error.value.code == "INVALID_TEMPLATE_TABLE_COLUMN_OVERLAP"


def test_required_repeating_table_uses_fast_path_and_recovers_four_columns() -> None:
    first = TableOCR()
    second = TableOCR2()
    service = DataExtractionService(
        options=ExtractionOptions(auto_enable_ai=False),
        ocr_provider=first,
        field_ocr_providers=(first, second),
        template_generation_provider=TableAI(),
        template_options=TemplateExtractionOptions(table_consensus_count=2),
    )
    document = DocumentInput("invoice.png", _png(), "invoice-first")
    built = service.build_template_candidate(TemplateBuildRequest(
        document, "invoice-family", (), "invoice-template", TemplateBuildMode.AI_REQUIRED
    ))
    service.approve_template(TemplateApprovalRequest(built.candidate, "reviewer", save=True))

    full_calls_before = first.full_calls
    result = service.extract_with_template(TemplateExtractionRequest(
        DocumentInput("invoice-2.png", _png(), "invoice-second"), family="invoice-family"
    ))

    assert result.generic_extraction_performed is False
    assert result.match.status.value == "accepted"
    assert first.full_calls == full_calls_before
    assert result.tables[0].rows == (
        ("Blue Pen", "2", "12.50", "25.00"),
        ("Notebook Pack", "1", "40.00", "40.00"),
        ("Desk Lamp", "1", "48.50", "48.50"),
    )
    assert result.tables[0].verified is True
    assert result.tables[0].status == "verified_consensus"
    assert result.semantic_complete is True
    assert "template_table_fast_path_used" in result.diagnostics


def test_sqlite_roundtrip_preserves_table_body_and_column_geometry(tmp_path: Path) -> None:
    store = SQLiteTemplateStore(tmp_path / "templates.db")
    provider = TableOCR()
    service = DataExtractionService(
        options=ExtractionOptions(auto_enable_ai=False),
        ocr_provider=provider,
        template_generation_provider=TableAI(),
        template_store=store,
    )
    built = service.build_template_candidate(TemplateBuildRequest(
        DocumentInput("invoice.png", _png(), "invoice-first"),
        "invoice-family", (), "invoice-template", TemplateBuildMode.AI_REQUIRED,
    ))
    approved = service.approve_template(TemplateApprovalRequest(built.candidate, "reviewer", save=True))
    loaded = store.get(approved.template_id, approved.version)
    assert loaded is not None
    table = loaded.tables[0]
    assert table.normalized_body_bbox == (0.10, 0.25, 0.90, 0.65)
    assert tuple(column.name for column in table.column_definitions) == (
        "Description", "Qty", "Unit Price", "Amount"
    )
    assert table.stop_anchors == ("Subtotal", "VAT", "TOTAL")
