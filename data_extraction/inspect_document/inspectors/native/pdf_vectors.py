"""Find likely content-bearing vector graphics, without claiming semantic recognition.

Excludes page background, borders, table line work and tiny decorations.
The raster is an *inspection candidate*, never sufficient for auto-approval.
"""
from __future__ import annotations

import fitz
from data_extraction.shared.models import BoundingBox


def _repeated_row_bands(
    rectangles: list[fitz.Rect],
    page_rect: fitz.Rect,
) -> set[int]:
    """Return wide, short, regularly spaced fills likely to be table bands."""
    candidates = [
        rect for rect in rectangles
        if rect.width / max(1.0, page_rect.width) >= 0.65
        and rect.height / max(1.0, page_rect.height) <= 0.08
    ]
    ignored: set[int] = set()
    for rect in candidates:
        peers = [
            other for other in candidates
            if other is not rect
            and abs(other.x0 - rect.x0) <= 2
            and abs(other.x1 - rect.x1) <= 2
            and abs(other.height - rect.height) <= max(1.0, rect.height * 0.2)
        ]
        if len(peers) < 2:
            continue
        rows = sorted([rect, *peers], key=lambda item: item.y0)
        gaps = [right.y0 - left.y1 for left, right in zip(rows, rows[1:])]
        positive_gaps = [gap for gap in gaps if gap >= 0]
        if len(positive_gaps) < 2:
            continue
        median_gap = sorted(positive_gaps)[len(positive_gaps) // 2]
        tolerance = max(2.0, abs(median_gap) * 0.25)
        if all(abs(gap - median_gap) <= tolerance for gap in positive_gaps):
            ignored.update(id(item) for item in rows)
    return ignored


def vector_candidate_bbox(
    page: fitz.Page,
    table_boxes: list[BoundingBox],
) -> BoundingBox | None:
    page_rect: fitz.Rect = page.rect * page.derotation_matrix
    area: float = float(page_rect.width * page_rect.height)
    if area <= 0:
        return None
    significant: list[fitz.Rect] = []
    complex_strokes: list[fitz.Rect] = []
    filled_rectangles: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect", fitz.Rect())) & page_rect
        if rect.is_empty or rect.width <= 0 or rect.height <= 0:
            continue
        if any((rect & fitz.Rect(table)).get_area() >= rect.get_area() * .85
               for table in table_boxes):
            continue
        size_ratio: float = rect.get_area() / area
        if size_ratio > .8:  # page fills/background
            continue
        filled: bool = drawing.get("fill") is not None
        if filled and size_ratio >= .002 and rect.width >= 12 and rect.height >= 12:
            significant.append(rect)
            filled_rectangles.append(rect)
        elif len(drawing.get("items", ())) >= 3 and size_ratio >= .015:
            complex_strokes.append(rect)
    repeated_bands = _repeated_row_bands(filled_rectangles, page_rect)
    if repeated_bands:
        significant = [rect for rect in significant if id(rect) not in repeated_bands]
    # A lone full-width strip at the page edge is normally a header/background,
    # not a content-bearing chart. Native text and image inspection already
    # preserve the content layered over it.
    if len(significant) == 1:
        header = significant[0]
        if (
            header.y0 <= page_rect.y0 + 2
            and header.width / max(1.0, page_rect.width) >= 0.70
            and header.height / max(1.0, page_rect.height) <= 0.12
        ):
            significant = []
    if not (len(significant) >= 2 or any(x.get_area()/area >= .025 for x in significant)
            or complex_strokes):
        return None
    rects: list[fitz.Rect] = significant + complex_strokes
    overall: fitz.Rect = fitz.Rect(rects[0])
    for shape in rects[1:]:
        overall |= shape
    overall = fitz.Rect(overall.x0 - 4, overall.y0 - 4,
                        overall.x1 + 4, overall.y1 + 4) & page_rect
    return (float(overall.x0), float(overall.y0),
            float(overall.x1), float(overall.y1))
