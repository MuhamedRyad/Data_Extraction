# v0.15 — Template Learning & Reusable Structured Extraction

## لماذا أضفنا الـTemplate؟

اختبارات v0.12–v0.14 أثبتت أن تحسين OCR العام وحده لا يمكنه ضمان قراءة كل فاتورة أو إيصال. نفس الرقم قد يختلف بسبب جودة المسح، الخط، الضغط، اللغة أو المحرك. v0.15 يغيّر السؤال من «اقرأ أي فاتورة من الصفر» إلى «هل هذا Layout معروف؟ وإذا كان معروفًا، أين توجد الحقول التي يجب إعادة قراءتها؟».

## المسار الجديد

```text
Unknown document
    ↓
Generic extraction
    ↓
TemplateGenerationProvider (AI/Vision اختياري)
    ↓
Candidate only
    ↓
Core validation + human approval
    ↓
Versioned Template Store

Repeated similar document
    ↓
Cheap preflight match
    ↓
Known layout?
 ┌── yes ───────────────────────────┐
 │ normalized field crops           │
 │ independent OCR verification     │
 │ schema / consensus               │
 └───────────────────────────────────┘
    ↓ no / ambiguous / drift
Full generic extraction + review / new template version
```

## ما الذي يحفظه الـTemplate؟

- `template_id`, `family`, `version`, `status`.
- نوع المستند المتوقع.
- Anchors نصية متوقعة.
- `normalized_label_bbox` و`normalized_value_bbox` من 0 إلى 1 بدل Pixel coordinates ثابتة.
- Regex/schema للحقل، وOCR-confusion template اختياري.
- repeating table definitions والـheader anchors.
- page count / aspect ratios / region counts / table counts.
- first-page visual average hash كإشارة Match رخيصة، وليس كهوية موثوقة.
- `content_hash` يحمي Candidate من التعديل بين التوليد والاعتماد.

## قاعدة الأمان الأساسية

الـTemplate **لا يثبت قيمة**. هو يحدد أين نبحث فقط.

مثال: لو الـTemplate يقول إن `TOTAL` موجود في أسفل يمين الصفحة، v0.15 يستطيع Crop هذه المنطقة مباشرةً وتشغيل OCR عليها. القيمة لا تصبح `verified_consensus` إلا إذا اتفق عدد المصادر المستقلة المطلوب في `FieldVerificationOptions`.

```text
Template matched = 98%
TOTAL crop OCR A = 112.45
TOTAL crop OCR B = 112.45
→ verified_consensus

Template matched = 98%
TOTAL crop OCR A = 112.45
TOTAL crop OCR B = 112.46
→ review_required
```

## Fast Path

في Template field-only معروف ومعتمد، `extract_with_template()` يبدأ بـ`inspect()` فقط ثم Preflight Match. إذا كان Match قويًا، وكل Required Field له Anchor أو normalized value bbox، لا يتم تشغيل الـGeneric extraction الكامل. عند وجود normalized bbox يمكن قص الحقل مباشرةً بدون Whole-page Layout OCR.

إذا كان الـTemplate يحتاج جدول بنود، أو الـMatch غير مؤكد، يتم الرجوع للمسار الكامل. هذه نقطة مقصودة: لا نوفر تكلفة على حساب فقد Structure.

## Matching وDrift

الإشارات المستخدمة حسب المتاح:

- page count;
- page aspect ratios;
- visual fingerprint للـPDF/Image؛
- native text anchors إن كانت متاحة في `inspect()`;
- normalized anchor geometry;
- document kind وtable structure في Full Match.

الـvisual hash إشارة بسيطة وليس Document identity. عند البحث عبر Families من غير native anchors لا يتم اعتماد Visual-only match تلقائيًا. كما أن Templates المتقاربة جدًا تدخل Review بدل اختيار واحد صامتًا.

## Versioning

الـTemplate لا يُعدّل In-place. `template_id + version` immutable في `InMemoryTemplateStore` و`SQLiteTemplateStore`. عند تغيّر التصميم ننشئ Version جديدًا، مثل:

```text
supplier-a-invoice
  v1  approved
  v2  candidate → approved
```

## AI Provider

الـCore لا يربط نفسه بموديل بعينه. العقد هو `TemplateGenerationProvider.generate_template(document, context)`. يمكن توصيل Vision/LLM خارجي لاحقًا. Output الموديل لا يدخل Production تلقائيًا:

1. bounded contract validation;
2. required-field enforcement;
3. regex/bbox validation;
4. content hash;
5. human approval.

## Repeating tables

الـTemplate يمكن أن يصف جدولًا متكررًا. إذا وجدنا Table candidate مطابقًا، نحافظ على الصفوف كـ`TemplateTableResult`. لو الجدول مصدره OCR/Document AI غير موثوق، `verified=False` و`template_table_values_require_independent_verification`، حتى لو Structure matched.

## ما لم ندّعِ حله

- v0.15 لا يجعل Tesseract/EasyOCR دقيقين 100%.
- لا يوجد AI model bundled داخل الحزمة؛ يوجد Provider contract.
- visual average hash ليس مقاومًا لكل perspective/rotation أو layouts المتشابهة جدًا.
- direct normalized bbox مناسب للـlayouts المتقاربة؛ الانحراف الكبير يذهب إلى fallback/drift path.
- table OCR values لا تصبح مالية موثقة بمجرد نجاح Template match.
- أول Template يحتاج مراجعة/اعتماد؛ لا يوجد auto-promotion من AI إلى Production.

## نتيجة الاختبارات

- إجمالي: **338 passed / 0 failed**.
- v0.15 focused: **13 tests**.
- تم اختبار: candidate/approval gate، hash tamper rejection، SQLite persistence، next version، match rejection، visual fingerprint، direct-bbox fast path، عدم تشغيل Generic/Layout OCR في الحالة المناسبة، required field enforcement، repeating-table behavior، وOCR consensus.
