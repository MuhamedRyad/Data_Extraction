from __future__ import annotations

import io
import json
import time
from dataclasses import dataclass, field
from threading import Lock

import fitz
import httpx
from PIL import Image

from data_extraction.dispatch_extraction.extract_image import ExtractImageAction
from data_extraction.dispatch_extraction.extract_table import ExtractTableAction
from data_extraction.dispatch_extraction.extract_text import ExtractTextAction
from data_extraction.dispatch_extraction.service import DispatchRegionsAction
from data_extraction.inspect_document.service import InspectDocumentService
from data_extraction.integrations.template_generation import (
    OpenAIResponsesTemplateOptions,
    OpenAIResponsesTemplateProvider,
)
from data_extraction import DataExtractionService
from data_extraction.options import ExtractionOptions
from data_extraction.parallel_execution import BoundedCallGate
from data_extraction.shared.models import (
    CandidateRegion,
    DocumentInput,
    FileKind,
    ImageRoute,
    InspectedDocument,
    PageCandidates,
    PageKind,
    ProviderTextResult,
    RegionKind,
    SourceSurface,
)
from data_extraction.template_extraction.contracts import TemplateGenerationContext


def _png() -> bytes:
    image: Image.Image = Image.new("RGB", (80, 40), "white")
    stream: io.BytesIO = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _document(page_count: int) -> InspectedDocument:
    image_bytes: bytes = _png()
    pages: tuple[PageCandidates, ...] = tuple(
        PageCandidates(
            number=page_number,
            kind=PageKind.PHYSICAL_PAGE,
            label=f"Page {page_number}",
            regions=(CandidateRegion(
                region_id=f"p{page_number}:image:1",
                kind=RegionKind.IMAGE,
                bbox=(0.0, 0.0, 80.0, 40.0),
                image_bytes=image_bytes,
                image_mime_type="image/png",
                image_role="rendered_page",
                source_surface=SourceSurface.PDF_PAGE_RENDER,
            ),),
            width=80.0,
            height=40.0,
        )
        for page_number in range(1, page_count + 1)
    )
    return InspectedDocument(
        source_id="parallel:test",
        file_name="parallel.pdf",
        sha256="abc123",
        file_kind=FileKind.PDF,
        pages=pages,
    )


@dataclass
class _SlowOCR:
    delay_seconds: float = 0.06
    calls: int = 0
    active: int = 0
    max_active: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            return ProviderTextResult("PAGE TEXT", 0.99, provider_name="slow-ocr")
        finally:
            with self._lock:
                self.active -= 1


@dataclass
class _SlowVision:
    delay_seconds: float = 0.04
    calls: int = 0
    active: int = 0
    max_active: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def analyze(self, image_bytes: bytes, mime_type: str, role: str) -> ProviderTextResult:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_seconds)
            return ProviderTextResult("VISION TEXT", 0.95, provider_name="slow-vision")
        finally:
            with self._lock:
                self.active -= 1


def _router(ocr: _SlowOCR | None = None, vision: _SlowVision | None = None,
            *, ocr_limit: int = 4, vision_limit: int = 2) -> DispatchRegionsAction:
    return DispatchRegionsAction(
        ExtractTextAction(),
        ExtractTableAction(),
        ExtractImageAction(
            ocr,
            vision,
            ocr_gate=BoundedCallGate(ocr_limit),
            vision_gate=BoundedCallGate(vision_limit),
        ),
    )


def test_parallel_pages_preserve_exact_output_order_and_contract() -> None:
    document: InspectedDocument = _document(6)
    manifest = InspectDocumentService.build_manifest(document)
    sequential_ocr: _SlowOCR = _SlowOCR(delay_seconds=0.01)
    parallel_ocr: _SlowOCR = _SlowOCR(delay_seconds=0.01)
    sequential_options = ExtractionOptions(
        image_route=ImageRoute.OCR_ONLY,
        enable_structured_document_analysis=False,
        enable_parallel_execution=False,
    )
    parallel_options = ExtractionOptions(
        image_route=ImageRoute.OCR_ONLY,
        enable_structured_document_analysis=False,
        enable_parallel_execution=True,
        max_parallel_page_groups=4,
        max_parallel_ocr_calls=2,
    )
    sequential = _router(sequential_ocr, ocr_limit=1).execute(
        document, manifest, sequential_options
    )
    parallel = _router(parallel_ocr, ocr_limit=2).execute(
        document, manifest, parallel_options
    )
    assert parallel == sequential
    assert parallel.document_order_region_ids == tuple(
        f"p{page}:image:1" for page in range(1, 7)
    )
    assert tuple(
        region.position.document_order_index
        for page in parallel.pages
        for region in page.regions
        if region.position is not None
    ) == tuple(range(1, 7))


def test_parallel_pages_are_faster_and_ocr_gate_is_bounded() -> None:
    document: InspectedDocument = _document(8)
    manifest = InspectDocumentService.build_manifest(document)
    sequential_ocr: _SlowOCR = _SlowOCR(delay_seconds=0.06)
    parallel_ocr: _SlowOCR = _SlowOCR(delay_seconds=0.06)
    sequential_options = ExtractionOptions(
        image_route=ImageRoute.OCR_ONLY,
        enable_structured_document_analysis=False,
        enable_parallel_execution=False,
    )
    parallel_options = ExtractionOptions(
        image_route=ImageRoute.OCR_ONLY,
        enable_structured_document_analysis=False,
        enable_parallel_execution=True,
        max_parallel_page_groups=8,
        max_parallel_ocr_calls=2,
    )
    start: float = time.perf_counter()
    _router(sequential_ocr, ocr_limit=1).execute(document, manifest, sequential_options)
    sequential_seconds: float = time.perf_counter() - start
    start = time.perf_counter()
    _router(parallel_ocr, ocr_limit=2).execute(document, manifest, parallel_options)
    parallel_seconds: float = time.perf_counter() - start

    assert sequential_ocr.calls == parallel_ocr.calls == 8
    assert parallel_ocr.max_active == 2
    assert parallel_seconds < sequential_seconds * 0.72


def test_parallel_vision_never_exceeds_global_budget_or_concurrency_gate() -> None:
    document: InspectedDocument = _document(10)
    manifest = InspectDocumentService.build_manifest(document)
    vision: _SlowVision = _SlowVision()
    options = ExtractionOptions(
        image_route=ImageRoute.VISION_ONLY,
        enable_structured_document_analysis=False,
        enable_parallel_execution=True,
        max_parallel_page_groups=10,
        max_parallel_ai_calls=2,
        max_vision_calls=3,
    )
    result = _router(vision=vision, vision_limit=2).execute(document, manifest, options)
    assert vision.calls == 3
    assert result.total_vision_calls == 3
    assert vision.max_active <= 2
    unresolved: int = sum(
        1 for page in result.pages for region in page.regions
        if region.method.value == "unresolved"
    )
    assert unresolved == 7


def test_openai_page_batches_run_bounded_parallel_and_merge_in_page_order() -> None:
    def run(parallel_enabled: bool) -> tuple[float, int, int, tuple[str, ...]]:
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
                user_content = payload["input"][1]["content"]
                text: str = "\n".join(
                    str(item.get("text", ""))
                    for item in user_content
                    if isinstance(item, dict)
                )
                pages: list[int] = [
                    int(line.removeprefix("VISUAL PAGE "))
                    for line in text.splitlines()
                    if line.startswith("VISUAL PAGE ")
                ]
                proposal_payload = {
                    "document_kind": "general",
                    "fields": [],
                    "tables": [],
                    "anchors": [],
                    "diagnostics": [f"batch:{','.join(str(page) for page in pages)}"],
                }
                return httpx.Response(
                    200,
                    json={
                        "status": "completed",
                        "output_text": json.dumps(proposal_payload),
                    },
                    request=request,
                )
            finally:
                with lock:
                    active -= 1

        pdf: fitz.Document = fitz.open()
        for page_number in range(1, 13):
            page: fitz.Page = pdf.new_page(width=300, height=400)
            page.insert_text((40, 80), f"PAGE {page_number}")
        document = DocumentInput("twelve.pdf", pdf.tobytes(), "twelve")
        pdf.close()
        context = TemplateGenerationContext(
            "twelve",
            "twelve",
            "sha",
            (),
            12,
            "general",
            tuple("" for _ in range(12)),
            tuple((300.0, 400.0) for _ in range(12)),
        )
        provider = OpenAIResponsesTemplateProvider(
            api_key="test",
            options=OpenAIResponsesTemplateOptions(
                max_pages_per_batch=3,
                max_ai_batches_per_document=4,
                parallel_ai_batches_enabled=parallel_enabled,
                max_parallel_ai_batches=2,
            ),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        start: float = time.perf_counter()
        proposal = provider.generate_template(document, context)
        elapsed: float = time.perf_counter() - start
        batch_diagnostics: tuple[str, ...] = tuple(
            item for item in proposal.diagnostics if item.startswith("batch:")
        )
        return elapsed, calls, max_active, batch_diagnostics

    sequential_seconds, sequential_calls, sequential_active, sequential_diagnostics = run(False)
    parallel_seconds, parallel_calls, parallel_active, parallel_diagnostics = run(True)

    assert sequential_calls == parallel_calls == 4
    assert sequential_active == 1
    assert parallel_active == 2
    assert parallel_seconds < sequential_seconds * 0.80
    expected: tuple[str, ...] = (
        "batch:1,2,3",
        "batch:4,5,6",
        "batch:7,8,9",
        "batch:10,11,12",
    )
    assert sequential_diagnostics == parallel_diagnostics == expected


def test_public_service_wires_parallel_page_and_ocr_limits() -> None:
    image: Image.Image = Image.new("RGB", (300, 120), "white")
    stream: io.BytesIO = io.BytesIO()
    image.save(stream, format="PNG")
    pdf: fitz.Document = fitz.open()
    for _ in range(6):
        page: fitz.Page = pdf.new_page(width=300, height=120)
        page.insert_image(page.rect, stream=stream.getvalue())
    source = DocumentInput("six-scans.pdf", pdf.tobytes(), "six-scans")
    pdf.close()
    ocr: _SlowOCR = _SlowOCR(delay_seconds=0.04)
    service = DataExtractionService(
        ExtractionOptions(
            image_route=ImageRoute.OCR_ONLY,
            enable_structured_document_analysis=False,
            enable_parallel_execution=True,
            max_parallel_page_groups=6,
            max_parallel_ocr_calls=2,
        ),
        ocr_provider=ocr,
    )
    result = service.extract(source)
    assert len(result.pages) == 6
    assert ocr.calls == 6
    assert ocr.max_active == 2
    assert tuple(page.number for page in result.pages) == (1, 2, 3, 4, 5, 6)
