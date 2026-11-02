from __future__ import annotations

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
        method: ExtractionMethod = region.extraction_method_override or ExtractionMethod.NATIVE
        if method == ExtractionMethod.DOCUMENT_AI:
            needs_review = True  # External model evidence is never authoritative by default.
            reasons += ("document_ai_table_requires_review",)
        return ExtractedRegion(region.region_id, RegionKind.TABLE, region.bbox,
                               method, None, table, None,
                               needs_review, reasons, "document_ai_structured_table" if method == ExtractionMethod.DOCUMENT_AI else "native_table_direct",
                               (method,), region.original_source_kind or RegionKind.TABLE,
                               selected_provider_name=region.extraction_provider_name,
                               selected_model_id=region.extraction_model_id)
