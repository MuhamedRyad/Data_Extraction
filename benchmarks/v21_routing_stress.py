from __future__ import annotations
import io, json, sys
from dataclasses import replace
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from data_extraction import *

class OCR:
    def __init__(self,text): self.text=text; self.calls=0
    def recognize(self,b,m): self.calls+=1; return ProviderTextResult(self.text,.98,provider_name='stress-ocr')

B={
'invoice_number':((.25,.15,.48,.20),(.05,.15,.22,.20)),
'date':((.72,.15,.91,.20),(.55,.15,.68,.20)),
'subtotal':((.78,.62,.93,.67),(.55,.62,.72,.67)),
'tax':((.78,.69,.93,.74),(.55,.69,.72,.74)),
'total':((.78,.78,.93,.84),(.55,.78,.72,.84)),
}
LABELS={
'en':{'invoice_number':'Invoice No','date':'Date','subtotal':'Subtotal','tax':'VAT','total':'TOTAL'},
'ar':{'invoice_number':'رقم الفاتورة','date':'التاريخ','subtotal':'المجموع الفرعي','tax':'الضريبة','total':'الإجمالي'},
'mixed':{'invoice_number':'Invoice No / رقم الفاتورة','date':'Date / التاريخ','subtotal':'Subtotal / المجموع الفرعي','tax':'VAT / الضريبة','total':'TOTAL / الإجمالي'},
}
PAT={'invoice_number':r'INV-\d{4}-\d{3}','date':r'\d{2}/\d{2}/\d{4}','subtotal':r'\d+[.,]\d{2}','tax':r'\d+[.,]\d{2}','total':r'\d+[.,]\d{2}'}

def text_for(lang,profile,i):
    l=LABELS[lang]; lines=['TAX INVOICE' if lang!='ar' else 'فاتورة ضريبية','NOVA SUPPLIES' if lang!='ar' else 'شركة النور']
    vals={'invoice_number':f'INV-2026-{100+i:03d}','date':f'{(i%27)+1:02d}/11/2026','subtotal':'100.00','tax':'5.00','total':'105.00'}
    for n in ('invoice_number','date','subtotal','tax','total'):
        if profile=='missing_total' and n=='total': continue
        if profile=='missing_invoice' and n=='invoice_number': continue
        lines.append(f"{l[n]} {vals[n]}")
    return '\n'.join(lines)

def field_defs(lang):
    out=[]
    for n in ('invoice_number','date','subtotal','tax','total'):
        vb,lb=B[n]
        out.append(TemplateFieldDefinition(n,n,n in {'invoice_number','date','total'},1,(LABELS[lang][n],),PAT[n],TemplateFieldMode.LABEL_RELATIVE,vb,lb,minimum_confidence=.7))
    return tuple(out)

class AI:
    def __init__(self,lang,trap=False): self.lang=lang; self.trap=trap; self.generate_calls=0; self.refine_calls=0
    def generate_template(self,d,c):
        self.generate_calls+=1
        return TemplateGenerationProposal('invoice',field_defs(self.lang),diagnostics=('stress_generate',))
    def refine_template(self,d,p,c):
        self.refine_calls+=1; out=[]
        for f in p.fields:
            vb,lb=B.get(f.name,(f.normalized_value_bbox,f.normalized_label_bbox))
            if self.trap and self.refine_calls==1 and f.name=='total': vb=B['subtotal'][0]
            out.append(replace(f,normalized_value_bbox=vb,normalized_label_bbox=lb))
        return replace(p,fields=tuple(out),diagnostics=p.diagnostics+('stress_refine',))

class Reviewer:
    def __init__(self): self.calls=0
    def review_template(self,d,c,ctx):
        self.calls+=1; by={f.name:f for f in c.fields}; issues=[]
        if 'subtotal' in by and 'total' in by and by['subtotal'].normalized_value_bbox==by['total'].normalized_value_bbox:
            issues.append(TemplateReviewIssue('field','semantic_value_region_mismatch','total points to subtotal',.99,'total'))
        v=TemplateReviewVerdict.PASS if not issues else TemplateReviewVerdict.REJECT
        return TemplateReviewResult(v,.99,tuple(issues),ctx.candidate_hash,'stress-reviewer')

def png():
    im=Image.new('RGB',(900,1200),'white'); b=io.BytesIO(); im.save(b,format='PNG'); return b.getvalue()

def run(langs=None):
    rows=[]; idx=0
    langs=tuple(langs or ('en','ar','mixed'))
    for lang in langs:
        profiles=(['complete']*12)+(['missing_total']*6)+(['missing_invoice']*6)+(['semantic_trap']*6)
        for profile in profiles:
            idx+=1; trap=profile=='semantic_trap'; txt=text_for(lang,'complete' if trap else profile,idx)
            ocr=OCR(txt); ai=AI(lang,trap=trap); rev=Reviewer()
            svc=DataExtractionService(ocr_provider=ocr,field_ocr_providers=(ocr,),template_generation_provider=ai,template_review_provider=rev,template_options=TemplateExtractionOptions(max_refinement_attempts=1))
            r=svc.build_template_candidate(TemplateBuildRequest(DocumentInput('x.png',png(),f'{lang}:{idx}'),f'{lang}-{idx}',required_fields=('invoice_number','date','total'),mode=TemplateBuildMode.ADAPTIVE))
            expected_route='ai_generate_review' if profile.startswith('missing_') else 'deterministic_refine_review'
            ok=(r.route.value==expected_route and r.review is not None and r.review.verdict==TemplateReviewVerdict.PASS)
            if profile=='semantic_trap': ok=ok and ai.generate_calls==0 and ai.refine_calls==2 and rev.calls==2 and 'adaptive_review_issue_refinement_retry' in r.diagnostics
            if profile=='complete': ok=ok and ai.generate_calls==0 and ai.refine_calls>=1
            if profile.startswith('missing_'): ok=ok and ai.generate_calls==1
            rows.append({'lang':lang,'profile':profile,'ok':ok,'route':r.route.value,'q':r.quality.score if r.quality else None,'gen':ai.generate_calls,'refine':ai.refine_calls,'review':rev.calls,'verdict':r.review.verdict.value if r.review else None})
    summary={
      'cases':len(rows),'passed':sum(r['ok'] for r in rows),'failed':sum(not r['ok'] for r in rows),
      'by_language':{lang:{'passed':sum(r['ok'] for r in rows if r['lang']==lang),'total':sum(1 for r in rows if r['lang']==lang)} for lang in langs},
      'by_profile':{p:{'passed':sum(r['ok'] for r in rows if r['profile']==p),'total':sum(1 for r in rows if r['profile']==p)} for p in ('complete','missing_total','missing_invoice','semantic_trap')},
      'full_generate_calls':sum(r['gen'] for r in rows),'refine_calls':sum(r['refine'] for r in rows),'review_calls':sum(r['review'] for r in rows),
    }
    out={'summary':summary,'cases':rows}; suffix='_'.join(langs).upper(); path=ROOT/'benchmarks'/'v21_bilingual_intensive_artifacts'/f'V21_ROUTING_STRESS_{suffix}.json'; path.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8'); print(json.dumps(summary,ensure_ascii=False,indent=2)); print('WROTE',path)
    if summary['failed']: sys.exit(2)
if __name__=='__main__': run(sys.argv[1:] or None)
