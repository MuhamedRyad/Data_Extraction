"""Optional image preprocessing profiles for multi-pass OCR.

No pixel in the source document is changed, only the provider input buffer.
Do not treat preprocessing variants as independent validation of OCR truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from PIL import Image, ImageFilter, ImageOps
from io import BytesIO

from ...shared.models import ProviderTextResult
from ..contracts import OCRProvider


class PreprocessMode(StrEnum):
    GRAYSCALE = "grayscale"
    AUTOCONTRAST = "autocontrast"
    SHARPEN = "sharpen"


@dataclass(frozen=True, slots=True)
class PreprocessedOCRProvider:
    provider: OCRProvider
    mode: PreprocessMode
    max_pixels: int = 8_000_000

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        with Image.open(BytesIO(image_bytes)) as original:
            if original.width * original.height > self.max_pixels:
                raise ValueError("OCR input exceeds image pixel limit")
            grayscale: Image.Image = ImageOps.grayscale(original)
            if self.mode == PreprocessMode.GRAYSCALE:
                processed = grayscale
            elif self.mode == PreprocessMode.AUTOCONTRAST:
                processed = ImageOps.autocontrast(grayscale)
            elif self.mode == PreprocessMode.SHARPEN:
                processed = grayscale.filter(ImageFilter.UnsharpMask(radius=2, percent=130))
            else:
                raise ValueError("Unsupported preprocessing mode")
            stream: BytesIO = BytesIO()
            processed.save(stream, format="PNG")
            return self.provider.recognize(stream.getvalue(), "image/png")
