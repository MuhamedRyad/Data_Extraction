from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

from ...template_extraction.errors import TemplateExtractionError
from ...template_extraction.contracts import (
    DocumentTemplate,
    TemplateAnchor,
    TemplateFieldDefinition,
    TemplateFieldMode,
    TemplateLayoutFingerprint,
    TemplateStatus,
    TemplateTableColumnDefinition,
    TemplateTableDefinition,
)


def _bbox(value):
    return tuple(value) if value is not None else None


def _serialize(template: DocumentTemplate) -> str:
    return json.dumps(asdict(template), ensure_ascii=False, sort_keys=True, default=str)


def _deserialize(payload: str) -> DocumentTemplate:
    data = json.loads(payload)
    fingerprint = data["fingerprint"]
    return DocumentTemplate(
        template_id=data["template_id"],
        family=data["family"],
        version=int(data["version"]),
        status=TemplateStatus(data["status"]),
        document_kind=data["document_kind"],
        fields=tuple(
            TemplateFieldDefinition(
                name=item["name"], field_kind=item["field_kind"], required=bool(item["required"]),
                page_number=int(item["page_number"]), anchors=tuple(item["anchors"]), pattern=item["pattern"],
                mode=TemplateFieldMode(item["mode"]), normalized_value_bbox=_bbox(item.get("normalized_value_bbox")),
                normalized_label_bbox=_bbox(item.get("normalized_label_bbox")), template=item.get("template"),
                minimum_confidence=float(item.get("minimum_confidence", 0.88)),
            ) for item in data["fields"]
        ),
        tables=tuple(
            TemplateTableDefinition(
                name=item["name"], page_number=int(item["page_number"]),
                normalized_bbox=_bbox(item.get("normalized_bbox")),
                header_anchors=tuple(item["header_anchors"]), columns=tuple(item["columns"]),
                repeating_rows=bool(item.get("repeating_rows", True)), required=bool(item.get("required", False)),
                normalized_header_bbox=_bbox(item.get("normalized_header_bbox")),
                normalized_body_bbox=_bbox(item.get("normalized_body_bbox")),
                column_definitions=tuple(
                    TemplateTableColumnDefinition(
                        name=column["name"],
                        normalized_x0=float(column["normalized_x0"]),
                        normalized_x1=float(column["normalized_x1"]),
                        value_kind=column.get("value_kind", "text"),
                        required=bool(column.get("required", True)),
                    ) for column in item.get("column_definitions", [])
                ),
                stop_anchors=tuple(item.get("stop_anchors", [])),
                minimum_rows=int(item.get("minimum_rows", 1)),
            ) for item in data["tables"]
        ),
        fingerprint=TemplateLayoutFingerprint(
            page_count=int(fingerprint["page_count"]),
            page_aspect_ratios=tuple(fingerprint["page_aspect_ratios"]),
            page_region_counts=tuple(int(value) for value in fingerprint["page_region_counts"]),
            page_table_counts=tuple(int(value) for value in fingerprint["page_table_counts"]),
            anchors=tuple(
                TemplateAnchor(
                    text=item["text"], page_number=int(item["page_number"]),
                    normalized_bbox=_bbox(item.get("normalized_bbox")), required=bool(item.get("required", True)),
                    visual_hash=item.get("visual_hash"),
                ) for item in fingerprint["anchors"]
            ),
            layout_signature=fingerprint["layout_signature"], visual_hash=fingerprint.get("visual_hash"),
            page_visual_hashes=tuple(fingerprint.get("page_visual_hashes", ())),
        ),
        source_sha256=data["source_sha256"], source_id=data["source_id"], generator_name=data["generator_name"],
        content_hash=data["content_hash"], approved_by=data.get("approved_by"), schema_version=data.get("schema_version", "document-template.v0.1"),
    )


class SQLiteTemplateStore:
    """Small persistent template registry with versioned immutable rows."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS document_templates (
                    template_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    family TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (template_id, version)
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS ix_document_templates_family_status ON document_templates(family, status)")

    def save(self, template: DocumentTemplate) -> None:
        payload = _serialize(template)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT payload FROM document_templates WHERE template_id=? AND version=?",
                (template.template_id, template.version),
            ).fetchone()
            if existing is not None:
                if existing[0] == payload:
                    return
                raise TemplateExtractionError(
                    "TEMPLATE_VERSION_CONFLICT", "Template versions are immutable; create a new version"
                )
            connection.execute(
                "INSERT INTO document_templates(template_id, version, family, status, content_hash, payload) VALUES (?, ?, ?, ?, ?, ?)",
                (template.template_id, template.version, template.family, template.status.value, template.content_hash, payload),
            )

    def get(self, template_id: str, version: int) -> DocumentTemplate | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM document_templates WHERE template_id=? AND version=?", (template_id, version)
            ).fetchone()
        return _deserialize(row[0]) if row else None

    def list_approved(self, family: str | None = None) -> tuple[DocumentTemplate, ...]:
        sql = "SELECT payload FROM document_templates WHERE status='approved'"
        params: tuple[object, ...] = ()
        if family is not None:
            sql += " AND family=?"
            params = (family,)
        sql += " ORDER BY template_id, version DESC"
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(_deserialize(row[0]) for row in rows)

    def next_version(self, template_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM document_templates WHERE template_id=?", (template_id,)
            ).fetchone()
        return int(row[0]) + 1
