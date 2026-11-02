from __future__ import annotations

from data_extraction import (
    DataExtractionService,
    ExtractDocumentRequest,
    ExtractionOptions,
    InspectDocumentRequest,
)
from data_extraction.template_extraction import TemplateBuildMode, TemplateBuildRequest
from data_extraction.shared.models import DocumentInput


def test_public_result_api_returns_success_without_changing_legacy_api() -> None:
    service = DataExtractionService(options=ExtractionOptions(auto_enable_ai=False))
    request = ExtractDocumentRequest(file_name="x.txt", content=b"hello", source_id="src")
    result = service.extract_result(request)
    assert result.is_success
    assert result.value is not None
    assert result.value.extraction.pages[0].regions[0].text == "hello"

    legacy = service.extract(DocumentInput("x.txt", b"hello", "src"))
    assert legacy.pages[0].regions[0].text == "hello"


def test_public_result_api_converts_expected_failure() -> None:
    service = DataExtractionService(options=ExtractionOptions(auto_enable_ai=False))
    request = ExtractDocumentRequest(file_name="x.bin", content=b"\x00bad", source_id="src")
    result = service.extract_result(request)
    assert result.is_failure
    assert result.error is not None
    assert result.error.code == "UNSUPPORTED_BINARY"


def test_inspect_result_is_explicit() -> None:
    service = DataExtractionService(options=ExtractionOptions(auto_enable_ai=False))
    result = service.inspect_result(
        InspectDocumentRequest(file_name="x.txt", content=b"hello", source_id="src")
    )
    assert result.is_success
    assert result.value is not None
    assert result.value.manifest.page_count == 1


def test_template_expected_failure_is_result_not_exception_on_new_boundary() -> None:
    service = DataExtractionService(options=ExtractionOptions(auto_enable_ai=False))
    request = TemplateBuildRequest(
        document=DocumentInput("x.txt", b"hello", "src"),
        family="family",
        required_fields=("missing",),
        mode=TemplateBuildMode.AI_REQUIRED,
    )
    result = service.build_template_candidate_result(request)
    assert result.is_failure
    assert result.error is not None


def test_template_router_rejects_blank_family() -> None:
    from data_extraction.template_extraction import TemplateExtractionRouter

    service = DataExtractionService()
    router = TemplateExtractionRouter(service._template_application)
    result = router.build_candidate({
        "document": {"file_name": "sample.txt", "content": b"hello", "source_id": "test"},
        "family": "   ",
    })

    assert result.is_failure
    assert result.unwrap_error().code == "INVALID_TEMPLATE_BUILD_REQUEST"


def test_template_router_builds_candidate_from_strict_input() -> None:
    from data_extraction.template_extraction import TemplateExtractionRouter

    service = DataExtractionService()
    router = TemplateExtractionRouter(service._template_application)
    result = router.build_candidate({
        "document": {"file_name": "sample.txt", "content": b"Invoice No INV-12345", "source_id": "test"},
        "family": "sample-family",
        "mode": "deterministic",
    })

    assert result.is_success
    assert result.unwrap().candidate.family == "sample-family"
