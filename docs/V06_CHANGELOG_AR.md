# Data Extraction Feature — Checkpoint v0.6.0

## النطاق
الاستخراج فقط. لا Chunking، ولا RAG، ولا Embeddings، ولا Agent Orchestration.

## اللي اتضاف
1. `DoclingTableProvider` اختياري بيقرأ الجداول الأصلية من `DoclingDocument.tables` بخلاياها، مش Markdown.
2. تحويل إحداثيات Docling `BOTTOMLEFT` إلى `TOPLEFT` والمحافظة على رقم الصفحة الحقيقي.
3. `UnstructuredTableProvider` اختياري يستخرج `metadata.text_as_html`، ويحافظ على `rowSpan` و`colSpan` ويربط الجدول بإحداثيات المستند عندما تكون متاحة ومعتبرة.
4. `ExtractStructuredTablesAction` بتضيف جدولًا مشتقًا وتربطه بمنطقة الصورة الأصلية وتترك النص والصورة كما هما.
5. `TableDetails.cell_locations` وخانة `method=document_ai` واسم Provider وModel ID و`derived_model_table_overlay` لتجنب الخلط مع النص الأصلي.
6. Fail-closed: أي نتيجة غير صالحة أو استثناء لا يغير البيانات الأصلية، ويعلّم الصفحات للمراجعة.
7. `inspect()` لا يستدعي Docling أو Unstructured. التشغيل اختياري في `extract()` فقط.

## الاختبارات
- 181 اختبار من النسخة السابقة، و49 اختبار جديد = **230/230 ناجح**.
- تم تشغيل PDF عربي هجين حقيقي من 5 صفحات: **52 منطقة**، مع تساوي ترتيب الـManifest والـExtraction وبدون تكرار ID.
- الصفحات التي فيها OCR مازالت محتاجة مراجعة. **لا يوجد تشغيل فعلي لمحرك Docling/Unstructured هنا.** اختبارات المحولات تمت بمخرجات محاكاة وعينات HTML.
- النتائج: `benchmarks/arabic_real_v0_6_summary.json`.

## أهم حدود النسخة
- دقة OCR العربية وحل الجداول المصورة بدون حدود أو الممددة عبر الصفحات غير مثبتة.
- لا يوجد Benchmark Accuracy موحّد فعلي ضد Docling أو Unstructured حتى تتوفر أوزان نماذجهم وتشغيلهم.
- مسارات External Inference محتاجة Runtime Isolation، Version Pinning، ومراقبة ذاكرة/مهلة تنفيذ للـProduction.
- عند إضافة جدول من موديل، النص المستخرج من الصورة قد يتكرر دلاليًا؛ الـJSON يوثّق العلاقة بدل حذف النص.

## المقارنة مع الكورسات
- Docling في كورس KGP Talkie بيحوّل لماركداون ثم يحدد الجداول؛ هنا بنقرأ الخلايا من Document Model مباشرة.
- Unstructured في كورس Paulo بيعرض HTML للجدول؛ هنا بنحوّله لخلايا ومواقع وروابط وتحقق صارم، من غير تلخيص LLM.

## الخطوة الجاية
تشغيل محركات Docling وUnstructured الحقيقية على Golden Dataset واحدة، ثم قياس Accuracy للحقول والجداول والـReading Order وLatency/Cost، مع كل Provider كمسار قابل للتعطيل. لا نعتبر الـ230 اختبار برهانًا على دقة نماذج خارجية.
