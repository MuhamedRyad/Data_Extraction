"""Deterministic dispatch of typed regions; extraction and assembly are separate."""
from __future__ import annotations

from dataclasses import dataclass

from .extract_image import ExtractImageAction, VisionBudget
from .extract_table import ExtractTableAction
from .extract_text import ExtractTextAction
from .assemble_document import assemble_page, assemble_document
from ..analyze_layout.describe_layout import enrich_extracted
from ..parallel_execution import run_bounded_ordered
from ..shared.models import (
    ExtractedRegion,
    ExtractionResult,
    FileManifest,
    InspectedDocument,
    PageCandidates,
    PageResult,
    RegionKind,
)
from ..options import ExtractionOptions


@dataclass(frozen=True, slots=True)
class _PageWorkItem:
    page: PageCandidates
    document_order_offset: int


@dataclass(frozen=True, slots=True)
class _PageWorkResult:
    page: PageResult
    diagnostics: tuple[str, ...]


class DispatchRegionsAction:
    def __init__(self, text_action: ExtractTextAction,
                 table_action: ExtractTableAction, image_action: ExtractImageAction) -> None:
        self._text_action: ExtractTextAction = text_action
        self._table_action: ExtractTableAction = table_action
        self._image_action: ExtractImageAction = image_action

    def execute(self, document: InspectedDocument, manifest: FileManifest,
                options: ExtractionOptions) -> ExtractionResult:
        budget: VisionBudget = VisionBudget()
        document_order: list[str] = [
            region.region_id
            for page in document.pages
            for region in page.regions
        ]
        work_items: list[_PageWorkItem] = []
        offset: int = 0
        for page in document.pages:
            work_items.append(_PageWorkItem(page=page, document_order_offset=offset))
            offset += len(page.regions)

        def process_page(item: _PageWorkItem) -> _PageWorkResult:
            local_diagnostics: list[str] = []
            extracted: list[ExtractedRegion] = []
            for index, region in enumerate(item.page.regions, start=1):
                output: ExtractedRegion
                if region.kind == RegionKind.TEXT:
                    output = self._text_action.execute(region)
                elif region.kind == RegionKind.TABLE:
                    output = self._table_action.execute(region)
                elif region.kind == RegionKind.IMAGE:
                    output = self._image_action.execute(region, options, budget)
                else:
                    raise ValueError("Unsupported region kind")
                provider_name: str | None = (
                    "native_file_adapter" if region.kind != RegionKind.IMAGE else
                    "ocr_provider" if output.method.value == "ocr" else
                    "vision_provider" if output.method.value == "vision_model" else None
                )
                document_index: int = item.document_order_offset + index
                output = enrich_extracted(
                    region,
                    output,
                    item.page,
                    document,
                    index,
                    document_index,
                    provider_name,
                )
                extracted.append(output)
                if output.requires_review:
                    local_diagnostics.append(
                        f"{region.region_id}: {', '.join(output.review_reasons)}"
                    )
            page_result: PageResult = assemble_page(
                item.page, extracted, local_diagnostics, options
            )
            return _PageWorkResult(page=page_result, diagnostics=tuple(local_diagnostics))

        parallel_enabled: bool = (
            options.enable_parallel_execution
            and options.max_parallel_page_groups > 1
            and len(work_items) > 1
        )
        completed: tuple[_PageWorkResult, ...] = run_bounded_ordered(
            work_items,
            process_page,
            max_workers=options.max_parallel_page_groups,
            enabled=parallel_enabled,
        )
        pages: list[PageResult] = [item.page for item in completed]
        diagnostics: list[str] = [
            diagnostic
            for item in completed
            for diagnostic in item.diagnostics
        ]
        return assemble_document(manifest, pages, budget.calls, diagnostics, document_order)
