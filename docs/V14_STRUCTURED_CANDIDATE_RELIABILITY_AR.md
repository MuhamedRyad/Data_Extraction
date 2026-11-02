# v0.14 — Structured Candidate Reliability

## لماذا الإصدار؟

اختبارات v0.13 الواقعية أثبتت أن فصل `text_coverage` عن `semantic_complete` كان إصلاح سلامة صحيحًا، لكن بقيت أربع مشاكل عملية:

1. بعض النماذج منخفضة ثقة التصنيف كانت تفلت كـ`general`، فيعود `content_complete=true` رغم غياب علاقات السؤال/الإجابة.
2. مرشح اسم التاجر كان قد يختار label مثل `Seller's Name` أو كلمة عامة مثل `Issuance` أو fragment OCR مشوه.
3. TOC detection كان قد يربط رقم القسم القريب بالعنوان بدل رقم الصفحة الموجود في العمود الخارجي، كما أن المواد القانونية المرقمة قد تبدو كفهرس.
4. OCR table reconstruction كان يشترط عدد tokens متساويًا تقريبًا في كل صف، وهذا يفشل في إيصالات borderless لأن وصف المنتج قد يكون كلمة أو ثلاث كلمات.

## الإصلاحات

### Structured suspicion gate

أضيف حد ثانٍ `structured_document_suspicion_confidence` (افتراضي 0.40). إذا كانت الصفحة تحمل أدلة واضحة على Form/Invoice/Receipt لكن confidence أقل من حد القبول 0.72، لا تتحول تلقائيًا إلى `general`. تبقى structured candidate مع:

- `semantic_complete=false`
- `requires_review=true`
- diagnostic: `structured_kind_suspected_below_acceptance_threshold`

### Merchant candidate quality

الترتيب الجديد:

1. Seller/Merchant label صريح → ابحث عن القيمة التالية المعقولة.
2. لو لا يوجد label، استخدم header candidate محافظ.
3. لا تروّج labels العامة، address-like lines، أو fragments ذات punctuation مشبوه مثل `Ad>odige`.

القيمة تظل غير موثقة إذا مصدرها OCR.

### TOC binding

بدل المرور على الأرقام واختيار أقرب عنوان، v0.14 يمر على صف العنوان ويختار الرقم الموجود على نفس الصف والأقرب إلى **حافة الصفحة**. هذا يحل نمط:

`section 1 | title | page 11`

بحيث `11` هو page reference، لا `1`.

كما يرفض صفحات يغلب عليها `Article / Section / المادة / الباب / الفصل` كـTOC في غياب عنوان محتويات صريح.

### Flexible financial table recovery

إذا فشل strict grid reconstruction، والمستند يبدو ماليًا، يتم تجميع OCR tokens حسب Y ثم البحث عن عمود مبالغ مصطفّ. يمكن أن يكون وصف الصف بطول متغير:

- `Blue Pen | 10.00`
- `Large Notebook Pack | 20.00`
- `TOTAL | 30.00`

لا يتم اختراع خلية مفقودة، وكل قيمة تأتي من token OCR موجود فعلًا.

## اختبار Tesseract حقيقي

تم إنشاء إيصال raster borderless وتشغيل Tesseract المحلي (`eng`) فعليًا. النتيجة:

- OCR confidence ≈ 0.957
- تم بناء جدول 3×2 من token geometry
- تم استخراج merchant/address/date/total candidates
- `semantic_complete=false` لأن القيم الحساسة ليست independently verified

هذا يثبت التكامل والاسترداد البنيوي، وليس دقة SROIE العامة.

## ما لم يدّعِ الإصدار حله

- لا يوجد تصحيح تخميني لـ`112.46 → 112.45` أو `NO.89 → NO.59`.
- adaptive language conflict يظل Review signal وليس مصححًا تلقائيًا.
- دقة merchant/address/amounts على الأربع صور SROIE نفسها تحتاج إعادة benchmark على نفس الصور في بيئة المستخدم.
- استخراج جداول معقدة متعددة الأعمدة من صور الفواتير قد يحتاج OCR/layout provider أقوى من Tesseract/EasyOCR.
