from __future__ import annotations
from data_extraction.providers.docling.adapter import DoclingTableProvider
from data_extraction.providers.unstructured.adapter import UnstructuredTableProvider
from data_extraction.providers.structure_contracts import StructuredTablePrediction
from data_extraction.providers.ocr.tesseract import TesseractOCRProvider
from data_extraction.providers.contracts import OCRProvider, VisionProvider

def test_legacy_docling_alias() -> None:
    from data_extraction.structure.docling_provider import DoclingTableProvider as Old
    assert DoclingTableProvider is Old

def test_legacy_unstructured_alias() -> None:
    from data_extraction.structure.unstructured_provider import UnstructuredTableProvider as Old
    assert UnstructuredTableProvider is Old

def test_legacy_provider_protocols_and_tesseract() -> None:
    from data_extraction.integrations.providers import OCRProvider as OldOCR, VisionProvider as OldVision, TesseractOCRProvider as OldTesseract
    assert OCRProvider is OldOCR and VisionProvider is OldVision and TesseractOCRProvider is OldTesseract

def test_structured_contract_alias() -> None:
    from data_extraction.structure.contracts import StructuredTablePrediction as Old
    assert StructuredTablePrediction is Old
