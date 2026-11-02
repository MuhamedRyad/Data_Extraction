# Data Extraction v0.9.0 — دمج أفضل ميزة متبقية من الكود القديم

**الأساس:** v0.8.0  
**النطاق:** Data Extraction فقط. لا Chunking ولا RAG ولا Knowledge approval.  
**الهدف:** نقل قوة الـField-Level / Sensitive-Value OCR من `document_ingestion` إلى تصميم Generic وآمن.

## ما الذي تم أخذه من القديم؟

القديم كان أقوى في أربع نقاط مترابطة:

1. اكتشاف Label للحقل ثم تحديد منطقة القيمة بالـgeometry.
2. إعادة OCR للـField crop بدل الاعتماد على OCR الصفحة كلها.
3. مقارنة أكثر من قراءة للقيمة الحساسة.
4. التحقق من القيمة باستخدام Schema قبل اعتمادها، مع إبقاء الحالات الملتبسة للمراجعة.

تم نقل **الفكرة والقدرة** وليس القواعد الخاصة بالدومين. لا توجد قواعد hard-coded مثل `INV-...` أو `PAY-...` داخل الـCore؛ المستهلك يعرّف الحقول بنفسه.

## التصميم الجديد

```text
data_extraction/
  verify_fields/
    request.py       # FieldDefinition + FieldVerificationRequest
    response.py      # candidates/status/evidence
    options.py       # budgets + geometry thresholds
    service.py       # label → value region → crop → OCR ensemble → schema/consensus
    validators.py
    errors.py
```

أضيف كذلك عقد اختياري عام:

```python
OCRLayoutProvider.recognize_regions(image_bytes, mime_type)
```

يرجع `OCRTextRegion(text, bbox, confidence)`، ولذلك يستطيع الـFeature العمل على Scan/Image وليس فقط PDF native text. `EasyOCRProvider` و`TesseractOCRProvider` يدعمان العقد الجديد مع الإبقاء على `OCRProvider.recognize()` كما هو.

## قاعدة الأمان الأساسية

`verify_fields()` **لا يغير** نتيجة `extract()`.

الحالات:

- `verified_source`: قيمة Native source تطابق الـSchema ولا يوجد تعارض قوي.
- `verified_consensus`: قراءتان مستقلتان أو أكثر اتفقتا على نفس قيمة Schema-valid.
- `review_required`: يوجد Candidate محتمل لكن الأدلة غير كافية أو متعارضة.
- `provider_unavailable`: المصدر غير كاف ولا يوجد OCR مستقل.
- `label_not_found` / `value_region_not_found`: نقص صريح، وليس نجاحًا صامتًا.

الـTemplate (`A`, `a`, `9`, `X`) يسمح بتوليد اقتراح deterministic لأخطاء OCR الشائعة مثل `O/0` و`I/1`، لكن **الـtransform وحده لا يصبح Verified**. يحتاج Independent consensus.

## مثال Generic

```python
field = FieldDefinition(
    name="invoice_number",
    labels=("Invoice Number", "رقم الفاتورة"),
    pattern=r"^INV-\d{4}-[A-Z]\d{3}$",
    template="INV-9999-A999",
    minimum_confidence=0.80,
)
```

هذا مجرد Config من المستهلك وليس Business Rule داخل المكتبة.

## ما لم يتم نسخه من القديم؟

- لا يتم تعديل `ParsedBlock.text` أو النص المستخرج تلقائيًا.
- لا توجد قواعد Kuwait/domain-specific للأرقام أو العملات أو Prefixes.
- لا يتم اعتبار OCR confidence وحده حقيقة.
- لا يتم استخدام schema transform كدليل مستقل.
- Document review / reindexing خارج نطاق Data Extraction.

## Budgets / Fail-closed

`FieldVerificationOptions` يحدد حدودًا لعدد الحقول، occurrences، طول candidate، page/crop pixels، وعدد OCR calls. فشل OCR أو crop يظهر في diagnostics ويترك الحقل للمراجعة بدل اختراع نتيجة.

## الاختبارات

الإصدار v0.9.0: **293 passed**.

الاختبارات الجديدة تشمل:

- Native field صحيح بدون OCR.
- قيمة Native خاطئة + اتفاق محركي OCR مستقلين.
- Template transform من محاولة واحدة يظل Review-only.
- Scan/Image: OCR layout geometry ثم crop verification.
- Email malformed لا يُقبل بصمت.
- Label مفقود يرجع حالة صريحة.
- Arabic-Indic digits normalization كدليل مع الحفاظ على source extraction.

هذه الاختبارات تثبت العقود والسلوك، وليست Benchmark لـCER/WER الحقيقي لمحركات OCR.
