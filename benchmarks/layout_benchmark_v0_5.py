"""Reproducible offline reading-order benchmark with explicit golden tokens.

The only executed engines are PyMuPDF, pdfplumber, and our own feature.
Docling, Surya, PaddleOCR are NOT rated without running their models.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import json
import os
import subprocess
import sys
import time

import fitz
import pdfplumber

from data_extraction import DataExtractionService, DocumentInput


@dataclass(frozen=True)
class Fixture:
    name: str
    pages: tuple[tuple[tuple[float, float, str], ...], ...]
    golden: tuple[str, ...]
    width: float = 520.0
    height: float = 470.0


def fixtures() -> tuple[Fixture, ...]:
    return (
        Fixture('two_columns', (
            ((30, 50, 'LX1'), (30, 140, 'LX2'), (300, 50, 'RX1'), (300, 140, 'RX2')),
        ), ('LX1','LX2','RX1','RX2')),
        Fixture('three_columns', (
            ((20, 50, 'LX1'), (20, 140, 'LX2'), (200, 50, 'MX1'), (200, 140, 'MX2'), (370, 50, 'RX1'), (370, 140, 'RX2')),
        ), ('LX1','LX2','MX1','MX2','RX1','RX2')),
        Fixture('heading_two_columns_footer', (
            ((30, 30, 'HEAD1'), (30, 80, 'LX1'), (30, 180, 'LX2'), (300, 80, 'RX1'), (300, 180, 'RX2'), (30, 340, 'FOOT1')),
        ), ('HEAD1','LX1','LX2','RX1','RX2','FOOT1')),
        Fixture('two_physical_pages', (
            ((30, 50, 'LX1'), (30, 140, 'LX2'), (300, 50, 'RX1'), (300, 140, 'RX2')),
            ((30, 50, 'LX3'), (30, 140, 'LX4'), (300, 50, 'RX3'), (300, 140, 'RX4')),
        ), ('LX1','LX2','RX1','RX2','LX3','LX4','RX3','RX4')),
        Fixture('spanning_middle_block', (
            ((30,50,'LX1'),(30,125,'LX2'),(300,50,'RX1'),(300,125,'RX2'),
             (30,210,'WIDE1 a very long full page width section divider explaining the next section'),(30,275,'LX3'),(30,350,'LX4'),(300,275,'RX3'),(300,350,'RX4')),
        ), ('LX1','LX2','RX1','RX2','WIDE1','LX3','LX4','RX3','RX4')),
    )


def make_pdf(fixture: Fixture) -> bytes:
    document: fitz.Document = fitz.open()
    for inputs in fixture.pages:
        page: fitz.Page = document.new_page(width=fixture.width, height=fixture.height)
        for x, y, value in inputs:
            page.insert_text((x,y),value,fontname='helv',fontsize=11)
    payload: bytes = document.tobytes()
    document.close()
    return payload


def from_feature(blob: bytes) -> list[str]:
    output = DataExtractionService().extract(DocumentInput('benchmark.pdf',blob,'golden:layout'))
    return [token for page in output.pages for region in page.regions
            for token in (region.text or '').split()]


def from_fitz(blob: bytes) -> list[str]:
    output: list[str] = []
    with fitz.open(stream=blob,filetype='pdf') as document:
        for page in document:
            output.extend(page.get_text('text',sort=True).split())
    return output


def from_pdfplumber(blob: bytes) -> list[str]:
    import io
    output: list[str] = []
    with pdfplumber.open(io.BytesIO(blob)) as document:
        for page in document.pages:
            output.extend(word['text'] for word in page.extract_words())
    return output


def measure(ground: tuple[str, ...], actual: list[str]) -> dict[str, object]:
    expected_set: set[str] = set(ground)
    filtered: list[str] = [token for token in actual if token in expected_set]
    indexed: dict[str,int] = {token: idx for idx,token in enumerate(filtered)}
    correct_pairs: int = sum(indexed[ground[i]] < indexed[ground[j]]
                             for i in range(len(ground)) for j in range(i+1,len(ground))
                             if ground[i] in indexed and ground[j] in indexed)
    all_pairs: int = len(ground)*(len(ground)-1)//2
    completeness: bool = len(filtered)==len(ground) and len(set(filtered))==len(ground)
    return {'tokens': filtered, 'pairwise_order_score': round(correct_pairs / all_pairs,4),
            'exact_order': filtered == list(ground), 'complete': completeness,
            'missing_tokens': [x for x in ground if x not in filtered]}


def from_legacy_v04(blob: bytes) -> list[str]:
    """Optional only; set BASELINE_V04_DIR to the v0.4 source directory."""
    source_directory: str | None = os.environ.get('BASELINE_V04_DIR')
    if source_directory is None:
        raise ValueError('v0.4 baseline directory not configured')
    code: str = (
        'import sys; from data_extraction import DataExtractionService,DocumentInput; '
        'out=DataExtractionService().extract(DocumentInput("baseline.pdf",sys.stdin.buffer.read(),"baseline")); '
        'print(" ".join((r.text or "") for p in out.pages for r in p.regions))'
    )
    process: subprocess.CompletedProcess[bytes] = subprocess.run(
        [sys.executable,'-c',code],input=blob,
        env={**os.environ,'PYTHONPATH':source_directory}, cwd=source_directory,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,check=True)
    return process.stdout.decode('utf-8').split()


def main() -> None:
    engines: dict[str,Callable[[bytes],list[str]]] = {
        'feature_v0_5':from_feature, 'pymupdf_sort':from_fitz,
        'pdfplumber_default':from_pdfplumber,
    }
    if os.environ.get('BASELINE_V04_DIR'):
        engines['feature_v0_4'] = from_legacy_v04
    results: dict[str,object] = {'fixture_count':len(fixtures()),
                                 'comparison_type':'synthetic_vector_text_order_only',
                                 'external_ml_engines':'not_run_unavailable',
                                 'cases':{},'summary':{}}
    cases: dict[str,dict[str,object]] = {}
    for fixture in fixtures():
        blob: bytes = make_pdf(fixture)
        measured: dict[str,object] = {}
        for engine,fn in engines.items():
            started: float = time.perf_counter()
            try:
                actual: list[str] = fn(blob)
                outcome: dict[str,object] = measure(fixture.golden,actual)
                outcome['elapsed_ms'] = round(1000*(time.perf_counter()-started),2)
                measured[engine] = outcome
            except Exception as exc:
                measured[engine] = {'error_type':type(exc).__name__}
        cases[fixture.name] = {'golden':fixture.golden,'page_count':len(fixture.pages),'engines':measured}
    results['cases'] = cases
    for engine in engines:
        rows: list[dict[str,object]] = [cases[f.name]['engines'][engine] for f in fixtures()]  # type: ignore[index]
        success: list[dict[str,object]] = [row for row in rows if 'exact_order' in row]
        results['summary'][engine] = {  # type: ignore[index]
            'successful_runs':len(success),
            'exact_order_cases':sum(bool(row['exact_order']) for row in success),
            'mean_pairwise_order_score':round(sum(float(row['pairwise_order_score']) for row in success)/len(success),4) if success else None,
            'all_tokens_preserved_cases':sum(bool(row['complete']) for row in success),
        }
    path: Path = Path(__file__).with_name('layout_benchmark_v0_5_results.json')
    path.write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(results['summary'],indent=2))
    print(f'Saved: {path}')


if __name__ == '__main__':
    main()
