# v0.17 — Selective AI Routing by Default

## القرار

الـAI أصبح fallback افتراضيًا **فقط عندما يحتاجه الجزء الحالي من المستند**. لا يتم إرسال المستند كله لمجرد أن AI متاح.

المسار الافتراضي:

```text
Native reliable
  -> Native extraction

Raster text
  -> OCR
  -> إذا كفاية: توقف
  -> إذا ضعيف/غير محلول: AI Vision

Raster receipt/invoice/form
  -> OCR
  -> إذا النص قوي لكن structure ما زالت flat/missing: AI Vision

PDF native layout
  -> deterministic reading order
  -> إذا complex/mixed/diagram فقط: AI layout fallback

Template learning
  -> عند build_template_candidate ومع وجود المفتاح: AI template provider تلقائيًا
  -> Candidate فقط، والاعتماد البشري ما زال مطلوبًا
```

## Default configuration

`ExtractionOptions.auto_enable_ai = True`.

الـCore يبحث عن `OPENAI_API_KEY`. إذا كان موجودًا ولم يحقن المستخدم Provider آخر، يتم إنشاء:

- `OpenAIResponsesDocumentAIProvider` كـVision fallback.
- نفس الـprovider كـLayoutOrder fallback للصفحات المعقدة فقط.
- `OpenAIResponsesTemplateProvider` لإنشاء Template Candidate أول مرة.

إذا لم يوجد المفتاح:

- لا يوجد أي network call.
- Native/OCR يستمران كالمعتاد.
- الجزء الذي يحتاج AI يبقى review/unresolved بدل التخمين.

## الأولوية للـproviders المخصصة

إذا مرر المستخدم `vision_provider` أو `layout_provider` أو `template_generation_provider`، فالـprovider الصريح يتقدم على الـauto provider.

## Structured image escalation

v0.16 كان يمكن أن يتوقف عند OCR confidence عالية حتى لو فاتورة/إيصال بقي Flat OCR بلا Structure. في v0.17، إذا:

- `image_route=AUTO`
- AI متاح
- النص يبدو Receipt/Invoice/Form
- ولا توجد structured table recovered

فلا تعتبر confidence وحدها سببًا لإيقاف الـpipeline؛ يتم استدعاء Vision AI تلقائيًا.

## الخصوصية والحوكمة

السلوك الافتراضي لا يعني إرسال كل الملفات. الإرسال Selective ومحدود بالـregions/pages التي فشلت فيها الطرق المحلية أو احتاجت فهمًا بنيويًا. `store=false` هو الافتراضي في OpenAI adapters، والـbudgets (`max_vision_calls`, `max_layout_calls`) ما زالت enforced.

لإيقاف أي External AI:

```python
ExtractionOptions(auto_enable_ai=False)
```

## ما تم إثباته

اختبارات v0.17 المركزة أثبتت:

1. Native PDF text: صفر AI calls.
2. Strong ordinary OCR: صفر AI calls.
3. Low-confidence OCR: AI Vision call تلقائي.
4. Strong invoice OCR لكن Flat/No structure: AI Vision call تلقائي.
5. `auto_enable_ai=False`: لا AI call حتى مع وجود المفتاح.
6. `AI_REQUIRED` template build: يتم تجهيز OpenAI template provider تلقائيًا من نفس المفتاح.

الـHTTP في الاختبارات Mocked، لذلك الاختبارات تثبت orchestration/contracts/selective routing ولا تدّعي قياس جودة موديل خارجي فعلي.
