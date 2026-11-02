from __future__ import annotations

import io
import json
import math
import random
import re
import sys
from dataclasses import dataclass, replace, asdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont
import pytesseract

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from data_extraction import (
    DataExtractionService, DocumentInput, ExtractionOptions, ProviderTextResult,
    TemplateAdaptiveRoute, TemplateAnchor, TemplateApprovalRequest, TemplateBuildMode,
    TemplateBuildRequest, TemplateExtractionOptions, TemplateExtractionRequest,
    TemplateFieldDefinition, TemplateFieldMode, TemplateGenerationProposal,
    TemplateReviewIssue, TemplateReviewResult, TemplateReviewVerdict,
)
from data_extraction.providers.ocr.tesseract import TesseractOCRProvider
from data_extraction.template_extraction.store import InMemoryTemplateStore

FONT_EN = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_EN_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_AR = "/usr/share/opentype/fonts-hosny-amiri/Amiri-Regular.ttf"
FONT_AR_BOLD = "/usr/share/fonts/opentype/fonts-hosny-amiri/Amiri-Bold.ttf"
OUT = ROOT / "benchmarks" / "v21_bilingual_intensive_artifacts"
OUT.mkdir(parents=True, exist_ok=True)

BBox = tuple[float, float, float, float]

@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: str
    label: str
    value: str
    required: bool
    label_bbox: BBox
    value_bbox: BBox
    pattern: str

@dataclass(frozen=True)
class DocSpec:
    family: str
    language: str
    variant: str
    fields: tuple[FieldSpec, ...]
    width: int
    height: int
    document_kind: str = "invoice"


class TesseractProfile:
    def __init__(self, name: str, psm: int, language: str = "ara+eng") -> None:
        self.name = name
        self.psm = psm
        self.language = language
        self.calls = 0

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        self.calls += 1
        with Image.open(io.BytesIO(image_bytes)) as image:
            image = image.convert("L")
            text = pytesseract.image_to_string(image, lang=self.language, config=f"--psm {self.psm}").strip()
        return ProviderTextResult(text=text, confidence=None, provider_name=self.name)


def norm_bbox(px: tuple[int, int, int, int], width: int, height: int) -> BBox:
    return (px[0] / width, px[1] / height, px[2] / width, px[3] / height)


def bbox_iou(a: BBox | None, b: BBox | None) -> float:
    if a is None or b is None:
        return 0.0
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1-x0) * max(0.0, y1-y0)
    aa = max(0.0,a[2]-a[0])*max(0.0,a[3]-a[1])
    bb = max(0.0,b[2]-b[0])*max(0.0,b[3]-b[1])
    return inter / max(1e-9, aa+bb-inter)


def center_distance(a: BBox | None, b: BBox | None) -> float:
    if a is None or b is None:
        return 9.0
    ac=((a[0]+a[2])/2,(a[1]+a[3])/2); bc=((b[0]+b[2])/2,(b[1]+b[3])/2)
    return math.dist(ac,bc)


class OracleTemplateAI:
    """Benchmark oracle for AI layer only. OCR/core remain real.

    It uses known synthetic ground-truth geometry so this benchmark measures
    routing/orchestration separately from live-model quality.
    """
    def __init__(self, specs: dict[str, DocSpec]) -> None:
        self.specs = specs
        self.generate_calls = 0
        self.refine_calls = 0

    def _spec(self, document: DocumentInput) -> DocSpec:
        return self.specs[document.source_id]

    def _fields(self, spec: DocSpec) -> tuple[TemplateFieldDefinition, ...]:
        return tuple(
            TemplateFieldDefinition(
                name=f.name, field_kind=f.kind, required=f.required, page_number=1,
                anchors=(f.label,), pattern=f.pattern, mode=TemplateFieldMode.LABEL_RELATIVE,
                normalized_value_bbox=f.value_bbox, normalized_label_bbox=f.label_bbox,
                minimum_confidence=0.70,
            ) for f in spec.fields
        )

    def generate_template(self, document: DocumentInput, context) -> TemplateGenerationProposal:
        self.generate_calls += 1
        spec=self._spec(document)
        return TemplateGenerationProposal(
            document_kind=spec.document_kind,
            fields=self._fields(spec),
            anchors=(TemplateAnchor("invoice" if spec.language=="en" else "فاتورة",1,None,False),),
            diagnostics=("benchmark_oracle_full_generation",),
        )

    def refine_template(self, document: DocumentInput, proposal, context) -> TemplateGenerationProposal:
        self.refine_calls += 1
        spec=self._spec(document)
        by_name={f.name:f for f in spec.fields}
        fields=[]
        for field in proposal.fields:
            truth=by_name.get(field.name)
            if truth is None:
                fields.append(field)
            else:
                fields.append(replace(field,
                    normalized_value_bbox=truth.value_bbox,
                    normalized_label_bbox=truth.label_bbox))
        return replace(proposal, fields=tuple(fields), diagnostics=proposal.diagnostics+("benchmark_oracle_geometry_refined",))


class OracleReviewer:
    def __init__(self, specs: dict[str, DocSpec]) -> None:
        self.specs=specs
        self.calls=0

    def review_template(self, document: DocumentInput, candidate, context) -> TemplateReviewResult:
        self.calls+=1
        spec=self.specs[document.source_id]
        truth={f.name:f for f in spec.fields}
        by_name={f.name:f for f in candidate.fields}
        issues=[]
        for name, expected in truth.items():
            got=by_name.get(name)
            if got is None:
                if expected.required:
                    issues.append(TemplateReviewIssue("field","missing_required_semantic_field",f"Missing {name}",0.99,name))
                continue
            # Center distance is robust to a slightly wider crop while still catching wrong semantic mapping.
            if center_distance(got.normalized_value_bbox, expected.value_bbox) > 0.055:
                issues.append(TemplateReviewIssue("field","semantic_value_region_mismatch",f"{name} value region does not point to expected semantic value",0.99,name))
            if got.normalized_label_bbox is not None and center_distance(got.normalized_label_bbox, expected.label_bbox) > 0.08:
                issues.append(TemplateReviewIssue("field","semantic_label_region_mismatch",f"{name} label region mismatch",0.98,name))
        verdict=TemplateReviewVerdict.PASS if not issues else TemplateReviewVerdict.REJECT
        return TemplateReviewResult(
            verdict=verdict, confidence=0.99, issues=tuple(issues),
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name="OracleReviewer",
            diagnostics=("benchmark_oracle_semantic_review",),
        )


def draw_pair(draw: ImageDraw.ImageDraw, label: str, value: str, y: int, lang: str, width: int,
              label_x: int, value_x: int, font_label, font_value, split: bool) -> tuple[tuple[int,int,int,int],tuple[int,int,int,int]]:
    if lang == "ar":
        # right aligned label and value, intentionally separated so ground-truth boxes are unambiguous
        lb=draw.textbbox((label_x,y),label,font=font_label,anchor="ra",direction="rtl",language="ar")
        draw.text((label_x,y),label,font=font_label,fill="black",anchor="ra",direction="rtl",language="ar")
        vb=draw.textbbox((value_x,y),value,font=font_value,anchor="ra",direction="ltr")
        draw.text((value_x,y),value,font=font_value,fill="black",anchor="ra",direction="ltr")
    elif lang == "mixed":
        lb=draw.textbbox((label_x,y),label,font=font_label,anchor="la",direction="ltr")
        draw.text((label_x,y),label,font=font_label,fill="black",anchor="la")
        vb=draw.textbbox((value_x,y),value,font=font_value,anchor="la",direction="ltr")
        draw.text((value_x,y),value,font=font_value,fill="black",anchor="la")
    else:
        lb=draw.textbbox((label_x,y),label,font=font_label,anchor="la")
        draw.text((label_x,y),label,font=font_label,fill="black",anchor="la")
        vb=draw.textbbox((value_x,y),value,font=font_value,anchor="la")
        draw.text((value_x,y),value,font=font_value,fill="black",anchor="la")
    return lb,vb


def generate_document(family: str, language: str, variant: str, index: int, degradation: str,
                      shift: tuple[int,int]=(0,0), split: bool=False) -> tuple[bytes,DocSpec,dict[str,str]]:
    W,H=1400,1800
    dx,dy=shift
    image=Image.new("RGB",(W,H),"white")
    d=ImageDraw.Draw(image)
    en=ImageFont.truetype(FONT_EN,42); enb=ImageFont.truetype(FONT_EN_BOLD,50)
    ar=ImageFont.truetype(FONT_AR,48); arb=ImageFont.truetype(FONT_AR_BOLD,58)
    if language=="ar":
        title="فاتورة ضريبية"; merchant="شركة النور للتجارة"; f=ar; fb=arb
        labels={"invoice_number":"رقم الفاتورة","date":"التاريخ","subtotal":"المجموع الفرعي","tax":"الضريبة","total":"الإجمالي"}
    elif language=="mixed":
        title="فاتورة ضريبية / TAX INVOICE"; merchant="Al Noor Trading / شركة النور"; f=en; fb=enb
        labels={"invoice_number":"Invoice No / رقم الفاتورة","date":"Date / التاريخ","subtotal":"Subtotal / المجموع الفرعي","tax":"VAT / الضريبة","total":"TOTAL / الإجمالي"}
    else:
        title="TAX INVOICE"; merchant="NOVA SUPPLIES LTD"; f=en; fb=enb
        labels={"invoice_number":"Invoice No","date":"Date","subtotal":"Subtotal","tax":"VAT","total":"TOTAL"}
    # use stable format, values vary by index
    values={
        "invoice_number": f"INV-2026-{500+index:03d}",
        "date": f"{(index%27)+1:02d}/11/2026",
        "subtotal": f"{100+index*3:.2f}",
        "tax": f"{5+index*0.15:.2f}",
    }
    values["total"]=f"{float(values['subtotal'])+float(values['tax']):.2f}"

    if language=="ar":
        d.text((1320+dx,70+dy),title,font=fb,fill="black",anchor="ra",direction="rtl",language="ar")
        d.text((1320+dx,170+dy),merchant,font=f,fill="black",anchor="ra",direction="rtl",language="ar")
        label_x=1320+dx; value_x=650+dx
    else:
        d.text((80+dx,70+dy),title,font=fb,fill="black")
        d.text((80+dx,170+dy),merchant,font=f,fill="black")
        label_x=100+dx; value_x=(760 if split else 470)+dx
    if language=="mixed":
        label_x=100+dx; value_x=(850 if split else 650)+dx

    y_positions={"invoice_number":300,"date":390,"subtotal":1260,"tax":1350,"total":1450}
    fields=[]
    patterns={
        "invoice_number":r"INV-\d{4}-\d{3}",
        "date":r"\d{2}/\d{2}/\d{4}",
        "subtotal":r"\d+[.,]\d{2}","tax":r"\d+[.,]\d{2}","total":r"\d+[.,]\d{2}",
    }
    for name in ("invoice_number","date","subtotal","tax","total"):
        yy=y_positions[name]+dy
        font_label=fb if name=="total" else f
        font_value=fb if name=="total" else f
        lb,vb=draw_pair(d,labels[name],values[name],yy,language,W,label_x,value_x,font_label,font_value,split)
        pad=8
        lb=(max(0,lb[0]-pad),max(0,lb[1]-pad),min(W,lb[2]+pad),min(H,lb[3]+pad))
        vb=(max(0,vb[0]-pad),max(0,vb[1]-pad),min(W,vb[2]+pad),min(H,vb[3]+pad))
        fields.append(FieldSpec(name,name,labels[name],values[name],name in {"invoice_number","date","total"},norm_bbox(lb,W,H),norm_bbox(vb,W,H),patterns[name]))

    # Visual structure + table-like middle content so documents aren't trivially empty.
    x0,x1=90+dx,1310+dx; top=570+dy
    d.rectangle((x0,top,x1,1080+dy),outline="black",width=3)
    for yy in (650,760,870,980): d.line((x0,yy+dy,x1,yy+dy),fill="black",width=2)
    for xx in (620+dx,820+dx,1040+dx): d.line((xx,top,xx,1080+dy),fill="black",width=2)
    if language=="ar":
        d.text((1280+dx,590+dy),"الوصف",font=ar,fill="black",anchor="ra",direction="rtl",language="ar")
    else:
        d.text((110+dx,590+dy),"Description",font=en,fill="black")
    d.text((650+dx,590+dy),"Qty",font=en,fill="black")
    d.text((850+dx,590+dy),"Price",font=en,fill="black")
    d.text((1070+dx,590+dy),"Amount",font=en,fill="black")
    for r in range(3):
        d.text((110+dx,690+r*110+dy),f"Item {r+1}",font=en,fill="black")
        d.text((660+dx,690+r*110+dy),str(r+1),font=en,fill="black")
        d.text((850+dx,690+r*110+dy),f"{10+r*5:.2f}",font=en,fill="black")
        d.text((1070+dx,690+r*110+dy),f"{(r+1)*(10+r*5):.2f}",font=en,fill="black")

    if degradation=="blur":
        image=image.filter(ImageFilter.GaussianBlur(radius=1.2))
    elif degradation=="low_contrast":
        image=ImageEnhance.Contrast(image).enhance(0.55)
    elif degradation=="noise":
        rng=random.Random(index*777+len(family))
        pix=image.load()
        for _ in range(18000):
            x=rng.randrange(W); y=rng.randrange(H); v=rng.randrange(180,256); pix[x,y]=(v,v,v)
    elif degradation=="skew":
        image=image.rotate(1.2,resample=Image.Resampling.BICUBIC,expand=False,fillcolor="white")
    stream=io.BytesIO(); image.save(stream,format="PNG",compress_level=3)
    spec=DocSpec(family,language,variant,tuple(fields),W,H)
    return stream.getvalue(),spec,values


def normalized_value(value: str | None) -> str:
    if value is None: return ""
    s=value.strip().replace(" ","")
    # OCR sometimes uses comma for decimal; normalize for exact business-value comparison.
    return s.replace(",",".")


def run_language(language: str, family_start: int, family_count: int=1) -> list[dict[str,Any]]:
    results=[]
    for fi in range(family_count):
        family=f"{language}-family-{family_start+fi}"
        style=[("clean",False),("blur",False),("low_contrast",True),("noise",True)][fi % 4]
        degradation,split=style
        source_base=f"{family}:onboard"
        onboard_bytes,onboard_spec,onboard_values=generate_document(family,language,"onboard",family_start+fi,degradation,split=split)
        repeat_bytes,repeat_spec,repeat_values=generate_document(family,language,"repeat",family_start+fi+20,"clean",split=split)
        drift_bytes,drift_spec,drift_values=generate_document(family,language,"drift",family_start+fi+40,"clean",shift=(110,85),split=split)
        specs={source_base:onboard_spec,f"{family}:repeat":repeat_spec,f"{family}:drift":drift_spec}
        ai=OracleTemplateAI(specs); reviewer=OracleReviewer(specs); store=InMemoryTemplateStore()
        primary=TesseractOCRProvider(language="ara+eng", reconstruct_raster_tables=True)
        p1=TesseractProfile("tesseract-psm7",7); p2=TesseractProfile("tesseract-psm6",6)
        service=DataExtractionService(
            ocr_provider=primary,
            field_ocr_providers=(p1,p2),
            template_generation_provider=ai,
            template_review_provider=reviewer,
            template_store=store,
            options=ExtractionOptions(auto_enable_ai=False,auto_enable_local_ocr=False,ocr_confidence_threshold=0.78),
            template_options=TemplateExtractionOptions(max_refinement_attempts=2),
        )
        onboard_doc=DocumentInput(f"{family}.png",onboard_bytes,source_base)
        requested=("invoice_number","date","total")
        build=service.build_template_candidate(TemplateBuildRequest(onboard_doc,family,required_fields=requested,mode=TemplateBuildMode.ADAPTIVE))
        approved=False; approval_error=None
        try:
            service.approve_template(TemplateApprovalRequest(build.candidate,"benchmark-human",review=build.review))
            approved=True
        except Exception as exc:
            approval_error=f"{type(exc).__name__}:{exc}"
        repeated=None; drift=None
        if approved:
            repeated=service.extract_with_template(TemplateExtractionRequest(DocumentInput(f"{family}-repeat.png",repeat_bytes,f"{family}:repeat"),family=family))
            drift=service.extract_with_template(TemplateExtractionRequest(DocumentInput(f"{family}-drift.png",drift_bytes,f"{family}:drift"),family=family))
        repeat_fields={x.name:normalized_value(x.value) for x in repeated.fields} if repeated else {}
        exact={name:(repeat_fields.get(name)==normalized_value(repeat_values[name])) for name in requested}
        verified={x.name:x.verified for x in repeated.fields} if repeated else {}
        result={
            "family":family,"language":language,"onboard_degradation":degradation,"split_layout":split,
            "route":build.route.value if build.route else None,
            "initial_quality":build.quality.score if build.quality else None,
            "final_review":build.review.verdict.value if build.review else None,
            "approval":approved,"approval_error":approval_error,
            "ai_generate_calls":ai.generate_calls,"ai_refine_calls":ai.refine_calls,"ai_review_calls":reviewer.calls,
            "repeat_match":repeated.match.score if repeated else None,
            "repeat_match_status":repeated.match.status.value if repeated else None,
            "repeat_generic_extraction":repeated.generic_extraction_performed if repeated else None,
            "repeat_fields":repeat_fields,"repeat_exact":exact,"repeat_verified":verified,
            "repeat_semantic_complete":repeated.semantic_complete if repeated else None,
            "drift_match":drift.match.score if drift else None,
            "drift_status":drift.match.status.value if drift else None,
            "drift_detected":drift.drift_detected if drift else None,
            "drift_generic_extraction":drift.generic_extraction_performed if drift else None,
        }
        results.append(result)
        # save one representative image per language + each problematic case later
        if fi==0:
            (OUT/f"sample_{language}_onboard.png").write_bytes(onboard_bytes)
    return results


def summarize(rows: list[dict[str,Any]]) -> dict[str,Any]:
    by_lang={}
    for lang in sorted({r['language'] for r in rows}):
        subset=[r for r in rows if r['language']==lang]
        fields_total=len(subset)*3
        repeated_exact=sum(sum(1 for ok in r['repeat_exact'].values() if ok) for r in subset)
        repeated_verified=sum(sum(1 for name in ('invoice_number','date','total') if r['repeat_verified'].get(name)) for r in subset)
        by_lang[lang]={
            "families":len(subset),
            "approved":f"{sum(r['approval'] for r in subset)}/{len(subset)}",
            "full_ai_generation_families":sum(r['ai_generate_calls']>0 for r in subset),
            "refinement_families":sum(r['ai_refine_calls']>0 for r in subset),
            "repeat_required_exact":f"{repeated_exact}/{fields_total}",
            "repeat_required_verified":f"{repeated_verified}/{fields_total}",
            "repeat_fast_path":f"{sum(r['repeat_generic_extraction'] is False for r in subset)}/{len(subset)}",
            "drift_flagged_or_not_accepted":f"{sum((r['drift_detected'] is True) or (r['drift_status']!='accepted') for r in subset)}/{len(subset)}",
            "routes":{route:sum(r['route']==route for r in subset) for route in sorted({r['route'] for r in subset})},
        }
    fields_total=len(rows)*3
    return {
        "families":len(rows),"documents_exercised":len(rows)*3,
        "required_fields_per_family":3,"required_field_checks_per_stage":fields_total,
        "overall":{
            "approved":f"{sum(r['approval'] for r in rows)}/{len(rows)}",
            "full_ai_generation_families":sum(r['ai_generate_calls']>0 for r in rows),
            "refinement_families":sum(r['ai_refine_calls']>0 for r in rows),
            "repeat_required_exact":f"{sum(sum(r['repeat_exact'].values()) for r in rows)}/{fields_total}",
            "repeat_required_verified":f"{sum(sum(1 for name in ('invoice_number','date','total') if r['repeat_verified'].get(name)) for r in rows)}/{fields_total}",
            "repeat_fast_path":f"{sum(r['repeat_generic_extraction'] is False for r in rows)}/{len(rows)}",
            "drift_flagged_or_not_accepted":f"{sum((r['drift_detected'] is True) or (r['drift_status']!='accepted') for r in rows)}/{len(rows)}",
        },
        "by_language":by_lang,
    }

if __name__=="__main__":
    langs=sys.argv[1:] or ["en","ar","mixed"]
    all_rows=[]
    starts={"en":1,"ar":101,"mixed":201}
    for lang in langs:
        rows=run_language(lang,starts[lang],4)
        all_rows.extend(rows)
        print(json.dumps({"language":lang,"summary":summarize(rows)},ensure_ascii=False,indent=2))
    out={"benchmark":"v21_bilingual_intensive","notes":[
        "Local Tesseract 5.5.0 ara+eng is real for generic OCR and field crops.",
        "Two Tesseract PSM profiles are used for consensus; they are different profiles of the same engine, not independent vendors.",
        "AI generation/refinement/review are benchmark oracles using known synthetic geometry; this isolates routing/core behavior and does not claim live-model accuracy.",
        "Each family exercises onboarding, repeated-layout fast path, and shifted-layout drift.",
    ],"summary":summarize(all_rows),"cases":all_rows}
    path=OUT/"V21_BILINGUAL_INTENSIVE.json"; path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding="utf-8")
    print("WROTE",path)
