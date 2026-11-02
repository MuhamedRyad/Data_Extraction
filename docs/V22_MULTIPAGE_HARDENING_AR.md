# v0.22 — Multi-page Template Hardening & AI Page Batching

## المشكلة

في v0.21.1 كان الـTemplate نفسه Page-aware، لكن الـFast Preflight للـPDF الممسوح كان يعتمد بصريًا على الصفحة الأولى فقط. لذلك كان ممكن:

```text
Page 1: unchanged
Page 2: TOTAL 123.45 -> SALARY 999.99
Page 3: unchanged
```

ويظل الـPreflight يقبل الـTemplate، ثم يقرأ `999.99` من نفس bbox القديمة ويعاملها كـ`TOTAL`.

## الإصلاح

### 1. Per-page visual fingerprint

`TemplateLayoutFingerprint` يحتفظ الآن بـ`page_visual_hashes` لكل صفحة، مع إبقاء `visual_hash` القديم كـalias للصفحة الأولى للتوافق.

### 2. Stable anchor visual hash

كل `TemplateAnchor` يمكن أن يحمل `visual_hash` مستقل لمنطقة الـlabel الثابتة مثل:

- `Invoice No`
- `TOTAL`
- `Date`
- header منطقة جدول متكرر

القيم المتغيرة نفسها لا تستخدم كمرجع Template truth.

### 3. Page-level match

كل صفحة مستهدفة تحصل على:

- visual score
- anchor score
- geometry score عندما يتوفر native preview
- page shape score
- status مستقل: accepted / review_required / rejected

النتيجة العامة لا تصبح `accepted` إذا أي صفحة مستهدفة تحتاج review.

### 4. Selective page fallback

عند drift في صفحات محددة:

```text
stable page -> Template Fast Path
changed page -> Generic Native/OCR/Vision only on that page
```

ولا يتم استخدام bbox القديمة لأي field/table على الصفحة المتغيرة. الحقل يرجع:

```text
status = template_page_drift
value = null
requires_review = true
```

### 5. AI batching

Template Generation / Refinement / Review تستخدم batches متجاورة bounded:

```text
1-4
5-8
9-12
...
```

الافتراضي:

- `max_pages_per_ai_batch = 4`
- `max_ai_batches_per_document = 25`

الـAI يحتفظ بأرقام الصفحات الأصلية، ولا يعيد ترقيم الصفحة داخل الـbatch.

الصفحات غير المتجاورة لا يتم ربطها في Batch واحدة صامتًا.

### 6. Backward compatibility fail-closed

Template قديم متعدد الصفحات لا يملك per-page visual evidence كافية لا يدخل Fast Path على صفحة raster حرجة. يتم تصعيده للمراجعة/الاستخراج بدل الثقة في page 1.

## Real Tesseract benchmark

### نفس structure، قيمة مختلفة

```text
TOTAL 123.45 -> TOTAL 999.99
```

النتيجة:

- match = 1.0
- changed_pages = []
- generic extraction = false
- generic OCR = 0
- field OCR consensus = 2 observations
- total = 999.99 verified

### Semantic drift في Page 2

```text
TOTAL 123.45 -> SALARY 999.99
```

النتيجة:

- status = review_required
- changed_pages = [2]
- generic_extraction_pages = [2]
- Page 1 regions processed generically = 0
- Page 2 generic OCR calls = 1
- Page 3 regions processed generically = 0
- total = null
- total status = template_page_drift

## Safety rule

الـTemplate ما زال يحدد **أين نبحث فقط**. نجاح OCR على رقم في bbox لا يكفي إذا الـpage/anchor semantics تغيرت.
