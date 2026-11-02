# v0.21.1 — Intensive Arabic / English Validation

## Executive result

- Full regression: **371/371 passed**.
- Compileall: **passed**.
- Routing stress: **90/90 passed** (30 EN + 30 AR + 30 mixed).
- Real OCR matrix: **24 documents** (8 EN + 8 AR + 8 mixed) across clean/blur/low-contrast/noise/skew.
- End-to-end template cases: EN, AR and mixed all produced **3/3 required repeated fields exactly** with accepted template fast path.

## OCR-only matrix

| Language | Invoice classification | Required exact | All 5 financial fields exact | False invoice-as-address |
|---|---:|---:|---:|---:|
| English | 8/8 | 24/24 | 40/40 | 0 |
| Arabic | 8/8 | 16/24 | 32/40 | 0 |
| Mixed | 8/8 | 20/24 | 30/40 | 0 |

Arabic misses are now fail-closed: when `الإجمالي` is garbled, the system leaves `total` missing/review-required instead of incorrectly copying `المجموع الفرعي`.

## Adaptive routing stress

**90/90 passed.** Per language: 12 complete candidates, 6 missing-total, 6 missing-invoice, 6 semantic traps. Missing required fields escalated to full generation. Semantic traps were repaired with one bounded Reviewer → Refiner → Reviewer retry without full generation.

## End-to-end repeated-layout result

| Language | Route | Full Generate | Refine | Review | Repeat match | Required exact | Fast path |
|---|---|---:|---:|---:|---:|---:|---|
| English | deterministic_refine_review | 0 | 1 | 1 | 1.000 | 3/3 | True |
| Arabic | deterministic_refine_review | 0 | 1 | 1 | 1.000 | 3/3 | True |
| Mixed | ai_generate_review | 1 | 0 | 1 | 0.997 | 3/3 | True |

The threshold change from `0.55` to `0.50` keeps clean EN/AR at cheap deterministic-refine-review while the weaker mixed case (`adaptive_candidate_quality:0.480`) still escalates to full AI generation. Hard failures still override the score and force generation.

## Real Arabic existing-image smoke

The existing Arabic invoice crop was classified as invoice with confidence **0.96**, but OCR-only field coverage was **0.333** and extraction remained `partial_success` / `partial`. This confirms that real Arabic scans still need selective AI / template onboarding rather than OCR-only trust.

## Bugs found and fixed

1. `المجموع الفرعي` could be mistaken for `total` through the broad Arabic `المجموع` label. Fixed fail-closed.
2. `Invoice No...` / `رقم الفاتورة...` could become an address candidate. Fixed.
3. Medium candidates rejected by the semantic reviewer had no second repair chance. Added one bounded retry.
4. Real clean candidates around score `0.52` were unnecessarily escalated because medium threshold was `0.55`. Tuned to `0.50`; missing required fields still force generation.
5. Small numeric/date crops can lose characters. Added one schema-driven upscale retry only after schema-invalid numeric OCR; it never counts as independent consensus and it does not mask alphanumeric geometry errors.

## Important limits

- No live external AI model was called. The AI layer in E2E is an oracle/simulation so these tests validate routing/contracts, not GPT model accuracy.
- The 24-document OCR matrix is controlled synthetic data; Tesseract itself is real.
- One OCR crop provider was used in the final E2E cases, so correct values remain `review_required` rather than `verified_consensus`.
