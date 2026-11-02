# v0.16 — AI-Generated Template Candidates

## القرار المعماري

في v0.15 أثبتنا أن الـTemplate Runtime نفسه يعمل جيدًا عندما يكون الـTemplate صحيحًا: المستند المتكرر يمكن مطابقته ثم قص الحقول المتوقعة فقط وتشغيل OCR/Consensus عليها بدل إعادة فهم المستند من الصفر.

v0.16 ينقل **تعلم الـTemplate لأول مرة** إلى AI/Vision Provider بدل الاعتماد على Regex/Geometry learner كمسار رئيسي.

```text
First unseen document
        ↓
Generic extraction context + original visual pages
        ↓
AI/Vision TemplateGenerationProvider
        ↓
STRICT TemplateGenerationProposal
        ↓
Core deterministic validation
        ↓
Candidate only
        ↓
Human approval
        ↓
Immutable approved Template v1
        ↓
Repeated documents → cheap match → direct field crops → OCR consensus
```

## ما الذي يفعله الـAI؟

الموديل يقترح فقط:

- `document_kind`؛
- الحقول الدلالية مثل `invoice_number`, `date`, `subtotal`, `tax`, `total`؛
- stable anchors مثل `Invoice No`, `TOTAL`, `Date`؛
- `normalized_value_bbox` و`normalized_label_bbox` داخل `[0,1]`؛
- regex/schema يصف **شكل القيمة** ولا ينسخ قيمة المستند الحالي؛
- repeating-table region، headers، وأسماء الأعمدة؛
- anchors/fingerprint hints للمطابقة المستقبلية.

الـAI **لا يعتمد Template ولا يثبت قيمة مالية**.

## ما الذي يظل مسؤولية الـCore؟

الـCore يظل صاحب السلطة في:

1. التحقق من العقد.
2. التأكد أن الـbbox داخل الصفحة.
3. التأكد أن page references داخل عدد الصفحات الحقيقي.
4. منع أسماء الحقول والجداول المكررة.
5. منع Required fields مختلفة من مشاركة نفس tight value region تقريبًا.
6. التأكد أن regex صالح ومحدود.
7. التأكد أن الجدول له location قابلة لإعادة الاستخدام.
8. فرض `required_fields` التي يطلبها caller حتى لو الـAI نسيها.
9. Content hash قبل الاعتماد.
10. Human approval الصريح.
11. Immutable versioning وdrift detection.
12. OCR/field consensus بعد مطابقة Template.

## Build modes

### `AI_REQUIRED`

المسار الموصى به عند onboarding مستند جديد في Production.

لو لم يتم حقن Provider:

```text
TEMPLATE_AI_PROVIDER_REQUIRED
```

ولا يتم الرجوع تلقائيًا إلى heuristic learner.

### `AI_PREFERRED`

يستخدم AI لو موجودًا، وإلا يحافظ على deterministic bootstrap القديم للمراجعة البشرية.

### `DETERMINISTIC`

يمنع AI عمدًا ويستخدم bootstrap المحلي.

## OpenAI Responses Adapter

تمت إضافة integration اختيارية:

```text
data_extraction/
  integrations/
    template_generation/
      openai_responses.py
```

`OpenAIResponsesTemplateProvider`:

- يرسل الصورة الأصلية أو أول صفحات PDF بعد render آمن؛
- يستخدم normalized visuals، لذلك تغيير resolution لا يغير إحداثيات الـTemplate؛
- يرسل النص المستخرج كـcontext مساعد، وليس كمصدر وحيد؛
- يطلب Structured Output بعقد JSON Schema صارم؛
- يستخدم Responses API؛
- `store_response=False` افتراضيًا؛
- لا يقرأ API key من الكود: إما constructor أو `OPENAI_API_KEY`؛
- يسجل `openai_model:*` و`openai_response_id:*` فقط في diagnostics عند توفره، وليس محتوى المستند.

الموديل configurable. الـCore غير مربوط بـOpenAI؛ أي Provider آخر يمكنه تنفيذ `TemplateGenerationProvider`.

## Validation جديد بعد تجربة v0.15

تجربة الفاتورة في v0.15 أظهرت أن Template generator ضعيف يمكن أن يضع `Subtotal` و`TOTAL` على نفس المنطقة ثم Runtime يكرر الخطأ باستمرار.

v0.16 يمنع هذا النوع من الخطأ للحقول المطلوبة ذات الـtight regions:

```text
invoice_number bbox ─┐
                     ├─ near-identical → REJECT candidate
TOTAL bbox ──────────┘
```

الـlarge full-page placeholder regions الخاصة بالـdeterministic bootstrap لا تعامل كأنها precise AI claims؛ تظل Candidate تحتاج Refinement/Approval.

## Clean-wheel smoke test

استخدمنا صورتي NOVA الحقيقيتين من benchmark v0.15:

- first invoice: `INV-2026-001`, date `01/11/2026`, total `106.05`؛
- repeated invoice: `INV-2026-002`, date `02/11/2026`, subtotal `118.50`, VAT `5.93`, total `124.43`.

في البيئة الحالية لا يوجد OpenAI API key، لذلك تم استخدام `httpx.MockTransport` لإرجاع نفس شكل Responses API Structured Output. الاختبار يثبت **integration path** وليس جودة model خارجية.

النتيجة:

```text
AI HTTP requests                 1
Visual image included            yes
store=false                      yes
candidate generator              OpenAIResponsesTemplateProvider
candidate status                 candidate
approved status                  approved
repeated match                   accepted
match score                      1.0
generic extraction repeated doc  false
invoice_number                   INV-2026-002 ✅
date                             02/11/2026 ✅
subtotal                         118.50 ✅
tax                              5.93 ✅
total                            124.43 ✅
required fields verified         yes
semantic_complete                true
requires_review                  false
```

Field values ما زالت تتطلب OCR consensus مستقل؛ الـAI Template لا يجعلها verified.

## الاختبارات

- إجمالي collection: **343**.
- إجمالي passed: **343/343** موزعة على مجموعات بسبب timeout التشغيل الأحادي الطويل.
- اختبارات v0.16 الجديدة: **5**.
- `compileall`: Passed.
- Wheel build: Passed.
- Clean install: Passed.
- Clean-wheel AI integration smoke: Passed.

## ما لم نثبته بعد

1. لم يتم استدعاء OpenAI API الحقيقي لعدم وجود API key في runtime.
2. لذلك لا ندعي أن `gpt-6.1-sol` أو أي موديل آخر سيولد Template صحيحًا 100% على SROIE/ZATCA.
3. جودة الـAI candidate يجب قياسها لاحقًا بـTemplate accuracy benchmark: field-region IoU، anchor correctness، required-field recall، table-region IoU، ثم repeated-document extraction accuracy.
4. Human approval يظل مطلوبًا قبل reuse التلقائي.

## الخلاصة

v0.16 يجعل التصميم المقصود واضحًا: **AI يفهم المستند الجديد ويقترح Template، بينما الـCore يظل السلطة الحتمية في validation/approval/versioning/matching/verification.**
