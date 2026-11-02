# v0.12 — حالة المشاكل الثمانية بعد الإصلاح

## عيوب مثبتة

| # | المشكلة | حالة v0.12 | مستوى الإثبات |
|---|---|---|---|
| 1 | ربط عنوان الفهرس برقم الصفحة | تم إنشاء `table_of_contents` من Geometry، مع صف مستقل لكل عنوان ورقم حتى لو الرقم مكرر | Regression صناعي مباشر؛ الأصل Consumer Rights غير متاح هنا |
| 2 | OCR منخفض الثقة داخل الصور | أضيف bounded preprocessing recovery عند عدم وجود Vision؛ يظل Review ولا يعتبر Consensus | Regression Provider صناعي؛ أصل Ministry Guide غير متاح هنا |
| 3 | فقد بنية الجداول المصورة | أضيف token-level OCR geometry وإعادة بناء candidate rows/columns | Regression 3×2؛ الناتج غير authoritative ويحتاج Review |
| 4 | أرقام + عربي مختلط الاتجاه | تحسن الربط البنيوي للفهرس والجداول؛ لا يوجد reverse/rewrite صامت للنص العام | حل جزئي متعمد؛ الحالات العامة ما زالت Review |
| 5 | الفهرس يصنف `diagram_hierarchy` | TOC evidence ينتج `page_semantic_table_of_contents` قبل قواعد diagram | Regression مباشر؛ الأصل Consumer Rights غير متاح هنا |
| 6 | unresolved count يبالغ بسبب Navigation متكرر | تمت إضافة `critical_unresolved_region_count` و`noncritical_unresolved_region_count` مع كشف repeated edge images | Regression 3 صفحات: raw=3، critical=0، noncritical=3 |

## حدود تحقق وليست عيوبًا تم الادعاء بحلها

| # | الحد | الحالة |
|---|---|---|
| 7 | figure-inside-columns pairwise = 0.8571 | ما زال Known Limit؛ v0.12 لم يدّعِ حله |
| 8 | ربط عينات المقارنة بأصولها | `shnayer_first5` تم ربطه بالأصل المتاح: 5/5 render bytes و5/5 text match. Consumer Rights وMinistry Guide بالمسارين المحليين غير متاحين هنا. Surya/LayoutReader/PP-StructureV3 لم تُشغّل في البيئة. |

## ملاحظات أمان

- TOC source text لا يُحذف؛ يبقى `toc_source_text_support` ويرتبط بالجدول المنظم عبر `parent_region_id`.
- OCR preprocessing ليس تحققًا مستقلاً؛ اختلاف النص يضيف `ocr_preprocessing_text_changed_review`.
- OCR table reconstruction ترجع Candidate فقط، وتضيف `ocr_table_structure_requires_verification`.
- repeated navigation لا تعتبر extracted ولا تختفي؛ فقط تصبح non-critical في حساب أثر الفقد عندما يتكرر نفس الأصل عند حافة الصفحة.
