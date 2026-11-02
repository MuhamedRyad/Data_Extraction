# Data Extraction v0.10.0 — Recovery Pipeline & Region-Level Completeness

## الهدف

v0.10 لا يضيف Warning Detection فقط؛ يحول الحالات القابلة للاسترجاع إلى Recovery فعلي ومحدود الموارد، مع فصل واضح بين:

- اكتمال المحتوى `content_complete`
- الحاجة للمراجعة `requires_review`
- الجاهزية الصارمة للنشر `ready_for_publication`
- حالة الاستخراج `complete / complete_with_review / partial_success / failed`

`ready_for_publication` بقي strict حفاظًا على التوافق الأمني، لكن لم يعد هو الإشارة الوحيدة التي يضطر downstream للاعتماد عليها.

## 1. Adaptive image recovery

عندما تتجاوز الصورة أو PDF render قيمة `max_image_pixels`:

1. لا يتم رفع الحد تلقائيًا.
2. إذا `adaptive_image_recovery=True` يتم خفض الحجم / DPI داخل الميزانية.
3. لا يسمح بالهبوط تحت `min_image_dpi` في PDF rendering.
4. يسجل `image_downscaled_for_processing_review`.
5. إذا تعذر البقاء داخل الحدود الآمنة يظل `IMAGE_TOO_LARGE` fail-closed.

هذا يشمل standalone images، PDF embedded/vector/page render، وكذلك render المستخدم للـLayout Provider.

## 2. Active OCR recovery

المسار الأساسي للصورة أصبح:

Native discovery → OCR → bounded tiled OCR عند ضعف OCR الكبير → Vision fallback → unresolved.

Tiled OCR:

- اختياري وقابل للإيقاف.
- لا يستخدم لإعادة بناء raster tables لأن تقسيم الجدول قد يدمر البنية.
- محدود بـ `max_ocr_tile_calls_per_region`.
- الناتج يظل Review Evidence (`ocr_tiled_recovery_review`).
- لا يدعي authority أو confidence غير الذي أعاده provider.

## 3. Vector recovery

Vector candidates يتم rasterize ثم تمر عبر نفس OCR/Vision recovery.

إذا نجح provider:

- `content_complete=True`
- `recovery_applied=True`
- `recovery_notes` تحتوي `vector_raster_recovery`

مع بقاء `vector_graphics_detected_review` للمراجعة. بهذا يصبح الـVector Detection trigger للمعالجة عندما يوجد provider، وليس مجرد warning فقط.

## 4. Region-level completeness

كل `ExtractedRegion` أصبح يحمل:

- `content_complete`
- `critical_for_completeness`
- `recovery_applied`
- `recovery_notes`

وكل Page/Document يحمل:

- `content_complete`
- `completeness_ratio`
- `unresolved_region_count`
- `review_region_count`
- `extraction_status`

الحالات:

- `complete`: المحتوى كامل ولا توجد مراجعة.
- `complete_with_review`: المحتوى كامل لكن توجد Evidence تحتاج مراجعة.
- `partial_success`: تم استخراج جزء معتبر لكن توجد مناطق حرجة غير مستخرجة.
- `failed`: لا توجد منطقة محتوى مكتملة.

## 5. Decorative candidates

Tiny embedded raster graphics يمكن اعتبارها non-critical لاكتمال المحتوى فقط عندما:

- لها geometry معروفة.
- دورها `embedded_image` أو `worksheet_drawing`.
- مساحتها أقل من `decorative_region_max_area_ratio`.

المنطقة لا تُحذف ولا تختفي من النتائج، وتظل `requires_review` و`ready_for_publication` يظل strict. هذا يمنع صورة زخرفية صغيرة من مساواتها بصورة scan كبيرة أو جدول غير مستخرج في completeness metric.

## 6. Local OCR optional auto-configuration

`auto_enable_local_ocr=True` يجهز Tesseract adapter محليًا عندما يختار المستخدم ذلك. لا يوجد download تلقائي ولا cloud call مخفي. إذا runtime/dependency غير متاح، provider failure يعالج fail-closed.

Default يبقى `False` للحفاظ على deterministic dependency injection.

## 7. Generic field discovery

أضيف `discover_fields()` كميزة اختيارية منفصلة عن `verify_fields()`.

تكتشف فقط أشكال عامة:

- email
- URL
- phone
- date/time
- money
- identifier
- numeric code

ولا تستنتج business semantics مثل `invoice_number` أو `civil_id`. بعد discovery يمكن للدومين بناء `FieldDefinition` ثم استخدام `verify_fields()` للتحقق المستقل.

## 8. Reading order

الـnative PDF column resolver الموجود سابقًا يظل يطبق ترتيب الأعمدة عندما تكون الهندسة كافية، والـLayout Provider يظل fallback اختياريًا. v0.10 أصلح فشل Render الصفحات الكبيرة للـLayout Provider باستخدام adaptive budget.

لم يتم الادعاء بأن أي heuristic يضمن semantic reading order بنسبة 100%. Mixed RTL/LTR داخل النص يظل محافظًا: proposal منفصل ما لم توجد Evidence كافية؛ لا يتم تغيير source text صامتًا.

## 9. ما لم نعتبره Bug

- OCR/Vision الحقيقي يحتاج Provider فعليًا؛ لا يمكن استخراج pixels دلاليًا بدون engine.
- Docling/Unstructured يظلان optional integrations.
- Document-AI tables تظل derived overlays وليست authoritative source replacement.
- Full Job Checkpoint/Resume ليس جزءًا من v0.10؛ يظل Feature مستقلة لاحقة.

## الاختبارات

- v0.9 baseline: 293 passed.
- v0.10: 302 passed.
- 9 حالات جديدة تغطي adaptive image/PDF rendering، vector recovery، partial success، tiny decorative completeness، tiled OCR، field discovery، وadaptive layout render.
- Local Tesseract smoke test نجح على صورة مولدة: `Invoice AB-12345 Total 120.50`، method=`ocr`، confidence≈0.924، status=`complete_with_review`.

هذه الاختبارات تثبت السلوك والـcontracts، وليست Ground-Truth benchmark لثلاثة ملفات المستخدم الحقيقية التي لم تكن متاحة في جلسة البناء الحالية.
