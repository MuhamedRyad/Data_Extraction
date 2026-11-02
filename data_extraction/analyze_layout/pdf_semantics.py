"""Conservative PDF semantic annotations. Never drop or rewrite source text."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from hashlib import sha256
from math import ceil
import unicodedata

from ..shared.models import PageCandidates, CandidateRegion, RegionKind
from .pdf_heading_classifier import PdfBlockStyle, PdfHeadingClassifier


def _edge_key(text: str) -> str:
    normalized: str = unicodedata.normalize("NFC", text).casefold()
    return " ".join("#" if value.isdecimal() else value for value in normalized).strip()


def enrich_pdf_semantics(
    pages: tuple[PageCandidates, ...],
    styles: tuple[PdfBlockStyle, ...],
) -> tuple[PageCandidates, ...]:
    """Annotate repeated page edges and heading *candidates*; no content deletion."""
    if not pages:
        return pages
    anchors: frozenset[str] = PdfHeadingClassifier.heading_anchors(styles)
    repeated: set[tuple[str, str]] = set()
    repeated_images: set[tuple[str, str]] = set()
    if len(pages) >= 2:
        by_key: dict[tuple[str, str], set[int]] = defaultdict(set)
        for page in pages:
            if not page.height:
                continue
            for region in page.regions:
                if region.kind != RegionKind.TEXT or not region.bbox:
                    continue
                edge: str | None = (
                    "page_header_candidate" if region.bbox[1] / page.height <= .12 else
                    "page_footer_candidate" if region.bbox[3] / page.height >= .88 else None
                )
                if edge:
                    by_key[(_edge_key(region.text or ""), edge)].add(page.number)
        threshold: int = max(2, ceil(len(pages) * .5))
        repeated = {key for key, sources in by_key.items() if key[0] and len(sources) >= threshold}

        image_sources: dict[tuple[str, str], set[int]] = defaultdict(set)
        for page in pages:
            if not page.height or not page.width:
                continue
            page_area: float = max(1.0, page.width * page.height)
            for region in page.regions:
                if region.kind != RegionKind.IMAGE or region.image_bytes is None or region.bbox is None:
                    continue
                area_ratio: float = max(0.0, region.bbox[2] - region.bbox[0]) * max(0.0, region.bbox[3] - region.bbox[1]) / page_area
                if area_ratio > 0.30:
                    continue
                edge: str | None = (
                    "top" if region.bbox[1] / page.height <= .20 else
                    "bottom" if region.bbox[3] / page.height >= .80 else None
                )
                if edge is None:
                    continue
                fingerprint: str = sha256(region.image_bytes).hexdigest()
                image_sources[(fingerprint, edge)].add(page.number)
        repeated_images = {key for key, sources in image_sources.items() if len(sources) >= threshold}
    output: list[PageCandidates] = []
    for page in pages:
        annotations: list[CandidateRegion] = []
        warnings: list[str] = list(page.warnings)
        for region in page.regions:
            role: str | None = region.semantic_role
            if region.kind == RegionKind.IMAGE and region.image_bytes is not None and region.bbox and page.height:
                edge: str | None = (
                    "top" if region.bbox[1] / page.height <= .20 else
                    "bottom" if region.bbox[3] / page.height >= .80 else None
                )
                fingerprint: str = sha256(region.image_bytes).hexdigest()
                if edge is not None and (fingerprint, edge) in repeated_images:
                    role = "repeated_navigation_candidate"
                    warnings.append("pdf_repeated_navigation_inferred_review")
            if region.kind == RegionKind.TEXT and region.bbox and page.height:
                if (region.bbox[1] / page.height <= .12
                    and (_edge_key(region.text or ""), "page_header_candidate") in repeated):
                    role = "page_header_candidate"
                elif (region.bbox[3] / page.height >= .88
                      and (_edge_key(region.text or ""), "page_footer_candidate") in repeated):
                    role = "page_footer_candidate"
            if role:
                warnings.append("pdf_page_furniture_inferred_review")
            elif role is None and region.region_id in anchors:
                role = "heading_candidate"
                warnings.append("pdf_heading_inferred_review")
            annotations.append(replace(region, semantic_role=role) if role else region)
        output.append(replace(page, regions=tuple(annotations),
                              warnings=tuple(dict.fromkeys(warnings))))
    return tuple(output)
