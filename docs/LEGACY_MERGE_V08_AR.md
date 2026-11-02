# Generic Data Extraction v0.8.0 — Legacy capability integration

**Date:** 2026-10-31  
**Base:** v0.7.0, unchanged original archive.  
**Compared legacy:** `document_ingestion.zip`, `document_review.zip`.  
**Boundary:** extraction only. No chunking, RAG, knowledge approval, or agent workflow.

## Summary of implemented changes

| Area | What changed | Evidence and behavior |
|---|---|---|
| P0: ready gate | Central `warning_policy.py` with `INCOMPLETE`, `REVIEW_REQUIRED`, and informational severity. A table detection crash and unsupported content no longer yield `ready_for_publication=True`. | New integration tests reproduce table detector failure with PyMuPDF; original tests retained. |
| P0: vector PDF | `pdf_vectors.py` identifies nontrivial filled/chart-like vector drawings outside native tables; generates review-only rendered candidate with native text masked. Does **not** invent chart text. | New vector-with-text test detects candidate and makes result not ready; ruled native table is not misclassified. |
| P0: DOCX unsupported | Identify presence of unsupported `w:customXml` and mark incomplete. Existing unsupported OOXML warnings now block readiness rather than hiding. | Synthetic customXML test. Unsupported content is still not extracted automatically. |
| P1: DOCX heading style | Map heading style and inherited outline styles to `RegionRelations.region_role` (`heading_1`...`heading_9`); keep original text and provenance. | Test with real generated DOCX `Heading 2`. |
| P1: PDF heading/page furniture | Adapted domain-independent PDF typography classifier and repeated top/bottom page text identification, as **review-only candidate roles** (not asserted truth). | Test 3-page repeated header; no text dropped. |
| P1: Arabic text | Ported legacy geometry word-run resolver into `analyze_layout/pdf_native_reading_order.py`. Added `DataExtractionService.inspect_pdf_rtl_proposals(request)`; return only proposals with identical word token multiset; don't change source text. | Unit test for mixed-direction candidate and raw preservation. Real corpus accuracy remains unproven. |
| P1: OCR evidence | `assess_ocr_quality()` reports character count, confidence, SHA-256 text fingerprint, digit and mixed-script indicators. Does **not** reject a short but valid page. Mixed RTL/LTR OCR is flagged for review. | Short Arabic test and mixed RTL-LTR test. |
| P2: OCR Cache | `CachedOCRProvider`: opt-in, scoped by explicit tenant scope and provider signature, hashed image bytes and MIME; validated reads and atomic writes; on cache write failure returns actual OCR success. | Scoped cache/invalid-cache tests. This is per-image restart reuse, **not** a full job checkpoint. |
| P2: Multipass | `MultiPassOCRProvider` requires complete agreement across profiles; detects differing outputs and marks OCR unresolved. `compare()` retains all attempts and optional `comparison_sink` receives them for an external audit store. | Previous legacy counterexample reproduced: extra account line no longer silently discarded in a chosen output. |
| P2: Raster OCR tables | Conservative, optional OCR word-geometry candidate table reconstruction (`table_geometry.py`), only enabled on Tesseract when requested and grid is detected. Any OCR table remains review-required. | Three-row grid synthetic test; no blind domain/header rules. |
| P2: EasyOCR/preprocessing | Optional lazy EasyOCR provider (`download_enabled=False`, no weights fetched); grayscale, autocontrast, sharpen provider wrappers usable as multipass profiles. | Mocked EasyOCR and preprocessing unit tests; no live EasyOCR benchmark. |

## Compatibility

- Existing `DataExtractionService.inspect()`/`.extract()` methods, existing input/output fields, and `extraction.v0.6` schema version retained; no removal of fields.
- Internal `CandidateRegion` gains optional semantic-role and read-only RTL proposal fields; `ExtractionOptions` adds optional `analyze_pdf_rtl_geometry=False` (default unchanged).
- Release packaging version is `0.8.0`; output schema stays `extraction.v0.6` intentionally.
- New candidate roles use **existing** `RegionRelations.region_role` field; no new mandatory JSON fields.
- Explicit new method `inspect_pdf_rtl_proposals()` returns optional annotations separately from legacy JSON.

## Safe usage examples

```python
from pathlib import Path
from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.providers.ocr.tesseract import TesseractOCRProvider
from data_extraction.providers.ocr.cache import CachedOCRProvider
from data_extraction.providers.ocr.multi_pass import MultiPassOCRProvider
from data_extraction.providers.ocr.preprocessing import PreprocessedOCRProvider, PreprocessMode

base = TesseractOCRProvider(language="ara+eng", reconstruct_raster_tables=True)
# Preprocessing profiles are alternative recognitions, not independent ground truth.
ensemble = MultiPassOCRProvider((
    base,
    PreprocessedOCRProvider(base, PreprocessMode.AUTOCONTRAST),
))
cache = CachedOCRProvider(ensemble, Path("/trusted-private-cache"),
                          provider_signature="tesseract-ara-eng-configuration-v1",
                          scope="tenant-isolated-example")
service = DataExtractionService(ExtractionOptions(), ocr_provider=cache)
request = DocumentInput("sample.pdf", Path("sample.pdf").read_bytes(), "source-1")
result = service.extract(request)
print(result.ready_for_publication, result.diagnostics)
# For a separate, non-destructive PDF RTL analysis:
proposals = service.inspect_pdf_rtl_proposals(request)
```

**Warning:** strict consensus is deliberately conservative; enabling it for different image preprocessing profiles may lower throughput/coverage. OCR confidence, even when high, is not proof of correctness. Cache directory must be access controlled; cached recognized text is stored in plaintext, so disable for sensitive documents or add encryption at rest externally. Choose a provider signature that includes model versions and preprocessing settings.

## Excluded or still unproven

1. Automatic changing/correcting account numbers, dates, monetary fields, invoice codes: intentionally NOT copied because generic extraction must not invent values.
2. Knowledge-specific `document_review` workflows and chunking/reindexing: intentionally NOT copied.
3. Full durable OCR preprocessing run `checkpoint/resume` across workers: **NOT implemented**; OCR cache reduces repeated per-image work only.
4. Image/OCR word text to table conversion is heuristic, opt-in, not validated against a real bilingual table corpus.
5. EasyOCR actual model inference and OCR CER/WER accuracy are **not benchmarked** in this environment; tests use mocks for optional modules.
6. PDF vectors: the significance heuristic detects the synthetic filled chart used in re-audit, not all plots (e.g. thin line-only charts, complex PDFs). False positives/negatives still possible.
7. PDF headings and reading-order suggestions are metadata requiring review; they are not ground truth. RTL proposal intentionally doesn't overwrite native text.
8. DOCX custom XML is flagged as incomplete, not losslessly extracted. A production validation corpus remains necessary.
9. Caching is best-effort per image; no distributed locking, eviction/TTL or encryption provided.

## Tests

Run `python -m pytest -q` in the source folder. Test report: `benchmarks/pytest_v0_8.xml` and `benchmarks/V08_TEST_REPORT.json`. The included 263 original v0.7 tests plus the new checks test behavior but do **not** prove OCR quality relative to the old system.
