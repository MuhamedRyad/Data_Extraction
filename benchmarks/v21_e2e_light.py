from __future__ import annotations
import io,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'benchmarks'))
from v21_bilingual_intensive import generate_document, OracleTemplateAI, OracleReviewer, TesseractProfile, normalized_value
from data_extraction import *
from data_extraction.providers.ocr.tesseract import TesseractOCRProvider
from data_extraction.template_extraction.store import InMemoryTemplateStore

def run(lang:str):
    fam=f'e2e-{lang}'
    start={'en':20,'ar':120,'mixed':220}[lang]
    ob,s1,v1=generate_document(fam,lang,'onboard',start,'clean',split=(lang=='mixed'))
    rp,s2,v2=generate_document(fam,lang,'repeat',start+1,'clean',split=(lang=='mixed'))
    specs={f'{fam}:onboard':s1,f'{fam}:repeat':s2}
    ai=OracleTemplateAI(specs); reviewer=OracleReviewer(specs); store=InMemoryTemplateStore()
    primary=TesseractOCRProvider(language='ara+eng',reconstruct_raster_tables=False)
    crop=TesseractProfile('tesseract-psm7',7)
    svc=DataExtractionService(
      ocr_provider=primary,field_ocr_providers=(crop,),template_generation_provider=ai,
      template_review_provider=reviewer,template_store=store,
      options=ExtractionOptions(auto_enable_ai=False,auto_enable_local_ocr=False,ocr_confidence_threshold=.78),
      template_options=TemplateExtractionOptions(max_refinement_attempts=1),
    )
    doc=DocumentInput(f'{fam}.png',ob,f'{fam}:onboard')
    b=svc.build_template_candidate(TemplateBuildRequest(doc,fam,required_fields=('invoice_number','date','total'),mode=TemplateBuildMode.ADAPTIVE))
    approved=False; err=None
    try:
      svc.approve_template(TemplateApprovalRequest(b.candidate,'benchmark',review=b.review)); approved=True
    except Exception as e: err=f'{type(e).__name__}:{e}'
    rr=None
    if approved:
      rr=svc.extract_with_template(TemplateExtractionRequest(DocumentInput(f'{fam}-r.png',rp,f'{fam}:repeat'),family=fam))
    fields={x.name:normalized_value(x.value) for x in rr.fields} if rr else {}
    exact={k:fields.get(k)==normalized_value(v2[k]) for k in ('invoice_number','date','total')}
    out={'language':lang,'approved':approved,'approval_error':err,'route':b.route.value if b.route else None,'initial_quality':b.quality.score if b.quality else None,'hard_failures':list(b.quality.hard_failures) if b.quality else [],'review':b.review.verdict.value if b.review else None,'review_issues':[{'code':i.code,'name':i.name,'reason':i.reason} for i in b.review.issues] if b.review else [],'diagnostics':list(b.diagnostics),'generate_calls':ai.generate_calls,'refine_calls':ai.refine_calls,'review_calls':reviewer.calls,'repeat_match':rr.match.score if rr else None,'repeat_status':rr.match.status.value if rr else None,'repeat_fast_path':(rr.generic_extraction_performed is False) if rr else None,'repeat_fields':fields,'repeat_details':{x.name:{'value':x.value,'status':x.status,'verified':x.verified,'reasons':list(x.review_reasons)} for x in rr.fields} if rr else {},'repeat_exact':exact,'repeat_all_exact':all(exact.values()),'repeat_semantic_complete':rr.semantic_complete if rr else None,'crop_calls':crop.calls}
    p=ROOT/'benchmarks'/'v21_bilingual_intensive_artifacts'/f'V21_E2E_LIGHT_{lang.upper()}.json';p.write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(out,ensure_ascii=False,indent=2));print('WROTE',p)
if __name__=='__main__':run(sys.argv[1])
