# v0.19 — Template-Aware Repeating Tables

## المشكلة التي تم إصلاحها

اختبار v0.18 على فاتورة NOVA أثبت أن الحقول الثابتة كانت صحيحة 5/5، لكن جدول البنود كان يعتمد على Generic OCR Table Reconstruction. النتيجة كانت دمج `Description + Qty + Unit Price` في خلية واحدة، واعتبار `Subtotal / VAT / TOTAL` صفوفًا من جدول البنود.

v0.19 لا يحاول تحسين heuristic عام جديد. بدل ذلك يستفيد من الـTemplate نفسه.

## عقد الجدول الجديد

الـAI يستطيع اقتراح:

- `normalized_bbox`: منطقة الجدول العامة.
- `normalized_header_bbox`: منطقة Header.
- `normalized_body_bbox`: منطقة الصفوف المتكررة فقط.
- `column_definitions`: اسم كل عمود وحدوده الأفقية النسبية داخل body crop.
- `value_kind`: تلميح عام مثل `text`, `integer`, `decimal`.
- `stop_anchors`: علامات توقف مثل `Subtotal`, `VAT`, `TOTAL`.
- `minimum_rows`: أقل عدد صفوف متوقع قبل اعتبار الجدول مكتملًا.

كل geometry تظل normalized وليست pixel coordinates ثابتة.

## مسار الاستخراج المعروف

```text
Approved Template
      ↓
Preflight Match
      ↓ accepted
Crop repeating-row body only
      ↓
OCR token geometry
      ↓
Group tokens by row
      ↓
Assign token to known semantic column
      ↓
Required cell missing?
   ├─ no  → keep token result
   └─ yes → OCR that cell only
      ↓
Stop anchor reached?
   ├─ yes → stop; summary is not a line item
   └─ no  → continue
```

المسار لا يحتاج Generic Full-Page Extraction عندما يكون الـTemplate مطابقًا والجدول لديه geometry كافية.

## Cell OCR fallback

Tesseract في الاختبار الحقيقي قرأ الوصف والأسعار ولكنه أسقط أرقام `Qty` الصغيرة عند OCR للجدول كله. لذلك v0.19 أضاف عقدًا اختياريًا `OCRTableCellProvider`: بعد معرفة الصف والعمود مسبقًا، يمكن قراءة الخلية الصغيرة فقط. Tesseract adapter يستخدم page-segmentation مناسبًا للخلية وتلميح `value_kind`، من غير ربط الـCore بـTesseract.

## التحقق المالي

إصلاح Structure لا يعني أن القيم المالية أصبحت موثوقة تلقائيًا.

- OCR واحد → `template_structure_review`, `verified=false`.
- ملاحظتان مستقلتان متطابقتان للجدول → يمكن الوصول إلى `verified_consensus` حسب الإعدادات.
- Template match وحده لا يثبت قيمة مالية.

## Backward compatibility

Templates القديمة التي تحتوي فقط على `normalized_bbox/header_anchors/columns` ما زالت صالحة. إذا لم توجد `normalized_body_bbox + column_definitions`، لا يتم إجبارها على الـFast Path الجديد؛ تستخدم مسار v0.18 القديم.

SQLite store يدعم الحقول الجديدة ويحافظ على قراءة البيانات القديمة التي لا تحتوي عليها.

## نتيجة الاختبار الحقيقي

نفس فاتورة NOVA التي فشلت فيها بنية الجدول في v0.18:

### v0.18

```text
Blue Pen 2 12.50      | 25.00
Notebook Pack 1 40.00 | 40.00
Desk Lamp 1 48.50     | 48.50
Subtotal              | 113.50
VAT                   | 5.68
TOTAL                 | 119.18
```

### v0.19

```text
Blue Pen      | 2 | 12.50 | 25.00
Notebook Pack | 1 | 40.00 | 40.00
Desk Lamp     | 1 | 48.50 | 48.50
```

والنتيجة:

- Table exact: `3/3 rows × 4/4 columns`.
- Summary leaked rows: `0`.
- Invoice fixed fields: `5/5 exact`.
- Template match: `1.0`.
- Drift: `false`.
- Generic extraction on repeated invoice: `false`.
- Table values remain Review لأن benchmark استخدم Tesseract engine واحدًا فقط.

## اختبارات v0.19

تمت إضافة اختبارات لعقد column geometry، منع overlap غير الآمن، table fast path، cell fallback، stop anchors، verified table consensus عند وجود providerين مستقلين، وSQLite round-trip.

Full regression: **357/357 passed** على مجموعات منفصلة، بالإضافة إلى `compileall` والـreal Tesseract table benchmark.
