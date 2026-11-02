"""Find likely content-bearing vector graphics, without claiming semantic recognition.

Excludes page background, borders, table line work and tiny decorations.
The raster is an *inspection candidate*, never sufficient for auto-approval.
"""
from __future__ import annotations

import fitz
from data_extraction.shared.models import BoundingBox


def vector_candidate_bbox(
    page: fitz.Page,
    table_boxes: list[BoundingBox],
) -> BoundingBox | None:
    area: float = float(page.rect.width * page.rect.height)
    if area <= 0:
        return None
    significant: list[fitz.Rect] = []
    complex_strokes: list[fitz.Rect] = []
    for drawing in page.get_drawings():
        rect = fitz.Rect(drawing.get("rect", fitz.Rect())) & page.rect
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
        elif len(drawing.get("items", ())) >= 3 and size_ratio >= .015:
            complex_strokes.append(rect)
    if not (len(significant) >= 2 or any(x.get_area()/area >= .025 for x in significant)
            or complex_strokes):
        return None
    rects: list[fitz.Rect] = significant + complex_strokes
    overall: fitz.Rect = fitz.Rect(rects[0])
    for shape in rects[1:]:
        overall |= shape
    overall = fitz.Rect(overall.x0 - 4, overall.y0 - 4,
                        overall.x1 + 4, overall.y1 + 4) & page.rect
    return (float(overall.x0), float(overall.y0),
            float(overall.x1), float(overall.y1))
