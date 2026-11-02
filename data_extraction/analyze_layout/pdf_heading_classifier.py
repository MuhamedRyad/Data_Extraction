"""Legacy-derived, domain-neutral PDF heading candidate classifier.

Candidates are deliberately review-required; visual style is not proof.
"""


import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass


_BOLD_FONT_RE = re.compile(r"(?:bold|semibold|demibold|black|heavy)", re.IGNORECASE)
_URL_OR_EMAIL_RE = re.compile(r"(?:https?://|www\.|@|\S+@\S+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PdfSpanStyle:
    font_size: float
    character_count: int
    bold: bool


@dataclass(frozen=True, slots=True)
class PdfBlockStyle:
    citation_anchor: str
    text: str
    page_number: int
    bbox: tuple[float, float, float, float]
    page_height: float
    line_count: int
    spans: tuple[PdfSpanStyle, ...]


class PdfHeadingClassifier:
    """Conservative PDF heading inference from typography and geometry.

    The classifier deliberately avoids vocabulary rules so it can be reused by
    every domain. Font size is compared with both the document body and the
    current page. Bold is useful only when the document's body style is not
    already bold. Geometry and text-shape guards reject common page numbers,
    monetary values, URLs, and footer noise.
    """

    _MAX_HEADING_CHARACTERS = 160
    _MAX_HEADING_LINES = 3
    _FOOTER_START_RATIO = 0.86
    _TOP_REGION_RATIO = 0.18
    _MIN_SIZE_RATIO = 1.12
    _MIN_SIZE_DELTA = 1.0
    _STRONG_SIZE_RATIO = 1.35
    _BOLD_RATIO = 0.65
    _BODY_BOLD_RATIO_LIMIT = 0.45

    @classmethod
    def heading_anchors(
        cls, styles: list[PdfBlockStyle] | tuple[PdfBlockStyle, ...]
    ) -> frozenset[str]:
        if not styles:
            return frozenset()
        body_size = cls._dominant_size(styles)
        if body_size is None or body_size <= 0.0:
            return frozenset()
        page_sizes = {
            page_number: cls._dominant_size(page_styles)
            for page_number, page_styles in cls._styles_by_page(styles).items()
        }
        page_bold_ratios = {
            page_number: cls._styles_bold_ratio(page_styles)
            for page_number, page_styles in cls._styles_by_page(styles).items()
        }
        body_bold_ratio = cls._body_bold_ratio(styles, body_size)
        return frozenset(
            style.citation_anchor
            for style in styles
            if cls._is_heading(
                style,
                body_size=body_size,
                page_size=page_sizes.get(style.page_number),
                body_bold_ratio=body_bold_ratio,
                page_bold_ratio=page_bold_ratios.get(style.page_number, 0.0),
            )
        )

    @classmethod
    def _is_heading(
        cls,
        style: PdfBlockStyle,
        *,
        body_size: float,
        page_size: float | None,
        body_bold_ratio: float,
        page_bold_ratio: float,
    ) -> bool:
        text = " ".join(style.text.split())
        if not cls._has_heading_text_shape(text):
            return False
        if style.line_count < 1 or style.line_count > cls._MAX_HEADING_LINES:
            return False
        if style.page_height <= 0.0:
            return False
        if style.bbox[1] >= style.page_height * cls._FOOTER_START_RATIO:
            return False

        font_size = cls._dominant_span_size(style.spans)
        if font_size is None:
            return False
        enlarged = (
            font_size >= body_size * cls._MIN_SIZE_RATIO
            and font_size - body_size >= cls._MIN_SIZE_DELTA
        )
        strongly_enlarged = font_size >= body_size * cls._STRONG_SIZE_RATIO
        locally_enlarged = (
            page_size is not None
            and font_size >= page_size * cls._MIN_SIZE_RATIO
            and font_size - page_size >= cls._MIN_SIZE_DELTA
        )
        near_page_top = style.bbox[1] <= style.page_height * cls._TOP_REGION_RATIO
        size_signal = strongly_enlarged or (
            enlarged and (locally_enlarged or near_page_top)
        )

        bold_signal = (
            body_bold_ratio < cls._BODY_BOLD_RATIO_LIMIT
            and page_bold_ratio < cls._BODY_BOLD_RATIO_LIMIT
            and cls._bold_ratio(style.spans) >= cls._BOLD_RATIO
            and font_size >= body_size * 0.95
        )
        return size_signal or bold_signal

    @classmethod
    def _has_heading_text_shape(cls, text: str) -> bool:
        if not text or len(text) > cls._MAX_HEADING_CHARACTERS:
            return False
        if _URL_OR_EMAIL_RE.search(text) is not None:
            return False
        if text[0] in {"•", "◦", "▪", "‣", "⁃", "\uf0b7"}:
            return False
        if len(text.split()) >= 7 and (
            unicodedata.category(text[0]).startswith("P")
            or unicodedata.category(text[-1]).startswith("P")
        ):
            return False
        sentence_marks = (".", "!", "?", "؟", "؛")
        if len(text.split()) >= 7 and (
            text.startswith(sentence_marks) or text.endswith(sentence_marks)
        ):
            return False
        alphabetic = sum(character.isalpha() for character in text)
        digits = sum(character.isdecimal() for character in text)
        if alphabetic < 3:
            return False
        return digits < alphabetic

    @staticmethod
    def _styles_by_page(
        styles: list[PdfBlockStyle] | tuple[PdfBlockStyle, ...],
    ) -> dict[int, list[PdfBlockStyle]]:
        grouped: dict[int, list[PdfBlockStyle]] = defaultdict(list)
        for style in styles:
            grouped[style.page_number].append(style)
        return grouped

    @classmethod
    def _dominant_size(cls, styles: list[PdfBlockStyle]) -> float | None:
        return cls._weighted_size_mode(
            span
            for style in styles
            for span in style.spans
        )

    @classmethod
    def _dominant_span_size(
        cls, spans: tuple[PdfSpanStyle, ...]
    ) -> float | None:
        return cls._weighted_size_mode(iter(spans))

    @staticmethod
    def _weighted_size_mode(spans) -> float | None:
        weights: Counter[float] = Counter()
        for span in spans:
            if span.font_size > 0.0 and span.character_count > 0:
                size_bin = round(span.font_size * 2.0) / 2.0
                weights[size_bin] += span.character_count
        if not weights:
            return None
        return max(weights, key=lambda size: (weights[size], -size))

    @staticmethod
    def _bold_ratio(spans: tuple[PdfSpanStyle, ...]) -> float:
        total = sum(span.character_count for span in spans)
        if total == 0:
            return 0.0
        bold = sum(span.character_count for span in spans if span.bold)
        return bold / total

    @classmethod
    def _body_bold_ratio(
        cls,
        styles: list[PdfBlockStyle] | tuple[PdfBlockStyle, ...],
        body_size: float,
    ) -> float:
        body_spans = tuple(
            span
            for style in styles
            for span in style.spans
            if round(span.font_size * 2.0) / 2.0 == body_size
        )
        return cls._bold_ratio(body_spans)

    @classmethod
    def _styles_bold_ratio(cls, styles: list[PdfBlockStyle]) -> float:
        return cls._bold_ratio(
            tuple(span for style in styles for span in style.spans)
        )


def is_bold_pdf_span(*, flags: object, font_name: object) -> bool:
    """Interpret PyMuPDF's bold flag and common embedded font names safely."""

    try:
        has_bold_flag = bool(int(flags) & 16)
    except (TypeError, ValueError):
        has_bold_flag = False
    return has_bold_flag or _BOLD_FONT_RE.search(str(font_name)) is not None
