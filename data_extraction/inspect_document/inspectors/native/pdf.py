"""Native PDF page, table, text and image detection."""
from __future__ import annotations

import io
import fitz
from PIL import Image, ImageDraw
from ..raster_table_hint import is_raster_grid_table
from .pdf_vectors import vector_candidate_bbox
from data_extraction.shared.models import (
    PageKind,
    RegionKind,
    BoundingBox,
    TableCells,
    SourceSurface,
)
from data_extraction.shared.errors import ExtractionError
from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.models import (
    CandidateRegion,
    PageCandidates,
    PDFBlockStyleEvidence,
    PDFPageAnalysisEvidence,
    PDFSpanStyleEvidence,
    PDFTextLineEvidence,
)
from data_extraction.inspect_document.request import InspectDocumentRequest
from .common import _ensure_regions


def _inside(candidate: BoundingBox, bbox: BoundingBox) -> bool:
    x0: float; y0: float; x1: float; y1: float
    x0, y0, x1, y1 = candidate
    return x0 >= bbox[0] - 2 and y0 >= bbox[1] - 2 and x1 <= bbox[2] + 2 and y1 <= bbox[3] + 2


def _font_flags(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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


def _validate_pdf(pdf: fitz.Document, options: InspectDocumentOptions) -> None:
    if pdf.needs_pass:
        raise ExtractionError(
            "ENCRYPTED_PDF",
            "Password protected PDF is not supported",
        )
    if len(pdf) > options.max_pages:
        raise ExtractionError(
            "TOO_MANY_PAGES",
            "Page count exceeds configured limit",
        )


def _skipped_page(page: fitz.Page, page_number: int) -> PageCandidates:
    return PageCandidates(
        number=page_number,
        kind=PageKind.PHYSICAL_PAGE,
        label=str(page_number),
        regions=(),
        warnings=("selective_page_not_processed",),
        width=float(page.rect.width),
        height=float(page.rect.height),
    )


def _extract_tables(
    page: fitz.Page,
    page_number: int,
    warnings: list[str],
) -> tuple[list[CandidateRegion], list[BoundingBox]]:
    regions: list[CandidateRegion] = []
    table_boxes: list[BoundingBox] = []
    try:
        table_result = page.find_tables()
        for found_table in table_result.tables:
            table_cells: TableCells = [
                [None if cell is None else str(cell) for cell in row]
                for row in found_table.extract()
            ]
            if not table_cells:
                continue
            table_bbox: BoundingBox = tuple(
                float(item) for item in found_table.bbox
            )  # type: ignore[assignment]
            table_boxes.append(table_bbox)
            regions.append(CandidateRegion(
                region_id=f"p{page_number}:table:{len(table_boxes)}",
                kind=RegionKind.TABLE,
                bbox=table_bbox,
                table=table_cells,
                source_surface=SourceSurface.NATIVE_PDF_TABLE,
            ))
    except (ValueError, RuntimeError, AttributeError):
        warnings.append("table_detection_failed")
    return regions, table_boxes


def _extract_text(
    raw: dict,
    page_number: int,
    page_height: float,
    table_boxes: list[BoundingBox],
) -> tuple[
    list[CandidateRegion],
    list[BoundingBox],
    list[PDFTextLineEvidence],
    list[PDFBlockStyleEvidence],
]:
    regions: list[CandidateRegion] = []
    text_lines_evidence: list[PDFTextLineEvidence] = []
    block_styles_evidence: list[PDFBlockStyleEvidence] = []
    native_text_boxes: list[BoundingBox] = [
        tuple(float(value) for value in block["bbox"])  # type: ignore[misc]
        for block in raw.get("blocks", [])
        if block.get("type") == 0
    ]
    text_index: int = 0
    for block_index, block in enumerate(raw.get("blocks", []), start=1):
        if block.get("type") != 0:
            continue
        region, line_evidence, style_evidence, text_index = _extract_text_block(
            block=block,
            block_index=block_index,
            page_number=page_number,
            page_height=page_height,
            table_boxes=table_boxes,
            text_index=text_index,
        )
        if region is not None:
            regions.append(region)
            text_lines_evidence.extend(line_evidence)
            if style_evidence is not None:
                block_styles_evidence.append(style_evidence)
    return regions, native_text_boxes, text_lines_evidence, block_styles_evidence


def _extract_text_block(
    block: dict,
    block_index: int,
    page_number: int,
    page_height: float,
    table_boxes: list[BoundingBox],
    text_index: int,
) -> tuple[
    CandidateRegion | None,
    list[PDFTextLineEvidence],
    PDFBlockStyleEvidence | None,
    int,
]:
    block_bbox: BoundingBox = tuple(
        float(value) for value in block["bbox"]
    )  # type: ignore[assignment]
    lines: list[str] = []
    kept_boxes: list[BoundingBox] = []
    line_records: list[tuple[str, BoundingBox]] = []
    for line in block.get("lines", []):
        kept_spans: list[str] = []
        line_boxes: list[BoundingBox] = []
        for span in line.get("spans", []):
            span_bbox: BoundingBox = tuple(
                float(value) for value in span.get("bbox", block_bbox)
            )  # type: ignore[assignment]
            # Avoid duplicating table text even when its enclosing text block
            # straddles the table boundary.
            if any(_inside(span_bbox, table_box) for table_box in table_boxes):
                continue
            kept_spans.append(str(span.get("text", "")))
            kept_boxes.append(span_bbox)
            line_boxes.append(span_bbox)
        if kept_spans and line_boxes:
            line_value: str = "".join(kept_spans).strip()
            if line_value:
                lines.append(line_value)
                line_records.append((line_value, (
                    min(box[0] for box in line_boxes),
                    min(box[1] for box in line_boxes),
                    max(box[2] for box in line_boxes),
                    max(box[3] for box in line_boxes),
                )))

    value: str = "\n".join(lines).strip()
    if not value or not kept_boxes:
        return None, [], None, text_index

    text_index += 1
    visible_box: BoundingBox = (
        min(box[0] for box in kept_boxes),
        min(box[1] for box in kept_boxes),
        max(box[2] for box in kept_boxes),
        max(box[3] for box in kept_boxes),
    )
    text_region_id: str = f"p{page_number}:text:{text_index}"
    spans: tuple[PDFSpanStyleEvidence, ...] = tuple(
        PDFSpanStyleEvidence(
            font_size=float(span.get("size", 0)),
            character_count=len(str(span.get("text", ""))),
            font_flags=_font_flags(span.get("flags")),
            font_name=str(span.get("font", "")),
        )
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if not any(
            _inside(
                tuple(float(value) for value in span.get("bbox", block_bbox)),
                table_box,
            )
            for table_box in table_boxes
        )
    )
    style_evidence = PDFBlockStyleEvidence(
        citation_anchor=text_region_id,
        text=value,
        page_number=page_number,
        bbox=visible_box,
        page_height=page_height,
        line_count=len(lines),
        spans=spans,
    )
    region = CandidateRegion(
        region_id=text_region_id,
        kind=RegionKind.TEXT,
        bbox=visible_box,
        text=value,
        source_surface=SourceSurface.NATIVE_PDF_TEXT,
        original_index=block_index,
    )
    text_evidence = [
        PDFTextLineEvidence(line_text, line_bbox, text_region_id)
        for line_text, line_bbox in line_records
    ]
    return region, text_evidence, style_evidence, text_index


def _build_image_region(
    page: fitz.Page,
    block: dict,
    block_index: int,
    page_number: int,
    image_index: int,
    native_text_boxes: list[BoundingBox],
    options: InspectDocumentOptions,
    warnings: list[str],
) -> CandidateRegion:
    bbox: BoundingBox = tuple(
        float(value) for value in block["bbox"]
    )  # type: ignore[assignment]
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
    return CandidateRegion(
        region_id=f"p{page_number}:image:{image_index}",
        kind=RegionKind.IMAGE,
        bbox=bbox,
        image_bytes=image_bytes,
        image_mime_type="image/png",
        image_role=image_role,
        content_hint=RegionKind.TABLE if table_hint else None,
        source_surface=SourceSurface.PDF_EMBEDDED_IMAGE,
        native_text_masked=has_native_overlay,
        original_index=block_index,
    )


def _extract_images(
    page: fitz.Page,
    raw: dict,
    page_number: int,
    native_text_boxes: list[BoundingBox],
    options: InspectDocumentOptions,
    warnings: list[str],
    image_count: int,
) -> tuple[list[CandidateRegion], int]:
    regions: list[CandidateRegion] = []
    image_index: int = 0
    for block_index, block in enumerate(raw.get("blocks", []), start=1):
        if block.get("type") != 1:
            continue
        image_count += 1
        if image_count > options.max_image_regions:
            raise ExtractionError(
                "TOO_MANY_IMAGES",
                "Image region count exceeds limit",
            )
        image_index += 1
        regions.append(_build_image_region(
            page=page,
            block=block,
            block_index=block_index,
            page_number=page_number,
            image_index=image_index,
            native_text_boxes=native_text_boxes,
            options=options,
            warnings=warnings,
        ))
    return regions, image_count


def _extract_vector_candidate(
    page: fitz.Page,
    page_number: int,
    regions: list[CandidateRegion],
    table_boxes: list[BoundingBox],
    native_text_boxes: list[BoundingBox],
    options: InspectDocumentOptions,
    warnings: list[str],
    image_count: int,
) -> tuple[CandidateRegion | None, int]:
    try:
        # A vector-only page must still be rasterized in full below.
        vector_bbox: BoundingBox | None = (
            vector_candidate_bbox(page, table_boxes) if regions else None
        )
    except (ValueError, RuntimeError, AttributeError, TypeError):
        warnings.append("vector_detection_failed_review")
        return None, image_count
    if vector_bbox is None:
        return None, image_count

    image_count += 1
    if image_count > options.max_image_regions:
        raise ExtractionError(
            "TOO_MANY_IMAGES",
            "Image region count exceeds limit",
        )
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
        return None, image_count

    warnings.append("vector_graphics_detected_review")
    return CandidateRegion(
        region_id=f"p{page_number}:vector:1",
        kind=RegionKind.IMAGE,
        bbox=vector_bbox,
        image_bytes=graphics_bytes,
        image_mime_type="image/png",
        image_role="vector_graphic_candidate",
        source_surface=SourceSurface.PDF_PAGE_RENDER,
        native_text_masked=bool(native_overlap),
    ), image_count


def _fallback_render_page(
    page: fitz.Page,
    page_number: int,
    options: InspectDocumentOptions,
    warnings: list[str],
    image_count: int,
) -> tuple[CandidateRegion, int]:
    image_count += 1
    if image_count > options.max_image_regions:
        raise ExtractionError(
            "TOO_MANY_IMAGES",
            "Image region count exceeds limit",
        )
    bbox: BoundingBox = tuple(float(value) for value in page.rect)  # type: ignore[assignment]
    rendered, page_downscaled = _render_pdf_image(page, bbox, options)
    if page_downscaled:
        warnings.append("image_downscaled_for_processing_review")
    table_hint: bool = (
        options.detect_raster_table_grids and is_raster_grid_table(rendered)
    )
    warnings.append("page_has_no_native_regions")
    return CandidateRegion(
        region_id=f"p{page_number}:image:rendered",
        kind=RegionKind.IMAGE,
        bbox=bbox,
        image_bytes=rendered,
        image_mime_type="image/png",
        image_role="raster_grid_table" if table_hint else "rendered_page",
        content_hint=RegionKind.TABLE if table_hint else None,
        source_surface=SourceSurface.PDF_PAGE_RENDER,
        original_index=None,
    ), image_count


def _build_page(
    page: fitz.Page,
    page_number: int,
    regions: list[CandidateRegion],
    warnings: list[str],
    text_evidence: list[PDFTextLineEvidence],
    style_evidence: list[PDFBlockStyleEvidence],
) -> PageCandidates:
    return PageCandidates(
        number=page_number,
        kind=PageKind.PHYSICAL_PAGE,
        label=str(page_number),
        regions=tuple(regions),
        warnings=tuple(dict.fromkeys(warnings)),
        width=float(page.rect.width),
        height=float(page.rect.height),
        pdf_analysis_evidence=PDFPageAnalysisEvidence(
            tuple(text_evidence),
            tuple(style_evidence),
        ),
    )


def _inspect_page(
    page: fitz.Page,
    page_number: int,
    options: InspectDocumentOptions,
    image_count: int,
) -> tuple[PageCandidates, int]:
    raw: dict = page.get_text("dict", sort=True)
    warnings: list[str] = []
    regions, table_boxes = _extract_tables(page, page_number, warnings)
    (
        text_regions,
        native_text_boxes,
        text_evidence,
        style_evidence,
    ) = _extract_text(
        raw=raw,
        page_number=page_number,
        page_height=float(page.rect.height),
        table_boxes=table_boxes,
    )
    regions.extend(text_regions)
    image_regions, image_count = _extract_images(
        page=page,
        raw=raw,
        page_number=page_number,
        native_text_boxes=native_text_boxes,
        options=options,
        warnings=warnings,
        image_count=image_count,
    )
    regions.extend(image_regions)
    vector_region, image_count = _extract_vector_candidate(
        page=page,
        page_number=page_number,
        regions=regions,
        table_boxes=table_boxes,
        native_text_boxes=native_text_boxes,
        options=options,
        warnings=warnings,
        image_count=image_count,
    )
    if vector_region is not None:
        regions.append(vector_region)
    if not regions:
        fallback_region, image_count = _fallback_render_page(
            page=page,
            page_number=page_number,
            options=options,
            warnings=warnings,
            image_count=image_count,
        )
        regions.append(fallback_region)
    _ensure_regions(regions, options)
    return _build_page(
        page=page,
        page_number=page_number,
        regions=regions,
        warnings=warnings,
        text_evidence=text_evidence,
        style_evidence=style_evidence,
    ), image_count


def _pdf_pages(
    request: InspectDocumentRequest,
    options: InspectDocumentOptions,
) -> tuple[PageCandidates, ...]:
    pages: list[PageCandidates] = []
    try:
        # open pdf from bytes in memory, using PyMuPDF
        with fitz.open(stream=request.content, filetype="pdf") as pdf:
            _validate_pdf(pdf, options)
            image_count: int = 0
            selected_pages: set[int] = set(options.selected_pdf_pages)
            for index in range(len(pdf)):
                page: fitz.Page = pdf[index]
                page_number: int = index + 1
                if selected_pages and page_number not in selected_pages:
                    pages.append(_skipped_page(page, page_number))
                    continue
                page_result, image_count = _inspect_page(
                    page=page,
                    page_number=page_number,
                    options=options,
                    image_count=image_count,
                )
                pages.append(page_result)
    except ExtractionError:
        raise
    except Exception as error:
        raise ExtractionError(
            "PDF_PARSE_FAILED",
            "Unable to inspect PDF",
        ) from error
    return tuple(pages)


