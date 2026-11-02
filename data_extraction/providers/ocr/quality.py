"""Domain-independent OCR diagnostics; short valid pages are never rejected."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import unicodedata

from ...shared.models import ProviderTextResult


@dataclass(frozen=True, slots=True)
class OCRQualityEvidence:
    text_sha256: str
    non_whitespace_characters: int
    mean_confidence: float | None
    contains_digits: bool
    mixes_rtl_and_ltr: bool
    review_reasons: tuple[str, ...]


def assess_ocr_quality(result: ProviderTextResult, minimum_confidence: float = .83) -> OCRQualityEvidence:
    """Evaluate extraction evidence, not expected domain-specific field content."""
    text: str = result.text
    directions: set[str] = {unicodedata.bidirectional(char) for char in text}
    mixed: bool = bool(directions & {"R", "AL"}) and bool(directions & {"L"})
    reasons: list[str] = []
    if not text.strip() and result.structured_table is None:
        reasons.append("ocr_no_content")
    if result.confidence is None:
        reasons.append("ocr_confidence_unknown")
    elif result.confidence < minimum_confidence:
        reasons.append("ocr_low_confidence")
    if mixed:
        reasons.append("ocr_mixed_direction_review")
    return OCRQualityEvidence(
        text_sha256=sha256(text.encode("utf-8")).hexdigest(),
        non_whitespace_characters=sum(not c.isspace() for c in text),
        mean_confidence=result.confidence,
        contains_digits=any(c.isdecimal() for c in text),
        mixes_rtl_and_ltr=mixed,
        review_reasons=tuple(reasons),
    )
