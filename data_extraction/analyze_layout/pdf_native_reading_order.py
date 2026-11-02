"""Optional mixed-direction native PDF word-geometry analysis.

Adapted from the provided legacy algorithm; source text is never overwritten.
Review of a proposal must check tokens and positions against the original PDF.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class _Word:
    text: str
    bbox: tuple[float, float, float, float]
    block_number: int

    @property
    def direction(self) -> str | None:
        rtl = sum(
            unicodedata.bidirectional(character) in {"R", "AL"}
            for character in self.text
        )
        ltr = sum(
            unicodedata.bidirectional(character) in {"L", "EN", "AN"}
            for character in self.text
        )
        if rtl > ltr:
            return "rtl"
        if ltr:
            return "ltr"
        return None


@dataclass(slots=True)
class _Run:
    direction: str | None
    words: list[_Word]

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(word.bbox[0] for word in self.words),
            min(word.bbox[1] for word in self.words),
            max(word.bbox[2] for word in self.words),
            max(word.bbox[3] for word in self.words),
        )


class PdfNativeReadingOrderResolver:
    """Resolve native mixed-direction PDF lines from word geometry.

    Atomic LTR runs retain their internal order, so identifiers, URLs, dates,
    phone groups, and amounts are never reversed character-by-character. Arabic
    runs are read right-to-left and the resulting logical text remains separate
    from the raw text stored on :class:`ParsedBlock`.
    """

    VERSION = "pdf-native-reading-order-v2"
    _MINIMUM_BLOCK_COVERAGE = 0.75
    _MINIMUM_VERTICAL_OVERLAP = 0.50
    _MAXIMUM_WORD_OVERLAP = 0.45
    _LTR_GAP_FACTOR = 2.5
    _MAXIMUM_ATOM_WORDS = 10
    _IDENTIFIER_RE = re.compile(r"[A-Z]{1,12}(?:-[A-Z0-9]{1,16})+")
    _EMAIL_RE = re.compile(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        re.IGNORECASE,
    )
    _URL_RE = re.compile(
        r"HTTPS?://(?:WWW\.)?[A-Z0-9.-]+(?:/[A-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
        re.IGNORECASE,
    )
    _DATE_RE = re.compile(r"(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})")
    _PHONE_RE = re.compile(r"(?:\+965|00965)\d{8}")

    @classmethod
    def _resolve_block_words(cls, words: list[_Word]) -> str | None:
        bands = cls._line_bands(words)
        resolved: list[str] = []
        for band in bands:
            line = cls._resolve_line(band)
            if line is None:
                return None
            if line:
                resolved.append(line)
        return "\n".join(resolved)

    @classmethod
    def _resolve_line(cls, words: list[_Word]) -> str | None:
        words = cls._atomicize_sensitive_values(words)
        leading_label, words = cls._extract_overlapping_rtl_label(words)
        words = cls._drop_degenerate_punctuation(words)
        if cls._has_ambiguous_overlap(words):
            return None
        visual = sorted(words, key=lambda item: item.bbox[0])
        runs: list[_Run] = []
        for word in visual:
            direction = word.direction
            if (
                runs
                and direction is not None
                and runs[-1].direction == direction
                and cls._can_join_run(runs[-1], word)
            ):
                runs[-1].words.append(word)
            else:
                runs.append(_Run(direction, [word]))
        rightmost_strong = next(
            (
                word
                for word in sorted(
                    words, key=lambda item: item.bbox[2], reverse=True
                )
                if word.direction is not None
            ),
            None,
        )
        base_rtl = (
            rightmost_strong is not None and rightmost_strong.direction == "rtl"
        )
        if cls._looks_like_label_value_pair(runs):
            label_runs = tuple(
                run for run in runs if run.bbox[2] < cls._largest_gap_split(runs)
            )
            value_runs = tuple(run for run in runs if run not in label_runs)
            ordered_runs = (
                *sorted(label_runs, key=lambda run: run.bbox[0], reverse=True),
                *sorted(value_runs, key=lambda run: run.bbox[0], reverse=base_rtl),
            )
        else:
            ordered_runs = tuple(
                sorted(runs, key=lambda run: run.bbox[0], reverse=base_rtl)
            )
        parts: list[str] = []
        for run in ordered_runs:
            ordered_words = sorted(
                run.words,
                key=lambda word: word.bbox[0],
                reverse=run.direction == "rtl",
            )
            parts.extend(word.text for word in ordered_words)
        resolved = cls._join_parts(parts)
        return f"{leading_label}: {resolved}" if leading_label else resolved

    @classmethod
    def resolve_region_text(
        cls,
        page,
        bbox: tuple[float, float, float, float],
    ) -> str | None:
        """Resolve one native cell/region from its own word geometry."""

        try:
            raw_words = page.get_text("words", clip=bbox, sort=False)
        except Exception:
            return None
        words = cls._coerce_words(raw_words)
        if not words:
            return None
        return cls._resolve_block_words(list(words))

    @classmethod
    def _atomicize_sensitive_values(cls, words: list[_Word]) -> list[_Word]:
        """Collapse geometry fragments that form one typed LTR value.

        PDF generators frequently emit punctuation and the Latin prefix of an
        identifier as separate words.  Bidi run ordering must happen only after
        those fragments become a single semantic value.
        """

        visual = sorted(words, key=lambda item: item.bbox[0])
        candidates: list[tuple[int, int, str]] = []
        for start in range(len(visual)):
            maximum = min(len(visual), start + cls._MAXIMUM_ATOM_WORDS)
            for end in range(start + 2, maximum + 1):
                selected_words = visual[start:end]
                if not cls._atom_is_contiguous(selected_words):
                    continue
                value = cls._atomic_value(selected_words)
                if value is not None:
                    candidates.append((start, end, value))
        selected: dict[int, tuple[int, str]] = {}
        occupied: set[int] = set()
        for start, end, value in sorted(
            candidates,
            key=lambda item: (item[1] - item[0], len(item[2])),
            reverse=True,
        ):
            if any(index in occupied for index in range(start, end)):
                continue
            selected[start] = (end, value)
            occupied.update(range(start, end))
        output: list[_Word] = []
        index = 0
        while index < len(visual):
            atom = selected.get(index)
            if atom is None:
                output.append(visual[index])
                index += 1
                continue
            end, value = atom
            selected_words = visual[index:end]
            output.append(
                _Word(
                    value,
                    cls._union_bbox(selected_words),
                    selected_words[0].block_number,
                )
            )
            index = end
        return output

    @staticmethod
    def _atom_is_contiguous(words: list[_Word]) -> bool:
        maximum_height = max(word.bbox[3] - word.bbox[1] for word in words)
        return all(
            right.bbox[0] - left.bbox[2] <= maximum_height * 1.25
            for left, right in zip(words, words[1:])
        )

    @classmethod
    def _atomic_value(cls, words: list[_Word]) -> str | None:
        ascending = "".join(word.text.strip() for word in words)
        descending = "".join(word.text.strip() for word in reversed(words))
        for candidate in (ascending, descending):
            if cls._EMAIL_RE.fullmatch(candidate):
                return candidate
            if cls._URL_RE.fullmatch(candidate):
                return candidate
            if cls._IDENTIFIER_RE.fullmatch(candidate):
                return candidate
            date = cls._DATE_RE.fullmatch(candidate)
            if date is not None:
                return (
                    f"{date.group('day')}/{date.group('month')}/"
                    f"{date.group('year')}"
                )
            phone = cls._PHONE_RE.fullmatch(candidate)
            if phone is not None:
                prefix = "+965" if candidate.startswith("+965") else "00965"
                digits = candidate[len(prefix) :]
                return f"{prefix} {digits[:4]} {digits[4:]}"
        return None

    @classmethod
    def _drop_degenerate_punctuation(cls, words: list[_Word]) -> list[_Word]:
        """Discard oversized punctuation boxes that cover several real words."""

        retained: list[_Word] = []
        for word in words:
            if any(character.isalnum() for character in word.text):
                retained.append(word)
                continue
            overlaps = sum(
                other is not word
                and any(character.isalnum() for character in other.text)
                and cls._horizontal_overlap_ratio(word.bbox, other.bbox)
                > cls._MAXIMUM_WORD_OVERLAP
                for other in words
            )
            if overlaps < 2:
                retained.append(word)
        return retained

    @classmethod
    def _extract_overlapping_rtl_label(
        cls, words: list[_Word]
    ) -> tuple[str | None, list[_Word]]:
        """Recover an RTL label when PDF punctuation overlaps its word box.

        Some generators store ``المتوقع: ... OCR.`` as a ``:.`` glyph over the
        label's rectangle. Geometry identifies the label; the period remains at
        its visual position while the colon is attached to the leading label.
        """

        for punctuation in words:
            if ":" not in punctuation.text or any(
                character.isalnum() for character in punctuation.text
            ):
                continue
            overlapping = tuple(
                word
                for word in words
                if word is not punctuation
                and word.direction == "rtl"
                and cls._horizontal_overlap_ratio(word.bbox, punctuation.bbox)
                > cls._MAXIMUM_WORD_OVERLAP
            )
            if len(overlapping) != 1:
                continue
            label = overlapping[0]
            retained = [
                word
                for word in words
                if word is not label and word is not punctuation
            ]
            if "." in punctuation.text:
                retained.append(_Word(".", punctuation.bbox, punctuation.block_number))
            return label.text.rstrip(":"), retained
        return None, words

    @staticmethod
    def _horizontal_overlap_ratio(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> float:
        overlap = max(
            0.0,
            min(first[2], second[2]) - max(first[0], second[0]),
        )
        minimum_width = min(first[2] - first[0], second[2] - second[0])
        return overlap / minimum_width if minimum_width > 0.0 else 0.0

    @classmethod
    def _can_join_run(cls, run: _Run, word: _Word) -> bool:
        gap = max(0.0, word.bbox[0] - run.bbox[2])
        height = max(
            word.bbox[3] - word.bbox[1],
            run.bbox[3] - run.bbox[1],
        )
        return gap <= height * cls._LTR_GAP_FACTOR

    @classmethod
    def _looks_like_label_value_pair(cls, runs: list[_Run]) -> bool:
        if len(runs) < 2:
            return False
        split = cls._largest_gap_split(runs)
        if split <= 0.0:
            return False
        left = tuple(run for run in runs if run.bbox[2] < split)
        right = tuple(run for run in runs if run.bbox[0] > split)
        if not left or not right:
            return False
        left_text = " ".join(word.text for run in left for word in run.words)
        right_text = " ".join(word.text for run in right for word in run.words)
        return (
            cls._is_rtl(left_text)
            and any(character in left_text for character in ":：")
            and any(character.isdigit() or character.isascii() for character in right_text)
        )

    @staticmethod
    def _largest_gap_split(runs: list[_Run]) -> float:
        ordered = sorted(runs, key=lambda run: run.bbox[0])
        gaps = tuple(
            (right.bbox[0] - left.bbox[2], (left.bbox[2] + right.bbox[0]) / 2.0)
            for left, right in zip(ordered, ordered[1:])
        )
        if not gaps:
            return 0.0
        gap, split = max(gaps)
        maximum_height = max(run.bbox[3] - run.bbox[1] for run in ordered)
        return split if gap > maximum_height * 3.0 else 0.0

    @classmethod
    def _line_bands(cls, words: list[_Word]) -> tuple[list[_Word], ...]:
        bands: list[list[_Word]] = []
        for word in sorted(words, key=lambda item: (item.bbox[1], item.bbox[0])):
            matching = next(
                (
                    band
                    for band in bands
                    if cls._vertical_overlap(word.bbox, cls._union_bbox(band))
                    >= cls._MINIMUM_VERTICAL_OVERLAP
                ),
                None,
            )
            if matching is None:
                bands.append([word])
            else:
                matching.append(word)
        return tuple(sorted(bands, key=lambda band: cls._union_bbox(band)[1]))

    @classmethod
    def _has_ambiguous_overlap(cls, words: list[_Word]) -> bool:
        for index, first in enumerate(words):
            for second in words[index + 1 :]:
                overlap = max(
                    0.0,
                    min(first.bbox[2], second.bbox[2])
                    - max(first.bbox[0], second.bbox[0]),
                )
                minimum_width = min(
                    first.bbox[2] - first.bbox[0],
                    second.bbox[2] - second.bbox[0],
                )
                if minimum_width > 0.0 and overlap / minimum_width > cls._MAXIMUM_WORD_OVERLAP:
                    return True
        return False

    @staticmethod
    def _join_parts(parts: list[str]) -> str:
        value = " ".join(part.strip() for part in parts if part.strip())
        value = re.sub(r"\s+([:،؛,.!?])", r"\1", value)
        value = re.sub(r"([([])\s+", r"\1", value)
        value = re.sub(r"\s+([])])", r"\1", value)
        value = re.sub(r"\bد\.\s*ك\.", "د.ك.", value)
        value = re.sub(r"\b([A-Z]{1,12})\s+(-[A-Z0-9])", r"\1\2", value)
        return " ".join(value.split())

    @staticmethod
    def _coerce_words(raw_words: object) -> tuple[_Word, ...]:
        try:
            values = tuple(raw_words)  # type: ignore[arg-type]
        except TypeError:
            return ()
        words: list[_Word] = []
        for value in values:
            if not isinstance(value, (tuple, list)) or len(value) < 8:
                continue
            try:
                bbox = tuple(float(coordinate) for coordinate in value[:4])
                text = str(value[4]).strip()
                block_number = int(value[5])
            except (TypeError, ValueError):
                continue
            if not text or len(bbox) != 4 or bbox[0] > bbox[2] or bbox[1] > bbox[3]:
                continue
            words.append(_Word(text, bbox, block_number))
        return tuple(words)

    @staticmethod
    def _union_bbox(words: list[_Word]) -> tuple[float, float, float, float]:
        return (
            min(word.bbox[0] for word in words),
            min(word.bbox[1] for word in words),
            max(word.bbox[2] for word in words),
            max(word.bbox[3] for word in words),
        )

    @staticmethod
    def _vertical_overlap(
        first: tuple[float, float, float, float],
        second: tuple[float, float, float, float],
    ) -> float:
        overlap = max(0.0, min(first[3], second[3]) - max(first[1], second[1]))
        minimum_height = min(first[3] - first[1], second[3] - second[1])
        return overlap / minimum_height if minimum_height > 0.0 else 0.0

    @staticmethod
    def _bbox_coverage(
        subject: tuple[float, float, float, float],
        container: tuple[float, float, float, float],
    ) -> float:
        width = max(0.0, subject[2] - subject[0])
        height = max(0.0, subject[3] - subject[1])
        area = width * height
        if area == 0.0:
            return 0.0
        intersection_width = max(
            0.0,
            min(subject[2], container[2]) - max(subject[0], container[0]),
        )
        intersection_height = max(
            0.0,
            min(subject[3], container[3]) - max(subject[1], container[1]),
        )
        return intersection_width * intersection_height / area

    @staticmethod
    def _is_rtl(text: str) -> bool:
        rtl = sum(
            unicodedata.bidirectional(character) in {"R", "AL"}
            for character in text
        )
        ltr = sum(
            unicodedata.bidirectional(character) == "L"
            for character in text
        )
        return rtl > ltr


@dataclass(frozen=True, slots=True)
class PDFReadingOrderProposal:
    original_text: str
    proposed_text: str
    same_token_multiset: bool


def propose_mixed_direction_text(page: object, bbox: tuple[float, float, float, float],
                                 original_text: str) -> PDFReadingOrderProposal | None:
    """Return a non-destructive proposal only for mixed Arabic/Latin text."""
    if not (re.search(r"[\u0600-\u06FF]", original_text)
            and re.search(r"[A-Za-z]", original_text)):
        return None
    proposed: str | None = PdfNativeReadingOrderResolver.resolve_region_text(page, bbox)
    if not proposed or " ".join(original_text.split()) == " ".join(proposed.split()):
        return None
    raw_tokens: Counter[str] = Counter(re.findall(r"\w+", original_text, flags=re.UNICODE))
    candidate_tokens: Counter[str] = Counter(re.findall(r"\w+", proposed, flags=re.UNICODE))
    return PDFReadingOrderProposal(original_text, proposed, raw_tokens == candidate_tokens)


@dataclass(frozen=True, slots=True)
class PDFRegionOrderProposal:
    region_id: str
    page_number: int
    original_text: str
    suggested_text: str
    bbox: tuple[float, float, float, float]
