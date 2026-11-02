# v0.21 — Adaptive Template Routing: Cheap First, AI Only When Needed

## القرار

v0.21 يضيف مسارًا جديدًا صريحًا باسم `TemplateBuildMode.ADAPTIVE`.

الفكرة:

```text
OCR / Native extraction
        ↓
Deterministic Template Candidate
        ↓
Core quality assessment
        ├─ High   → Independent AI Reviewer
        ├─ Medium → AI geometry refinement → Independent AI Reviewer
        └─ Low    → Full AI Template Generation → validation/refinement → Reviewer
        ↓
Explicit human approval remains the authority
```

الـmodes القديمة (`AI_PREFERRED`, `AI_REQUIRED`, `DETERMINISTIC`) لم تتغير؛ `ADAPTIVE` اختياري صريح للحفاظ على backward compatibility.

## Quality Gate

الـCore لا يعتمد على confidence واحدة. `TemplateCandidateQuality` يسجل:

- `required_field_coverage`
- `geometry_precision`
- `anchor_coverage`
- `schema_valid_ratio`
- `table_structure_ratio`
- `hard_failures`
- weighted `score`

Missing required fields تعتبر hard failure، ولذلك لا نحاول إصلاح geometry فقط؛ نصعّد إلى Full AI Generator عندما يكون متاحًا.

الـgeometry لا تعتبر جيدة لمجرد وجود bbox. الـCore يقيس مساحة الـbbox؛ full-page/very broad regions تحصل على geometry precision منخفضة حتى لو OCR وجد قيمة تطابق regex.

## Reviewer-driven repair

v0.20 كان الـReviewer يكتشف semantic mapping errors ولكنه لا يعدل الـTemplate.

v0.21 يسمح للـAdaptive Orchestrator، عند وجود field-level reviewer issue، بتحويل المشكلة إلى bounded `TemplateRefinementFieldFeedback` ثم تشغيل geometry-only refiner ثم إعادة الـIndependent Review.

مثال:

```text
TOTAL → bbox بتاع Subtotal
Core schema check → money valid
Reviewer → semantic_value_region_mismatch
Refiner → moves TOTAL bbox only
Reviewer again → PASS
Full generator calls → 0
```

الـCore ما زال يمنع الـRefiner من تغيير field name/kind/required/page/anchors/regex/table semantics.

## Real NOVA benchmark

تم تشغيل v0.21 على فاتورتي NOVA الموجودتين من benchmark v0.19 باستخدام Tesseract فعلي محليًا.

First invoice:

- deterministic initial quality = `0.566`
- route = `deterministic_refine_review`
- full AI generation calls = `0`
- AI refinement calls = `1`
- reviewer calls = `1`
- final quality = `0.874`

Second invoice (same layout):

- template preflight match = `1.0`
- match status = `accepted`
- generic extraction = `false`
- `invoice_number = INV-2026-102` ✅
- `date = 02/11/2026` ✅
- `subtotal = 113.50` ✅
- `tax = 5.68` ✅
- `total = 119.18` ✅
- 5/5 exact
- all five = `verified_consensus`
- `semantic_complete = true`

الـAI refinement/reviewer في benchmark كانا simulated providers بgeometry معروفة، بينما OCR/field crops كانت Tesseract حقيقية. لذلك الاختبار يثبت routing/cost shape + repeated extraction accuracy مع OCR حقيقي، وليس جودة موديل خارجي live.

## الاختبارات

- New v0.21 adaptive tests: 4/4 passed.
- Full project collection: 367 tests.
- Full regression: 367/367 passed في مجموعات bounded.
- `compileall`: passed.

## القرار العملي

لـonboarding جديد نوصي بـ:

```python
TemplateBuildRequest(..., mode=TemplateBuildMode.ADAPTIVE)
```

إذا أردت إجبار Full AI لأول مستند، استخدم `AI_REQUIRED`. وإذا أردت منع AI بالكامل، استخدم `DETERMINISTIC`.
