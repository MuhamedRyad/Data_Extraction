# Five-page Arabic PDF — smoke benchmark (2026-10-30)

Input: `extraction_quality_test_v2_5pages.pdf` (user's prior sample; **not bundled**).
Tool run: `DataExtractionService(ocr_provider=TesseractOCRProvider(language="ara+eng"))`

| Page | Inspect candidates | Extract route | OCR confidence (mean Tesseract word confidence) | Decision |
|---|---|---|---|---|
| 1 | 28 text | native | — | no review |
| 2 | 7 text + 2 tables | native | — | no review |
| 3 | 1 image | OCR | 0.8666 | review numeric/codes |
| 4 | 1 image | OCR | 0.8987 | review numeric/codes |
| 5 | 12 text + 1 image | native text + image OCR | 0.9103 | review numeric/codes |

- `ready_for_publication: false`
- `vision calls: 0`
- Coverage: all 5 pages classified with at least one region, 3 image regions routed to OCR.
- A high OCR confidence is **not** evidence of correct reference codes. No ground-truth field accuracy measured.
