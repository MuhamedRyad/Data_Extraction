from __future__ import annotations

from data_extraction.inspect_document import InspectDocumentRouter


def test_inspect_router_returns_typed_result_without_exceptions() -> None:
    router = InspectDocumentRouter()
    result = router.execute({
        "file_name": "note.txt",
        "content": b"hello world",
        "source_id": "src-1",
    })
    assert result.is_success
    assert result.value is not None
    assert result.value.manifest.page_count == 1
    assert result.value.manifest.file_kind.value == "txt"


def test_inspect_router_rejects_unknown_fields_at_boundary() -> None:
    router = InspectDocumentRouter()
    result = router.execute({
        "file_name": "note.txt",
        "content": b"hello world",
        "source_id": "src-1",
        "debug": True,
    })
    assert result.is_failure
    assert result.error is not None
    assert result.error.code == "INVALID_INSPECT_DOCUMENT_REQUEST"

from data_extraction.discover_fields import FieldDiscoveryRouter, FieldDiscoveryWorkflowService
from data_extraction.shared.models import ExtractionResult
from data_extraction.verify_fields import FieldVerificationInput


def test_field_discovery_boundary_is_strict_and_result_based() -> None:
    from data_extraction import DataExtractionService
    capability = DataExtractionService(options=__import__("data_extraction").ExtractionOptions(auto_enable_ai=False))
    router = FieldDiscoveryRouter(FieldDiscoveryWorkflowService(capability.extract))
    result = router.execute({
        "document": {
            "file_name": "note.txt",
            "content": b"Contact a@example.com on 2026-11-01",
            "source_id": "src",
        },
        "kinds": ("email", "date"),
    })
    assert result.is_success
    assert result.value is not None
    assert {item.kind.value for item in result.value.fields} == {"email", "date"}


def test_field_verification_input_rejects_extra_fields() -> None:
    from pydantic import ValidationError
    import pytest
    with pytest.raises(ValidationError):
        FieldVerificationInput.model_validate({
            "document": {"file_name": "x.png", "content": b"abc", "source_id": "src"},
            "fields": ({"name": "total", "labels": ("TOTAL",), "pattern": r"\\d+", "extra": 1},),
        })
