# Checkpoint v0.7.0 — تنظيم Feature-First

## القرار
إعادة تنظيم **الكود الموجود** وليس كتابة استخراج جديد. المخرجات والعقود العامة القديمة محفوظة.

## تدفق التشغيل

`DataExtractionService` → `inspect_document/service.py` (مع `detect_file`) → `analyze_layout/service.py` (اختياري) → `dispatch_extraction/extract_structured_tables.py` (اختياري) → `dispatch_extraction/service.py` → `dispatch_extraction/extract_text.py` أو `extract_table.py` أو `extract_image.py` → `dispatch_extraction/assemble_document.py` → `validate_extraction/service.py` → `ExtractionResult`.

`inspect()` لا يشغّل OCR ولا محركات Document AI. حالة فشل Provider والـReview Flags لم تتغير.

## التغييرات الجوهرية

- `actions/detect_file_action.py` → `detect_file/service.py`.
- `actions/inspect_file_action.py` → `inspect_document/service.py`.
- `actions/analyze_layout_action.py` و`describe_layout_action.py` و`resolve_reading_order_action.py` → `analyze_layout/`.
- `router.py` انقسم فعليًا: التوزيع في `dispatch_extraction/service.py` وتجميع النتيجة في `dispatch_extraction/assemble_document.py`.
- `extract_text_action.py`, `extract_table_action.py`, `extract_image_action.py`, `extract_structured_tables_action.py` → ملفات داخل `dispatch_extraction/`.
- `native_adapters.py` اتفصل إلى `providers/native/pdf.py`, `word.py`, `excel.py`, `flat_files.py`, `common.py`, `adapter.py`.
- Docling وUnstructured بقوا تحت `providers/` كـAdapters مستقلة؛ وجودهم اختياري.
- OCR/Vision Protocols بقوا في `providers/contracts.py` وTesseract في `providers/ocr/tesseract.py`.
- `models.py` و`errors.py` نقلنا التنفيذ إلى `shared/`؛ المسارات القديمة تظل Aliases.
- الـUpload Validation والـProvider Output Validation والـReadiness Gate في `validate_extraction/`.
- تم إضافة Tests محلية داخل فولدرات الـFeatures؛ الاختبارات القديمة **لم تُنقل أو تُحذف** وتعمل كـRegression.

## التوافق

1. `DataExtractionService`, `DocumentInput`, `ExtractionOptions`, `FileManifest`, `ExtractionResult` تبقى قابلة للاستيراد من `data_extraction`.
2. المسارات القديمة تظل تعمل مؤقتًا من خلال Re-exports، وهي طبقة توافق فقط؛ لا يوجد نسخ للكود التنفيذي.
3. لا تغيير في JSON Schema: `extraction.v0.6`.
4. الـDispatcher ينفذ الاستخراج، و`assemble_document.py` فقط يعيد تركيب النتائج. لا تكرار للأدوار.
5. لا Chunking أو RAG أو Agents أو Reindexing داخل المشروع.

## إثبات عدم التغيير

- 230 اختبارًا سابقًا + 33 اختبارًا جديدًا = 263 ناجحًا.
- تشغيل فعلي للـNative PDF على ملف عربي من خمس صفحات وعلى PDF جدول تجريبي؛ نفس JSON بالضبط بين المصدر الأصلي v0.6.0 والمصدر المعاد تنظيمه في عمليتي inspect وextract (ImageRoute.DISABLED).
- الـCLI أصبح يكتب JSON صالحًا إلى stdout ويحوّل رسائل الطرف الثالث إلى stderr؛ لم يتم إخفاء الأخطاء.
- بدون تشغيل حي لنماذج Docling أو Unstructured. Accuracy على الصور والجداول المعقدة لم يتم تقييمها في هذا الـCheckpoint.

## اختبارات لاحقة

تشغيل نسختين في بيئة معزولة بكل Provider حقيقي، Golden Dataset عربي/إنجليزي، ومقارنة دقة المحتوى وبنية الجداول وترتيب القراءة والوقت والذاكرة والتكلفة. دي مرحلة Benchmark مستقلة عن إعادة تنظيم الملفات.
