"""Optional review-only OCR word-geometry table reconstruction.

Adapted from legacy table geometry concepts, without domain-specific header
vocabulary. Layout is inferred only for repeated aligned rows and columns.
Never invent missing cells; this is a *candidate*, not authoritative table data.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from ...shared.models import TableCells


@dataclass(frozen=True, slots=True)
class OCRWord:
    text: str
    left: float
    top: float
    right: float
    bottom: float
    confidence: float | None = None

    @property
    def center_x(self) -> float:
        return (self.left + self.right) / 2

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


def reconstruct_candidate_table(words: tuple[OCRWord, ...]) -> TableCells | None:
    """Require repeated rows and stable columns; return None when ambiguous."""
    selected: list[OCRWord] = sorted(
        (word for word in words if word.text.strip()
         and word.right > word.left and word.bottom > word.top),
        key=lambda word: (word.center_y, word.left),
    )
    if len(selected) < 6:
        return None
    height: float = median(word.bottom - word.top for word in selected)
    vertical_tolerance: float = max(2.0, height * .58)
    rows: list[list[OCRWord]] = []
    for word in selected:
        if rows and abs(word.center_y - median(item.center_y for item in rows[-1])) <= vertical_tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    if len(rows) < 3:
        return None
    complete_rows: list[list[OCRWord]] = [sorted(row, key=lambda w: w.center_x)
                                            for row in rows if len(row) >= 2]
    if len(complete_rows) < 3:
        return None
    # A weakly structured page may contain arbitrary multiple words per line.
    # Require the same number of separated horizontal clusters in >=3 rows.
    widths: dict[int, int] = {}
    for row in complete_rows:
        widths[len(row)] = widths.get(len(row), 0) + 1
    columns: int = max(widths, key=lambda width: (widths[width], width))
    if columns < 2 or widths[columns] < 3:
        return None
    baselines: list[list[float]] = [
        [word.center_x for word in row] for row in complete_rows if len(row) == columns
    ]
    centers: list[float] = [float(median(row[index] for row in baselines))
                             for index in range(columns)]
    gap: float = min(right - left for left, right in zip(centers, centers[1:]))
    if gap <= 2 * height:
        return None
    tolerance: float = min(gap * .30, max(height * 2, gap * .2))
    if any(abs(x - centers[index]) > tolerance
           for row in baselines for index, x in enumerate(row)):
        return None
    output: TableCells = []
    for row in rows:
        cells: list[list[OCRWord]] = [[] for _ in centers]
        for word in row:
            nearest: int = min(range(columns), key=lambda index: abs(word.center_x - centers[index]))
            if abs(word.center_x - centers[nearest]) > gap * .45:
                return None
            cells[nearest].append(word)
        if sum(bool(cell) for cell in cells) < 2:
            return None
        output.append([" ".join(word.text for word in sorted(cell, key=lambda item: item.left))
                       if cell else None for cell in cells])
    return output


def reconstruct_financial_candidate_table(words: tuple[OCRWord, ...]) -> TableCells | None:
    """Recover a conservative two-column label/description + amount table.

    This fallback is intended for receipts/invoices where descriptions contain a
    variable number of OCR tokens, so the strict equal-width grid reconstruction
    cannot succeed. It never invents values: every emitted cell comes from OCR
    token geometry on the same visual row.
    """
    import re

    amount_re = re.compile(r"^[\(\-+]?(?:\d{1,3}(?:[, ]\d{3})+|\d+)[.,]\d{2}\)?$")
    selected: list[OCRWord] = sorted(
        (word for word in words if word.text.strip()
         and word.right > word.left and word.bottom > word.top),
        key=lambda word: (word.center_y, word.left),
    )
    if len(selected) < 6:
        return None
    height: float = median(word.bottom - word.top for word in selected)
    vertical_tolerance: float = max(2.0, height * .62)
    rows: list[list[OCRWord]] = []
    for word in selected:
        if rows and abs(word.center_y - median(item.center_y for item in rows[-1])) <= vertical_tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])

    candidates: list[tuple[str, str, float]] = []
    amount_x: list[float] = []
    for row in rows:
        ordered: list[OCRWord] = sorted(row, key=lambda item: item.left)
        amount_indices: list[int] = [
            index for index, word in enumerate(ordered)
            if amount_re.fullmatch(word.text.replace(" ", "")) is not None
        ]
        if not amount_indices:
            continue
        # Financial totals/prices are normally the right-most numeric value in
        # an LTR row. For RTL rows the geometry still gives us a stable amount
        # column; choosing the outer-most numeric token is safer than flattening.
        amount_index: int = amount_indices[-1]
        amount_word: OCRWord = ordered[amount_index]
        label_words: list[OCRWord] = ordered[:amount_index]
        if not label_words:
            continue
        label: str = " ".join(word.text for word in label_words).strip()
        if len(label) < 2 or not any(character.isalpha() for character in label):
            continue
        candidates.append((label, amount_word.text.strip(), amount_word.center_x))
        amount_x.append(amount_word.center_x)

    if len(candidates) < 3:
        return None
    amount_center: float = float(median(amount_x))
    tolerance: float = max(height * 3.0, 24.0)
    aligned: list[tuple[str, str]] = [
        (label, amount) for label, amount, x in candidates
        if abs(x - amount_center) <= tolerance
    ]
    if len(aligned) < 3:
        return None
    return [[label, amount] for label, amount in aligned]
