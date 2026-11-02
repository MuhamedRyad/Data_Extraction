# v0.23 — Bounded Parallel Execution

## القرار

بعد v0.22 أصبح المستند Page-aware والـAI يعمل على Page Batches. v0.23 يغيّر **طريقة التنفيذ فقط**: الأعمال المستقلة يمكن أن تعمل بالتوازي داخل حدود صريحة، بينما ترتيب النتيجة والعقود لا يتغيران.

```text
Inspect / Plan
      ↓
Independent pages / page batches
      ↓
Bounded Parallel Runner
  ├─ page workers
  ├─ OCR call gate
  ├─ Vision/AI call gate
  └─ AI template batch gate
      ↓
Deterministic merge by original page/order
```

## الحدود الافتراضية

```python
ExtractionOptions(
    enable_parallel_execution=True,
    max_parallel_page_groups=4,
    max_parallel_ocr_calls=4,
    max_parallel_ai_calls=2,
)

TemplateExtractionOptions(
    enable_parallel_ai_batches=True,
    max_parallel_ai_batches=2,
)
```

يمكن إلغاء التنفيذ المتوازي بالكامل بـ:

```python
ExtractionOptions(enable_parallel_execution=False)
```

## ما الذي يعمل بالتوازي؟

- صفحات الاستخراج المستقلة بعد Inspect/Layout planning.
- OCR على صفحات مختلفة، لكن داخل `max_parallel_ocr_calls`.
- Vision calls على صفحات مختلفة، مع الحفاظ على `max_vision_calls` global budget وبحد `max_parallel_ai_calls`.
- AI Template Generation / Review / Refinement page batches، بحد `max_parallel_ai_batches`.

## ما الذي يظل Sequential عمدًا؟

- Validate / Detect / Inspect والتخطيط الحتمي.
- ترتيب Regions داخل الصفحة الواحدة؛ هذا يمنع nested worker explosions ويحافظ على بساطة الـreading-order semantics.
- Assembly النهائي وDocument-level quality validation.
- Whole-document structure provider call إن كان مزود الخدمة نفسه يعالج المستند كعملية واحدة.

## الأمان والحتمية

- `VisionBudget` أصبح thread-safe؛ 10 workers لا يمكنهم تجاوز `max_vision_calls`.
- نتائج الصفحات تُدمج حسب ترتيب الإدخال، وليس حسب worker completion order.
- `document_order_index` يُحسب مسبقًا من Source order؛ انتهاء Page 8 قبل Page 2 لا يغيّر الـJSON.
- Worker exception يفشل العملية ولا يتحول إلى نجاح جزئي صامت.
- OCR وAI لكل منهما semaphore مستقل حتى لا يؤدي page concurrency إلى provider storm.

## Benchmark

على نفس المدخل ونفس providers:

- 8 صفحات + OCR provider بطيء مضبوط: تقريبًا `1.49x` أسرع عند OCR concurrency=2.
- 6 صفحات + Tesseract حقيقي: تقريبًا `1.45x` أسرع، مع `same_output=true` و`same_document_order=true`.
- 12 صفحة AI template، 4 batches، concurrency=2 مع Mock HTTP latency: تقريبًا `1.72x` أسرع، مع نفس batch merge order.

الأرقام تعتمد على CPU/provider/network. الهدف ليس ضمان speedup ثابت، بل منع الانتظار التسلسلي عندما توجد أعمال مستقلة مع الحفاظ على budgets والحتمية.

## الاختبارات

- 5 اختبارات v0.23 مركزة: output parity، OCR bound، Vision global budget، AI batch bound/order، public service wiring.
- Full regression: **383/383** unique tests passed في مجموعات محدودة لتجنب single-process timeout.
- `compileall`: Passed.
