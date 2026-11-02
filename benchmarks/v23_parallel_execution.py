from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

import fitz
import httpx
from PIL import Image, ImageDraw

from data_extraction import (
    DataExtractionService,
    DocumentInput,
    ExtractionOptions,
    ImageRoute,
    ProviderTextResult,
    TesseractOCRProvider,
)
from data_extraction.integrations.template_generation import (
    OpenAIResponsesTemplateOptions,
    OpenAIResponsesTemplateProvider,
)
from data_extraction.template_extraction.contracts import TemplateGenerationContext


@dataclass
class SlowOCR:
    delay_seconds: float
    calls: int = 0
    active: int = 0
    max_active: int = 0
    lock: Lock = field(default_factory=Lock, repr=False)

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        with self.lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            return ProviderTextResult("PAGE TEXT 123", 0.99, provider_name="slow-ocr")
        finally:
            with self.lock:
                self.active -= 1


def scanned_pdf(page_count: int) -> bytes:
    pdf: fitz.Document = fitz.open()
    for page_number in range(1, page_count + 1):
        image: Image.Image = Image.new("RGB", (1000, 420), "white")
        draw: ImageDraw.ImageDraw = ImageDraw.Draw(image)
        draw.text((70, 90), f"INVOICE PAGE {page_number}", fill="black")
        draw.text((70, 190), f"TOTAL {1000 + page_number}.50", fill="black")
        stream: io.BytesIO = io.BytesIO()
        image.save(stream, format="PNG")
        page: fitz.Page = pdf.new_page(width=500, height=210)
        page.insert_image(page.rect, stream=stream.getvalue())
    output: bytes = pdf.tobytes()
    pdf.close()
    return output


def run_slow_ocr(source: DocumentInput, parallel: bool) -> tuple[float, SlowOCR, object]:
    provider = SlowOCR(0.06)
    service = DataExtractionService(
        ExtractionOptions(
            image_route=ImageRoute.OCR_ONLY,
            enable_structured_document_analysis=False,
            enable_parallel_execution=parallel,
            max_parallel_page_groups=8,
            max_parallel_ocr_calls=2,
            detect_raster_table_grids=False,
        ),
        ocr_provider=provider,
    )
    start: float = time.perf_counter()
    result = service.extract(source)
    return time.perf_counter() - start, provider, result


def run_real_tesseract(source: DocumentInput, parallel: bool) -> tuple[float, object]:
    service = DataExtractionService(
        ExtractionOptions(
            image_route=ImageRoute.OCR_ONLY,
            enable_structured_document_analysis=False,
            enable_parallel_execution=parallel,
            max_parallel_page_groups=4,
            max_parallel_ocr_calls=2,
            detect_raster_table_grids=False,
        ),
        ocr_provider=TesseractOCRProvider(language="eng"),
    )
    start: float = time.perf_counter()
    result = service.extract(source)
    return time.perf_counter() - start, result


def run_mock_ai(parallel: bool) -> tuple[float, int, int, tuple[str, ...]]:
    active: int = 0
    max_active: int = 0
    calls: int = 0
    lock: Lock = Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, max_active, calls
        payload = json.loads(request.content.decode("utf-8"))
        with lock:
            calls += 1
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.12)
            content = payload["input"][1]["content"]
            text: str = "\n".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
            pages: list[int] = [
                int(line.removeprefix("VISUAL PAGE "))
                for line in text.splitlines()
                if line.startswith("VISUAL PAGE ")
            ]
            proposal = {
                "document_kind": "general",
                "fields": [],
                "tables": [],
                "anchors": [],
                "diagnostics": [f"batch:{','.join(str(page) for page in pages)}"],
            }
            return httpx.Response(
                200,
                json={"status": "completed", "output_text": json.dumps(proposal)},
                request=request,
            )
        finally:
            with lock:
                active -= 1

    pdf: fitz.Document = fitz.open()
    for page_number in range(1, 13):
        page: fitz.Page = pdf.new_page(width=300, height=400)
        page.insert_text((40, 80), f"PAGE {page_number}")
    document = DocumentInput("twelve.pdf", pdf.tobytes(), "v23:ai")
    pdf.close()
    context = TemplateGenerationContext(
        "v23", "v23", "sha", (), 12, "general",
        tuple("" for _ in range(12)),
        tuple((300.0, 400.0) for _ in range(12)),
    )
    provider = OpenAIResponsesTemplateProvider(
        api_key="test",
        options=OpenAIResponsesTemplateOptions(
            max_pages_per_batch=3,
            max_ai_batches_per_document=4,
            parallel_ai_batches_enabled=parallel,
            max_parallel_ai_batches=2,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    start: float = time.perf_counter()
    proposal = provider.generate_template(document, context)
    elapsed: float = time.perf_counter() - start
    diagnostics: tuple[str, ...] = tuple(
        item for item in proposal.diagnostics if item.startswith("batch:")
    )
    return elapsed, calls, max_active, diagnostics


def main() -> None:
    slow_source = DocumentInput("eight.pdf", scanned_pdf(8), "v23:slow")
    tesseract_source = DocumentInput("six.pdf", scanned_pdf(6), "v23:tesseract")
    slow_seq_seconds, slow_seq_provider, slow_seq_result = run_slow_ocr(slow_source, False)
    slow_par_seconds, slow_par_provider, slow_par_result = run_slow_ocr(slow_source, True)
    tesseract_seq_seconds, tesseract_seq_result = run_real_tesseract(tesseract_source, False)
    tesseract_par_seconds, tesseract_par_result = run_real_tesseract(tesseract_source, True)
    ai_seq_seconds, ai_seq_calls, ai_seq_active, ai_seq_diag = run_mock_ai(False)
    ai_par_seconds, ai_par_calls, ai_par_active, ai_par_diag = run_mock_ai(True)

    payload = {
        "version": "0.23.0",
        "slow_ocr": {
            "pages": 8,
            "max_parallel_ocr_calls": 2,
            "sequential_seconds": slow_seq_seconds,
            "parallel_seconds": slow_par_seconds,
            "speedup": slow_seq_seconds / slow_par_seconds,
            "parallel_max_active": slow_par_provider.max_active,
            "same_output": slow_seq_result.pages == slow_par_result.pages,
            "same_document_order": (
                slow_seq_result.document_order_region_ids
                == slow_par_result.document_order_region_ids
            ),
        },
        "real_tesseract": {
            "pages": 6,
            "max_parallel_ocr_calls": 2,
            "sequential_seconds": tesseract_seq_seconds,
            "parallel_seconds": tesseract_par_seconds,
            "speedup": tesseract_seq_seconds / tesseract_par_seconds,
            "same_output": tesseract_seq_result.pages == tesseract_par_result.pages,
            "same_document_order": (
                tesseract_seq_result.document_order_region_ids
                == tesseract_par_result.document_order_region_ids
            ),
        },
        "mock_ai_batches": {
            "pages": 12,
            "pages_per_batch": 3,
            "batches": 4,
            "max_parallel_ai_batches": 2,
            "sequential_seconds": ai_seq_seconds,
            "parallel_seconds": ai_par_seconds,
            "speedup": ai_seq_seconds / ai_par_seconds,
            "sequential_calls": ai_seq_calls,
            "parallel_calls": ai_par_calls,
            "sequential_max_active": ai_seq_active,
            "parallel_max_active": ai_par_active,
            "same_batch_merge_order": ai_seq_diag == ai_par_diag,
            "batch_merge_order": ai_par_diag,
        },
    }
    output = Path(__file__).with_name("V23_PARALLEL_EXECUTION_BENCHMARK.json")
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
