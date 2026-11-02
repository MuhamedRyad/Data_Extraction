# Detect File Service Refactor

## Intent

إزالة الازدواجية بين `DocumentInput` و`DetectFileRequest`، وجعل
`DetectFileService` نقطة التنفيذ الوحيدة للـ detect-file feature.

## Goals

- استخدام `DetectFileRequest` كـ input contract الوحيد.
- حذف `DocumentInput` من feature وإلغاء التحويل `to_domain()`.
- حذف `DetectFileAction` و`_DetectFileCore`.
- إبقاء سلوك اكتشاف PDF والصور وDOCX/XLSX وCSV وملفات النص كما هو.
- تحديث الاختبارات لتستخدم `DetectFileService` و`DetectFileRequest`.

## Non-goals

- إضافة أنواع ملفات جديدة.
- تغيير شكل `DetectFileResponse` أو أخطاء الـ router.
- تغيير سلوك validation أو رسائل الأخطاء الحالية.

## Approaches

1. الإبقاء على `DocumentInput` و`_DetectFileCore` مع حذف facade فقط: أقل تغيير،
   لكنه يترك الازدواجية.
2. تمرير raw mapping إلى service وحذف request contract: يقلل عدد الملفات،
   لكنه يخلط validation بالـ service.
3. **المختار:** جعل `DetectFileRequest` هو input الوحيد، ونقل منطق الكشف إلى
   `DetectFileService` وحذف الـ DTO والـ facade والـ core helper. هذا يزيل
   الازدواجية ويحافظ على فصل validation في الـ request والـ routing في الـ router.

## Remaining implementation

- تعديل `request.py` لإزالة `DocumentInput` و`to_domain()`.
- تبسيط `service.py` إلى `DetectFileService` واحد.
- تحديث `tests/test_detect_file.py` وإضافة تغطية لمسار `Result`.
- تشغيل اختبار feature ثم suite المشروع كاملة.
