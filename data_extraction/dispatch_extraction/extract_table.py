from __future__ import annotations

import re
from collections import Counter

from ..shared.models import CandidateRegion, ExtractedRegion, ExtractionMethod, RegionKind, TableCells


class ExtractTableAction:
    def execute(self, region: CandidateRegion) -> ExtractedRegion:
        if region.kind != RegionKind.TABLE:
            raise ValueError("ExtractTableAction accepts table candidates only")
        table: TableCells = region.table or []
        widths: set[int] = {len(row) for row in table}
        needs_review: bool = (not table) or len(widths) != 1
        reasons: tuple[str, ...] = ("table_has_irregular_row_width",) if len(widths) > 1 else (
            ("empty_table",) if not table else ()
        )
        text_mismatch: bool = False
        if region.table_text_evidence:
            evidence_words = Counter(re.findall(r"\w+", " ".join(item.text for item in region.table_text_evidence).casefold()))
            table_words = Counter(re.findall(r"\w+", " ".join(cell or "" for row in table for cell in row).casefold()))
            if evidence_words - table_words:
                text_mismatch = True
                needs_review = True
                reasons += ("native_table_text_mismatch_review",)
            if region.table_cell_locations:
                cell_words: dict[tuple[int, int], Counter[str]] = {}
                ambiguous = False
                for item in region.table_text_evidence:
                    x0, y0, x1, y1 = item.bbox
                    matches = [cell for cell in region.table_cell_locations
                               if cell.bbox is not None
                               and x0 >= cell.bbox[0] - 2 and y0 >= cell.bbox[1] - 2
                               and x1 <= cell.bbox[2] + 2 and y1 <= cell.bbox[3] + 2]
                    if len(matches) != 1:
                        ambiguous = True
                        continue
                    cell = matches[0]
                    cell_words.setdefault((cell.row, cell.column), Counter()).update(
                        re.findall(r"\w+", item.text.casefold())
                    )
                for (row, column), words in cell_words.items():
                    value = table[row][column] if row < len(table) and column < len(table[row]) else None
                    if words - Counter(re.findall(r"\w+", (value or "").casefold())):
                        text_mismatch = True
                        needs_review = True
                        reasons += ("native_table_cell_text_mismatch_review",)
                        break
                if ambiguous:
                    needs_review = True
                    reasons += ("native_table_cell_assignment_uncertain_review",)
            else:
                needs_review = True
                reasons += ("native_table_cell_geometry_unavailable_review",)
        method: ExtractionMethod = region.extraction_method_override or ExtractionMethod.NATIVE
        if method == ExtractionMethod.DOCUMENT_AI:
            needs_review = True  # External model evidence is never authoritative by default.
            reasons += ("document_ai_table_requires_review",)
        return ExtractedRegion(region.region_id, RegionKind.TABLE, region.bbox,
                               method, None, table, None,
                               needs_review, reasons, "document_ai_structured_table" if method == ExtractionMethod.DOCUMENT_AI else "native_table_direct",
                               (method,), region.original_source_kind or RegionKind.TABLE,
                               selected_provider_name=region.extraction_provider_name,
                               selected_model_id=region.extraction_model_id,
                               content_complete=not text_mismatch,
                               table_text_evidence=region.table_text_evidence)
