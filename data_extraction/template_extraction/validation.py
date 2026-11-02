from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict

from ..shared.models import BoundingBox
from .contracts import (
    DocumentTemplate, TemplateFieldDefinition, TemplateTableColumnDefinition, TemplateTableDefinition,
)
from .errors import TemplateExtractionError
from .options import TemplateExtractionOptions


def _valid_bbox(value: BoundingBox | None) -> bool:
    if value is None:
        return True
    x0: float = float(value[0])
    y0: float = float(value[1])
    x1: float = float(value[2])
    y1: float = float(value[3])
    return 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0


def _bbox_iou(first: BoundingBox, second: BoundingBox) -> float:
    left: float = max(first[0], second[0])
    top: float = max(first[1], second[1])
    right: float = min(first[2], second[2])
    bottom: float = min(first[3], second[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection: float = (right - left) * (bottom - top)
    first_area: float = (first[2] - first[0]) * (first[3] - first[1])
    second_area: float = (second[2] - second[0]) * (second[3] - second[1])
    union: float = first_area + second_area - intersection
    return intersection / union if union > 0 else 0.0


def validate_field(field: TemplateFieldDefinition, options: TemplateExtractionOptions) -> None:
    if not field.name.strip() or not field.field_kind.strip():
        raise TemplateExtractionError("INVALID_TEMPLATE_FIELD", "Template field name/kind is required")
    if field.page_number < 1:
        raise TemplateExtractionError("INVALID_TEMPLATE_FIELD_PAGE", "Template field page number must be >= 1")
    if not field.anchors and field.normalized_value_bbox is None:
        raise TemplateExtractionError("INVALID_TEMPLATE_FIELD_LOCATION", "Field requires anchors or normalized value bbox")
    if not _valid_bbox(field.normalized_value_bbox) or not _valid_bbox(field.normalized_label_bbox):
        raise TemplateExtractionError("INVALID_TEMPLATE_BBOX", "Template field normalized bbox must be within 0..1")
    if not field.pattern.strip():
        raise TemplateExtractionError("INVALID_TEMPLATE_PATTERN", "Template field regex is required")
    if len(field.pattern) > options.max_pattern_chars or re.search(r"\\[1-9]", field.pattern):
        raise TemplateExtractionError("INVALID_TEMPLATE_PATTERN", "Template field regex is too complex or uses backreferences")
    if any(not anchor.strip() or len(anchor) > options.max_anchor_chars for anchor in field.anchors):
        raise TemplateExtractionError("INVALID_TEMPLATE_ANCHOR", "Template anchor is empty or too long")
    try:
        re.compile(field.pattern)
    except re.error as exc:
        raise TemplateExtractionError("INVALID_TEMPLATE_PATTERN", "Template field regex is invalid") from exc
    if not 0.0 <= field.minimum_confidence <= 1.0:
        raise TemplateExtractionError("INVALID_TEMPLATE_CONFIDENCE", "Field confidence must be within 0..1")


def _validate_table_column(column: TemplateTableColumnDefinition) -> None:
    if not column.name.strip():
        raise TemplateExtractionError("INVALID_TEMPLATE_TABLE_COLUMN", "Template table column name is required")
    if not 0.0 <= column.normalized_x0 < column.normalized_x1 <= 1.0:
        raise TemplateExtractionError(
            "INVALID_TEMPLATE_TABLE_COLUMN_RANGE",
            "Template table column range must be ordered and normalized within 0..1",
        )
    if not column.value_kind.strip():
        raise TemplateExtractionError("INVALID_TEMPLATE_TABLE_COLUMN_KIND", "Template table column value kind is required")


def validate_table(table: TemplateTableDefinition) -> None:
    if not table.name.strip() or table.page_number < 1:
        raise TemplateExtractionError("INVALID_TEMPLATE_TABLE", "Template table name/page is invalid")
    if not _valid_bbox(table.normalized_bbox) or not _valid_bbox(table.normalized_header_bbox) \
            or not _valid_bbox(table.normalized_body_bbox):
        raise TemplateExtractionError("INVALID_TEMPLATE_BBOX", "Template table normalized bbox must be within 0..1")
    if not table.columns:
        raise TemplateExtractionError("INVALID_TEMPLATE_TABLE_COLUMNS", "Template table requires at least one column")
    if not table.header_anchors and table.normalized_bbox is None and table.normalized_body_bbox is None:
        raise TemplateExtractionError(
            "INVALID_TEMPLATE_TABLE_LOCATION", "Template table requires header anchors or normalized geometry"
        )
    if table.minimum_rows < 1:
        raise TemplateExtractionError("INVALID_TEMPLATE_TABLE_MINIMUM_ROWS", "Template minimum row count must be >= 1")
    if any(not anchor.strip() for anchor in table.stop_anchors):
        raise TemplateExtractionError("INVALID_TEMPLATE_TABLE_STOP_ANCHOR", "Template table stop anchors cannot be empty")
    if not table.column_definitions:
        return
    if len(table.column_definitions) != len(table.columns):
        raise TemplateExtractionError(
            "INVALID_TEMPLATE_TABLE_COLUMN_GEOMETRY",
            "Template table column geometry must describe every declared semantic column",
        )
    names: tuple[str, ...] = tuple(column.name for column in table.column_definitions)
    if names != table.columns:
        raise TemplateExtractionError(
            "INVALID_TEMPLATE_TABLE_COLUMN_ORDER",
            "Template table column geometry names/order must match declared columns",
        )
    previous_end: float = 0.0
    for index, column in enumerate(table.column_definitions):
        _validate_table_column(column)
        if index > 0 and column.normalized_x0 < previous_end - 0.01:
            raise TemplateExtractionError(
                "INVALID_TEMPLATE_TABLE_COLUMN_OVERLAP",
                "Template table column ranges may not materially overlap",
            )
        previous_end = column.normalized_x1
    if table.normalized_body_bbox is None:
        raise TemplateExtractionError(
            "INVALID_TEMPLATE_TABLE_BODY_GEOMETRY",
            "Column-aware table templates require a normalized repeating-row body bbox",
        )


def _validate_page_references(template: DocumentTemplate) -> None:
    page_count: int = template.fingerprint.page_count
    if page_count < 1:
        raise TemplateExtractionError("INVALID_TEMPLATE_PAGE_COUNT", "Template fingerprint page count must be >= 1")
    invalid_fields: tuple[str, ...] = tuple(
        field.name for field in template.fields if field.page_number > page_count
    )
    if invalid_fields:
        raise TemplateExtractionError(
            "TEMPLATE_FIELD_PAGE_OUT_OF_RANGE",
            f"Template fields reference pages outside the document: {', '.join(invalid_fields)}",
        )
    invalid_tables: tuple[str, ...] = tuple(
        table.name for table in template.tables if table.page_number > page_count
    )
    if invalid_tables:
        raise TemplateExtractionError(
            "TEMPLATE_TABLE_PAGE_OUT_OF_RANGE",
            f"Template tables reference pages outside the document: {', '.join(invalid_tables)}",
        )
    if any(anchor.page_number < 1 or anchor.page_number > page_count for anchor in template.fingerprint.anchors):
        raise TemplateExtractionError(
            "TEMPLATE_ANCHOR_PAGE_OUT_OF_RANGE", "Template anchor references a page outside the document"
        )


def _validate_required_value_regions(template: DocumentTemplate) -> None:
    required_with_regions: tuple[TemplateFieldDefinition, ...] = tuple(
        field for field in template.fields if field.required and field.normalized_value_bbox is not None
    )
    for index, first in enumerate(required_with_regions):
        assert first.normalized_value_bbox is not None
        for second in required_with_regions[index + 1:]:
            if first.page_number != second.page_number:
                continue
            assert second.normalized_value_bbox is not None
            first_area: float = (first.normalized_value_bbox[2] - first.normalized_value_bbox[0]) * (
                first.normalized_value_bbox[3] - first.normalized_value_bbox[1]
            )
            second_area: float = (second.normalized_value_bbox[2] - second.normalized_value_bbox[0]) * (
                second.normalized_value_bbox[3] - second.normalized_value_bbox[1]
            )
            # Large fallback regions from deterministic bootstrapping are explicitly
            # imprecise and require human refinement. Collision protection targets
            # tight regions that claim two required values occupy the same place.
            if max(first_area, second_area) > 0.50:
                continue
            overlap: float = _bbox_iou(first.normalized_value_bbox, second.normalized_value_bbox)
            if overlap >= 0.90:
                raise TemplateExtractionError(
                    "TEMPLATE_REQUIRED_FIELD_REGION_COLLISION",
                    f"Required fields '{first.name}' and '{second.name}' share nearly the same value region",
                )


def validate_template(template: DocumentTemplate, options: TemplateExtractionOptions) -> None:
    if not template.template_id.strip() or not template.family.strip() or template.version < 1:
        raise TemplateExtractionError("INVALID_TEMPLATE_IDENTITY", "Template identity is invalid")
    if not template.document_kind.strip():
        raise TemplateExtractionError("INVALID_TEMPLATE_DOCUMENT_KIND", "Template document kind is required")
    if len(template.fields) > options.max_fields_per_template:
        raise TemplateExtractionError("TEMPLATE_FIELD_LIMIT_EXCEEDED", "Template field count exceeds configured limit")
    if len(template.tables) > options.max_tables_per_template:
        raise TemplateExtractionError("TEMPLATE_TABLE_LIMIT_EXCEEDED", "Template table count exceeds configured limit")
    names: list[str] = [field.name for field in template.fields]
    if len(names) != len(set(names)):
        raise TemplateExtractionError("DUPLICATE_TEMPLATE_FIELD", "Template field names must be unique")
    table_names: list[str] = [table.name for table in template.tables]
    if len(table_names) != len(set(table_names)):
        raise TemplateExtractionError("DUPLICATE_TEMPLATE_TABLE", "Template table names must be unique")
    for field in template.fields:
        validate_field(field, options)
    for table in template.tables:
        validate_table(table)
    _validate_page_references(template)
    _validate_required_value_regions(template)


def template_content_hash(template: DocumentTemplate) -> str:
    payload: dict[str, object] = {
        "template_id": template.template_id,
        "family": template.family,
        "version": template.version,
        "document_kind": template.document_kind,
        "fields": asdict(template)["fields"],
        "tables": asdict(template)["tables"],
        "fingerprint": asdict(template)["fingerprint"],
        "source_sha256": template.source_sha256,
    }
    raw: bytes = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()
