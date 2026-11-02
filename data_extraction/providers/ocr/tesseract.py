"""Optional Tesseract OCR adapter; import does not load OCR weights."""
from __future__ import annotations

import io
import unicodedata
from collections import defaultdict
from dataclasses import dataclass

from PIL import Image

from ...shared.models import ProviderTextResult
from ..contracts import OCRTextRegion
from .table_geometry import OCRWord, reconstruct_candidate_table
from ..native.image_layout import is_raster_grid_table


@dataclass(frozen=True, slots=True)
class TesseractOCRProvider:
    language: str = "ara+eng"
    reconstruct_raster_tables: bool = False

    def _read_data(self, image_bytes: bytes) -> dict:
        import pytesseract
        from pytesseract import Output

        with Image.open(io.BytesIO(image_bytes)) as image:
            return pytesseract.image_to_data(image, lang=self.language, output_type=Output.DICT)

    def recognize_tokens(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        data: dict = self._read_data(image_bytes)
        output: list[OCRTextRegion] = []
        texts = data.get("text", ())
        for index, word in enumerate(texts):
            if not isinstance(word, str) or not word.strip():
                continue
            try:
                left: float = float(data["left"][index])
                top: float = float(data["top"][index])
                width: float = float(data["width"][index])
                height: float = float(data["height"][index])
                raw_confidence: float = float(data["conf"][index])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if width <= 0 or height <= 0:
                continue
            confidence: float | None = raw_confidence / 100.0 if raw_confidence >= 0 else None
            output.append(OCRTextRegion(
                word.strip(), (left, top, left + width, top + height), confidence
            ))
        return tuple(output)

    def recognize_regions(self, image_bytes: bytes, mime_type: str) -> tuple[OCRTextRegion, ...]:
        data: dict = self._read_data(image_bytes)
        groups: dict[tuple[int, int, int], list[tuple[float, float, float, float, str, float | None]]] = defaultdict(list)
        texts = data.get("text", ())
        for index, word in enumerate(texts):
            if not isinstance(word, str) or not word.strip():
                continue
            try:
                left: float = float(data["left"][index])
                top: float = float(data["top"][index])
                width: float = float(data["width"][index])
                height: float = float(data["height"][index])
                raw_confidence: float = float(data["conf"][index])
                block_number: int = int(data.get("block_num", [0] * len(texts))[index])
                paragraph_number: int = int(data.get("par_num", [0] * len(texts))[index])
                line_number: int = int(data.get("line_num", [index] * len(texts))[index])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            confidence: float | None = raw_confidence / 100.0 if raw_confidence >= 0 else None
            groups[(block_number, paragraph_number, line_number)].append(
                (left, top, left + width, top + height, word.strip(), confidence)
            )
        regions: list[OCRTextRegion] = []
        for group in groups.values():
            rtl_chars: int = sum(
                unicodedata.bidirectional(character) in ("R", "AL")
                for item in group for character in item[4]
            )
            ltr_chars: int = sum(
                unicodedata.bidirectional(character) == "L"
                for item in group for character in item[4]
            )
            ordered = sorted(group, key=lambda item: item[0], reverse=rtl_chars > ltr_chars)
            known: list[tuple[float, int]] = [
                (float(item[5]), max(1, len(item[4])))
                for item in ordered if item[5] is not None
            ]
            confidence: float | None = (
                sum(score * weight for score, weight in known) / sum(weight for _, weight in known)
                if known else None
            )
            regions.append(OCRTextRegion(
                text=" ".join(item[4] for item in ordered),
                bbox=(
                    min(item[0] for item in group),
                    min(item[1] for item in group),
                    max(item[2] for item in group),
                    max(item[3] for item in group),
                ),
                confidence=confidence,
            ))
        regions.sort(key=lambda region: ((region.bbox[1] + region.bbox[3]) / 2.0, region.bbox[0]))
        return tuple(regions)


    def recognize_cell(self, image_bytes: bytes, mime_type: str, value_kind: str) -> ProviderTextResult:
        """Read one already-segmented table cell with a layout hint.

        This is intentionally optional. The template core remains vendor-neutral;
        Tesseract can use a tighter page-segmentation mode once row/column geometry
        has already isolated a single cell.
        """
        import pytesseract

        kind: str = value_kind.strip().casefold()
        config: str = "--psm 7"
        if kind in {"integer", "quantity", "count"}:
            config += " -c tessedit_char_whitelist=0123456789"
        elif kind in {"decimal", "amount", "currency", "number"}:
            config += " -c tessedit_char_whitelist=0123456789.,-+()"
        with Image.open(io.BytesIO(image_bytes)) as image:
            normalized: Image.Image = image.convert("L")
            if max(normalized.size) < 500:
                normalized = normalized.resize(
                    (max(1, normalized.width * 3), max(1, normalized.height * 3)),
                    Image.Resampling.LANCZOS,
                )
            text: str = pytesseract.image_to_string(
                normalized, lang=self.language, config=config
            ).strip()
        return ProviderTextResult(
            text=text, confidence=None, provider_name="tesseract-cell"
        )

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        import pytesseract

        with Image.open(io.BytesIO(image_bytes)) as image:
            text: str = pytesseract.image_to_string(image, lang=self.language).strip()
        regions: tuple[OCRTextRegion, ...] = self.recognize_regions(image_bytes, mime_type)
        known_confidences: list[float] = [
            float(region.confidence) for region in regions if region.confidence is not None
        ]
        average: float | None = (
            sum(known_confidences) / len(known_confidences)
            if known_confidences else None
        )
        proposed_table = None
        if self.reconstruct_raster_tables and is_raster_grid_table(image_bytes):
            detected: list[OCRWord] = [
                OCRWord(region.text, *region.bbox, region.confidence)
                for region in self.recognize_tokens(image_bytes, mime_type) if region.text
            ]
            proposed_table = reconstruct_candidate_table(tuple(detected))
        return ProviderTextResult(
            text=text,
            confidence=average,
            structured_table=proposed_table,
            provider_name="tesseract",
        )
