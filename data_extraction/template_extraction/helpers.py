from __future__ import annotations

import math
import re

from ..shared.models import BoundingBox, ExtractionResult


def norm(value: str) -> str:
    return " ".join(value.casefold().split())


def normalize_bbox(
    bbox: BoundingBox | None,
    width: float | None,
    height: float | None,
) -> BoundingBox | None:
    if bbox is None or width is None or height is None or width <= 0 or height <= 0:
        return None
    return (
        max(0.0, min(1.0, bbox[0] / width)),
        max(0.0, min(1.0, bbox[1] / height)),
        max(0.0, min(1.0, bbox[2] / width)),
        max(0.0, min(1.0, bbox[3] / height)),
    )


def center(bbox: BoundingBox) -> tuple[float, float]:
    return ((bbox[0] + bbox[2]) / 2.0, (bbox[1] + bbox[3]) / 2.0)


def bbox_distance(first: BoundingBox, second: BoundingBox) -> float:
    return math.dist(center(first), center(second))


def safe_template_id(family: str, source_sha256: str) -> str:
    slug: str = re.sub(r"[^a-zA-Z0-9_-]+", "-", family.strip()).strip("-").lower() or "template"
    return f"{slug}-{source_sha256[:12]}"


def page_text(extraction: ExtractionResult) -> tuple[str, ...]:
    return tuple(
        "\n".join((region.text or "") for region in page.regions if (region.text or "").strip())
        for page in extraction.pages
    )


def document_kind(extraction: ExtractionResult) -> str:
    kinds: list[str] = [
        page.structured_document_kind
        for page in extraction.pages
        if page.structured_document_kind != "general"
    ]
    return kinds[0] if kinds else "general"
