# v0.13 — Structured Document Reliability

## لماذا صدر هذا الإصدار؟

الاختبارات الواقعية على إيصالات وفواتير مصورة كشفت خطأ دلاليًا مهمًا في v0.12: نجاح OCR في إرجاع أي نص كان يجعل المنطقة `content_complete=true`، ثم تُحسب نسبة اكتمال المستند من هذه العلامة. بذلك كان من الممكن أن يظهر `content_complete=true` و`completeness_ratio=1.0` رغم فقد إجمالي الفاتورة أو خطأ رقم حساس.

إصلاح v0.13 لا يدعي أن OCR أصبح معصومًا من الخطأ. الإصلاح هو أن النظام لم يعد يخلط بين **تغطية النص/البكسلات** وبين **اكتمال المعنى والحقول المطلوبة**.

## العقود الجديدة

لصفحات `receipt` / `invoice` / `form` يخرج النظام إضافةً إلى العقود القديمة:

- `text_coverage_complete` و`text_coverage_ratio`: هل مناطق المصدر الحرجة تم تمثيلها نصيًا/بنيويًا؟
- `field_coverage_ratio`: نسبة الحقول المطلوبة التي وُجد لها Candidate.
- `verified_field_coverage_ratio`: نسبة الحقول المطلوبة التي لديها Evidence مستقل كافٍ للاعتماد.
- `semantic_gap_count`: عدد الحقول المطلوبة التي ما زالت بلا Verification.
- `semantic_complete` و`semantic_status`: اكتمال دلالي مستقل عن نجاح OCR الخام.

للمستندات المنظمة يصبح `content_complete` محافظًا: نجاح OCR وحده لا يكفي. في مستند OCR فقط، وجود `total=112.45` مرة واحدة لا يجعله Verified حتى لو كانت ثقة المحرك عالية.

## Structured field candidates

المحلل العام يدعم مرشحات من نوع: merchant name، address، invoice number، date، subtotal، tax، total. هذه **Candidates** وليست حقائق تجارية. النص الأصلي لا يُستبدل ولا يتم تصحيح رقم حساس صامتًا.

الـNative text الذي يطابق البنية يمكن اعتباره دليلًا مباشرًا في الحالات الحالية، بينما OCR raster يبقى بحاجة إلى Evidence مستقل مثل provider ثانٍ/field crop verification/Vision وفق سياسة التطبيق.

## الفواتير والجداول بدون Grid

في v0.12 كان استرجاع جدول OCR مرتبطًا غالبًا بوجود raster grid. v0.13 يسمح للمستند المالي المرشح بطلب `OCRTextRegion` token geometry حتى لو لم توجد خطوط جدول مرسومة. إذا أمكن تجميع tokens إلى صفوف/أعمدة، يُنتج جدول مرشح مع review flag، وتُستخدم صفوفه كEvidence لاكتشاف Total/Tax/Subtotal حتى لو النص المسطح فقد الرقم. الجدول الناتج غير authoritative.

## OCR language profiles

عند تفعيل local Tesseract يمكن استخدام `AdaptiveTesseractOCRProvider` مع profiles محدودة مثل `ara+eng` و`eng`. إذا اختلفت الأرقام، يُسجل `adaptive_tesseract_numeric_conflict` و`ocr_numeric_profile_conflict_review`. اختيار قراءة أعلى confidence لا يمحو الخلاف ولا يجعل القيمة Verified. لا توجد downloads مخفية.

## TOC false positives

تم تشديد `detect_toc_table()` باستخدام محاذاة عمود أرقام الصفحات وترتيب الأرقام وموقع العمود، مع السماح بفهرس قصير إذا كان لديه Evidence قوي. الهدف تقليل تحويل جداول إحصائية إلى `table_of_contents` من غير كسر الفهارس الصغيرة.

## ما الذي ثبت؟

- 318/318 tests ناجحة.
- اختبار حقيقي بمحرك Tesseract المحلي على صورة إيصال صناعية: النص والمرشحات الرئيسية قُرئت، لكن النتيجة بقيت `semantic_complete=false` و`content_complete=false` لأن كل الحقول الحساسة جاءت من Observation واحدة. هذا هو السلوك المطلوب.
- اختبار regression عالي الثقة (`0.925`) مع فاتورة ناقصة حقول يثبت أن high OCR confidence لا ينتج completeness زائفًا.
- اختبار borderless invoice يثبت أن Total/Tax يمكن استعادتهما من token geometry حتى عندما يغيبان من النص المسطح.

## ما الذي لم يثبت بعد؟

صور SROIE الأربع الأصلية، ملف ZATCA المحلي، وصورة FUNSD المستخدمة في التقييم لم تكن متاحة داخل بيئة التنفيذ هنا. لذلك لا ندعي أن أخطاء OCR المحددة مثل `112.45 → 112.46` أو `NO.59 → NO.89` اختفت. v0.13 يمنع هذه الأخطاء من التحول تلقائيًا إلى إشارة اكتمال/اعتماد، ويوفر بنية أفضل لاكتشافها والتحقق منها. القياس الحقيقي التالي يجب أن يعيد تشغيل نفس corpus ويحسب field exact match وnormalized edit distance وamount/date accuracy.

## قرار الإنتاج

حتى ينجح benchmark الواقعي، الإيصالات والفواتير المصورة تظل **review-required**. لا ينبغي لأي downstream الاعتماد على `text_coverage_complete` وحده. للاعتماد المالي يجب استخدام `semantic_complete`/`verified_field_coverage_ratio` أو سياسة أشد خاصة بالدومين.
