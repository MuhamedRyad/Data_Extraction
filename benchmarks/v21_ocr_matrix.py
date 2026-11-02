from __future__ import annotations
import json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'benchmarks'))
from v21_bilingual_intensive import generate_document, normalized_value
from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions
from data_extraction.providers.ocr.tesseract import TesseractOCRProvider

OUT=ROOT/'benchmarks'/'v21_bilingual_intensive_artifacts'; OUT.mkdir(exist_ok=True)

def run(lang:str,count:int=8):
    degs=['clean','blur','low_contrast','noise','skew','clean','blur','noise']
    rows=[]
    for i in range(count):
        split=(i%3==2)
        data,spec,values=generate_document(f'ocr-{lang}-{i}',lang,'ocr',300+i,degs[i%len(degs)],split=split)
        ocr=TesseractOCRProvider(language='ara+eng', reconstruct_raster_tables=True)
        svc=DataExtractionService(ocr_provider=ocr, options=ExtractionOptions(auto_enable_ai=False,auto_enable_local_ocr=False,ocr_confidence_threshold=0.78))
        res=svc.extract(DocumentInput(f'{lang}-{i}.png',data,f'ocr:{lang}:{i}'))
        p=res.pages[0]
        fields={f.field_kind:normalized_value(f.value) for f in p.structured_fields}
        expected={k:normalized_value(v) for k,v in values.items()}
        required=('invoice_number','date','total')
        all5=('invoice_number','date','subtotal','tax','total')
        row={
          'case':i,'language':lang,'degradation':degs[i%len(degs)],'split':split,
          'kind':p.structured_document_kind,'kind_confidence':p.structured_document_confidence,
          'ocr_confidence':next((r.confidence for r in p.regions if (r.text or '').strip()),None),
          'fields':fields,
          'required_exact':{k:fields.get(k)==expected[k] for k in required},
          'all5_exact':{k:fields.get(k)==expected[k] for k in all5},
          'false_address':fields.get('address'),
          'extraction_status':res.extraction_status.value,'semantic_status':res.semantic_status,
        }
        rows.append(row)
        if i==0: (OUT/f'ocr_sample_{lang}.png').write_bytes(data)
        print(lang,i,degs[i%len(degs)],row['kind'],sum(row['required_exact'].values()),'/3',sum(row['all5_exact'].values()),'/5', 'addr=',row['false_address'])
    summary={
      'language':lang,'documents':len(rows),
      'invoice_classified':sum(r['kind']=='invoice' for r in rows),
      'required_exact':sum(sum(r['required_exact'].values()) for r in rows),
      'required_total':len(rows)*3,
      'all5_exact':sum(sum(r['all5_exact'].values()) for r in rows),
      'all5_total':len(rows)*5,
      'false_address_count':sum(bool(r['false_address']) and ('invoice' in r['false_address'].casefold() or 'الفاتورة' in r['false_address']) for r in rows),
    }
    path=OUT/f'V21_OCR_MATRIX_{lang.upper()}.json'; path.write_text(json.dumps({'summary':summary,'cases':rows},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2)); print('WROTE',path)
if __name__=='__main__': run(sys.argv[1],int(sys.argv[2]) if len(sys.argv)>2 else 8)
