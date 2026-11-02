# v0.20 — Independent AI Template Reviewer

## لماذا أضفناه؟

v0.19 حل جدول البنود المتكرر، لكن اختبار Approval كشف Gap مختلفة: الـCore يستطيع التأكد أن الـbbox داخل الصفحة وأن الـRegex صالح وأن الـcrop ينتج قيمة Money صحيحة، لكنه لا يستطيع دائمًا إثبات أن هذه القيمة تخص **المعنى الصحيح**.

الحالة التي أثبتت المشكلة:

```text
Subtotal = 113.50
TOTAL    = 119.18
```

تم إنشاء Candidate متعمد الخطأ حيث `total.normalized_value_bbox` يساوي نفس bbox الخاص بـ`subtotal`. لأن `113.50` قيمة Money صحيحة، مرّ `template_geometry_self_validation_passed`. هذا يثبت أن Geometry/Schema validation وحدها غير كافية لمنع semantic swap بين حقول من نفس النوع.

## المسار الجديد

```text
AI Generator
    ↓
Core contract + geometry validation
    ↓
Crop/schema self-validation
    ↓
Bounded geometry refinement عند الحاجة
    ↓
Independent AI Template Reviewer
    ├─ PASS
    ├─ REVIEW_REQUIRED
    └─ REJECT
    ↓
Explicit Human Approval
```

## مسؤولية الـReviewer

الـReviewer لا يولد Template ولا يعدله ولا يحفظه. هو Critic مستقل يحاول falsify الـCandidate بصريًا، ويراجع خصوصًا:

- field name ↔ visible label؛
- label ↔ value bbox؛
- semantic swaps بين قيم من نفس النوع مثل `Subtotal`, `VAT`, `TOTAL`؛
- page reference والموضع المرئي؛
- table header/body/columns؛
- عدم تسريب summary rows داخل repeating line items.

## العقود الجديدة

- `TemplateReviewProvider`
- `TemplateReviewContext`
- `TemplateReviewResult`
- `TemplateReviewIssue`
- `TemplateReviewVerdict`: `pass | review_required | reject`

`TemplateBuildResult.review` يحمل نتيجة المراجع عندما يكون Provider متاحًا.

## قواعد Fail-Closed

1. Review مربوط بـ`candidate.content_hash`؛ Review قديم لا يصلح لـCandidate معدّل.
2. لو Reviewer configured وReview غير موجود، Approval يُرفض بـ`TEMPLATE_AI_REVIEW_REQUIRED`.
3. `REJECT` يمنع Approval بـ`TEMPLATE_AI_REVIEW_REJECTED`.
4. `REVIEW_REQUIRED` لا يمنع الـHuman Approval؛ هو triage صريح للإنسان.
5. PASS أقل من `minimum_ai_review_confidence` يتحول إلى `review_required`.
6. PASS مع issues يتحول إلى `review_required`.
7. Provider failure لا يتحول إلى نجاح؛ يبقى بدون Review ثم Approval fail-closed عندما يكون reviewer configured.

## استقلال الـAI Reviewer

عند وجود `OPENAI_API_KEY` والـauto AI مفعّل، يتم إنشاء `OpenAIResponsesTemplateReviewer` كـinstance منفصل عن `OpenAIResponsesTemplateProvider`. حتى لو استخدم الاثنان نفس model configuration فهما Calls منفصلة، والـReviewer يستقبل Prompt Critic مستقل ولا يحصل على reasoning الخاص بالـGenerator.

الـOpenAI review call يرسل:

- original visual pages/images؛
- complete candidate JSON؛
- candidate hash؛
- extracted text context كمساعدة فقط؛
- strict JSON schema للـverdict/issues.

`store=false` يظل الافتراضي.

## الاختبار الحاسم

تم بناء Candidate به `TOTAL` على bbox بتاع `Subtotal` عمدًا.

- Core crop/schema validation: **PASS**.
- Independent reviewer: **REJECT**.
- Issue: `semantic_value_region_mismatch` على field `total`.
- `approve_template()`: **blocked**.

ده يثبت إن الـReviewer يضيف قيمة مختلفة عن الـCore deterministic validation، وليس مجرد Layer مكرر.

## الاختبارات

- Tests الجديدة: **6/6 passed**.
- Template regression v0.15→v0.20: **38/38 passed**.
- Full collected suite: **363 tests**.
- كل المجموعات تم تشغيلها بنجاح، بما فيها feature tests وregression/stress وlegacy v0.8.
- `compileall`: passed.
- OpenAI reviewer HTTP path: mocked؛ تم التأكد من إرسال visual + candidate واستخدام schema باسم `document_template_semantic_review`.

## Limit

لم يتم إجراء live external OpenAI call في هذا البناء. اختبار الـAI adapter يثبت الـorchestration/contract/visual payload. جودة موديل حي على dataset واقعي ما زالت Benchmark منفصلة.
