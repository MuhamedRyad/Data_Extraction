"""Analyze PDF inspection evidence without changing the source observations."""
from __future__ import annotations

from dataclasses import replace
import math
import re

import fitz

from .options import AnalyzeLayoutOptions
from .pdf_heading_classifier import PdfBlockStyle, PdfSpanStyle, is_bold_pdf_span
from .pdf_native_reading_order import propose_mixed_direction_text
from .pdf_semantics import enrich_pdf_semantics
from .pdf_toc import NativeTextLine, detect_toc_table
from .resolve_reading_order import resolve_pdf_regions
from ..inspect_document.models import CandidateRegion, InspectedDocument, PageCandidates
from ..reading_order.flow_roles import classify_flow_roles
from ..reading_order.page_semantics import semantic_warning
from ..shared.errors import ExtractionError
from ..shared.models import FileKind, PageKind, RegionKind


def analyze_pdf_layout(
    document: InspectedDocument,
    source_content: bytes,
    options: AnalyzeLayoutOptions,
) -> InspectedDocument:
    """Enrich an inspected PDF with deterministic layout and semantic evidence."""
    if document.file_kind != FileKind.PDF:
        return document
    if (
        options.max_regions_per_page < 1
        or not math.isfinite(options.decorative_region_max_area_ratio)
        or options.decorative_region_max_area_ratio < 0
    ):
        raise ExtractionError("INVALID_OPTIONS", "Invalid analyze layout options")
    pdf: fitz.Document | None = None
    if options.analyze_pdf_rtl_geometry:
        try:
            pdf = fitz.open(stream=source_content, filetype="pdf")
        except Exception:
            # Native text is retained even when optional geometry cannot be read.
            pdf = None
    try:
        pages: list[PageCandidates] = []
        for page in document.pages:
            if page.kind != PageKind.PHYSICAL_PAGE or "selective_page_not_processed" in page.warnings:
                pages.append(page)
                continue
            pages.append(_analyze_page(page, len(document.pages), options, pdf))
    finally:
        if pdf is not None:
            pdf.close()

    styles: tuple[PdfBlockStyle, ...] = tuple(
        PdfBlockStyle(
            citation_anchor=style.citation_anchor,
            text=style.text,
            page_number=style.page_number,
            bbox=style.bbox,
            page_height=style.page_height,
            line_count=style.line_count,
            spans=tuple(
                PdfSpanStyle(
                    font_size=span.font_size,
                    character_count=span.character_count,
                    bold=is_bold_pdf_span(flags=span.font_flags, font_name=span.font_name),
                )
                for span in style.spans
            ),
        )
        for page in document.pages
        if page.pdf_analysis_evidence is not None
        for style in page.pdf_analysis_evidence.block_styles
    )
    return replace(document, pages=enrich_pdf_semantics(tuple(pages), styles))


def _analyze_page(
    page: PageCandidates,
    document_page_count: int,
    options: AnalyzeLayoutOptions,
    pdf: fitz.Document | None,
) -> PageCandidates:
    regions: list[CandidateRegion] = list(page.regions)
    warnings: list[str] = list(page.warnings)
    evidence = page.pdf_analysis_evidence
    if evidence is not None and page.width is not None and page.height is not None:
        lines: tuple[NativeTextLine, ...] = tuple(
            NativeTextLine(line.text, line.bbox, line.source_region_id)
            for line in evidence.text_lines
        )
        toc = detect_toc_table(
            lines,
            page.width,
            page.height,
            page.number,
            document_page_count=document_page_count,
        )
        if toc is not None:
            source_ids: set[str] = set(toc.source_region_ids)
            regions = [
                replace(
                    region,
                    semantic_role="toc_source_text_support",
                    parent_region_id=toc.table_region.region_id,
                )
                if region.region_id in source_ids else region
                for region in regions
            ]
            regions.append(toc.table_region)
            warnings.append("pdf_toc_rows_reconstructed_review")
    if len(regions) > options.max_regions_per_page:
        raise ExtractionError("TOO_MANY_REGIONS", "Too many content regions in a page")

    if options.analyze_pdf_rtl_geometry:
        regions = _add_rtl_proposals(regions, page.number, pdf, warnings)
    regions = classify_flow_roles(
        regions,
        float(page.width or 0.0),
        float(page.height or 0.0),
        decorative_area_ratio=options.decorative_region_max_area_ratio,
    )
    regions, resolution = resolve_pdf_regions(
        regions,
        float(page.width or 0.0),
        float(page.height or 0.0),
    )
    warnings.extend(resolution.warnings)
    warnings.append(semantic_warning(resolution.semantic_class))
    if resolution.used_spatial_graph:
        warnings.append("reading_order_spatial_graph_applied")
    return replace(page, regions=tuple(regions), warnings=tuple(dict.fromkeys(warnings)))


def _add_rtl_proposals(
    regions: list[CandidateRegion],
    page_number: int,
    pdf: fitz.Document | None,
    warnings: list[str],
) -> list[CandidateRegion]:
    pdf_page: fitz.Page | None = None
    if pdf is not None:
        try:
            pdf_page = pdf[page_number - 1]
        except (IndexError, ValueError, RuntimeError):
            pdf_page = None
    output: list[CandidateRegion] = []
    for region in regions:
        text = region.text or ""
        mixed_direction = bool(re.search(r"[\u0600-\u06ff]", text) and re.search(r"[A-Za-z]", text))
        if region.kind != RegionKind.TEXT or region.bbox is None or not mixed_direction:
            output.append(region)
            continue
        if pdf_page is None:
            warnings.append("pdf_rtl_geometry_unavailable_review")
            output.append(region)
            continue
        try:
            proposal = propose_mixed_direction_text(pdf_page, region.bbox, text)
        except (ValueError, RuntimeError, AttributeError, TypeError):
            warnings.append("pdf_rtl_geometry_unavailable_review")
            output.append(region)
            continue
        if proposal is not None:
            warnings.append("pdf_rtl_geometry_mismatch_review")
            if proposal.same_token_multiset:
                region = replace(region, reading_order_proposal=proposal.proposed_text)
        output.append(region)
    return output
