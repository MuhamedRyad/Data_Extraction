# v0.18.0 — Template Self-Validation & Geometry Refinement

## الهدف

الإصدار v0.18 يحل المشكلة التي ظهرت في الاختبار البصري لـ v0.17: نموذج AI قد يفهم معنى الحقل بشكل صحيح لكنه يضع `normalized_value_bbox` بشكل تقريبي، فتقص منطقة ناقصة أو تحتوي label/punctuation زائد.

المسار الجديد:

```text
AI Generate
  ↓
Template Candidate
  ↓
Core crops proposed fields
  ↓
OCR + schema check
  ├─ schema-valid crop → keep geometry
  └─ invalid crop → AI geometry refinement
                     ↓
                 bounded retry
                     ↓
                 crop/schema recheck
  ↓
Candidate remains unapproved
  ↓
Human approval
```

## ما يسمح للـAI بتغييره

مرحلة الـrefinement مسموح لها بتغيير:

- `normalized_value_bbox`
- `normalized_label_bbox`

ولا يسمح لها بتغيير:

- field name / kind
- required flag
- page number
- labels / anchors
- regex / template
- confidence threshold
- table semantics
- document kind

الـCore يرفض أي محاولة تغيير semantic contract بكود `TEMPLATE_REFINEMENT_*`.

## معنى نجاح self-validation

`template_geometry_self_validation_passed` لا يعني أن القيمة المالية Verified. معناه فقط أن crop المقترح قادر على إنتاج قيمة تطابق schema من OCR observation واحدة على الأقل. الاعتماد المالي النهائي ما زال يحتاج `verified_source` أو `verified_consensus` حسب Field Verification policy.

## حدود المحاولات

`TemplateExtractionOptions.max_refinement_attempts` افتراضيًا = 2. لا يوجد loop غير محدود.

إذا لم ينجح refinement:

- يبقى Template `candidate`.
- يضاف `template_geometry_self_validation_incomplete`.
- لا يتم اعتماده تلقائيًا.

إذا فشل AI provider تقنيًا، يبقى candidate للمراجعة ولا يتحول الفشل التقني إلى semantic success.

## OpenAI adapter

`OpenAIResponsesTemplateProvider` يدعم الآن `refine_template()` بالإضافة إلى `generate_template()`.

طلب refinement يرسل:

- visual page/image
- current complete template
- core crop/schema feedback
- attempt number / max attempts

ويطلب Structured Output كامل. ومع ذلك الـCore لا يثق بأي semantic changes في الرد.

## الاختبار الواقعي

على فاتورة NOVA الأولى:

- AI simulation أعطى geometry خاطئة عمدًا لـ`invoice_number` و`total`.
- Tesseract crop check لم يجد schema-valid values.
- v0.18 طلب refinement مرة واحدة.
- geometry تعدلت إلى المناطق الصحيحة.
- final crop/schema check نجح.

على فاتورة NOVA الثانية بنفس التصميم:

- template match = `1.0`
- status = `accepted`
- generic extraction = `false`
- `invoice_number = INV-2026-002`
- `total = 124.43`

في الاختبار الواقعي استخدمنا OCR provider واحد فقط، ولذلك القيم بقيت `review_required` بسبب عدم وجود independent consensus. هذا مقصود ولا يعد فشلًا للـgeometry refinement.
