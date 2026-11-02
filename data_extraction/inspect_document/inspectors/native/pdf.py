"""Native PDF page, table, text and image detection."""
from __future__ import annotations

import io
from dataclasses import replace
import fitz
from PIL import Image, ImageDraw
from data_extraction.providers.native.image_layout import is_raster_grid_table
from .common import _ensure_regions
from .pdf_vectors import vector_candidate_bbox
from data_extraction.analyze_layout.pdf_semantics import enrich_pdf_semantics
from data_extraction.analyze_layout.pdf_native_reading_order import propose_mixed_direction_text, PDFReadingOrderProposal
from data_extraction.analyze_layout.pdf_heading_classifier import (PdfBlockStyle, PdfSpanStyle, is_bold_pdf_span)
from data_extraction.analyze_layout.pdf_toc import NativeTextLine, detect_toc_table
from data_extraction.analyze_layout.resolve_reading_order import resolve_pdf_regions
from data_extraction.shared.models import (CandidateRegion, FileKind,
    InspectedDocument, PageCandidates, PageKind, RegionKind, BoundingBox,
    TableCells, SourceSurface, SourceLocation, CellMerge)
from data_extraction.shared.errors import ExtractionError
from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest
from data_extraction.reading_order.flow_roles import classify_flow_roles
from data_extraction.reading_order.page_semantics import semantic_warning

def _inside(candidate: BoundingBox, bbox: BoundingBox) -> bool:
    x0: float; y0: float; x1: float; y1: float
    x0, y0, x1, y1 = candidate
    return x0 >= bbox[0] - 2 and y0 >= bbox[1] - 2 and x1 <= bbox[2] + 2 and y1 <= bbox[3] + 2


def _render_pdf_image(page: fitz.Page, bbox: BoundingBox, options: InspectDocumentOptions,
                      native_boxes_to_mask: tuple[BoundingBox, ...] = ()) -> tuple[bytes, bool]:
    """Render a PDF region within a bounded pixel budget.

    v0.10 adaptively lowers render DPI instead of failing immediately when the
    requested raster would exceed max_image_pixels. The returned boolean is
    explicit evidence that recovery/downscaling was applied.
    """
    clip: fitz.Rect = fitz.Rect(bbox) & page.rect
    if clip.is_empty:
        raise ExtractionError("EMPTY_IMAGE_REGION", "Image region is outside the page")
    requested_scale: float = options.image_dpi / 72.0
    scale: float = requested_scale
    pixels: float = (clip.width * scale) * (clip.height * scale)
    downscaled: bool = False
    if pixels > options.max_image_pixels:
        if not options.adaptive_image_recovery:
            raise ExtractionError("IMAGE_TOO_LARGE", "Rasterized image region exceeds pixel budget")
        max_scale: float = (options.max_image_pixels / max(1.0, clip.width * clip.height)) ** 0.5
        minimum_scale: float = options.min_image_dpi / 72.0
        if max_scale < minimum_scale:
            raise ExtractionError("IMAGE_TOO_LARGE", "Rasterized image region exceeds safe adaptive budget")
        scale = min(requested_scale, max_scale)
        downscaled = scale < requested_scale
    pixmap: fitz.Pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
    if not native_boxes_to_mask:
        return pixmap.tobytes("png"), downscaled
    # Mask *only* areas backed by extracted native text. The raster beneath may
    # still contain different information, so the extracted OCR is review-only.
    raster: Image.Image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    painter: ImageDraw.ImageDraw = ImageDraw.Draw(raster)
    for native in native_boxes_to_mask:
        overlap: fitz.Rect = fitz.Rect(native) & clip
        if overlap.is_empty:
            continue
        xy: tuple[int, int, int, int] = (
            max(0, round((overlap.x0 - clip.x0) * scale)),
            max(0, round((overlap.y0 - clip.y0) * scale)),
            min(pixmap.width, round((overlap.x1 - clip.x0) * scale)),
            min(pixmap.height, round((overlap.y1 - clip.y0) * scale)),
        )
        painter.rectangle(xy, fill="white")
    stream: io.BytesIO = io.BytesIO()
    raster.save(stream, format="PNG")
    return stream.getvalue(), downscaled


def _pdf_pages(request: InspectDocumentRequest, options: InspectDocumentOptions) -> tuple[PageCandidates, ...]:
    pages: list[PageCandidates] = []
    styles: list[PdfBlockStyle] = []
    try:
        with fitz.open(stream=request.content, filetype="pdf") as pdf:
            if pdf.needs_pass:
                raise ExtractionError("ENCRYPTED_PDF", "Password protected PDF is not supported")
            if len(pdf) > options.max_pages:
                raise ExtractionError("TOO_MANY_PAGES", "Page count exceeds configured limit")
            image_count: int = 0
            selected_pages: set[int] = set(options.selected_pdf_pages)
            for index in range(len(pdf)):
                page: fitz.Page = pdf[index]
                page_number: int = index + 1
                if selected_pages and page_number not in selected_pages:
                    pages.append(PageCandidates(
                        page_number, PageKind.PHYSICAL_PAGE, str(page_number), (),
                        ("selective_page_not_processed",),
                        float(page.rect.width), float(page.rect.height),
                    ))
                    continue
                raw: dict = page.get_text("dict", sort=True)
                regions: list[CandidateRegion] = []
                warnings: list[str] = []
                table_boxes: list[BoundingBox] = []
                try:
                    table_result = page.find_tables()
                    for found_table in table_result.tables:
                        table_cells: TableCells = [
                            [None if cell is None else str(cell) for cell in row]
                            for row in found_table.extract()
                        ]
                        table_bbox: BoundingBox = tuple(float(item) for item in found_table.bbox)  # type: ignore[assignment]
                        if table_cells:
                            table_boxes.append(table_bbox)
                            regions.append(CandidateRegion(
                                region_id=f"p{index + 1}:table:{len(table_boxes)}",
                                kind=RegionKind.TABLE, bbox=table_bbox, table=table_cells,
                                source_surface=SourceSurface.NATIVE_PDF_TABLE,
                            ))
                except (ValueError, RuntimeError, AttributeError):
                    warnings.append("table_detection_failed")
                text_index: int = 0
                image_index: int = 0
                native_text_lines: list[NativeTextLine] = []
                native_text_boxes: list[BoundingBox] = [
                    tuple(float(n) for n in block["bbox"])  # type: ignore[misc]
                    for block in raw.get("blocks", [])
                    if block.get("type") == 0
                ]
                for block_index, block in enumerate(raw.get("blocks", []), start=1):
                    bbox: BoundingBox = tuple(float(n) for n in block["bbox"])  # type: ignore[assignment]
                    if block.get("type") == 0:
                        lines: list[str] = []
                        kept_boxes: list[BoundingBox] = []
                        line_records: list[tuple[str, BoundingBox]] = []
                        for line in block.get("lines", []):
                            kept_spans: list[str] = []
                            line_boxes: list[BoundingBox] = []
                            for span in line.get("spans", []):
                                span_bbox: BoundingBox = tuple(float(n) for n in span.get("bbox", bbox))
                                # Avoid duplicating table text even when its enclosing text
                                # block straddles the table boundary.
                                if any(_inside(span_bbox, table_bbox) for table_bbox in table_boxes):
                                    continue
                                kept_spans.append(str(span.get("text", "")))
                                kept_boxes.append(span_bbox)
                                line_boxes.append(span_bbox)
                            if kept_spans and line_boxes:
                                line_value: str = "".join(kept_spans).strip()
                                if line_value:
                                    lines.append(line_value)
                                    line_records.append((line_value, (
                                        min(item[0] for item in line_boxes),
                                        min(item[1] for item in line_boxes),
                                        max(item[2] for item in line_boxes),
                                        max(item[3] for item in line_boxes),
                                    )))
                        value: str = "\n".join(lines).strip()
                        if value and kept_boxes:
                            text_index += 1
                            visible_box: BoundingBox = (
                                min(item[0] for item in kept_boxes), min(item[1] for item in kept_boxes),
                                max(item[2] for item in kept_boxes), max(item[3] for item in kept_boxes),
                            )
                            text_region_id: str = f"p{index + 1}:text:{text_index}"
                            spans: tuple[PdfSpanStyle, ...] = tuple(
                                PdfSpanStyle(font_size=float(span.get("size", 0)),
                                             character_count=len(str(span.get("text", ""))),
                                             bold=is_bold_pdf_span(flags=span.get("flags"),
                                                                   font_name=span.get("font")))
                                for line in block.get("lines", [])
                                for span in line.get("spans", [])
                                if not any(_inside(tuple(float(n) for n in span.get("bbox", bbox)), table_box)
                                           for table_box in table_boxes)
                            )
                            styles.append(PdfBlockStyle(
                                citation_anchor=text_region_id, text=value,
                                page_number=index + 1, bbox=visible_box,
                                page_height=float(page.rect.height),
                                line_count=len(lines), spans=spans,
                            ))
                            order_proposal: str | None = None
                            if options.analyze_pdf_rtl_geometry:
                                try:
                                    suggested: PDFReadingOrderProposal | None = propose_mixed_direction_text(
                                        page, visible_box, value
                                    )
                                except (ValueError, RuntimeError, AttributeError, TypeError):
                                    suggested = None
                                if suggested is not None:
                                    if suggested.same_token_multiset:
                                        order_proposal = suggested.proposed_text
                                    warnings.append("pdf_rtl_geometry_mismatch_review")
                            regions.append(CandidateRegion(
                                region_id=text_region_id,
                                kind=RegionKind.TEXT, bbox=visible_box, text=value,
                                source_surface=SourceSurface.NATIVE_PDF_TEXT,
                                original_index=block_index,
                                reading_order_proposal=order_proposal,
                            ))
                            native_text_lines.extend(
                                NativeTextLine(line_text, line_bbox, text_region_id)
                                for line_text, line_bbox in line_records
                            )
                    elif block.get("type") == 1:
                        image_count += 1
                        if image_count > options.max_image_regions:
                            raise ExtractionError("TOO_MANY_IMAGES", "Image region count exceeds limit")
                        image_index += 1
                        is_scan: bool = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > (
                            page.rect.width * page.rect.height * 0.75
                        )
                        has_native_overlay: bool = any(
                            _inside(native_bbox, bbox) for native_bbox in native_text_boxes
                        )
                        masking: tuple[BoundingBox, ...] = tuple(
                            native_box for native_box in native_text_boxes
                            if not (fitz.Rect(native_box) & fitz.Rect(bbox)).is_empty
                        ) if has_native_overlay else ()
                        image_bytes, image_downscaled = _render_pdf_image(page, bbox, options, masking)
                        if image_downscaled:
                            warnings.append("image_downscaled_for_processing_review")
                        table_hint: bool = (
                            options.detect_raster_table_grids and is_raster_grid_table(image_bytes)
                        )
                        image_role: str = (
                            "native_text_overlay" if has_native_overlay else
                            "raster_grid_table" if table_hint else
                            "possible_scan" if is_scan else "embedded_image"
                        )
                        if has_native_overlay:
                            warnings.append("image_native_overlay_masked_for_ocr")
                        regions.append(CandidateRegion(
                            region_id=f"p{index + 1}:image:{image_index}", kind=RegionKind.IMAGE,
                            bbox=bbox, image_bytes=image_bytes, image_mime_type="image/png",
                            image_role=image_role,
                            content_hint=RegionKind.TABLE if table_hint else None,
                            source_surface=SourceSurface.PDF_EMBEDDED_IMAGE,
                            native_text_masked=has_native_overlay,
                            original_index=block_index,
                        ))
                toc_detection = detect_toc_table(
                    tuple(native_text_lines), float(page.rect.width), float(page.rect.height), index + 1, len(pdf)
                )
                if toc_detection is not None:
                    support_ids: set[str] = set(toc_detection.source_region_ids)
                    regions = [
                        replace(
                            region,
                            semantic_role="toc_source_text_support",
                            parent_region_id=toc_detection.table_region.region_id,
                        )
                        if region.region_id in support_ids else region
                        for region in regions
                    ]
                    regions.append(toc_detection.table_region)
                    warnings.append("pdf_toc_rows_reconstructed_review")

                # Content-bearing vector charts are invisible to get_text("dict").
                # Represent a raster inspection candidate, not a fabricated caption.
                try:
                    # A vector-only page must still be rasterized in full below.
                    vector_bbox: BoundingBox | None = (
                        vector_candidate_bbox(page, table_boxes) if regions else None
                    )
                except (ValueError, RuntimeError, AttributeError, TypeError):
                    vector_bbox = None
                    warnings.append("vector_detection_failed_review")
                if vector_bbox is not None:
                    image_count += 1
                    if image_count > options.max_image_regions:
                        raise ExtractionError("TOO_MANY_IMAGES", "Image region count exceeds limit")
                    native_overlap: tuple[BoundingBox, ...] = tuple(
                        box for box in native_text_boxes
                        if not (fitz.Rect(box) & fitz.Rect(vector_bbox)).is_empty
                    )
                    try:
                        graphics_bytes, vector_downscaled = _render_pdf_image(
                            page, vector_bbox, options, native_overlap
                        )
                        if vector_downscaled:
                            warnings.append("image_downscaled_for_processing_review")
                    except ExtractionError as graphics_error:
                        if graphics_error.code not in ("EMPTY_IMAGE_REGION", "IMAGE_TOO_LARGE"):
                            raise
                        warnings.append("unresolved_vector_graphics")
                    else:
                        regions.append(CandidateRegion(
                            region_id=f"p{index+1}:vector:1", kind=RegionKind.IMAGE,
                            bbox=vector_bbox, image_bytes=graphics_bytes,
                            image_mime_type="image/png", image_role="vector_graphic_candidate",
                            source_surface=SourceSurface.PDF_PAGE_RENDER,
                            native_text_masked=bool(native_overlap),
                        ))
                        warnings.append("vector_graphics_detected_review")
                if not regions:
                    # Vector-only or blank pages cannot be assumed to contain no content.
                    # Blank pages are still flagged for review, instead of inventing OCR text.
                    image_count += 1
                    if image_count > options.max_image_regions:
                        raise ExtractionError("TOO_MANY_IMAGES", "Image region count exceeds limit")
                    rendered, page_downscaled = _render_pdf_image(
                        page, tuple(float(n) for n in page.rect), options
                    )
                    if page_downscaled:
                        warnings.append("image_downscaled_for_processing_review")
                    table_hint: bool = options.detect_raster_table_grids and is_raster_grid_table(rendered)
                    regions.append(CandidateRegion(
                        region_id=f"p{index + 1}:image:rendered", kind=RegionKind.IMAGE,
                        bbox=tuple(float(n) for n in page.rect),
                        image_bytes=rendered,
                        image_mime_type="image/png",
                        image_role="raster_grid_table" if table_hint else "rendered_page",
                        content_hint=RegionKind.TABLE if table_hint else None,
                        source_surface=SourceSurface.PDF_PAGE_RENDER,
                        original_index=None,
                    ))
                    warnings.append("page_has_no_native_regions")
                _ensure_regions(regions, options)
                regions = classify_flow_roles(
                    regions, float(page.rect.width), float(page.rect.height),
                    options.decorative_region_max_area_ratio,
                )
                regions, order_resolution = resolve_pdf_regions(
                    regions, float(page.rect.width), float(page.rect.height)
                )
                warnings.extend(order_resolution.warnings)
                warnings.append(semantic_warning(order_resolution.semantic_class))
                if order_resolution.used_spatial_graph:
                    warnings.append("reading_order_spatial_graph_applied")
                pages.append(PageCandidates(index + 1, PageKind.PHYSICAL_PAGE,
                                            str(index + 1), tuple(regions),
                                            tuple(dict.fromkeys(warnings)),
                                            float(page.rect.width), float(page.rect.height)))
    except ExtractionError:
        raise
    except Exception as error:
        raise ExtractionError("PDF_PARSE_FAILED", "Unable to inspect PDF") from error
    return enrich_pdf_semantics(tuple(pages), tuple(styles))


