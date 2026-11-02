from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import sys
from pathlib import Path

from data_extraction import (DataExtractionService, DocumentInput,
                             ExtractionOptions, ImageRoute, TesseractOCRProvider,
                             DoclingTableProvider, UnstructuredTableProvider)


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser(description="Inspect/extract document")
    parser.add_argument("file", type=Path)
    parser.add_argument("--action", choices=["inspect", "extract"], default="inspect")
    parser.add_argument("--ocr", action="store_true", help="Enable local Tesseract OCR on images")
    parser.add_argument("--image-route", choices=[x.value for x in ImageRoute], default="auto")
    parser.add_argument("--structure-provider", choices=("none", "docling", "unstructured"),
                        default="none", help="Opt-in local structure model; can load/download weights")
    args: argparse.Namespace = parser.parse_args()
    file_path: Path = args.file
    request: DocumentInput = DocumentInput(file_path.name, file_path.read_bytes(),
                                           f"cli:{file_path.name}")
    service: DataExtractionService = DataExtractionService(
        options=ExtractionOptions(image_route=ImageRoute(args.image_route)),
        ocr_provider=TesseractOCRProvider() if args.ocr else None,
        structure_provider=(DoclingTableProvider() if args.structure_provider == "docling" else
                            UnstructuredTableProvider() if args.structure_provider == "unstructured" else None),
    )
    # Some document libraries print informational messages to stdout. The CLI
    # reserves stdout for a single JSON document; redirect diagnostics to stderr.
    with redirect_stdout(sys.stderr):
        result = service.inspect(request) if args.action == "inspect" else service.extract(request)
    print(service.to_json(result))


if __name__ == "__main__":
    main()
