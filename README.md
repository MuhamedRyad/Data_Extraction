# Generic Data Extraction Feature v0.24.0 — Engineering Standard Refactor

v0.24 is a behavior-preserving engineering refactor over v0.23. Extraction algorithms, template semantics, multi-page safety, OCR/AI routing, and output contracts are intentionally unchanged. The goal is to make the codebase follow the project Python/.NET-like engineering standard: explicit feature boundaries, typed Result/Error contracts, feature-owned options, strict request validation, and thinner orchestration services.

## Three-stage refactor

### Phase 1 — Result/Error + Options + strict contracts

- Added generic `Result[T]`, canonical `Error` and `ErrorType`.
- Expected feature/application failures can now return `Result.Failure(...)` instead of relying on exception control flow.
- Added immutable strict Pydantic contracts (`extra=forbid`, strict types) at the new public boundaries.
- Converted major options contracts to immutable Pydantic models and added feature-owned options modules, including `ParallelExecutionOptions`.
- Legacy runtime range checks remain in their historical place where moving them would change v0.23 behavior.

### Phase 2 — Feature boundaries + smaller orchestration

Canonical features now expose the familiar structure where applicable:

```text
feature/
  options.py
  request.py
  response.py
  errors.py
  service.py
  router.py
  tests/
```

The root `DataExtractionService` was reduced from about 903 lines to about 361 lines. Template orchestration moved into `template_extraction/workflow.py`; matching, visual fingerprints and helpers were split into focused modules. The low-level template service was reduced from about 1274 lines to about 983 lines without changing extraction behavior.

### Phase 3 — Public Result API + routers + compatibility facade

New canonical Result-based boundaries are available for extract, inspect, field discovery, field verification, and template operations. The existing v0.23 methods remain as compatibility facades so existing callers and regression tests do not break.

Example:

```python
from data_extraction import DataExtractionService, ExtractDocumentRequest

service = DataExtractionService()
result = service.extract_result(
    ExtractDocumentRequest(
        file_name="invoice.pdf",
        content=blob,
        source_id="tenant-a:invoice-1",
    )
)

if result.is_failure:
    error = result.unwrap_error()
    print(error.code, error.description)
else:
    extraction = result.unwrap().extraction
```

The legacy API remains valid:

```python
extraction = service.extract(document_input)
```

## Verification

- **398/398 tests passed** in bounded groups (`184 + 144 + 70`).
- `compileall`: passed.
- Python 3.11 syntax compatibility gate: passed.
- Canonical boundary parameter/return typing gate: passed.
- Wheel build and clean-install smoke are part of the release gate.
- `ruff` and `mypy` configuration is included in `pyproject.toml`; the executables were not installed in the current offline runtime, so this release does not falsely claim they were executed.

Important scope note: v0.24 intentionally does not mechanically annotate every historical local variable in the algorithmic core. Public/canonical parameters, returns, fields and new boundary code are explicitly typed; retrofitting every inferred internal local would create a large noisy diff unrelated to behavior and is tracked as a style-only cleanup, not a production correctness dependency.

See `docs/V24_ENGINEERING_REFACTOR_AR.md` for the detailed architecture report.

---

# Generic Data Extraction Feature v0.23.0 — Bounded Parallel Execution

v0.23 keeps the v0.22 multi-page/template contracts and changes execution latency only. Independent pages and AI page batches can now run concurrently under explicit OCR/AI/page bounds, while output is merged deterministically in the original page/region order. Vision budget reservation is thread-safe and parallel execution can be disabled to restore sequential behavior.

Default execution controls:

```python
ExtractionOptions(
    enable_parallel_execution=True,
    max_parallel_page_groups=4,
    max_parallel_ocr_calls=4,
    max_parallel_ai_calls=2,
)

TemplateExtractionOptions(
    enable_parallel_ai_batches=True,
    max_parallel_ai_batches=2,
)
```

Measured on the included benchmark: real local Tesseract over the same six-page scanned PDF improved from about 2.48s sequential to 1.71s parallel (~1.45x) with identical extracted page output and document order; mocked AI page batches improved ~1.72x at concurrency 2. **383/383 unique tests passed**, plus `compileall`. See `docs/V23_PARALLEL_EXECUTION_AR.md` and `benchmarks/V23_PARALLEL_EXECUTION_BENCHMARK.json`.

---

# Generic Data Extraction Feature v0.22.0 — Multi-page Template Hardening & AI Batching

v0.22 closes the multi-page fast-path safety gap found in v0.21.1. Template matching is now page-aware: every templated page can carry its own visual fingerprint, stable label/anchor crops carry independent visual hashes, and a later page can no longer inherit trust from an unchanged page 1.

Repeated PDF flow:

```text
Inspect all pages
  -> per-page visual/layout match
  -> stable pages: Template Fast Path
  -> changed pages: generic OCR/AI extraction only for those pages
  -> old template geometry is blocked on changed pages
```

AI template generation/review/refinement now uses bounded consecutive page batches (`4` pages by default) while preserving original page numbers. It no longer silently limits the visual template path to only the first three PDF pages. Non-consecutive target pages are kept in separate batches; batch count is bounded and fails closed when the configured limit would be exceeded.

Critical real Tesseract benchmark on a 3-page scanned PDF:

- same layout, new `TOTAL` value → `match=1.0`, generic extraction skipped, `TOTAL=999.99` verified by independent OCR consensus;
- page 2 changed from `TOTAL` to `SALARY` at the same geometry → `review_required`, `changed_pages=[2]`, only page 2 was generically OCR'd, old `total` geometry was blocked and returned `template_page_drift` with no value;
- generic OCR calls on unchanged case: `0`; on drift case: `1` (page 2 only).

Backward compatibility is fail-closed for old multi-page raster templates that do not contain v0.22 per-page/anchor visual evidence: they are sent to review/extraction instead of being accepted by a page-1-only fingerprint.

**378/378 tests collected; full regression passed in bounded groups.** See `docs/V22_MULTIPAGE_HARDENING_AR.md` and `benchmarks/V22_REAL_MULTIPAGE_BENCHMARK.json`.

---

# Generic Data Extraction Feature v0.21.1 — Bilingual Adaptive Hardening

Intensive Arabic/English validation after v0.21.0. New hardening: Arabic subtotal/total disambiguation, invoice-number/address false-positive prevention, adaptive medium threshold 0.50, one bounded Reviewer→Refiner retry for medium candidates, and schema-driven numeric/date crop upscale retry. Full regression: **371/371 passed**. See `benchmarks/v21_bilingual_intensive_artifacts/V21_1_BILINGUAL_INTENSIVE_REPORT_AR.md`.

---

# Generic Data Extraction Feature v0.21.0 — Adaptive Template Routing

## v0.21.0: OCR first, AI only when the candidate needs it

`TemplateBuildMode.ADAPTIVE` starts with the deterministic/OCR template learner, measures candidate quality, and escalates selectively: high quality → independent reviewer; medium quality → geometry refinement + reviewer; low/incomplete required-field coverage → full AI generation + the existing validation/refinement/review pipeline. Historical build modes keep their previous semantics.

The quality gate records required-field coverage, bbox precision, anchor coverage, crop/schema validity, table structure coverage, and explicit hard failures. Broad full-page boxes are not treated as high-quality geometry just because they contain a schema-valid value. Reviewer field issues can now trigger one bounded geometry repair followed by an independent re-review; semantic contracts remain immutable.

Real NOVA repeated-invoice benchmark with local Tesseract: initial deterministic quality `0.566` → `deterministic_refine_review`; **0 full generation calls**, 1 refinement, 1 review; second invoice matched `1.0`, skipped generic extraction, and returned **5/5 exact verified fields** (`invoice_number`, `date`, `subtotal`, `tax`, `total`).

**367/367 tests pass.** See `docs/V21_ADAPTIVE_TEMPLATE_ROUTING_AR.md`, `V21_TEST_REPORT.json`, and `V21_REAL_ADAPTIVE_BENCHMARK.json`.

---

# Generic Data Extraction Feature v0.20.0 — Independent AI Template Review

## v0.20.0: generator proposes, core validates, independent AI reviewer falsifies semantics

This version closes a real gap found in v0.19: a field crop can be structurally valid and schema-valid while still pointing at the wrong semantic value (for example `TOTAL` pointing to the `Subtotal` amount).

The onboarding path is now:

```text
AI generator
  -> core contract/geometry validation
  -> crop/schema self-validation + bounded geometry refinement
  -> independent AI semantic reviewer
  -> PASS / REVIEW_REQUIRED / REJECT
  -> explicit human approval
```

Key rules:

- the reviewer is a separate provider contract and, when auto-configured, a separate OpenAI adapter instance/request from the generator;
- review is tied to the candidate `content_hash`, so stale review evidence cannot approve a changed candidate;
- a low-confidence PASS is downgraded to `review_required`;
- PASS with issues is also downgraded to `review_required`;
- `REJECT` blocks approval; `REVIEW_REQUIRED` is explicitly triaged to the human reviewer;
- the reviewer cannot mutate the candidate or approve/persist it; the core still owns approval/versioning/store;
- existing setups without a review provider remain backward compatible.

The critical regression test deliberately maps `TOTAL` to the exact `Subtotal` value bbox. Core crop/schema validation still passes because both are valid money values, but the independent semantic reviewer returns `REJECT`, names `total`, and approval is blocked.

**363/363 tests pass** across feature, regression, legacy-integration, v0.15-v0.19, and new v0.20 groups. OpenAI review transport is covered with mocked HTTP and verifies that the reviewer receives the original visual plus the complete candidate under a strict review schema. No live external model call was made in this build.

See `docs/V20_AI_TEMPLATE_REVIEW_AR.md` and `benchmarks/V20_TEST_REPORT.json`.

---

## v0.19.0 — Template-Aware Repeating Tables

v0.19 fixes the main weakness found by the multi-document v0.18 benchmark: recurring invoice line-item tables no longer have to fall back to generic table reconstruction when an approved template already knows the layout.

The AI template contract can now describe a repeating table as **header + repeating-row body + semantic column ranges + stop anchors**. On a known layout, the runtime crops only the row body, obtains OCR token geometry, assigns tokens to the approved columns, and uses cell-level OCR only for missing required cells. Summary/footer rows such as `Subtotal`, `VAT`, and `TOTAL` stay outside the line-item result.

Flow: `Template match → body crop → token rows → column assignment → missing-cell OCR → stop-anchor guard → table result`.

Key safety rules remain unchanged: the template determines **where** to read, not whether a financial value is true. One OCR engine yields `template_structure_review`; independent matching table observations are required for verified consensus. Old templates without column geometry remain backward-compatible and continue through the legacy/generic table path.

On the exact invoice that exposed the v0.18 table problem, v0.19 changed the result from two merged columns plus three leaked summary rows to exactly **3 line items × 4 columns**, with `match_score=1.0`, no drift, and `generic_extraction_performed=false`. The five fixed invoice fields remained **5/5 exact**.

**357/357 tests pass** across the full regression split. See `docs/V19_TEMPLATE_AWARE_TABLES_AR.md` and `benchmarks/V19_REAL_TABLE_BENCHMARK.json`.

---

# Generic Data Extraction Feature v0.19.0

## v0.18.0 — Template Self-Validation & Geometry Refinement

AI-generated templates are now crop-tested before approval. If a proposed field region cannot produce a schema-valid crop, a refinement-capable AI provider may adjust **geometry only** in bounded retries. The core rejects semantic/schema/table changes and the template remains a candidate until explicit approval.

Flow: `Generate → Crop → Schema Check → Refine Geometry → Re-check → Human Approval`.

# Generic Data Extraction Feature v0.18.0 — Selective AI Routing by Default

## v0.18.0: Native first → OCR second → AI automatically only where needed

v0.17 turns the selective fallback policy into the default runtime behavior. If `OPENAI_API_KEY` is configured and the caller does not inject another provider, `DataExtractionService` automatically wires an OpenAI Responses document-AI provider for unresolved image regions, structurally incomplete raster invoices/forms, and complex PDF reading-order pages. Plain native text and strong ordinary OCR stay local and do **not** call AI.

Default policy:

```text
Native text/table reliable?  -> use native, 0 AI calls
Raster text?                 -> OCR first
OCR weak/unresolved?         -> AI Vision automatically
Structured raster + flat OCR -> AI Vision automatically
Complex PDF layout?          -> AI layout fallback automatically
Unknown template build?      -> AI template generator automatically
Known approved template?     -> template fast path, no AI unless drift/fallback requires it
```

Important controls:

- `auto_enable_ai=True` is the default. It only creates an external provider when `OPENAI_API_KEY` exists.
- no key means **no network call** and the old fail-closed review/unresolved behavior remains.
- explicit `vision_provider`, `layout_provider`, or `template_generation_provider` always take precedence over auto-wiring.
- automatic AI layout is always fallback-only; a manually injected layout provider keeps the caller's `layout_provider_fallback_only` setting.
- `auto_ai_for_structured_images=True` lets a strong but flat receipt/invoice/form OCR result escalate to AI when structure is still missing.
- `max_vision_calls` and `max_layout_calls` still cap model use.
- AI output remains review evidence; it does not make financial values authoritative by itself.
- set `ExtractionOptions(auto_enable_ai=False)` for a completely offline/no-external-AI run.

Minimal default usage:

```python
import os
from data_extraction import DataExtractionService, DocumentInput

os.environ["OPENAI_API_KEY"] = "..."
service = DataExtractionService()
result = service.extract(DocumentInput("document.pdf", content, "source-1"))
```

**349/349 tests pass.** Focused v0.17 tests prove zero AI calls for native PDF text and strong ordinary OCR, automatic AI fallback after weak OCR, automatic AI escalation for a strong-but-flat invoice, explicit opt-out, and automatic AI template-provider configuration from the same key. The release was tested with mocked Responses HTTP payloads; no live OpenAI call was made in this runtime because no API key was available. See `docs/V17_SELECTIVE_AI_ROUTING_AR.md`.

---

# Generic Data Extraction Feature v0.16.0 — AI-Generated Template Candidates

## v0.16.0: the AI model learns the first layout; the core validates it

v0.16 makes the AI/Vision model the preferred template learner for first-seen invoices, receipts, and forms. The model sees the original visual document plus extracted text context and must return a strict structured template proposal. The proposal is still **untrusted**: core validation, content hashing, explicit human approval, versioning, drift detection, and independent field verification remain deterministic.

New in v0.16:

- `TemplateBuildMode.AI_REQUIRED` fails closed if no AI provider is configured; `AI_PREFERRED` preserves the offline deterministic fallback.
- optional `OpenAIResponsesTemplateProvider` sends image/PDF visual pages to the Responses API and requests strict JSON-schema output; it defaults to `store=false`.
- AI output includes reusable anchors, normalized label/value regions, validation regexes, and repeating-table structure; actual transaction values are never stored as template truth.
- the core rejects out-of-range page references, empty/unsafe regex contracts, invalid table locations, duplicate names, and near-identical tight value regions for different required fields.
- the model can propose a candidate, but only `approve_template()` can promote it to an approved immutable version.
- repeated matching/extraction behavior from v0.15 remains unchanged: accepted known layouts can use direct field crops and independent OCR consensus without generic full-page extraction.

Recommended onboarding mode:

```python
from data_extraction import (
    DataExtractionService, OpenAIResponsesTemplateProvider, TemplateBuildMode,
    TemplateBuildRequest, TemplateApprovalRequest, SQLiteTemplateStore,
)

provider = OpenAIResponsesTemplateProvider(api_key=OPENAI_API_KEY)
service = DataExtractionService(
    template_generation_provider=provider,
    template_store=SQLiteTemplateStore("templates.db"),
    field_ocr_providers=(ocr_a, ocr_b),
)

candidate = service.build_template_candidate(
    TemplateBuildRequest(
        document=first_scan,
        family="supplier-a-invoice",
        required_fields=("invoice_number", "date", "total"),
        mode=TemplateBuildMode.AI_REQUIRED,
    )
).candidate

approved = service.approve_template(
    TemplateApprovalRequest(candidate=candidate, approved_by="reviewer")
)
```

**343/343 tests pass.** A clean-wheel smoke test using the real NOVA invoice images and a mocked OpenAI Responses HTTP response proved the full integration path: image sent to the AI adapter → strict template candidate → approval → repeated invoice match `1.0` → no generic extraction → 5/5 expected fields correct and verified by independent OCR consensus. No live external OpenAI request was made in this environment because no API key was available; this validates integration/orchestration, not real-model template accuracy. See `docs/V16_AI_TEMPLATE_GENERATION_AR.md`.

---

# Generic Data Extraction Feature v0.15.0 — Template Learning & Reusable Structured Extraction

## v0.15.0: learn a layout once, reuse it safely

v0.15 adds a standalone `template_extraction/` feature for recurring receipts, invoices, and forms. The first unknown layout may be sent to an injected AI/Vision `TemplateGenerationProvider`, but the provider can only create a **candidate**. Core validation plus explicit human approval are required before reuse. Approved templates are versioned and can be persisted with `SQLiteTemplateStore`.

The repeated-document path is intentionally different from “run OCR on everything again”:

- cheap preflight match uses page count/shape, a first-page visual fingerprint, and native anchors when available;
- known field templates may store normalized label/value boxes, so a repeated scan can crop only the expected field region and run independent field OCR consensus;
- a template tells the system **where to look**, never what financial value is true;
- required table templates preserve/reuse repeating table structure, but OCR-derived table values remain review-only until independently verified;
- low/ambiguous matches trigger full generic extraction or review instead of forcing an old template;
- template content is hashed before approval, versions are immutable, and a changed layout gets a new version;
- AI-generated regex/location contracts are bounded and validated before approval.

Public workflow:

```python
from data_extraction import (
    DataExtractionService, SQLiteTemplateStore,
    TemplateBuildRequest, TemplateApprovalRequest, TemplateExtractionRequest,
)

service = DataExtractionService(
    template_generation_provider=my_ai_template_provider,
    template_store=SQLiteTemplateStore("templates.db"),
    field_ocr_providers=(ocr_a, ocr_b),
)

# First unknown layout: AI proposes, human approves.
candidate = service.build_template_candidate(
    TemplateBuildRequest(document=first_scan, family="supplier-a-invoice")
).candidate
approved = service.approve_template(
    TemplateApprovalRequest(candidate=candidate, approved_by="reviewer")
)

# Similar future scan: preflight -> direct field crops -> independent verification.
result = service.extract_with_template(
    TemplateExtractionRequest(document=next_scan, family="supplier-a-invoice")
)
```

**338/338 tests pass.** The v0.15 tests prove the orchestration, safety gates, fast path, versioning, SQLite persistence, visual/layout matching, direct normalized field crops, independent OCR consensus, and repeating-table review behavior. They do **not** claim that a bundled AI model exists or that OCR character accuracy is solved. See `docs/V15_TEMPLATE_LEARNING_AR.md`.

---

## v0.14.0: fewer false structured candidates, stronger TOC binding, flexible receipt tables

v0.14 is a targeted follow-up to real-world v0.13 evaluation. It does **not** claim that Tesseract/EasyOCR character accuracy is solved. Instead it fixes four concrete reliability gaps: low-confidence forms escaping semantic analysis, bad merchant-label/header promotion, false TOCs on legal provisions / wrong numeric-column binding, and strict OCR table reconstruction that failed on borderless receipts with variable-length descriptions.

Key changes:

- Structured-document **suspicion gate**: a form/invoice/receipt with meaningful evidence but confidence below the acceptance threshold is review-gated instead of silently becoming `general`.
- Merchant candidates prefer explicit seller-label adjacency and reject generic labels / obvious OCR garbage / address-like lines.
- TOC row pairing selects the **outer page-reference column**, not a nearby section number; legal `Article/المادة/...` rows are rejected as TOC without explicit contents evidence.
- Flexible financial OCR table reconstruction supports variable token counts per row while preserving only token-observed values.
- A real local Tesseract test rebuilt a three-row borderless receipt table and recovered `TOTAL 30.00`; the result remained semantically unverified, as intended.

**325/325 tests pass.** This release improves extraction structure and candidate quality; it does not claim the exact SROIE OCR substitutions reported in v0.13 evaluation are corrected.

---

**Scope: file detection → inspect pages/regions → native text/table/image extraction → optional OCR/Vision/Docling/Unstructured structure extraction → ordered, rich JSON and fail-closed review flags.** No chunking, indexing, RAG, retrieval or agent runtime.

## v0.13.0: text coverage is no longer semantic completeness

v0.13 targets real receipt/invoice failures observed in SROIE-style receipts and invoice-image evaluation. The core semantic fix is that **an OCR engine returning text no longer proves that a structured financial document was extracted completely**.

- new conservative `structured_documents/` feature classifies likely `receipt`, `invoice`, `form`, or `general` pages and extracts reviewable field candidates;
- page/document results now separate `text_coverage_*` from `semantic_*`, `field_coverage_ratio`, `verified_field_coverage_ratio`, and `semantic_gap_count`;
- OCR-only merchant/address/date/amount/invoice candidates remain unverified until independent evidence exists; a high OCR confidence does not erase missing fields;
- borderless financial images can request OCR token geometry and reconstruct candidate table rows even when no drawn grid exists; those rows feed total/tax/subtotal candidate extraction;
- optional adaptive Tesseract profiles compare language configurations such as `ara+eng` and `eng`; text/number disagreements are surfaced as explicit review conflicts rather than hidden behind the highest confidence;
- TOC detection is stricter: page-number alignment/order evidence is required, reducing the known false classification of statistical tables as contents pages;
- the legacy `extraction.v0.6` schema identifier remains for compatibility; new fields are additive.

**318/318 tests pass.** A real local Tesseract integration test on a synthetic raster receipt achieved full text/candidate coverage, but v0.13 still correctly returned `content_complete=false` because all sensitive fields came from one OCR observation and were not independently verified. This validates the reliability gate, not SROIE accuracy. The exact four SROIE images and the local ZATCA/FUNSD files used in the user's evaluation were not available in this runtime, so this release does **not** claim those exact OCR errors are fixed. See `docs/V13_STRUCTURED_DOCUMENT_RELIABILITY_AR.md`.


## v0.12.0: TOC row binding, OCR/table recovery, and impact-aware unresolved counts

v0.12 targets concrete gaps found while reviewing real extraction outputs. It keeps the v0.11 reading-order engine and adds conservative structure recovery rather than silently rewriting source text:

- native PDF table-of-contents rows are reconstructed from line geometry into a two-column `table_of_contents` region (`title`, `page_number`); duplicate numbers such as `33 / 33` remain distinct rows because binding is geometric, not value-based;
- original TOC text regions are retained as `toc_source_text_support` and parented to the structured TOC region so downstream code can avoid double-counting while preserving source evidence;
- TOC pages receive `page_semantic_table_of_contents`, preventing the known false `diagram_hierarchy` route when TOC evidence is strong;
- low-confidence OCR can use bounded grayscale/autocontrast/sharpen recovery when no Vision fallback is available; transformed text is never treated as independent consensus and remains reviewable;
- OCR providers may expose token geometry; raster grid tables can then be reconstructed into candidate rows/columns with `ocr_table_geometry_reconstructed_review`;
- repeated edge image/navigation regions are detected across pages, retained for review, but can be non-critical for completeness;
- page/document results now expose `critical_unresolved_region_count` and `noncritical_unresolved_region_count` in addition to the raw `unresolved_region_count`;
- the previously derived `shnayer_first5.pdf` sample was revalidated against the available original `ش__ناير_2026.pdf`: all 5 pages matched at both raster-byte and extracted-text level.

The exact local `consumer_rights_guide_pilot_4pages.pdf` and `ministry_user_guide_textual.pdf` files were not available in this runtime, so v0.12 does **not** claim a visual re-validation of those two originals. The new TOC/OCR/table regressions reproduce the observed failure modes synthetically and fail closed. See `docs/V12_TOC_OCR_STRUCTURE_AR.md`.

**312/312 tests pass** (307 inherited + 5 focused v0.12 regressions). The v0.12 wheel was built, installed into a clean target, and passed a smoke extraction.

## v0.11.0: Hybrid reading order from real benchmark evidence

v0.11 keeps the v0.10 recovery pipeline and strengthens reading order without replacing it with an always-on model:

- new feature-first `reading_order/` package with typed flow roles, page semantic classes and auditable spatial-graph edges;
- native-text overlay/background/support graphics stay in provenance/extraction but no longer create false linear-order ambiguity by themselves;
- conservative page classes: `simple_prose`, `multi_column_prose`, `table_heavy`, `diagram_hierarchy`, `mixed_layout`, `unknown`;
- large vector hierarchies explicitly abstain with `reading_order_diagram_hierarchy_review_required` instead of inventing prose order;
- the proven LTR/RTL multi-column resolver now records graph evidence while preserving the v0.10 deterministic order;
- optional `layout_provider_fallback_only=True` reserves an injected layout model for pages already classified as mixed/diagram/complex; default remains `False` for backward compatibility;
- physical overlap relations are still preserved even when a support/background layer is excluded from *reading-flow ambiguity*.

**307 tests pass.** On the same first five pages of the real Arabic report used in the v0.10 audit, ambiguous pages dropped from **5/5 to 1/5**; the remaining ambiguous page is a genuine mixed chart/layout case. The organizational chart is now explicitly classified `diagram_hierarchy`. The controlled four-layout benchmark remains **4/4 exact** with pairwise accuracy **1.000** for single-column, LTR columns, RTL columns and spanning sections. The deliberately unsupported figure-inside-columns case still abstains rather than guessing.

This does **not** prove perfect semantic reading order. `geometric_estimate` means no known ambiguity was detected, not human-verified order. Surya, LayoutReader and PP-StructureV3 remain optional external providers and were not executable in this runtime. See `docs/V11_READING_ORDER_AR.md`.

## v0.10.0: Active recovery instead of warning-only handling

This release keeps v0.9 field verification and adds bounded recovery for real extraction gaps:

- adaptive image/PDF/Layout rendering instead of immediate `IMAGE_TOO_LARGE` where a safe lower DPI fits;
- OCR → bounded tiled OCR → Vision recovery for large/weak text images;
- vector raster candidates can be actively OCR/Vision recovered and retain recovery evidence;
- region/page/document completeness metrics and `ExtractionStatus` separate content loss from review policy;
- tiny embedded decorative candidates can be non-critical for completeness without being deleted or auto-approved;
- optional `auto_enable_local_ocr=True` for local Tesseract without hidden downloads;
- optional generic `discover_fields()` for email/URL/phone/date/time/money/identifier/numeric-code shapes;
- adaptive page rendering for optional Layout providers.

`ready_for_publication` remains strict. Use `content_complete` / `extraction_status` when the downstream question is whether content was extracted rather than whether every review flag is cleared. See `docs/V10_RECOVERY_PIPELINE_AR.md`.

## v0.9.0: Generic field-level / sensitive-value OCR verification

Built on v0.8.0. The main extraction schema remains `extraction.v0.6`; field verification is a separate opt-in feature and never rewrites extracted source text.

- New `verify_fields/` feature-first package with typed request/response/options/service/errors/validation contracts.
- Generic `FieldDefinition`: caller supplies labels + full-match regex and may supply an OCR-confusion template (`A`, `a`, `9`, `X`). No invoice/customer/payment rules are hard-coded into the generic runtime.
- Field geometry is located from native PDF text when available or from the new optional `OCRLayoutProvider.recognize_regions()` contract for scans/images.
- Value regions are cropped and independently re-read by one or more OCR providers. All alternatives, confidence, provider identity, schema validity and deterministic transforms are retained as evidence.
- Native source values that satisfy the configured schema can be verified directly. OCR-derived values require independent consensus; a template transform by itself is always review-only.
- EasyOCR and Tesseract now expose optional line-region geometry while retaining the original `OCRProvider.recognize()` API.
- Arabic/Persian decimal digits may be normalized for verification evidence without mutating the extracted source.
- Explicit budgets bound field count, occurrences, candidate length, page/crop pixels and OCR calls.
- **293 tests pass** after the change, including 9 new field-verification cases.

Example:

```python
from data_extraction import (
    DataExtractionService, DocumentInput,
    FieldDefinition, FieldVerificationRequest,
)

field = FieldDefinition(
    name="invoice_number",
    labels=("Invoice Number", "رقم الفاتورة"),
    pattern=r"^INV-\d{4}-[A-Z]\d{3}$",
    template="INV-9999-A999",
    minimum_confidence=0.80,
)

service = DataExtractionService(
    ocr_provider=primary_ocr,
    field_ocr_providers=(primary_ocr, independent_ocr),
)
result = service.verify_fields(
    FieldVerificationRequest(document=request, fields=(field,))
)
```

A `suggested_value` is evidence only; `extract()` output is not modified. See `docs/V09_FIELD_VERIFICATION_AR.md`.

## v0.8.0: Legacy capabilities with fail-closed completeness

Built on v0.7.0 without moving extraction into Knowledge/RAG/Agent Runtime. Repairs false-ready warnings, detects substantive vector graphics within text-bearing PDFs, annotates DOCX headings and PDF heading/header/footer candidates. Optional OCR features include scoped content-addressed cache, strict multipass consensus with audit callback, Arabic word-geometry suggestions (original text preserved), quality fingerprint, grid table reconstruction, lazy EasyOCR and image preprocessing profiles. The original 263 tests plus new integration checks pass.

Review-only heuristics do **not** certify extraction accuracy. Full job checkpoint/resume, automatic field correction, and document approval/reindexing are **not** part of this release. Details and examples: [`docs/LEGACY_MERGE_V08_AR.md`](docs/LEGACY_MERGE_V08_AR.md).

## v0.7.0: Feature-first layout without changing extraction.v0.6 contracts

**This version changes code organization, not the extraction schema or inference accuracy.**
The top-level `DataExtractionService.inspect()` / `.extract()` public API and typed contracts are retained. Historical import paths under `actions/`, `adapters/`, `layout/`, `structure/`, `integrations/`, `models.py`, `errors.py`, and `router.py` are **thin compatibility re-exports**, not duplicate implementations.

### Canonical implementation paths

```text
data_extraction/
  detect_file/             request.py · response.py · service.py · tests/
  inspect_document/        request.py · response.py · service.py · tests/
  analyze_layout/          request.py · response.py · contracts.py · service.py
                           resolve_reading_order.py · describe_layout.py · tests/
  dispatch_extraction/     service.py · extract_text.py · extract_table.py
                           extract_image.py · extract_structured_tables.py
                           assemble_document.py · tests/
  validate_extraction/     service.py · validate_upload.py · provider_validation.py · tests/
  providers/
    contracts.py · structure_contracts.py
    native/                adapter.py · pdf.py · word.py · excel.py
                           flat_files.py · image_layout.py · common.py
    docling/               adapter.py
    unstructured/          adapter.py
    ocr/                   tesseract.py
    vision/                __init__.py (vision protocol re-export)
    layout/                model_adapters.py
  shared/                  models.py · errors.py
  service.py               stable public orchestration
  options.py               stable public options
  actions/, adapters/, integrations/, layout/, structure/  compatibility re-exports only
  router.py, models.py, errors.py, validation.py, provider_validation.py  compatibility re-exports
```

New code should import from the canonical feature paths; legacy imports continue to work. Core source files have **one implementation each**. The native adapter is separated by file type. OCR/Vision protocols and third-party adapters remain independent from the extraction flow.

### Verification

- Existing **230** tests still pass without modifying those tests.
- Added **33** focused refactor/feature/compatibility tests: **263 tests passing** in total.
- Independent comparison against the original v0.6 source: `inspect()` and `extract()` JSON matched **byte-for-byte** for the five-page Arabic PDF and a table PDF with optional image inference disabled.
- `run_demo.py` now sends third-party diagnostic prints to stderr during inspection/extraction, keeping stdout parseable as JSON.
- The published data contract remains `extraction.v0.6` intentionally. This is not a model accuracy benchmark and does not indicate live Docling/Unstructured inference.

See `docs/V07_FEATURE_FIRST_AR.md` for migration details and `docs/V07_TEST_REPORT.json` for test evidence.

## v0.6: Optional structured table engines

- `DoclingTableProvider`: reads `DoclingDocument.tables[].data.table_cells` **directly** (not converted Markdown), maps page provenance and bottom-left/top-left coordinates into our PDF-point top-left system; preserves merged cell spans and available cell boxes/column-header labels. Docling engine is lazy-loaded and optional.
- `UnstructuredTableProvider`: invokes local `unstructured.partition.pdf.partition_pdf` with `hi_res` and table extraction, parses `metadata.text_as_html` into rectangular cells/merge metadata and maps `metadata.coordinates` from its pixel coordinate system to PDF-point coordinates. Unstructured engine is lazy-loaded and optional; may require model weights.
- `ExtractStructuredTablesAction`: validates each prediction and maps it into the original `CandidateRegion` tree as an **additional derived overlay**, preferably parented to the original image. Native text, native tables and image regions remain untouched. Predictions already overlapping a native table are skipped to prevent duplicate table structure records.
- Any derived table is **non-authoritative and requires review**; `method=document_ai`, `provenance.source_surface` and `provenance.original_region_kind` clarify whether it comes from an image. `relations.region_role=derived_model_table_overlay` distinguishes it from the underlying raster/OCR text; other components can avoid double-counting.
- **No loss / no silent guessing:** invalid page number, bbox, cells, merge geometry, provider identity, cell budget, or a provider failure rejects all external additions in that run, preserves native content, and flags review. Missing optional dependencies get a distinct safe warning. `inspect()` never invokes external structure engines.
- New `TableDetails.cell_locations` includes row/column, row/column spans, per-cell bbox when available, and Docling or HTML header flags. Page/region order remains explicit and stable; generated tables are inserted immediately after the parent image, with an independent `document_order_index`.
- `ExtractionResult.structure_provider_invocations` counts top-level structure-provider invocations. It **does not** claim to count internal OCR/model calls; these are provider-specific.
- Schema is now **`extraction.v0.6`**; existing v0.5 contracts remain largely compatible, but consumers should handle new optional fields and `ExtractionMethod.DOCUMENT_AI`.

## Execution and installation

Basic extraction without installing external document AI models:

```bash
pip install -e '.[ocr,dev]'
python -m pytest -q
python run_demo.py document.pdf --action inspect
python run_demo.py document.pdf --action extract --ocr
```

To opt in to **actual** Docling or Unstructured inference (the packages, model weights, and a compatible runtime must be available):

```bash
pip install -e '.[docling,ocr]'
python run_demo.py document.pdf --action extract --ocr --structure-provider docling

# Alternative engine; may need additional system libraries + local weights
pip install -e '.[unstructured,ocr]'
python run_demo.py document.pdf --action extract --ocr --structure-provider unstructured
```

Or inject custom implementations of `DocumentStructureProvider.predict_tables(document, options)` into `DataExtractionService(structure_provider=...)`, alongside existing `ocr_provider`, `vision_provider`, and `layout_provider` arguments.

### Limitations

1. **No live Docling/Unstructured model inference performed in this runtime.** These packages/models were not available here. Automated adapter tests use constructed Docling/Unstructured-shaped objects and HTML. Installing/updating model weights may require network access or a preloaded cache.
2. Document AI-derived table cells are evidence subject to review, **not** an established ground truth or guaranteed accuracy. The underlying image/OCR text remains in the result and can partially duplicate the derived table content; the relationships and overlay role explicitly mark this.
3. External providers can have their own memory/time/model-download cost. The feature bounds input/outputs and explicitly counts top-level structure provider calls but cannot enforce an execution timeout or fully control internals of an imported inference engine. Sandbox/weight pinning/execution timeouts are production integration tasks.
4. Chart extraction, table continuation across pages, borderless/rotated tables, Arabic exact OCR accuracy, table-cell exact match, and cross-engine benchmarks remain **unproven** and require a new independent gold dataset.
5. Optional converters operate only for PDFs in this integration. DOCX/XLSX remain under the existing native extraction and coverage rules.
6. The `UnstructuredTableProvider` shown here uses the **local Python partitioner**, not the paid Unstructured API; the Udemy example used that API, but no external credential is required by this adapter itself.
7. License and model weight terms should be reviewed before embedding optional dependencies into a commercial application.

## Verified tests (2026-10-30)

- **230 passing unit/regression/contract tests**, with **181 inherited** from v0.5 and **49 new** cases covering Docling model-shaped output, Unstructured HTML tables, merged cells, coordinate conversions, order, fail-closed behavior, and multi-page source order.
- Re-extracted the original five-page Arabic hybrid PDF with **native parsers + local Tesseract**: pages 1→5 preserved, **52 original regions**, `inspect`/`extract` region IDs match, no duplicate region IDs, `ready_for_publication=false` (OCR review). **Zero Docling/Unstructured calls** in that test, intentionally.
- The 2026-10-30 `benchmarks/arabic_real_v0_6_summary.json` contains the factual run summary.
- `examples/structured_table_docling_mock.json` and `examples/structured_table_unstructured_mock.json` are synthetic, clearly labeled adapter fixtures—**not** outputs of installed ML models.

## Structure

```text
data_extraction/
  actions/
    inspect_file_action.py
    analyze_layout_action.py
    extract_structured_tables_action.py   # optional v0.6 step
    extract_text_action.py
    extract_table_action.py
    extract_image_action.py
  structure/
    contracts.py                          # DocumentStructureProvider + predictions
    docling_provider.py                   # DoclingDocument -> predictions
    unstructured_provider.py              # HTML + positions -> predictions
  models.py                               # rich typed data contracts
  service.py                              # inspect / extract
  router.py                               # native and derived output dispatch
  validation.py                           # upload/limits
  ...
tests/
  test_v06_docling_structure.py
  test_v06_unstructured.py
  ...
benchmarks/
  arabic_real_v0_6_summary.json
```

**Engineering rule:** source bytes and complete original regions are never silently replaced by a model's inferred table. The feature produces structured extraction and provenance only; downstream chunking is explicitly outside its scope.