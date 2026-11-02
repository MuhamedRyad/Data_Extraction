from __future__ import annotations

import io
from dataclasses import dataclass
from statistics import median
from typing import Any

import fitz
from PIL import Image

from ..providers.contracts import OCRTextRegion
from ..shared.models import BoundingBox, DocumentInput
from .contracts import DocumentTemplate, TemplateTableColumnDefinition, TemplateTableDefinition
from .options import TemplateExtractionOptions
from .response import TemplateTableResult


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _clean(value: str) -> str:
    return " ".join(value.replace("\n", " ").split()).strip()


@dataclass(frozen=True, slots=True)
class _RowTokens:
    center_y: float
    tokens: tuple[OCRTextRegion, ...]


class TemplateTableExtractionService:
    """Extract repeating rows from a known template body using OCR token geometry.

    The AI/template supplies *where the row body and columns are*. This service
    never discovers a financial table from arbitrary page text. It only applies
    already-approved geometry, which keeps the fast path bounded and deterministic.
    """

    def __init__(
        self,
        providers: tuple[Any, ...],
        options: TemplateExtractionOptions | None = None,
    ) -> None:
        self._providers: tuple[Any, ...] = tuple(
            provider for provider in providers if callable(getattr(provider, "recognize_tokens", None))
        )
        self._options: TemplateExtractionOptions = options or TemplateExtractionOptions()

    @property
    def available(self) -> bool:
        return bool(self._providers)

    @staticmethod
    def definition_fast_path_ready(definition: TemplateTableDefinition) -> bool:
        return bool(
            definition.repeating_rows
            and definition.normalized_body_bbox is not None
            and definition.column_definitions
            and len(definition.column_definitions) == len(definition.columns)
        )

    def extract(
        self,
        document: DocumentInput,
        template: DocumentTemplate,
    ) -> tuple[TemplateTableResult, ...]:
        return tuple(self._extract_one(document, definition) for definition in template.tables)

    def _extract_one(
        self,
        document: DocumentInput,
        definition: TemplateTableDefinition,
    ) -> TemplateTableResult:
        if not self.definition_fast_path_ready(definition):
            return TemplateTableResult(
                definition.name,
                definition.page_number,
                (),
                "template_geometry_unavailable",
                False,
                None,
                ("template_table_column_geometry_unavailable",),
            )
        if not self._providers:
            return TemplateTableResult(
                definition.name,
                definition.page_number,
                (),
                "provider_unavailable",
                False,
                None,
                ("template_table_token_ocr_provider_not_configured",),
            )

        assert definition.normalized_body_bbox is not None
        try:
            crop: bytes = self._crop_normalized_page_region(
                document, definition.page_number, definition.normalized_body_bbox
            )
        except Exception:
            return TemplateTableResult(
                definition.name,
                definition.page_number,
                (),
                "crop_failed",
                False,
                None,
                ("template_table_body_crop_failed",),
            )

        observations: list[tuple[str, tuple[tuple[str | None, ...], ...], tuple[str, ...]]] = []
        calls: int = 0
        for index, provider in enumerate(self._providers, start=1):
            if calls >= self._options.max_template_table_ocr_calls:
                break
            calls += 1
            try:
                tokens: tuple[OCRTextRegion, ...] = tuple(provider.recognize_tokens(crop, "image/png"))
            except Exception:
                continue
            rows, diagnostics, used_calls = self._rows_from_tokens(
                crop, tokens, definition, provider,
                self._options.max_template_table_ocr_calls - calls,
            )
            calls += used_calls
            if rows:
                provider_name: str = type(provider).__name__ or f"provider_{index}"
                observations.append((provider_name, rows, diagnostics))

        if not observations:
            return TemplateTableResult(
                definition.name,
                definition.page_number,
                (),
                "not_found",
                False,
                f"template:{definition.name}:body",
                ("template_table_rows_not_recovered",),
            )

        groups: dict[tuple[tuple[str, ...], ...], list[tuple[str, tuple[str, ...]]]] = {}
        for provider_name, rows, diagnostics in observations:
            key: tuple[tuple[str, ...], ...] = tuple(
                tuple(_norm(cell or "") for cell in row) for row in rows
            )
            groups.setdefault(key, []).append((provider_name, diagnostics))
        ranked = sorted(
            groups.items(),
            key=lambda item: (len({name for name, _ in item[1]}), self._row_quality(item[0]), len(item[0])),
            reverse=True,
        )
        best_key, best_evidence = ranked[0]
        best_rows: tuple[tuple[str | None, ...], ...] = next(
            rows for _, rows, _ in observations
            if tuple(tuple(_norm(cell or "") for cell in row) for row in rows) == best_key
        )
        independent: int = len({name for name, _ in best_evidence})
        verified: bool = independent >= self._options.table_consensus_count
        reasons: list[str] = []
        for _, diagnostics in best_evidence:
            reasons.extend(diagnostics)
        if not verified:
            reasons.append("template_table_values_require_independent_verification")
        if len(best_rows) < definition.minimum_rows:
            reasons.append("template_table_minimum_rows_not_met")
            return TemplateTableResult(
                definition.name,
                definition.page_number,
                best_rows,
                "template_table_incomplete",
                False,
                f"template:{definition.name}:body",
                tuple(dict.fromkeys(reasons)),
            )
        return TemplateTableResult(
            definition.name,
            definition.page_number,
            best_rows,
            "verified_consensus" if verified else "template_structure_review",
            verified,
            f"template:{definition.name}:body",
            tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _row_quality(rows: tuple[tuple[str, ...], ...]) -> int:
        return sum(1 for row in rows for cell in row if cell.strip())

    def _rows_from_tokens(
        self,
        crop: bytes,
        tokens: tuple[OCRTextRegion, ...],
        definition: TemplateTableDefinition,
        provider: Any,
        remaining_calls: int,
    ) -> tuple[tuple[tuple[str | None, ...], ...], tuple[str, ...], int]:
        valid: list[OCRTextRegion] = [
            token for token in tokens
            if token.text.strip() and token.bbox[2] > token.bbox[0] and token.bbox[3] > token.bbox[1]
        ]
        if not valid:
            return (), ("template_table_no_tokens_in_body",), 0
        heights: list[float] = [token.bbox[3] - token.bbox[1] for token in valid]
        tolerance: float = max(2.0, float(median(heights)) * self._options.table_row_vertical_tolerance_ratio)
        sorted_tokens: list[OCRTextRegion] = sorted(
            valid, key=lambda item: (((item.bbox[1] + item.bbox[3]) / 2.0), item.bbox[0])
        )
        rows: list[list[OCRTextRegion]] = []
        for token in sorted_tokens:
            center_y: float = (token.bbox[1] + token.bbox[3]) / 2.0
            if rows:
                current_center: float = float(median(
                    (item.bbox[1] + item.bbox[3]) / 2.0 for item in rows[-1]
                ))
                if abs(center_y - current_center) <= tolerance:
                    rows[-1].append(token)
                    continue
            rows.append([token])
        grouped: list[_RowTokens] = [
            _RowTokens(
                center_y=float(median((item.bbox[1] + item.bbox[3]) / 2.0 for item in row)),
                tokens=tuple(sorted(row, key=lambda item: item.bbox[0])),
            )
            for row in rows
        ]

        with Image.open(io.BytesIO(crop)) as image:
            width: int = image.width
            height: int = image.height
        row_bands: list[tuple[int, int]] = self._row_bands(grouped, height)
        output: list[tuple[str | None, ...]] = []
        diagnostics: list[str] = ["template_table_column_geometry_applied"]
        calls: int = 0
        header_norms: tuple[str, ...] = tuple(_norm(item) for item in definition.header_anchors if item.strip())
        stop_norms: tuple[str, ...] = tuple(_norm(item) for item in definition.stop_anchors if item.strip())
        for row_index, row in enumerate(grouped):
            row_text: str = _norm(" ".join(token.text for token in row.tokens))
            header_hits: int = sum(1 for anchor in header_norms if anchor and anchor in row_text)
            if header_norms and header_hits >= max(1, min(2, len(header_norms))):
                continue
            if any(anchor in row_text for anchor in stop_norms):
                diagnostics.append("template_table_stop_anchor_reached")
                break
            cells: list[str | None] = []
            for column in definition.column_definitions:
                cell_tokens: list[OCRTextRegion] = [
                    token for token in row.tokens
                    if column.normalized_x0 <= ((token.bbox[0] + token.bbox[2]) / 2.0) / max(1.0, width) <= column.normalized_x1
                ]
                value: str = _clean(" ".join(token.text for token in sorted(cell_tokens, key=lambda item: item.bbox[0])))
                if not value and column.required and self._options.table_cell_fallback_enabled and calls < remaining_calls:
                    fallback: str | None = self._recognize_cell(
                        crop, row_bands[row_index], column, provider
                    )
                    calls += 1
                    if fallback:
                        value = fallback
                        diagnostics.append(f"template_table_cell_fallback:{column.name}")
                cells.append(value or None)
            if not any(cell for cell in cells):
                continue
            output.append(tuple(cells))
        return tuple(output), tuple(dict.fromkeys(diagnostics)), calls

    @staticmethod
    def _row_bands(rows: list[_RowTokens], height: int) -> list[tuple[int, int]]:
        centers: list[float] = [row.center_y for row in rows]
        if not centers:
            return []
        bands: list[tuple[int, int]] = []
        for index, center in enumerate(centers):
            top: float = 0.0 if index == 0 else (centers[index - 1] + center) / 2.0
            bottom: float = float(height) if index == len(centers) - 1 else (center + centers[index + 1]) / 2.0
            bands.append((max(0, int(top)), min(height, int(bottom))))
        return bands

    def _recognize_cell(
        self,
        body_crop: bytes,
        row_band: tuple[int, int],
        column: TemplateTableColumnDefinition,
        provider: Any,
    ) -> str | None:
        try:
            with Image.open(io.BytesIO(body_crop)) as source:
                width: int = source.width
                height: int = source.height
                inset: int = max(0, self._options.table_cell_inset_pixels)
                left: int = max(0, int(column.normalized_x0 * width) + inset)
                right: int = min(width, int(column.normalized_x1 * width) - inset)
                top: int = max(0, row_band[0] + inset)
                bottom: int = min(height, row_band[1] - inset)
                if right <= left or bottom <= top:
                    return None
                cell: Image.Image = source.crop((left, top, right, bottom))
                buffer: io.BytesIO = io.BytesIO()
                cell.save(buffer, format="PNG")
                cell_bytes: bytes = buffer.getvalue()
            recognize_cell = getattr(provider, "recognize_cell", None)
            if callable(recognize_cell):
                result = recognize_cell(cell_bytes, "image/png", column.value_kind)
            else:
                recognize = getattr(provider, "recognize", None)
                if not callable(recognize):
                    return None
                result = recognize(cell_bytes, "image/png")
            value: str = _clean(getattr(result, "text", "") or "")
            return value or None
        except Exception:
            return None

    @staticmethod
    def _crop_normalized_page_region(
        document: DocumentInput,
        page_number: int,
        normalized_bbox: BoundingBox,
    ) -> bytes:
        if page_number < 1:
            raise ValueError("page_number must be >= 1")
        if document.content.startswith(b"%PDF-"):
            with fitz.open(stream=document.content, filetype="pdf") as pdf:
                if page_number > pdf.page_count:
                    raise ValueError("page not found")
                page: fitz.Page = pdf[page_number - 1]
                rect: fitz.Rect = page.rect
                clip: fitz.Rect = fitz.Rect(
                    rect.x0 + normalized_bbox[0] * rect.width,
                    rect.y0 + normalized_bbox[1] * rect.height,
                    rect.x0 + normalized_bbox[2] * rect.width,
                    rect.y0 + normalized_bbox[3] * rect.height,
                )
                pix: fitz.Pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), clip=clip, alpha=False)
                return pix.tobytes("png")
        if page_number != 1:
            raise ValueError("image documents have one page")
        with Image.open(io.BytesIO(document.content)) as image:
            source: Image.Image = image.convert("RGB")
            left: int = max(0, int(normalized_bbox[0] * source.width))
            top: int = max(0, int(normalized_bbox[1] * source.height))
            right: int = min(source.width, int(normalized_bbox[2] * source.width))
            bottom: int = min(source.height, int(normalized_bbox[3] * source.height))
            if right <= left or bottom <= top:
                raise ValueError("empty crop")
            crop: Image.Image = source.crop((left, top, right, bottom))
            buffer: io.BytesIO = io.BytesIO()
            crop.save(buffer, format="PNG")
            return buffer.getvalue()
