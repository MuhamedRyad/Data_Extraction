"""Public entrypoint: inspect() produces safe manifest; extract() dispatches actions."""
from __future__ import annotations

from dataclasses import asdict, replace
import json
import os
from typing import Any, cast

from .dispatch_extraction.extract_image import ExtractImageAction
from .dispatch_extraction.extract_table import ExtractTableAction
from .dispatch_extraction.extract_text import ExtractTextAction
from .inspect_document.service import InspectDocumentService
from .inspection_pipeline import inspect_and_analyze, inspect_document_result
from .analyze_layout.options import AnalyzeLayoutOptions
from .analyze_layout.service import AnalyzeLayoutAction
from .dispatch_extraction.extract_structured_tables import ExtractStructuredTablesAction
from .providers.structure_contracts import DocumentStructureProvider
from .analyze_layout.contracts import LayoutOrderProvider
from .providers.contracts import OCRLayoutProvider, OCRProvider, VisionProvider
from .providers.ocr.tesseract import TesseractOCRProvider
from .providers.ocr.adaptive_tesseract import AdaptiveTesseractOCRProvider
from .shared.models import DocumentInput, ExtractionResult, ExtractionStatus, FileManifest, InspectedDocument, PageResult
from .analyze_layout.pdf_native_reading_order import PDFRegionOrderProposal
from .options import ExtractionOptions
from .dispatch_extraction.service import DispatchRegionsAction
from .verify_fields.options import FieldVerificationOptions
from .verify_fields.request import FieldVerificationRequest
from .verify_fields.response import FieldVerificationResult
from .verify_fields.service import FieldVerificationService
from .discover_fields import FieldDiscoveryRequest, FieldDiscoveryResult, FieldDiscoveryService
from .integrations.document_ai import (
    OpenAIResponsesDocumentAIOptions, OpenAIResponsesDocumentAIProvider,
)
from .integrations.template_generation import (
    OpenAIResponsesTemplateOptions, OpenAIResponsesTemplateProvider, OpenAIResponsesTemplateReviewer,
)
from .template_extraction import (
    DocumentTemplate,
    TemplateAdaptiveRoute,
    TemplateCandidateQuality,
    TemplateApprovalRequest,
    TemplateBuildMode,
    TemplateBuildRequest,
    TemplateBuildResult,
    TemplateExtractionError,
    TemplateExtractionFeatureService,
    TemplateExtractionOptions,
    TemplateExtractionRequest,
    TemplateExtractionResult,
    TemplateGenerationProvider,
    TemplateReviewProvider,
    TemplateReviewResult,
    TemplateReviewVerdict,
    TemplateMatchRequest,
    TemplateMatchResult,
    TemplateMatchStatus,
    TemplateStore,
)
from .template_extraction.contracts import (
    TemplateRefinementContext, TemplateRefinementFieldFeedback,
)
from .template_extraction.table_service import TemplateTableExtractionService
from .template_extraction.workflow import TemplateWorkflowService
from .parallel_execution import BoundedCallGate
from .extract_document import ExtractDocumentRequest, ExtractDocumentResponse, ExtractDocumentService
from .inspect_document import (
    InspectDocumentOptions,
    InspectDocumentRequest,
    InspectDocumentResponse,
    InspectDocumentService,
)
from .shared.result import Result
from .template_extraction.application import TemplateApplicationService
from .discover_fields import FieldDiscoveryInput, FieldDiscoveryWorkflowService
from .verify_fields import FieldVerificationInput, FieldVerificationWorkflowService


class DataExtractionService:
    def __init__(self, options: ExtractionOptions | None = None,
                 ocr_provider: OCRProvider | None = None,
                 vision_provider: VisionProvider | None = None,
                 layout_provider: LayoutOrderProvider | None = None,
                 structure_provider: DocumentStructureProvider | None = None,
                 field_ocr_providers: tuple[OCRProvider, ...] | None = None,
                 field_layout_provider: OCRLayoutProvider | None = None,
                 field_verification_options: FieldVerificationOptions | None = None,
                 template_generation_provider: TemplateGenerationProvider | None = None,
                 template_review_provider: TemplateReviewProvider | None = None,
                 template_store: TemplateStore | None = None,
                 template_options: TemplateExtractionOptions | None = None,
                 ai_http_client: Any | None = None) -> None:
        self._options: ExtractionOptions = options or ExtractionOptions()
        effective_template_options: TemplateExtractionOptions = template_options or TemplateExtractionOptions()
        ocr_gate: BoundedCallGate = BoundedCallGate(
            self._options.max_parallel_ocr_calls if self._options.enable_parallel_execution else 1
        )
        ai_gate: BoundedCallGate = BoundedCallGate(
            self._options.max_parallel_ai_calls if self._options.enable_parallel_execution else 1
        )
        auto_ai_key: str | None = os.environ.get("OPENAI_API_KEY") if self._options.auto_enable_ai else None
        auto_document_ai: OpenAIResponsesDocumentAIProvider | None = None
        if auto_ai_key:
            auto_document_ai = OpenAIResponsesDocumentAIProvider(
                api_key=auto_ai_key,
                options=OpenAIResponsesDocumentAIOptions(
                    model=self._options.ai_model,
                    base_url=self._options.ai_base_url,
                    timeout_seconds=self._options.ai_timeout_seconds,
                    image_detail=self._options.ai_image_detail,
                    max_visual_dimension=self._options.ai_max_visual_dimension,
                    max_output_tokens=self._options.ai_max_output_tokens,
                    store_response=self._options.ai_store_response,
                ),
                http_client=ai_http_client,
            )
        selected_vision: VisionProvider | None = vision_provider or auto_document_ai
        selected_layout: LayoutOrderProvider | None = layout_provider or auto_document_ai
        self._layout_uses_auto_ai: bool = layout_provider is None and auto_document_ai is not None
        selected_template_generator: TemplateGenerationProvider | None = template_generation_provider
        if selected_template_generator is None and auto_ai_key:
            selected_template_generator = OpenAIResponsesTemplateProvider(
                api_key=auto_ai_key,
                options=OpenAIResponsesTemplateOptions(
                    model=self._options.ai_model,
                    base_url=self._options.ai_base_url,
                    timeout_seconds=self._options.ai_timeout_seconds,
                    image_detail=self._options.ai_image_detail,
                    max_visual_dimension=self._options.ai_max_visual_dimension,
                    max_output_tokens=max(self._options.ai_max_output_tokens, 6000),
                    store_response=self._options.ai_store_response,
                    max_pages_per_batch=effective_template_options.max_pages_per_ai_batch,
                    max_ai_batches_per_document=effective_template_options.max_ai_batches_per_document,
                    parallel_ai_batches_enabled=(
                        self._options.enable_parallel_execution
                        and effective_template_options.enable_parallel_ai_batches
                    ),
                    max_parallel_ai_batches=min(
                        self._options.max_parallel_ai_calls,
                        effective_template_options.max_parallel_ai_batches,
                    ),
                ),
                http_client=ai_http_client,
            )
        selected_template_reviewer: TemplateReviewProvider | None = template_review_provider
        if selected_template_reviewer is None and auto_ai_key:
            # Separate instance and separate Responses call: generation reasoning is
            # not shared with the independent semantic critic.
            selected_template_reviewer = OpenAIResponsesTemplateReviewer(
                api_key=auto_ai_key,
                options=OpenAIResponsesTemplateOptions(
                    model=self._options.ai_model,
                    base_url=self._options.ai_base_url,
                    timeout_seconds=self._options.ai_timeout_seconds,
                    image_detail=self._options.ai_image_detail,
                    max_visual_dimension=self._options.ai_max_visual_dimension,
                    max_output_tokens=max(self._options.ai_max_output_tokens, 4000),
                    store_response=self._options.ai_store_response,
                    max_pages_per_batch=effective_template_options.max_pages_per_ai_batch,
                    max_ai_batches_per_document=effective_template_options.max_ai_batches_per_document,
                    parallel_ai_batches_enabled=(
                        self._options.enable_parallel_execution
                        and effective_template_options.enable_parallel_ai_batches
                    ),
                    max_parallel_ai_batches=min(
                        self._options.max_parallel_ai_calls,
                        effective_template_options.max_parallel_ai_batches,
                    ),
                ),
                http_client=ai_http_client,
            )
        self._selective_ai_routing_enabled: bool = selected_vision is not None or selected_layout is not None
        self._auto_ai_credentials_available: bool = auto_ai_key is not None
        selected_ocr: OCRProvider | None = ocr_provider
        if selected_ocr is None and self._options.auto_enable_local_ocr:
            # Lazy adapter: missing pytesseract/tesseract is handled by the image
            # recovery path as a provider failure, never at import/inspect time.
            selected_ocr = (
                AdaptiveTesseractOCRProvider(self._options.tesseract_language_candidates)
                if self._options.adaptive_tesseract_profiles and len(self._options.tesseract_language_candidates) > 1
                else TesseractOCRProvider(language=self._options.tesseract_language)
            )
        self._inspect_service: InspectDocumentService = InspectDocumentService(
            options=InspectDocumentOptions.from_extraction_options(self._options)
        )
        self._layout_action: AnalyzeLayoutAction | None = (
            AnalyzeLayoutAction(selected_layout) if selected_layout is not None else None)
        self._structure_action: ExtractStructuredTablesAction | None = (
            ExtractStructuredTablesAction(structure_provider) if structure_provider is not None else None)
        self._router: DispatchRegionsAction = DispatchRegionsAction(
            ExtractTextAction(), ExtractTableAction(),
            ExtractImageAction(
                selected_ocr, selected_vision, ocr_gate=ocr_gate, vision_gate=ai_gate
            ),
        )
        selected_field_providers: tuple[OCRProvider, ...] = (
            field_ocr_providers
            if field_ocr_providers is not None
            else ((selected_ocr,) if selected_ocr is not None else ())
        )
        selected_layout_provider: OCRLayoutProvider | None = field_layout_provider
        if selected_layout_provider is None and selected_ocr is not None \
                and callable(getattr(selected_ocr, "recognize_regions", None)):
            selected_layout_provider = cast(OCRLayoutProvider, selected_ocr)
        self._field_verifier: FieldVerificationService = FieldVerificationService(
            selected_field_providers,
            layout_provider=selected_layout_provider,
            options=field_verification_options,
        )
        self._field_discovery: FieldDiscoveryService = FieldDiscoveryService()
        self._template_options: TemplateExtractionOptions = effective_template_options
        table_provider_candidates: list[Any] = []
        for provider in ((selected_ocr,) + selected_field_providers):
            if provider is None or not callable(getattr(provider, "recognize_tokens", None)):
                continue
            if any(existing is provider for existing in table_provider_candidates):
                continue
            table_provider_candidates.append(provider)
        self._template_table_service: TemplateTableExtractionService = TemplateTableExtractionService(
            tuple(table_provider_candidates), self._template_options
        )
        self._template_feature: TemplateExtractionFeatureService = TemplateExtractionFeatureService(
            generator=selected_template_generator, reviewer=selected_template_reviewer,
            store=template_store, options=self._template_options
        )
        self._template_workflow: TemplateWorkflowService = TemplateWorkflowService(
            options=self._options,
            template_options=self._template_options,
            inspect_service=self._inspect_service,
            inspect_document=self._inspect_and_analyze_document,
            template_feature=self._template_feature,
            field_verifier=self._field_verifier,
            template_table_service=self._template_table_service,
            extract_document=self.extract,
            extract_document_with_options=self._extract_with_options,
        )
        self._extract_document_service: ExtractDocumentService = ExtractDocumentService(self.extract)
        self._field_discovery_workflow: FieldDiscoveryWorkflowService = FieldDiscoveryWorkflowService(
            self.extract, self._field_discovery
        )
        self._field_verification_workflow: FieldVerificationWorkflowService = FieldVerificationWorkflowService(
            self.extract, self._field_verifier
        )
        self._template_application: TemplateApplicationService = TemplateApplicationService(
            self._template_workflow
        )

    def inspect(self, request: DocumentInput) -> FileManifest:
        """Classify type and each page/region without OCR/model calls."""
        document = self._inspect_and_analyze_document(request)
        return self._inspect_service.build_manifest(document)

    def _inspect_and_analyze_document(
        self,
        request: DocumentInput,
        options: ExtractionOptions | None = None,
    ) -> InspectedDocument:
        effective_options: ExtractionOptions = options or self._options
        return inspect_and_analyze(
            InspectDocumentRequest.from_domain(request),
            self._inspect_service,
            InspectDocumentOptions.from_extraction_options(effective_options),
            AnalyzeLayoutOptions.from_extraction_options(effective_options),
        )

    def extract(self, request: DocumentInput) -> ExtractionResult:
        """Re-inspects the original bytes to avoid trusting client-supplied manifests."""
        return self._extract_with_options(request, self._options)

    def _extract_with_options(
        self, request: DocumentInput, options: ExtractionOptions,
    ) -> ExtractionResult:
        """Internal bounded extraction with an explicit immutable option set."""
        document = self._inspect_and_analyze_document(request, options)
        if self._layout_action is not None:
            layout_options: ExtractionOptions = (
                options.model_copy(update={"layout_provider_fallback_only": True})
                if self._layout_uses_auto_ai else options
            )
            document = self._layout_action.execute(request, document, layout_options)
        invocations: int = 0
        if self._structure_action is not None and document.file_kind.value == "pdf" \
                and options.max_structured_tables > 0 and not options.selected_pdf_pages:
            invocations = 1
            document = self._structure_action.execute(request, document, options)
        manifest: FileManifest = self._inspect_service.build_manifest(document)
        result: ExtractionResult = self._router.execute(document, manifest, options)
        diagnostics: tuple[str, ...] = result.diagnostics
        if options.selected_pdf_pages:
            diagnostics = tuple(dict.fromkeys(diagnostics + (
                "selective_pdf_page_extraction",
                "selected_pages:" + ",".join(str(value) for value in options.selected_pdf_pages),
            )))
        if self._selective_ai_routing_enabled:
            diagnostics = tuple(dict.fromkeys(diagnostics + ("selective_ai_routing_enabled",)))
        elif options.auto_enable_ai and not self._auto_ai_credentials_available:
            diagnostics = tuple(dict.fromkeys(diagnostics + ("auto_ai_credentials_not_configured",)))
        return replace(result, structure_provider_invocations=invocations, diagnostics=diagnostics)

    def discover_fields(self, request: FieldDiscoveryRequest) -> FieldDiscoveryResult:
        """Discover generic typed value shapes; domain meaning is never guessed."""
        extraction: ExtractionResult = self.extract(request.document)
        return self._field_discovery.discover(extraction, request.kinds)

    def verify_fields(self, request: FieldVerificationRequest) -> FieldVerificationResult:
        """Verify configured sensitive fields without mutating extracted source text.

        Field crops are independently re-read when OCR providers are configured.
        Schema/template transforms remain review evidence unless independent OCR
        observations reach consensus.
        """
        extraction: ExtractionResult = self.extract(request.document)
        return self._field_verifier.verify(request, extraction)


    def build_template_candidate(self, request: TemplateBuildRequest) -> TemplateBuildResult:
        return self._template_workflow.build_template_candidate(request)

    def approve_template(self, request: TemplateApprovalRequest) -> DocumentTemplate:
        return self._template_workflow.approve_template(request)

    def match_template(self, request: TemplateMatchRequest) -> TemplateMatchResult:
        return self._template_workflow.match_template(request)

    def extract_with_template(self, request: TemplateExtractionRequest) -> TemplateExtractionResult:
        return self._template_workflow.extract_with_template(request)

    def inspect_result(self, request: InspectDocumentRequest) -> Result[InspectDocumentResponse]:
        return inspect_document_result(
            request,
            self._inspect_service,
            analysis_options=AnalyzeLayoutOptions.from_extraction_options(self._options),
        )

    def extract_result(self, request: ExtractDocumentRequest) -> Result[ExtractDocumentResponse]:
        return self._extract_document_service.execute(request)

    def discover_fields_result(self, request: FieldDiscoveryInput) -> Result[FieldDiscoveryResult]:
        return self._field_discovery_workflow.execute(request)

    def verify_fields_result(self, request: FieldVerificationInput) -> Result[FieldVerificationResult]:
        return self._field_verification_workflow.execute(request)

    def build_template_candidate_result(self, request: TemplateBuildRequest) -> Result[TemplateBuildResult]:
        return self._template_application.build_candidate(request)

    def approve_template_result(self, request: TemplateApprovalRequest) -> Result[DocumentTemplate]:
        return self._template_application.approve(request)

    def match_template_result(self, request: TemplateMatchRequest) -> Result[TemplateMatchResult]:
        return self._template_application.match(request)

    def extract_with_template_result(self, request: TemplateExtractionRequest) -> Result[TemplateExtractionResult]:
        return self._template_application.extract(request)

    def inspect_pdf_rtl_proposals(self, request: DocumentInput) -> tuple[PDFRegionOrderProposal, ...]:
        """Optional, non-destructive native PDF bidi suggestions, separate from JSON schema.

        Only proposals with unchanged token multiset are included. An expert
        must review visual order before choosing any reordering. Extract() and
        inspect() never silently replace native source text with a suggestion.
        """
        options: ExtractionOptions = self._options.model_copy(
            update={"analyze_pdf_rtl_geometry": True}
        )
        inspected: InspectedDocument = self._inspect_and_analyze_document(request, options)
        suggestions: list[PDFRegionOrderProposal] = []
        for page in inspected.pages:
            for region in page.regions:
                if region.reading_order_proposal is not None and region.bbox is not None:
                    suggestions.append(PDFRegionOrderProposal(
                        region.region_id, page.number, region.text or "",
                        region.reading_order_proposal, region.bbox,
                    ))
        return tuple(suggestions)

    @staticmethod
    def to_dict(result: ExtractionResult | FileManifest | FieldVerificationResult | FieldDiscoveryResult | TemplateBuildResult | TemplateMatchResult | TemplateExtractionResult | DocumentTemplate) -> dict[str, Any]:
        """Safe JSON payload; raw bytes are never placed into output contracts."""
        return asdict(result)

    @staticmethod
    def to_json(result: ExtractionResult | FileManifest | FieldVerificationResult | FieldDiscoveryResult | TemplateBuildResult | TemplateMatchResult | TemplateExtractionResult | DocumentTemplate, indent: int | None = 2) -> str:
        """Strict JSON: refuse non-finite numbers instead of emitting NaN/Infinity."""
        return json.dumps(DataExtractionService.to_dict(result), ensure_ascii=False,
                          allow_nan=False, indent=indent)
