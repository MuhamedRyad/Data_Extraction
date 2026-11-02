from __future__ import annotations

import io
from dataclasses import replace

import fitz
from PIL import Image

from ..shared.models import BoundingBox, DocumentInput, FileKind
from .contracts import TemplateAnchor


def image_average_hash(image: Image.Image) -> str:
    gray = image.convert("L").resize((16, 16))
    pixels = list(gray.get_flattened_data())
    average = sum(pixels) / max(1, len(pixels))
    bits = "".join("1" if value >= average else "0" for value in pixels)
    return f"{int(bits, 2):064x}"


def page_visual_hashes(
    document: DocumentInput | None,
    file_kind: FileKind,
) -> tuple[str | None, ...]:
    """Return one cheap perceptual hash per physical visual page."""

    if document is None:
        return ()
    try:
        if file_kind == FileKind.IMAGE:
            with Image.open(io.BytesIO(document.content)) as image:
                return (image_average_hash(image),)
        if file_kind != FileKind.PDF:
            return ()
        output: list[str | None] = []
        with fitz.open(stream=document.content, filetype="pdf") as pdf:
            for page in pdf:
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(0.45, 0.45), alpha=False)
                    image = Image.open(io.BytesIO(pix.tobytes("png")))
                    output.append(image_average_hash(image))
                except Exception:
                    output.append(None)
        return tuple(output)
    except Exception:
        return ()


def visual_hash(document: DocumentInput | None, file_kind: FileKind) -> str | None:
    hashes = page_visual_hashes(document, file_kind)
    return hashes[0] if hashes else None


def crop_normalized(image: Image.Image, bbox: BoundingBox) -> Image.Image | None:
    width, height = image.size
    left = max(0, min(width - 1, round(bbox[0] * width)))
    top = max(0, min(height - 1, round(bbox[1] * height)))
    right = max(left + 1, min(width, round(bbox[2] * width)))
    bottom = max(top + 1, min(height, round(bbox[3] * height)))
    if right <= left or bottom <= top:
        return None
    return image.crop((left, top, right, bottom))


def with_anchor_visual_hashes(
    document: DocumentInput,
    file_kind: FileKind,
    anchors: tuple[TemplateAnchor, ...],
) -> tuple[TemplateAnchor, ...]:
    if not anchors:
        return anchors
    needed: set[int] = {
        anchor.page_number
        for anchor in anchors
        if anchor.normalized_bbox is not None
    }
    if not needed:
        return anchors
    images: dict[int, Image.Image] = {}
    try:
        if file_kind == FileKind.IMAGE:
            with Image.open(io.BytesIO(document.content)) as source:
                images[1] = source.convert("RGB").copy()
        elif file_kind == FileKind.PDF:
            with fitz.open(stream=document.content, filetype="pdf") as pdf:
                for page_number in needed:
                    if page_number < 1 or page_number > len(pdf):
                        continue
                    pix = pdf[page_number - 1].get_pixmap(
                        matrix=fitz.Matrix(1.25, 1.25),
                        alpha=False,
                    )
                    images[page_number] = Image.open(
                        io.BytesIO(pix.tobytes("png"))
                    ).convert("RGB")
    except Exception:
        return anchors

    output: list[TemplateAnchor] = []
    for anchor in anchors:
        value: str | None = anchor.visual_hash
        page_image: Image.Image | None = images.get(anchor.page_number)
        if value is None and page_image is not None and anchor.normalized_bbox is not None:
            crop: Image.Image | None = crop_normalized(page_image, anchor.normalized_bbox)
            if crop is not None:
                value = image_average_hash(crop)
        output.append(replace(anchor, visual_hash=value))
    return tuple(output)


def anchor_visual_scores(
    document: DocumentInput,
    file_kind: FileKind,
    anchors: tuple[TemplateAnchor, ...],
) -> dict[TemplateAnchor, float]:
    expected: tuple[TemplateAnchor, ...] = tuple(
        anchor
        for anchor in anchors
        if anchor.visual_hash is not None and anchor.normalized_bbox is not None
    )
    if not expected:
        return {}
    current: tuple[TemplateAnchor, ...] = with_anchor_visual_hashes(
        document,
        file_kind,
        tuple(replace(anchor, visual_hash=None) for anchor in expected),
    )
    scores: dict[TemplateAnchor, float] = {}
    for original, observed in zip(expected, current, strict=False):
        score: float | None = visual_similarity(original.visual_hash, observed.visual_hash)
        if score is not None:
            scores[original] = score
    return scores


def visual_similarity(first: str | None, second: str | None) -> float | None:
    if first is None or second is None or len(first) != len(second):
        return None
    try:
        a: int = int(first, 16)
        b: int = int(second, 16)
    except ValueError:
        return None
    bits: int = len(first) * 4
    distance: int = (a ^ b).bit_count()
    return max(0.0, 1.0 - distance / max(1, bits))
