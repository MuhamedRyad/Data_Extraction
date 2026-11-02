"""Run inspection and optional OCR against your own source; no model calls."""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from data_extraction import DataExtractionService, DocumentInput, TesseractOCRProvider
from data_extraction.models import ExtractionMethod


def main() -> None:
    parser: argparse.ArgumentParser = argparse.ArgumentParser()
    parser.add_argument("file", type=Path)
    parser.add_argument("--ocr", action="store_true")
    args: argparse.Namespace = parser.parse_args()
    path: Path = args.file
    request: DocumentInput = DocumentInput(path.name, path.read_bytes(), "benchmark:source")
    service: DataExtractionService = DataExtractionService(
        ocr_provider=TesseractOCRProvider() if args.ocr else None
    )
    result = service.extract(request)
    print("File:", result.manifest.file_kind, "Pages:", len(result.pages),
          "Ready:", result.ready_for_publication, "Vision calls:", result.total_vision_calls)
    for page in result.pages:
        counts: Counter[str] = Counter(region.kind.value for region in page.regions)
        methods: Counter[str] = Counter(region.method.value for region in page.regions)
        reviews: list[str] = [reason for reg in page.regions for reason in reg.review_reasons]
        print(f"  {page.kind.value} {page.label}: {dict(counts)} | routes {dict(methods)}"
              f" | review={page.requires_review} {reviews}")


if __name__ == "__main__":
    main()
