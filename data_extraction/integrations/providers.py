"""Backward-compatible OCR/Vision provider imports."""
from ..providers.contracts import OCRProvider, VisionProvider
from ..providers.ocr.tesseract import TesseractOCRProvider

__all__ = ["OCRProvider", "VisionProvider", "TesseractOCRProvider"]
