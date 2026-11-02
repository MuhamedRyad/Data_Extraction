"""Conservative PDF table-of-contents reconstruction from native text geometry.

The source text is preserved. This module adds a structured two-column TOC
candidate when repeated title/page-number row evidence is strong enough.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from ..shared.models import BoundingBox, CandidateRegion, RegionKind, SourceSurface, TableCellLocation


@dataclass(frozen=True, slots=True)
class NativeTextLine:
    text: str
    bbox: BoundingBox
    source_region_id: str


@dataclass(frozen=True, slots=True)
class TocDetection:
    table_region: CandidateRegion
    source_region_ids: tuple[str, ...]
    row_count: int


def _normalize_digits(value: str) -> str:
    output: list[str] = []
    for character in value:
        if character.isdecimal():
            try:
                output.append(str(unicodedata.digit(character)))
            except (TypeError, ValueError):
                output.append(character)
        else:
            output.append(character)
    return "".join(output)


def _page_number(value: str) -> str | None:
    normalized: str = _normalize_digits(value).strip()
    normalized = normalized.strip(".·•-–— ")
    if not re.fullmatch(r"\d{1,4}", normalized):
        return None
    number: int = int(normalized)
    if number <= 0:
        return None
    return normalized


def _center_y(box: BoundingBox) -> float:
    return (box[1] + box[3]) / 2.0


def _height(box: BoundingBox) -> float:
    return max(1.0, box[3] - box[1])


def _merge_bbox(boxes: list[BoundingBox]) -> BoundingBox:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def detect_toc_table(
    lines: tuple[NativeTextLine, ...],
    page_width: float,
    page_height: float,
    page_number: int,
    document_page_count: int | None = None,
) -> TocDetection | None:
    """Return a structured TOC only when at least three row pairs are geometric.

    Numeric-only lines are paired one-to-one with nonnumeric title lines on the
    same visual row. Identical page numbers remain distinct rows because the
    binding key is geometry, not value equality.
    """
    if page_width <= 0 or page_height <= 0 or len(lines) < 6:
        return None

    numbers: list[tuple[NativeTextLine, str]] = []
    titles: list[NativeTextLine] = []
    for line in lines:
        value: str = line.text.strip()
        if not value:
            continue
        page_ref: str | None = _page_number(value)
        if page_ref is not None:
            numbers.append((line, page_ref))
        elif len(value) >= 3 and any(character.isalpha() for character in value):
            titles.append(line)
    if len(numbers) < 3 or len(titles) < 3:
        return None

    # Pair *from the title row* and choose the numeric candidate closest to a
    # page edge. This avoids binding a section number near the title when the
    # same TOC row also contains the real page reference at the outer margin.
    used_numbers: set[int] = set()
    pairs: list[tuple[NativeTextLine, NativeTextLine, str]] = []
    for title in sorted(titles, key=lambda item: _center_y(item.bbox)):
        choices: list[tuple[tuple[float, float, float, float], int, NativeTextLine, str]] = []
        for number_index, (number_line, page_ref) in enumerate(numbers):
            if number_index in used_numbers:
                continue
            tolerance: float = max(_height(number_line.bbox), _height(title.bbox)) * 0.80 + 2.0
            distance: float = abs(_center_y(number_line.bbox) - _center_y(title.bbox))
            if distance > tolerance:
                continue
            separated: bool = (
                number_line.bbox[2] <= title.bbox[0] + page_width * 0.03
                or title.bbox[2] <= number_line.bbox[0] + page_width * 0.03
            )
            if not separated:
                continue
            center_x: float = (number_line.bbox[0] + number_line.bbox[2]) / 2.0
            edge_distance: float = min(center_x, page_width - center_x) / page_width
            horizontal_gap: float = (
                title.bbox[0] - number_line.bbox[2]
                if number_line.bbox[2] <= title.bbox[0]
                else number_line.bbox[0] - title.bbox[2]
            )
            plausible_ref_penalty: float = 0.0
            if document_page_count is not None and int(page_ref) > document_page_count + 5:
                plausible_ref_penalty = 1.0
            # lower tuple is better: plausible, outer edge, same row, large gap
            score = (plausible_ref_penalty, edge_distance, distance, -horizontal_gap)
            choices.append((score, number_index, number_line, page_ref))
        if not choices:
            continue
        _, number_index, number_line, page_ref = min(choices, key=lambda item: item[0])
        used_numbers.add(number_index)
        pairs.append((title, number_line, page_ref))

    if len(pairs) < 3:
        return None
    pairs.sort(key=lambda pair: min(_center_y(pair[0].bbox), _center_y(pair[1].bbox)))

    # v0.13: reject statistics/list tables that merely contain a text column and
    # a numeric column. Real TOC page references normally occupy one narrow edge
    # column and are mostly non-decreasing. A visible contents heading relaxes
    # the row-count requirement but never the geometric page-number column test.
    heading_text: str = " ".join(line.text.casefold() for line in lines)
    has_toc_heading: bool = any(token in heading_text for token in (
        "table of contents", "contents", "الفهرس", "المحتويات", "محتويات",
    ))
    legal_title_pattern = re.compile(r"^(?:article\b|section\b|المادة\b|مادة\b|الباب\b|الفصل\b)", re.IGNORECASE)
    legal_like: int = sum(1 for title, _, _ in pairs if legal_title_pattern.search(title.text.strip()))
    # Numbered legal provisions can look exactly like a two-column TOC. Without
    # an explicit contents heading, do not reinterpret a page dominated by
    # article/section labels as a table of contents.
    if not has_toc_heading and legal_like / max(1, len(pairs)) >= 0.40:
        return None
    number_centers: list[float] = [
        (number.bbox[0] + number.bbox[2]) / 2.0 for _, number, _ in pairs
    ]
    if max(number_centers) - min(number_centers) > page_width * 0.07:
        return None
    refs: list[int] = [int(page_ref) for _, _, page_ref in pairs]
    nondecreasing: int = sum(1 for left, right in zip(refs, refs[1:]) if right >= left)
    monotonic_ratio: float = nondecreasing / max(1, len(refs) - 1)
    if monotonic_ratio < (0.60 if has_toc_heading else 0.80):
        return None
    if not has_toc_heading and len(pairs) < 5:
        # Small TOCs are accepted only when the page-reference column sits near
        # a page edge and references are fully non-decreasing. This preserves
        # compact real TOCs while rejecting central statistics/value columns.
        mean_center: float = sum(number_centers) / len(number_centers)
        near_edge: bool = mean_center <= page_width * 0.18 or mean_center >= page_width * 0.82
        if not near_edge or monotonic_ratio < 1.0:
            return None

    # Require row spacing to be mostly monotonic and cover a non-trivial vertical
    # band. This avoids converting arbitrary page numbers/captions into a TOC.
    row_y: list[float] = [(_center_y(title.bbox) + _center_y(number.bbox)) / 2.0
                          for title, number, _ in pairs]
    if any(second <= first for first, second in zip(row_y, row_y[1:])):
        return None
    if row_y[-1] - row_y[0] < max(20.0, page_height * 0.06):
        return None

    rows: list[list[str | None]] = [[title.text.strip(), page_ref]
                                    for title, _, page_ref in pairs]
    locations: list[TableCellLocation] = []
    for row_index, (title, number, _) in enumerate(pairs):
        locations.extend((
            TableCellLocation(row_index, 0, 1, 1, title.bbox),
            TableCellLocation(row_index, 1, 1, 1, number.bbox),
        ))
    boxes: list[BoundingBox] = [line.bbox for pair in pairs for line in pair[:2]]
    source_ids: tuple[str, ...] = tuple(dict.fromkeys(
        line.source_region_id for pair in pairs for line in pair[:2]
    ))
    table = CandidateRegion(
        region_id=f"p{page_number}:toc:1",
        kind=RegionKind.TABLE,
        bbox=_merge_bbox(boxes),
        table=rows,
        source_surface=SourceSurface.NATIVE_PDF_TEXT,
        semantic_role="table_of_contents",
        table_cell_locations=tuple(locations),
    )
    return TocDetection(table, source_ids, len(rows))
