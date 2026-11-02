# v0.24.0 — Engineering Standard Refactor

## الهدف

الإصدار v0.24 لا يضيف خوارزمية Extraction جديدة. الهدف هو جعل الكود أقرب للـPython & AI Engineering Standard المتفق عليه، مع الحفاظ على سلوك v0.23 ونتائجها.

القاعدة التي تم الالتزام بها أثناء التعديل:

```text
غيّر شكل الكود والعقود
ولا تغيّر خوارزميات الاستخراج أو نتائجها
```

## المرحلة 1 — Result / Error / Options / Strict Contracts

تمت إضافة `Result[T]` حقيقي لاستخدامه عند حدود الـFeature/Application:

```text
Result.Success(Response)
Result.Failure(Error)
```

وتم توحيد الخطأ في عقد واحد:

```text
Error
├── type
├── code
├── description
└── metadata
```

مع `ErrorType` موحد لأخطاء validation/provider/timeout/security/internal وغيرها.

تمت إضافة `StrictContract` باستخدام Pydantic مع:

- strict types؛
- `extra="forbid"`؛
- immutable/frozen contracts؛
- تحويل واضح من transport request إلى domain request.

كما أصبحت الـOptions الرئيسية immutable typed contracts، وتم فصل `ParallelExecutionOptions`. وتم استكمال `options.py` داخل الـFeatures التي تم تحويلها للـcanonical layout. الـFeatures التي لا تملك إعدادات فعلية تحتفظ بعقد Options فارغ بدل اختراع configuration لا قيمة له.

## المرحلة 2 — Feature Boundaries وتقليل الـGod Services

الشكل المستهدف أصبح:

```text
feature/
├── options.py
├── request.py
├── response.py
├── errors.py
├── service.py
├── router.py
└── tests/
```

الـRouter مسؤول عن:

```text
raw input
→ strict validation
→ typed request
→ service/workflow
→ Result[Response]
```

بينما الـService لا يحتاج معرفة تفاصيل transport validation.

### أهم فصل معماري

قبل v0.24 كان `data_extraction/service.py` حوالي 903 سطر ويملك orchestration كثيرة. بعد الفصل أصبح حوالي 361 سطر، وأصبح دوره أقرب إلى composition/facade.

Template orchestration انتقل إلى:

```text
template_extraction/workflow.py
```

وتم فصل:

```text
template_extraction/matching.py
template_extraction/visual_fingerprints.py
template_extraction/helpers.py
template_extraction/application.py
template_extraction/router.py
```

الـlow-level template service نزل تقريبًا من 1274 إلى 983 سطر، مع بقاء المنطق نفسه.

## المرحلة 3 — Public Result API + Compatibility

تمت إضافة Result APIs على capability facade، منها:

```text
inspect_result(...)
extract_result(...)
discover_fields_result(...)
verify_fields_result(...)
build_template_candidate_result(...)
approve_template_result(...)
match_template_result(...)
extract_with_template_result(...)
```

والـLegacy APIs القديمة ما زالت موجودة حتى لا نكسر المستهلكين الحاليين:

```text
inspect(...)
extract(...)
discover_fields(...)
verify_fields(...)
...
```

الهدف هو migration تدريجي: الكود الجديد يستخدم Result API، بينما القديم يظل Compatibility Facade.

## Template Boundary

الـTemplate feature أصبح له strict input contracts مستقلة عن domain dataclasses:

```text
TemplateBuildInput
TemplateApprovalInput
TemplateMatchInput
TemplateExtractionInput
```

و`TemplateExtractionRouter` يحول raw mappings إلى requests typed ثم يستدعي `TemplateApplicationService` الذي يرجع Result.

## Options Ownership

تم التأكد من عدم نسيان الـOptions:

```text
detect_file/options.py
inspect_document/options.py
extract_document/options.py
discover_fields/options.py
verify_fields/options.py
template_extraction/options.py
parallel_execution/options.py
```

بعض الـOptions contracts فارغة حاليًا عمدًا لأن الـFeature لا يملك policy قابلة للضبط؛ وجود الملف يثبت ownership بدون اختراع إعدادات غير مستخدمة.

## الاختبارات والـGates

النتيجة النهائية:

```text
Group 1: 184 passed
Group 2: 144 passed
Group 3:  70 passed
--------------------
Total:   398 passed
```

بالإضافة إلى:

- `compileall`: PASS؛
- Python 3.11 syntax gate: PASS؛
- canonical parameter/return typing gate: PASS؛
- public export smoke: PASS.

تم اكتشاف وتصحيح نقطة compatibility أثناء الـGate: Router جديد استخدم في البداية generic syntax من Python 3.12، بينما الحزمة تعلن `requires-python >=3.11`. تم تغييره إلى `TypeVar` وإضافة syntax gate يقرأ السورس كله بقواعد Python 3.11.

## Static Tooling

تمت إضافة إعدادات `ruff` و`mypy` إلى `pyproject.toml`، لكن executables نفسها غير مثبتة في بيئة الاختبار الحالية ولا يوجد اعتماد على الإنترنت. لذلك لا يتم الادعاء بأن ruff/mypy تم تشغيلهما في هذا الإصدار. بدل ذلك تم تشغيل gates محلية على syntax وtyping signatures.

## نقطة صريحة بخصوص Explicit Locals

الـStandard المستهدف يفضل explicit typing حتى للـlocals. v0.24 يفرض typing واضحًا على العقود والحقول والـpublic/canonical parameters والـreturns، لكنه لم يجرِ تعديلًا ميكانيكيًا لمئات الـlocals القديمة داخل الخوارزميات لمجرد إضافة annotations. السبب هو الحفاظ على diff قابل للمراجعة وعدم خلط style-only edits مع refactor معماري كبير.

هذا لا يؤثر على runtime correctness أو Result contracts، لكنه يظل style debt لو كان الهدف الحرفي هو annotation لكل local في كل legacy algorithm.

## القرار

v0.24 يجعل الكود أوضح بكثير لمطور .NET/Python:

```text
Request  → ما يدخل
Options  → كيف تعمل الـFeature
Service  → business/application work
Router   → validation/boundary
Response → ما يخرج
Errors   → failures المتوقعة
Result   → success/failure flow
```

والأهم أن هذا تم بدون إعادة كتابة خوارزميات الـOCR/Templates/Multi-page/Parallelism التي ثبتت في v0.23.
