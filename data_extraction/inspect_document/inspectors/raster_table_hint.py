"""Conservative, OCR-free detection of rectangular raster grids.

A hint is NOT a reconstructed table: broken borders, borderless tables and
ornamental lines may be missed. No content is generated from pixels here.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps


def _distinct_line_positions(positions: list[int], separation: int) -> list[int]:
    groups: list[int] = []
    for position in positions:
        if not groups or position - groups[-1] >= separation:
            groups.append(position)
    return groups


def is_raster_grid_table(image_bytes: bytes, minimum_lines: int = 3) -> bool:
    """Return True only for a visibly bordered grid with horizontal/vertical strokes."""
    with Image.open(io.BytesIO(image_bytes)) as original:
        raster: Image.Image = ImageOps.grayscale(original)
        raster.thumbnail((900, 900))
        width: int = raster.width
        height: int = raster.height
        if width < 50 or height < 40:
            return False
        raw: bytes = raster.tobytes()

    dark: bytes = bytes(1 if pixel < 105 else 0 for pixel in raw)
    # Find long horizontal strokes, grouping neighboring anti-aliased rows.
    minimum_horizontal: int = int(width * 0.55)
    horizontal: list[int] = []
    for y in range(height):
        left: int = y * width
        if sum(dark[left:left + width]) >= minimum_horizontal:
            horizontal.append(y)

    # Find long vertical strokes. Lines need not run all the way to image borders.
    minimum_vertical: int = int(height * 0.45)
    vertical: list[int] = []
    for x in range(width):
        count: int = sum(dark[y * width + x] for y in range(height))
        if count >= minimum_vertical:
            vertical.append(x)

    rows: list[int] = _distinct_line_positions(horizontal, max(3, height // 70))
    columns: list[int] = _distinct_line_positions(vertical, max(3, width // 70))
    if len(rows) < minimum_lines or len(columns) < minimum_lines:
        return False
    # Verify intersections: text alone often creates horizontal/vertical projections.
    matches: int = 0
    for y in rows:
        for x in columns:
            if any(
                dark[yy * width + xx]
                for yy in range(max(0, y - 2), min(height, y + 3))
                for xx in range(max(0, x - 2), min(width, x + 3))
            ):
                matches += 1
    return matches >= int(0.75 * len(rows) * len(columns))
