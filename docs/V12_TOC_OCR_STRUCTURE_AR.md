# v0.12 — إصلاح الفهارس وOCR والجداول المصوّرة وقياس أثر المناطق غير المحلولة

## لماذا هذه النسخة؟

المراجعة الواقعية لـv0.11 أظهرت فرقًا بين وجود النص وبين الحفاظ على علاقته البنيوية. أهم مثال: الفهرس قد يحتوي العنوان ورقم الصفحة، لكن PDF يخزنهما في Blocks منفصلة أو يجمع رقمين متساويين مثل `33\n33`. استخراج النص وحده لا يضمن أن downstream يعرف أي `33` يخص أي عنوان.

v0.12 يعالج هذه الفجوة من غير تغيير النص الأصلي أو تخمين معنى تجاري.

## 1) Table of Contents كهيكل، وليس مجرد ترتيب نص

يتم جمع Native PDF lines مع Bounding Boxes، ثم البحث عن ثلاثة صفوف أو أكثر فيها:

- Title نصي.
- Page number رقمي فقط، مع دعم الأرقام العربية/الفارسية العشرية.
- تقارب رأسي قوي وفصل أفقي واضح.

عند وجود Evidence كافٍ يتم إنشاء Region من نوع Table:

```text
semantic_role = table_of_contents
rows = [
  [title_1, page_1],
  [title_2, page_2],
  ...
]
```

الأرقام المتكررة لا تندمج لأن الربط يعتمد على Y/geometry وليس على قيمة الرقم. النصوص الأصلية لا تُحذف؛ يتم وسمها `toc_source_text_support` وربطها بالـTOC table كـ`parent_region_id`.

هذا يمنع downstream من اضطراره لإعادة بناء العلاقة من نص مسطح.

## 2) تصنيف صفحة الفهرس

وجود TOC structured evidence يسبق قواعد `diagram_hierarchy`. لذلك الصفحة تحصل على:

```text
page_semantic_table_of_contents
```

بدل توجيهها لمسار مخطط هرمي لمجرد وجود خطوط/بطاقات Vector.

## 3) Low-confidence OCR recovery

إذا OCR منخفض الثقة ولا يوجد Vision fallback، يمكن تشغيل bounded preprocessing:

- grayscale
- autocontrast
- sharpen

هذه المحاولات ليست Providers مستقلة ولا تعتبر Consensus. إذا تحسنت النتيجة، يتم الاحتفاظ بـReview reasons مثل:

```text
ocr_preprocessing_recovery_review
ocr_preprocessing_text_changed_review
```

المصدر لا يتغير، ولا يتم الادعاء أن النص أصبح Ground Truth.

إذا Vision Provider موجود، يحتفظ المسار بالسلوك الأرخص/الأقوى: OCR ثم Vision بدل إضافة OCR passes غير ضرورية.

## 4) الجداول المصوّرة

تم إضافة optional token-geometry contract للـOCR. Tesseract وEasyOCR adapters يمكنهما إرجاع token boxes. عندما تكون الصورة Raster grid table:

1. OCR text.
2. token geometry.
3. grouping rows by Y.
4. stable column centers by X.
5. candidate structured table.

لا يتم اختراع خلية مفقودة. لو geometry غير مستقرة، يرجع النص كما هو مع `raster_table_structure_not_reconstructed`.

لو نجحت reconstruction فهي Review-only:

```text
ocr_table_geometry_reconstructed_review
ocr_table_structure_requires_verification
```

## 5) Mixed Arabic / numbers

v0.12 لا يعكس Strings العربية تلقائيًا. الإصلاح المؤكد هنا هو ربط الأرقام بالعناصر التي تخصها عندما تكون العلاقة هندسية مثل الفهرس أو خلايا الجدول. الحالات العامة المختلطة RTL/LTR ما زالت تحت Evidence/Review بدل إعادة كتابة النص بلا يقين.

## 6) المناطق المتكررة غير المحلولة

Raw count ما زال محفوظًا:

```text
unresolved_region_count
```

لكن تمت إضافة:

```text
critical_unresolved_region_count
noncritical_unresolved_region_count
```

Repeated edge images/navigation bars يمكن اكتشافها عبر content hash + edge location عبر عدة صفحات. المنطقة لا تُحذف ولا تعتبر مستخرجة؛ لكنها لا تساوي محتوى فريدًا مفقودًا في completeness impact.

## 7) حدود التحقق

تم العثور على الأصل `ش__ناير_2026.pdf` المستخدم لإنشاء `shnayer_first5.pdf`. مقارنة أول خمس صفحات أثبتت:

- raster render bytes match: 5/5.
- extracted text match: 5/5.

وبالتالي المقارنة السابقة لهذه العينة أصبحت مرتبطة بأصل متاح، وليس ملفًا مشتقًا مجهول المصدر.

لكن الملفين التاليين المذكورين بمسارات Windows المحلية لم يكونا متاحين في runtime:

- `consumer_rights_guide_pilot_4pages.pdf`
- `ministry_user_guide_textual.pdf`

لذلك لا يوجد ادعاء بأن v0.12 تم قياسه بصريًا على هذين الأصلين. حالات الفشل نفسها مغطاة باختبارات Regression صناعية إلى أن تتوفر الملفات.

كما أن حالة `figure inside columns` المعقدة ونتيجة 0.8571 السابقة ما زالت حدًا معروفًا، وليست مشكلة تم الادعاء بحلها في v0.12.
