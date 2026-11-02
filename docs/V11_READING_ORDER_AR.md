# v0.11 — Hybrid Reading Order & Semantic Page Flow

## الهدف

v0.11 لا يحاول حل ترتيب القراءة عن طريق `sort(y, x)` أكبر، ولا يشغّل Model على كل صفحة. التصميم مبني على نتائج benchmark فعلية:

1. افصل العناصر التي **تتداخل فيزيائيًا** عن العناصر التي **تشارك في مسار القراءة الخطي**.
2. صنّف الصفحة قبل محاولة فرض ترتيب واحد عليها.
3. استخدم deterministic geometry للحالات السهلة والأعمدة الواضحة.
4. امتنع عن التخمين في diagrams / hierarchies / mixed layouts غير المحسومة.
5. اترك Layout model كـfallback اختياري للحالات المعقدة.

## المكونات الجديدة

```text
data_extraction/reading_order/
  contracts.py
  flow_roles.py
  page_semantics.py
  spatial_graph.py
```

### Flow roles

الأدوار الداخلية الأساسية:

- `primary_text`
- `structural_table`
- `semantic_figure`
- `native_text_overlay_support`
- `decorative_background`
- `decorative_candidate`

الـregion لا يتم حذفه عند خروجه من linear reading flow. يظل موجودًا في extraction/provenance والعلاقات الفيزيائية.

### Page semantic classes

- `simple_prose`
- `multi_column_prose`
- `table_heavy`
- `diagram_hierarchy`
- `mixed_layout`
- `unknown`

هذه تصنيفات deterministic evidence وليست فهمًا دلاليًا كاملاً من Model.

## إصلاح الـfalse-positive الحقيقي

في v0.10، أول خمس صفحات من التقرير العربي الحقيقي كانت كلها:

```text
reading_order_quality = ambiguous_overlap
```

السبب في عدد من الصفحات كان طبقات `native_text_overlay` مرتبطة بالنص الأصلي، وليس تعارضًا حقيقيًا في القراءة.

v0.11 يحافظ على الـphysical overlap في `RegionRelations`، لكنه لا يجعل طبقة support/background وحدها سببًا في `AMBIGUOUS_OVERLAP`.

### النتيجة على نفس الملف

| النسخة | Ambiguous من أول 5 صفحات |
|---|---:|
| v0.10 | 5/5 |
| v0.11 | 1/5 |

v0.11:

- الصفحات 1–3: `simple_prose` + `geometric_estimate`.
- الصفحة 4: `table_heavy` + `geometric_estimate`.
- الصفحة 5: `mixed_layout` + `ambiguous_overlap` بسبب chart/layout حقيقي؛ لم يتم إخفاء المشكلة.

## Diagram / hierarchy

الـorganizational chart الحقيقي يحتوي 76 text regions وvector candidate كبير يغطي معظم الصفحة.

v0.10:

```text
ambiguous_overlap
```

v0.11 يضيف:

```text
page_semantic_diagram_hierarchy
reading_order_diagram_hierarchy_review_required
```

أي أن النظام لا يدّعي أن organizational chart عبارة عن prose يمكن ترتيبه خطيًا بثقة.

## Spatial graph

عند وجود أعمدة متكررة بوضوح، v0.11 يحتفظ بنفس السلوك الصحيح في v0.10 لكنه يبني Evidence graph بعلاقات precedence هندسية مثل:

```text
above
column_before
spanner_before
```

الـgraph يجب أن يكون acyclic وأن يغطي كل الـregions المشاركة قبل استخدام ترتيبه. أي فشل يؤدي إلى fallback/review، وليس ترتيبًا مفبركًا.

## RTL

RTL ما زال على مستوى ترتيب الـblocks/columns، وليس Reverse للنص.

- الصفحة ذات الغالب العربي تبدأ بالعمود الأيمن عندما توجد أدلة أعمدة قوية.
- النص داخل الـblock لا يتم قلبه.
- `raw_text` لا يتم استبداله تلقائيًا.

نتيجة benchmark controlled لـtwo-column RTL ما زالت Exact Match = 100%.

## Layout model fallback

الخيار الجديد:

```python
ExtractionOptions(layout_provider_fallback_only=True)
```

إذا كان مفعلاً، الـLayout provider لا يعمل على simple pages. يتم حجزه للحالات التي أعطت evidence مثل:

- `reading_order_complex_columns_review_required`
- `reading_order_diagram_hierarchy_review_required`
- `page_semantic_diagram_hierarchy`
- `page_semantic_mixed_layout`

القيمة الافتراضية `False` للحفاظ على سلوك API الحالي لمن يحقن Layout Provider ويتوقع تشغيله على كل الصفحات المؤهلة.

## Benchmark controlled

السيناريوهات الأربعة deterministic:

- single-column LTR
- two-column LTR
- two-column RTL
- spanning sections

v0.11:

```text
average pairwise accuracy = 1.000
exact = 4/4
```

الحالة المتعمدة `complex_with_figure` ما زالت غير محلولة semantic بالكامل:

```text
pairwise accuracy = 0.8571
reading_order_complex_columns_review_required
```

هذا مقصود: لا نريد رفع confidence عن طريق التخمين.

## ما لم يتم الادعاء بحله

1. لا يوجد ضمان 100% لكل document layout.
2. `geometric_estimate` لا يعني human-verified order.
3. figure captions والعلاقات الدلالية المعقدة ما زالت تحتاج provider أقوى في الحالات الصعبة.
4. Surya / LayoutReader / PP-StructureV3 لم تكن packages/model weights متاحة في runtime الحالي، لذلك لم تُسجل نتائج وهمية لها.
5. organizational charts تحتاج structural/vision interpretation إذا كان المطلوب hierarchy semantic، وليس مجرد text extraction.
6. Full Job Checkpoint/Resume ما زال Feature مستقلة غير منفذة في v0.11.

## ملفات الأدلة

- `benchmarks/V11_READING_ORDER_SYNTHETIC.json`
- `benchmarks/V11_REAL_PDF_DIAGNOSTICS.json`
- `benchmarks/V11_V10_REAL_READING_ORDER_COMPARISON.json`
- `benchmarks/V11_TEST_REPORT.json`
