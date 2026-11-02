"""Structure refactor guardrails; no optional model inference required."""
from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

import pytest

from data_extraction import DataExtractionService, DocumentInput, ExtractionOptions, ImageRoute
from data_extraction.dispatch_extraction.assemble_document import assemble_document
from data_extraction.dispatch_extraction.service import DispatchRegionsAction
from data_extraction.inspect_document.service import InspectDocumentService
from data_extraction.providers.native.adapter import NativeFileAdapter
from data_extraction.shared.models import RegionKind


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_PDF = ROOT / 'examples' / 'synthetic_table_page.pdf'


@pytest.mark.parametrize('action', ['inspect', 'extract'])
def test_cli_writes_only_json_to_stdout(action: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'run_demo.py'), str(EXAMPLE_PDF), '--action', action],
        cwd=ROOT, capture_output=True, text=True, timeout=60, check=True,
    )
    data = json.loads(proc.stdout)
    assert data['schema_version'] == 'extraction.v0.6'
    assert proc.stdout.lstrip().startswith('{')


def test_legacy_and_new_entrypoint_identity() -> None:
    from data_extraction.router import DispatchRegionsAction as OldDispatch
    from data_extraction.actions.inspect_file_action import InspectFileAction as OldInspect
    from data_extraction.adapters.native_adapters import NativeFileAdapter as OldNative
    assert DispatchRegionsAction is OldDispatch
    assert InspectDocumentService is OldInspect
    assert NativeFileAdapter is OldNative


def test_extract_content_is_equal_after_reinspection() -> None:
    input_data = DocumentInput('sample.pdf', EXAMPLE_PDF.read_bytes(), 'fixture')
    service = DataExtractionService(options=ExtractionOptions(image_route=ImageRoute.DISABLED))
    manifest = service.inspect(input_data)
    result = service.extract(input_data)
    assert result.manifest == manifest
    assert result.document_order_region_ids == manifest.document_order_region_ids
    assert all(r.region_id in result.document_order_region_ids for p in result.pages for r in p.regions)


def test_providers_are_imported_lazily() -> None:
    code = ('import sys; import data_extraction; '
            'assert "docling" not in sys.modules; '
            'assert "unstructured" not in sys.modules')
    subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True,
                   timeout=30, check=True)


def test_table_handler_is_in_dispatch_feature() -> None:
    from data_extraction.dispatch_extraction.extract_table import ExtractTableAction
    assert ExtractTableAction.__module__ == 'data_extraction.dispatch_extraction.extract_table'
