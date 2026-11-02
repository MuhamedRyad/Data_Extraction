"""Generic, opt-in discovery of typed value shapes in already-extracted text.

This feature intentionally does not infer domain semantics such as invoice,
civil-ID, request, or customer numbers. It only detects broad textual shapes.
Verification remains a separate feature.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

from ..shared.models import ExtractionMethod, ExtractionResult, RegionKind
from .options import FieldDiscoveryOptions
from .response import DiscoveredField, FieldDiscoveryKind, FieldDiscoveryResult


_PATTERNS: dict[FieldDiscoveryKind, re.Pattern[str]] = {
    FieldDiscoveryKind.EMAIL: re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I),
    FieldDiscoveryKind.URL: re.compile(r"\bhttps?://[^\s<>()\[\]{}]+", re.I),
    FieldDiscoveryKind.PHONE: re.compile(r"(?<!\d)(?:\+\d{1,3}[\s-]?)?(?:\d[\s-]?){7,14}\d(?!\d)"),
    FieldDiscoveryKind.DATE: re.compile(r"(?<!\d)(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4})(?!\d)"),
    FieldDiscoveryKind.TIME: re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)"),
    FieldDiscoveryKind.MONEY: re.compile(r"(?<!\w)(?:[A-Z]{3}\s*)?[-+]?\d{1,3}(?:[,\s]\d{3})*(?:\.\d{1,3})?(?:\s*[A-Z]{3})?(?!\w)"),
    FieldDiscoveryKind.IDENTIFIER: re.compile(r"(?<![\w-])(?=[A-Z0-9-]{5,40}(?![\w-]))(?=[A-Z0-9-]*[A-Z])(?=[A-Z0-9-]*\d)[A-Z0-9]+(?:-[A-Z0-9]+)+(?![\w-])", re.I),
    FieldDiscoveryKind.NUMERIC_CODE: re.compile(r"(?<!\d)\d{5,20}(?!\d)"),
}


class FieldDiscoveryService:
    def __init__(self, options: FieldDiscoveryOptions | None = None) -> None:
        self._options: FieldDiscoveryOptions = options or FieldDiscoveryOptions()

    def discover(
        self,
        extraction: ExtractionResult,
        kinds: Iterable[FieldDiscoveryKind],
    ) -> FieldDiscoveryResult:
        requested: tuple[FieldDiscoveryKind, ...] = tuple(dict.fromkeys(kinds))
        fields: list[DiscoveredField] = []
        seen: set[tuple[FieldDiscoveryKind, str, int, str]] = set()
        diagnostics: list[str] = []
        for page in extraction.pages:
            for region in page.regions:
                if region.kind != RegionKind.TEXT or not (region.text or "").strip():
                    continue
                text: str = region.text or ""
                for kind in requested:
                    pattern = _PATTERNS.get(kind)
                    if pattern is None:
                        diagnostics.append(f"unsupported_discovery_kind:{kind}")
                        continue
                    for match in pattern.finditer(text):
                        value: str = match.group(0).strip(".,;،؛")
                        # Money is deliberately conservative: ignore a bare short number.
                        if kind == FieldDiscoveryKind.MONEY and not (
                            re.search(r"[A-Z]{3}", value, re.I) or re.search(r"[.,]", value)
                        ):
                            continue
                        key = (kind, value.casefold(), page.number, region.region_id)
                        if key in seen:
                            continue
                        seen.add(key)
                        fields.append(DiscoveredField(
                            kind=kind,
                            value=value,
                            page_number=page.number,
                            region_id=region.region_id,
                            bbox=region.bbox,
                            extraction_method=region.method,
                            confidence=region.confidence,
                            requires_review=(
                                region.requires_review
                                or region.method != ExtractionMethod.NATIVE
                                or kind in {FieldDiscoveryKind.IDENTIFIER, FieldDiscoveryKind.NUMERIC_CODE}
                            ),
                        ))
        return FieldDiscoveryResult(
            fields=tuple(fields),
            requires_review=any(field.requires_review for field in fields),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )
