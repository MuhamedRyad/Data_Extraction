"""Word OOXML logical-order extraction including inline pictures."""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace
from typing import Callable

from docx import Document
from docx.document import Document as WordDocument
from docx.table import Table
from docx.text.paragraph import Paragraph
from PIL import Image

from ..raster_table_hint import is_raster_grid_table
from .common import _ensure_regions
from data_extraction.shared.errors import ExtractionError
from data_extraction.shared.models import (
    CandidateRegion,
    PageCandidates,
    PageKind,
    RegionKind,
    SourceLocation,
    SourceSurface,
    TableCells,
)
from data_extraction.inspect_document.options import InspectDocumentOptions
from data_extraction.inspect_document.request import InspectDocumentRequest


@dataclass(slots=True)
class _WordInspectionState:
    options: InspectDocumentOptions
    regions: list[CandidateRegion] = field(default_factory=list)
    warnings: list[str] = field(default_factory=lambda: ["docx_physical_pagination_unknown"])
    paragraph_counters: dict[tuple[str, int | None], int] = field(default_factory=dict)
    image_count: int = 0
    table_index: int = 0


def _emit_text(
    state: _WordInspectionState,
    text: str,
    source: SourceSurface,
    location: SourceLocation,
    semantic_role: str | None = None,
) -> None:
    if not text.strip():
        return
    state.regions.append(CandidateRegion(
        f"d1:text:{len(state.regions) + 1}",
        RegionKind.TEXT,
        text=text,
        source_surface=source,
        source_location=location,
        semantic_role=semantic_role,
    ))
    _ensure_regions(state.regions, state.options)


def _emit_image(
    state: _WordInspectionState,
    relation: str | None,
    part: object,
    location: SourceLocation,
    parent: str | None = None,
) -> None:
    if not relation:
        state.warnings.append("docx_external_image_reference_unresolved")
        return
    related_parts = getattr(part, "related_parts", {})
    if relation not in related_parts:
        state.warnings.append("docx_image_relationship_unresolved")
        return
    image_part = related_parts[relation]
    if not str(image_part.content_type).startswith("image/"):
        state.warnings.append("docx_non_raster_drawing_unsupported")
        return

    state.image_count += 1
    if state.image_count > state.options.max_image_regions:
        raise ExtractionError("TOO_MANY_IMAGES", "Image region count exceeds limit")
    with Image.open(io.BytesIO(image_part.blob)) as embedded:
        if embedded.width * embedded.height > state.options.max_image_pixels:
            raise ExtractionError("IMAGE_TOO_LARGE", "Embedded Word image exceeds pixel limit")
        normalized: Image.Image = embedded.convert("RGB")
        buffer: io.BytesIO = io.BytesIO()
        normalized.save(buffer, format="PNG")

    normalized_image: bytes = buffer.getvalue()
    hint: bool = (
        state.options.detect_raster_table_grids
        and is_raster_grid_table(normalized_image)
    )
    state.regions.append(CandidateRegion(
        f"d1:image:{len(state.regions) + 1}",
        RegionKind.IMAGE,
        image_bytes=normalized_image,
        image_mime_type="image/png",
        image_role="raster_grid_table" if hint else "embedded_image",
        content_hint=RegionKind.TABLE if hint else None,
        source_surface=SourceSurface.DOCX_EMBEDDED_IMAGE,
        source_location=location,
        parent_region_id=parent,
    ))
    _ensure_regions(state.regions, state.options)


def _heading_role(node: Paragraph, qn: Callable[[str], str]) -> str | None:
    """Inherit heading style through based-on chain without inferring content."""
    style = node.style
    for _ in range(12):
        if style is None:
            break
        identifier: str = str(style.style_id or "")
        name: str = str(style.name or "")
        match = re.match(r"^heading[ _-]*([1-9])$", identifier, re.I) or re.match(
            r"^heading[ _-]*([1-9])$", name, re.I
        )
        if match:
            return f"heading_{match.group(1)}"
        try:
            style = style.base_style
        except (AttributeError, KeyError):
            break

    # OOXML outline level is a better signal for custom heading styles.
    for element in node._element.xpath("./w:pPr/w:outlineLvl"):
        try:
            level = int(element.get(qn("w:val"), "-1"))
        except (ValueError, TypeError):
            continue
        if 0 <= level <= 8:
            return f"heading_{level + 1}"
    return None


def _parse_paragraph(
    state: _WordInspectionState,
    node: Paragraph,
    part: object,
    role: str,
    qn: Callable[[str], str],
    section_number: int | None = None,
    parent: str | None = None,
    row: int | None = None,
    column: int | None = None,
) -> None:
    key: tuple[str, int | None] = (role, section_number)
    state.paragraph_counters[key] = state.paragraph_counters.get(key, 0) + 1
    paragraph_index: int = state.paragraph_counters[key]
    base: SourceLocation = SourceLocation(
        part=role,
        paragraph_index=paragraph_index,
        table_index=state.table_index if parent else None,
        row_index=row,
        column_index=column,
        section_index=section_number,
    )
    if role == "header":
        source = SourceSurface.DOCX_HEADER
    elif role == "footer":
        source = SourceSurface.DOCX_FOOTER
    else:
        source = SourceSurface.DOCX_PARAGRAPH
    paragraph_semantic_role: str | None = _heading_role(node, qn) if role == "body" else None
    if node._element.xpath(".//wp:anchor"):
        state.warnings.append("docx_floating_image_layout_requires_review")

    buffered: list[str] = []
    start_run: int = 0
    run_number: int = 0

    def flush() -> None:
        nonlocal buffered, start_run
        if buffered:
            _emit_text(
                state,
                "".join(buffered),
                source,
                replace(base, run_index=start_run),
                paragraph_semantic_role,
            )
            buffered = []

    for element in node._element.iter():
        tag: str = element.tag
        if tag == qn("w:r"):
            run_number += 1
        elif tag in (qn("w:t"), qn("w:delText")):
            # Deleted content is not part of visible document flow.
            if tag != qn("w:delText"):
                if not buffered:
                    start_run = run_number
                buffered.append(element.text or "")
        elif tag == qn("w:tab"):
            if not buffered:
                start_run = run_number
            buffered.append("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            if not buffered:
                start_run = run_number
            buffered.append("\n")
        elif tag == qn("a:blip"):
            flush()
            _emit_image(
                state,
                element.get(qn("r:embed")),
                part,
                replace(base, run_index=run_number),
                parent,
            )
    flush()


def _parse_table(
    state: _WordInspectionState,
    node: Table,
    part: object,
    role: str,
    qn: Callable[[str], str],
    section_number: int | None,
) -> None:
    state.table_index += 1
    current_table: int = state.table_index
    cells: TableCells = [[cell.text for cell in row.cells] for row in node.rows]
    table_id: str = f"d1:table:{len(state.regions) + 1}"
    state.regions.append(CandidateRegion(
        table_id,
        RegionKind.TABLE,
        table=cells,
        source_surface=SourceSurface.DOCX_TABLE,
        source_location=SourceLocation(
            part=role,
            table_index=current_table,
            section_index=section_number,
        ),
    ))
    _ensure_regions(state.regions, state.options)

    visited_cell_elements: set[object] = set()
    for row_id, row in enumerate(node.rows):
        for col_id, cell in enumerate(row.cells):
            # python-docx repeats underlying <w:tc> for merged cells.
            cell_identity: object = cell._tc
            if cell_identity in visited_cell_elements:
                continue
            visited_cell_elements.add(cell_identity)
            # Table data is already represented in the parent matrix.
            # Preserve pictures in their actual cell and nested tables.
            for para_index, para in enumerate(cell.paragraphs, start=1):
                if para._element.xpath(".//wp:anchor"):
                    state.warnings.append("docx_floating_image_layout_requires_review")
                for blip in para._element.xpath(".//a:blip"):
                    _emit_image(
                        state,
                        blip.get(qn("r:embed")),
                        part,
                        SourceLocation(
                            part=role,
                            paragraph_index=para_index,
                            table_index=current_table,
                            row_index=row_id,
                            column_index=col_id,
                            section_index=section_number,
                        ),
                        table_id,
                    )
            if cell.tables:
                state.warnings.append("docx_nested_table_structure_requires_review")


def _parse_nodes(
    state: _WordInspectionState,
    nodes: object,
    part: object,
    role: str,
    qn: Callable[[str], str],
    section_number: int | None = None,
) -> None:
    for node in nodes:
        if isinstance(node, Paragraph):
            _parse_paragraph(state, node, part, role, qn, section_number)
        elif isinstance(node, Table):
            _parse_table(state, node, part, role, qn, section_number)
        else:
            state.warnings.append("docx_unrecognized_body_element")


def _warn_unsupported_content(document: WordDocument, warnings: list[str]) -> None:
    # Word draws charts, embedded OLE objects, or legacy VML outside normal
    # raster picture handling. Flag them rather than silently approving an
    # incomplete document.
    xml: object = document.element
    if xml.xpath(".//w:object") or xml.xpath(".//w:pict"):
        warnings.append("docx_legacy_or_embedded_objects_require_review")
    # Unsupported wrapper paragraphs may be invisible to iter_inner_content.
    if xml.xpath(".//w:customXml"):
        warnings.append("docx_customxml_unrepresented")
    if xml.xpath(".//w:sdt"):
        warnings.append("docx_content_controls_require_review")
    if xml.xpath(".//*[local-name()='chart']"):
        warnings.append("docx_chart_not_extracted")
    if xml.xpath(".//w:txbxContent"):
        warnings.append("docx_textbox_layout_requires_review")


def _parse_headers(
    document: WordDocument,
    state: _WordInspectionState,
    qn: Callable[[str], str],
    processed_parts: set[str],
) -> None:
    for section_index, section in enumerate(document.sections, start=1):
        for header in (section.header, section.first_page_header, section.even_page_header):
            # Avoid instantiating a new blank OOXML header when no variation is configured.
            if header.is_linked_to_previous and section_index > 1:
                continue
            token = str(header.part.partname)
            if token in processed_parts:
                continue
            processed_parts.add(token)
            _parse_nodes(state, header.iter_inner_content(), header.part, "header", qn, section_index)


def _parse_footers(
    document: WordDocument,
    state: _WordInspectionState,
    qn: Callable[[str], str],
    processed_parts: set[str],
) -> None:
    for section_index, section in enumerate(document.sections, start=1):
        for footer in (section.footer, section.first_page_footer, section.even_page_footer):
            if footer.is_linked_to_previous and section_index > 1:
                continue
            token = str(footer.part.partname)
            if token in processed_parts:
                continue
            processed_parts.add(token)
            _parse_nodes(state, footer.iter_inner_content(), footer.part, "footer", qn, section_index)


def _inspect_word_document(
    document: WordDocument,
    options: InspectDocumentOptions,
    qn: Callable[[str], str],
) -> PageCandidates:
    state = _WordInspectionState(options)
    _warn_unsupported_content(document, state.warnings)

    # Header/footer are unique logical parts, not one occurrence per page.
    processed_parts: set[str] = set()
    _parse_headers(document, state, qn, processed_parts)
    _parse_nodes(state, document.iter_inner_content(), document.part, "body", qn)
    _parse_footers(document, state, qn, processed_parts)
    return PageCandidates(
        1,
        PageKind.LOGICAL_DOCUMENT,
        "document",
        tuple(state.regions),
        tuple(dict.fromkeys(state.warnings)),
    )


def _word_pages(
    request: InspectDocumentRequest,
    options: InspectDocumentOptions,
) -> tuple[PageCandidates, ...]:
    """Read OOXML in stream order while preserving pictures in runs and cells.

    Header/footer sequence describes logical parts only. Without Word's layout
    engine we never claim physical page numbers or exact floating-object order.
    """
    from docx.oxml.ns import qn

    try:
        document: WordDocument = Document(io.BytesIO(request.content))
        return (_inspect_word_document(document, options, qn),)
    except ExtractionError:
        raise
    except Exception as error:
        raise ExtractionError("DOCX_PARSE_FAILED", "Unable to inspect Word document") from error
