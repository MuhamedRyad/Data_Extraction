"""Native PDF page, table, text and image detection."""
from __future__ import annotations

import math
from collections import defaultdict, deque
from collections.abc import Iterable
from dataclasses import replace
from typing import cast
import fitz
from ..raster_table_hint import is_raster_grid_table
from .pdf_vectors import vector_candidate_bbox
from data_extraction.shared.models import (
    PageKind,
    RegionKind,
    BoundingBox,
    TableCells,
    TableCellLocation,
    SourceSurface,
    TableTextEvidence,
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


def _as_bbox(value: object) -> BoundingBox | None:
    """Return finite, positive-area coordinates when geometry is usable."""
    try:
        coordinates = tuple(float(item) for item in cast(Iterable[float], value))
    except (TypeError, ValueError, OverflowError):
        return None
    if len(coordinates) != 4 or not all(math.isfinite(item) for item in coordinates):
        return None
    x0, y0, x1, y1 = coordinates
    if x0 >= x1 or y0 >= y1:
        return None
    return x0, y0, x1, y1


def _font_flags(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_pdf_image(page: fitz.Page, bbox: BoundingBox, options: InspectDocumentOptions) -> tuple[bytes, bool]:
    """Render a PDF region within a bounded pixel budget.

    v0.10 adaptively lowers render DPI instead of failing immediately when the
    requested raster would exceed max_image_pixels. The returned boolean is
    explicit evidence that recovery/downscaling was applied.
    """
    clip: fitz.Rect = (fitz.Rect(bbox) * page.rotation_matrix) & page.rect
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
        # Leave room for rounding the transformed rectangle to pixel boundaries.
        scale = min(requested_scale, max(minimum_scale, max_scale * 0.995))
        downscaled = scale < requested_scale
    minimum_scale: float = options.min_image_dpi / 72.0
    for attempt in range(4):
        pixmap: fitz.Pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        actual_pixels: int = pixmap.width * pixmap.height
        if actual_pixels <= options.max_image_pixels:
            break
        if not options.adaptive_image_recovery or scale <= minimum_scale or attempt == 3:
            raise ExtractionError("IMAGE_TOO_LARGE", "Rendered image exceeds pixel budget")
        next_scale: float = scale * (options.max_image_pixels / actual_pixels) ** 0.5 * 0.995
        if attempt == 2:
            next_scale = minimum_scale
        next_scale = max(minimum_scale, next_scale)
        if next_scale >= scale:
            raise ExtractionError("IMAGE_TOO_LARGE", "Rendered image exceeds pixel budget")
        del pixmap
        scale = next_scale
        downscaled = True
    return pixmap.tobytes("png"), downscaled


def _page_rect(page: fitz.Page) -> fitz.Rect:
    """Visible page bounds in the unrotated coordinates used by text and images."""
    return page.rect * page.derotation_matrix


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
        width=float(_page_rect(page).width),
        height=float(_page_rect(page).height),
    )


def _extract_tables(
    page: fitz.Page,
    page_number: int,
    warnings: list[str],
) -> tuple[list[CandidateRegion], list[BoundingBox]]:
    regions: list[CandidateRegion] = []
    table_boxes: list[BoundingBox] = []
    cropbox: fitz.Rect = fitz.Rect(page.cropbox)
    rotation: int = page.rotation
    try:
        # Table geometry must use the same unrotated coordinates as text.
        if rotation:
            page.set_rotation(0)
        table_result = page.find_tables()
        for found_table in table_result.tables:
            table_cells: TableCells = [
                [None if cell is None else str(cell) for cell in row]
                for row in found_table.extract()
            ]
            if not table_cells:
                continue
            table_bbox: BoundingBox | None = _as_bbox(found_table.bbox)
            if table_bbox is None:
                warnings.append("pdf_table_geometry_unavailable_review")
            else:
                table_boxes.append(table_bbox)
            rows = getattr(found_table, "rows", ())
            # A None cell can represent a merged cell. Do not claim one-cell
            # geometry when the table's span information is uncertain.
            cell_locations = tuple(
                TableCellLocation(row_index, column_index, 1, 1, cell_bbox)
                for row_index, row in enumerate(rows)
                for column_index, cell in enumerate(row.cells)
                if (cell_bbox := _as_bbox(cell)) is not None
            ) if all(_as_bbox(cell) is not None for row in rows for cell in row.cells) else ()
            regions.append(CandidateRegion(
                region_id=f"p{page_number}:table:{len(regions) + 1}",
                kind=RegionKind.TABLE,
                bbox=table_bbox,
                table=table_cells,
                table_cell_locations=cell_locations or None,
                source_surface=SourceSurface.NATIVE_PDF_TABLE,
            ))
    except (ValueError, RuntimeError, AttributeError):
        warnings.append("table_detection_failed")
    finally:
        # Some PyMuPDF versions alter the crop box when finding tables on a
        # rotated page. Keep subsequent geometry and renders in source space.
        if page.cropbox != cropbox:
            page.set_cropbox(cropbox)
        if page.rotation != rotation:
            page.set_rotation(rotation)
    return regions, table_boxes


def _extract_text(
    raw: dict,
    page_number: int,
    page_height: float,
    table_boxes: list[BoundingBox],
    warnings: list[str],
    table_evidence: dict[BoundingBox, list[TableTextEvidence]],
) -> tuple[
    list[CandidateRegion],
    list[BoundingBox],
    list[PDFTextLineEvidence],
    list[PDFBlockStyleEvidence],
]:
    regions: list[CandidateRegion] = []
    text_lines_evidence: list[PDFTextLineEvidence] = []
    block_styles_evidence: list[PDFBlockStyleEvidence] = []
    native_text_boxes: list[BoundingBox] = []
    text_index: int = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue
        block_bbox: BoundingBox | None = _as_bbox(block.get("bbox"))
        if block_bbox is None:
            warnings.append("pdf_text_geometry_unavailable_review")
        region, line_evidence, style_evidence, text_index, span_boxes = _extract_text_block(
            block=block,
            block_bbox=block_bbox,
            block_index=block.get("_source_index"),
            page_number=page_number,
            page_height=page_height,
            table_boxes=table_boxes,
            text_index=text_index,
            warnings=warnings,
            table_evidence=table_evidence,
        )
        if block_bbox is not None:
            native_text_boxes.append(block_bbox)
        else:
            native_text_boxes.extend(span_boxes)
        if region is not None:
            regions.append(region)
            text_lines_evidence.extend(line_evidence)
            if style_evidence is not None:
                block_styles_evidence.append(style_evidence)
    return regions, native_text_boxes, text_lines_evidence, block_styles_evidence


def _extract_text_block(
    block: dict,
    block_bbox: BoundingBox | None,
    block_index: int | None,
    page_number: int,
    page_height: float,
    table_boxes: list[BoundingBox],
    text_index: int,
    warnings: list[str],
    table_evidence: dict[BoundingBox, list[TableTextEvidence]],
) -> tuple[
    CandidateRegion | None,
    list[PDFTextLineEvidence],
    PDFBlockStyleEvidence | None,
    int,
    list[BoundingBox],
]:
    lines: list[str] = []
    kept_boxes: list[BoundingBox] = []
    native_span_boxes: list[BoundingBox] = []
    line_records: list[tuple[str, BoundingBox]] = []
    kept_spans_for_style: list[dict] = []
    uncertain_geometry: bool = False
    for line in block.get("lines", []):
        kept_spans: list[str] = []
        line_boxes: list[BoundingBox] = []
        line_geometry_complete: bool = True
        for span in line.get("spans", []):
            text = str(span.get("text", ""))
            span_bbox: BoundingBox | None = _as_bbox(span.get("bbox"))
            if span_bbox is not None:
                native_span_boxes.append(span_bbox)
                # Avoid duplicating table text only when its geometry is reliable.
                matching_tables = [box for box in table_boxes if _inside(span_bbox, box)]
                if matching_tables:
                    for table_box in dict.fromkeys(matching_tables):
                        table_evidence[table_box].append(TableTextEvidence(
                            text=text,
                            bbox=span_bbox,
                            original_index=block_index,
                        ))
                    if len(matching_tables) > 1:
                        warnings.append("pdf_table_text_ambiguous_review")
                    continue
                line_boxes.append(span_bbox)
                kept_boxes.append(span_bbox)
            else:
                if text.strip():
                    warnings.append("pdf_text_geometry_unavailable_review")
                    uncertain_geometry = True
                    line_geometry_complete = False
                if block_bbox is not None:
                    kept_boxes.append(block_bbox)
            kept_spans.append(text)
            kept_spans_for_style.append(span)
        if kept_spans:
            line_value: str = "".join(kept_spans).strip()
            if line_value:
                lines.append(line_value)
                if line_boxes and line_geometry_complete:
                    line_records.append((line_value, (
                        min(box[0] for box in line_boxes),
                        min(box[1] for box in line_boxes),
                        max(box[2] for box in line_boxes),
                        max(box[3] for box in line_boxes),
                    )))

    value: str = "\n".join(lines).strip()
    if not value:
        return None, [], None, text_index, native_span_boxes

    text_index += 1
    visible_box: BoundingBox | None = (
        (
            min(box[0] for box in kept_boxes),
            min(box[1] for box in kept_boxes),
            max(box[2] for box in kept_boxes),
            max(box[3] for box in kept_boxes),
        )
        if kept_boxes else None
    )
    text_region_id: str = f"p{page_number}:text:{text_index}"
    style_evidence: PDFBlockStyleEvidence | None = None
    if visible_box is not None and not uncertain_geometry:
        spans: tuple[PDFSpanStyleEvidence, ...] = tuple(
            PDFSpanStyleEvidence(
                font_size=float(span.get("size", 0)),
                character_count=len(str(span.get("text", ""))),
                font_flags=_font_flags(span.get("flags")),
                font_name=str(span.get("font", "")),
            )
            for span in kept_spans_for_style
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
    return region, text_evidence, style_evidence, text_index, native_span_boxes


def _build_image_region(
    page: fitz.Page,
    block: dict,
    bbox: BoundingBox,
    block_index: int | None,
    page_number: int,
    image_index: int,
    native_text_boxes: list[BoundingBox],
    options: InspectDocumentOptions,
    warnings: list[str],
    source_cache: dict[int, tuple[bytes, str, bytes | None]],
) -> CandidateRegion:
    is_scan: bool = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) > (
        page.rect.width * page.rect.height * 0.75
    )
    has_native_overlay: bool = any(
        not (fitz.Rect(native_bbox) & fitz.Rect(bbox)).is_empty
        for native_bbox in native_text_boxes
    )
    image_bytes, image_downscaled = _render_pdf_image(page, bbox, options)
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
        warnings.append("image_native_overlay_requires_review")
    original_image = _embedded_image(page, block, options, source_cache)
    if original_image is None:
        warnings.append("pdf_original_image_unavailable")
    return CandidateRegion(
        region_id=f"p{page_number}:image:{image_index}",
        kind=RegionKind.IMAGE,
        bbox=bbox,
        image_bytes=image_bytes,
        image_mime_type="image/png",
        image_role=image_role,
        content_hint=RegionKind.TABLE if table_hint else None,
        source_surface=SourceSurface.PDF_EMBEDDED_IMAGE,
        original_index=block_index,
        original_image_bytes=original_image[0] if original_image else None,
        original_image_mime_type=original_image[1] if original_image else None,
        original_image_mask_bytes=original_image[2] if original_image else None,
        original_image_ocr_safe=(
            original_image is not None
            and page.rotation == 0
            and float(block.get("width", 0)) * float(block.get("height", 0)) <= options.max_image_pixels
            and _source_image_is_upright(block, bbox, _page_rect(page))
        ),
    )


def _source_image_is_upright(
    block: dict, bbox: BoundingBox, visible_page: fitz.Rect,
) -> bool:
    try:
        a, b, c, d, _, _ = (float(value) for value in block["transform"])
    except (KeyError, TypeError, ValueError):
        return False
    tolerance: float = 1e-4
    return (
        a > 0 and d > 0 and abs(b) <= tolerance and abs(c) <= tolerance
        and bbox[0] >= visible_page.x0 - tolerance
        and bbox[1] >= visible_page.y0 - tolerance
        and bbox[2] <= visible_page.x1 + tolerance
        and bbox[3] <= visible_page.y1 + tolerance
    )


def _embedded_image(
    page: fitz.Page, block: dict, options: InspectDocumentOptions,
    cache: dict[int, tuple[bytes, str, bytes | None]],
) -> tuple[bytes, str, bytes | None] | None:
    """Preserve the embedded image separately from the composited page render."""
    if block.get("size", 0) > options.max_original_image_bytes:
        return None
    xref = block.get("xref", 0)
    if xref in cache:
        return cache[xref]
    try:
        if xref:
            source = page.parent.extract_image(xref)
            mask_xref = source.get("smask", 0)
            mask = page.parent.extract_image(mask_xref)["image"] if mask_xref else None
        else:
            # Inline images have no xref. Read only blocks inside this crop.
            blocks = page.get_text("dict", clip=fitz.Rect(block["bbox"]),
                                   flags=fitz.TEXTFLAGS_DICT, sort=False)["blocks"]
            matches = [item for item in blocks if item.get("type") == 1
                       and _as_bbox(item.get("bbox")) == _as_bbox(block.get("bbox"))]
            occurrence = block.get("_bbox_occurrence", 0)
            source = matches[occurrence]
            mask = source.get("mask")
        image = source["image"]
        retained_bytes = sum(len(value[0]) + len(value[2] or b"") for value in cache.values())
        if retained_bytes + len(image) + len(mask or b"") > options.max_original_image_bytes:
            return None
        ext = source.get("ext", "png")
        mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        result = (image, mime, mask)
        # Positive keys are reusable PDF xrefs; negative keys track inline bytes.
        cache[xref if xref else -len(cache) - 1] = result
        return result
    except (ValueError, RuntimeError, KeyError, IndexError, TypeError, AttributeError):
        return None


def _extract_images(
    page: fitz.Page,
    raw: dict,
    page_number: int,
    native_text_boxes: list[BoundingBox],
    options: InspectDocumentOptions,
    warnings: list[str],
    image_count: int,
    source_cache: dict[int, tuple[bytes, str, bytes | None]],
) -> tuple[list[CandidateRegion], int]:
    regions: list[CandidateRegion] = []
    image_index: int = 0
    for block in raw.get("blocks", []):
        if block.get("type") != 1:
            continue
        bbox: BoundingBox | None = _as_bbox(block.get("bbox"))
        if bbox is None:
            warnings.append("pdf_image_geometry_unavailable")
            continue
        # get_image_info also reports occurrences outside the visible crop.
        if (fitz.Rect(bbox) & _page_rect(page)).is_empty:
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
            bbox=bbox,
            block_index=block.get("_source_index"),
            page_number=page_number,
            image_index=image_index,
            native_text_boxes=native_text_boxes,
            options=options,
            warnings=warnings,
            source_cache=source_cache,
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
    vector_bbox = _as_bbox(vector_bbox)
    if vector_bbox is None:
        warnings.append("vector_detection_failed_review")
        return None, image_count

    if image_count >= options.max_image_regions:
        raise ExtractionError(
            "TOO_MANY_IMAGES",
            "Image region count exceeds limit",
        )
    try:
        graphics_bytes, vector_downscaled = _render_pdf_image(
            page, vector_bbox, options
        )
        if vector_downscaled:
            warnings.append("image_downscaled_for_processing_review")
    except ExtractionError as graphics_error:
        if graphics_error.code not in ("EMPTY_IMAGE_REGION", "IMAGE_TOO_LARGE"):
            raise
        warnings.append("unresolved_vector_graphics")
        return None, image_count

    image_count += 1
    warnings.append("vector_graphics_detected_review")
    return CandidateRegion(
        region_id=f"p{page_number}:vector:1",
        kind=RegionKind.IMAGE,
        bbox=vector_bbox,
        image_bytes=graphics_bytes,
        image_mime_type="image/png",
        image_role="vector_graphic_candidate",
        source_surface=SourceSurface.PDF_PAGE_RENDER,
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
    bbox: BoundingBox = tuple(float(value) for value in _page_rect(page))  # type: ignore[assignment]
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
        width=float(_page_rect(page).width),
        height=float(_page_rect(page).height),
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
    source_cache: dict[int, tuple[bytes, str, bytes | None]],
) -> tuple[PageCandidates, int]:
    # The "dict" output is a nested structure of blocks, lines and spans with
    # ؟flags that indicate text direction, ~ dont PRESERVE_IMAGES
    raw: dict = page.get_text(
        "dict", sort=False, flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES
    )
    # Image metadata retains inline and repeated occurrences without image bytes.
    image_info = page.get_image_info()
    # Resolving xrefs hashes image pixels. Avoid that work for oversized
    # images; bounded inline-style lookup remains available for the others.
    if all(image.get("size", 0) <= options.max_original_image_bytes for image in image_info):
        try:
            image_info = page.get_image_info(xrefs=True)
        except (ValueError, RuntimeError, AttributeError, TypeError):
            pass
    image_blocks = [dict(image, type=1) for image in image_info]
    image_occurrences: dict[BoundingBox, int] = defaultdict(int)
    for block in image_blocks:
        bbox = _as_bbox(block.get("bbox"))
        if bbox is not None:
            block["_bbox_occurrence"] = image_occurrences[bbox]
            image_occurrences[bbox] += 1
    # Blocks output contains only metadata and text, but retains source block
    # numbers even when the detailed text dictionary omits images.
    source_indices: dict[tuple[int, BoundingBox], deque[int]] = defaultdict(deque)
    for source in page.get_text("blocks", sort=False, flags=fitz.TEXTFLAGS_DICT):
        bbox = _as_bbox(source[:4])
        if bbox is not None and source[6] in (0, 1):
            source_indices[(source[6], bbox)].append(source[5] + 1)
    for block in raw.get("blocks", []) + image_blocks:
        bbox = _as_bbox(block.get("bbox"))
        indices = source_indices.get((block.get("type"), bbox)) if bbox is not None else None
        block["_source_index"] = indices.popleft() if indices else None
    def source_order(block: dict) -> tuple[bool, int]:
        index = block.get("_source_index")
        return index is None, index if index is not None else 0

    raw["blocks"] = sorted(raw.get("blocks", []) + image_blocks, key=source_order)
    warnings: list[str] = []
    regions, table_boxes = _extract_tables(page, page_number, warnings)
    table_evidence: dict[BoundingBox, list[TableTextEvidence]] = {box: [] for box in table_boxes}
    (
        text_regions,
        native_text_boxes,
        text_evidence,
        style_evidence,
    ) = _extract_text(
        raw=raw,
        page_number=page_number,
        page_height=float(_page_rect(page).height),
        table_boxes=table_boxes,
        warnings=warnings,
        table_evidence=table_evidence,
    )
    regions = [replace(region, table_text_evidence=tuple(table_evidence.get(region.bbox, ())))
               for region in regions]
    regions.extend(text_regions)
    image_regions, image_count = _extract_images(
        page=page,
        raw=raw,
        page_number=page_number,
        native_text_boxes=native_text_boxes,
        options=options,
        warnings=warnings,
        image_count=image_count,
        source_cache=source_cache,
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
            source_cache: dict[int, tuple[bytes, str, bytes | None]] = {}
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
                    source_cache=source_cache,
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


