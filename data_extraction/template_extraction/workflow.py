from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

from ..inspect_document import InspectDocumentService
from ..options import ExtractionOptions
from ..shared.models import DocumentInput, ExtractionResult, ExtractionStatus, FileManifest, InspectedDocument, PageResult
from ..verify_fields.request import FieldVerificationRequest
from ..verify_fields.response import FieldVerificationResult
from ..verify_fields.service import FieldVerificationService
from .contracts import DocumentTemplate, TemplateAdaptiveRoute, TemplateCandidateQuality, TemplateRefinementContext, TemplateRefinementFieldFeedback, TemplateReviewResult, TemplateReviewVerdict, TemplateMatchStatus
from .errors import TemplateExtractionError
from .options import TemplateExtractionOptions
from .request import TemplateApprovalRequest, TemplateBuildMode, TemplateBuildRequest, TemplateExtractionRequest, TemplateMatchRequest
from .response import TemplateBuildResult, TemplateExtractionResult, TemplateMatchResult
from .service import TemplateExtractionFeatureService
from .table_service import TemplateTableExtractionService


class TemplateWorkflowService:
    """Application orchestration for template onboarding, matching, and reuse.

    The low-level template algorithms remain in ``TemplateExtractionFeatureService``.
    This class owns cross-feature orchestration only, keeping the root capability
    service small and readable.
    """

    def __init__(
        self,
        *,
        options: ExtractionOptions,
        template_options: TemplateExtractionOptions,
        inspect_service: InspectDocumentService,
        inspect_document: Callable[[DocumentInput], InspectedDocument],
        template_feature: TemplateExtractionFeatureService,
        field_verifier: FieldVerificationService,
        template_table_service: TemplateTableExtractionService,
        extract_document: Callable[[DocumentInput], ExtractionResult],
        extract_document_with_options: Callable[[DocumentInput, ExtractionOptions], ExtractionResult],
    ) -> None:
        self._options: ExtractionOptions = options
        self._template_options: TemplateExtractionOptions = template_options
        self._inspect_service: InspectDocumentService = inspect_service
        self._inspect_document: Callable[[DocumentInput], InspectedDocument] = inspect_document
        self._template_feature: TemplateExtractionFeatureService = template_feature
        self._field_verifier: FieldVerificationService = field_verifier
        self._template_table_service: TemplateTableExtractionService = template_table_service
        self._extract_document: Callable[[DocumentInput], ExtractionResult] = extract_document
        self._extract_document_with_options: Callable[[DocumentInput, ExtractionOptions], ExtractionResult] = extract_document_with_options

    def build_template_candidate(self, request: TemplateBuildRequest) -> TemplateBuildResult:
        """Build an untrusted template candidate with explicit routing semantics.

        Legacy modes keep their original behavior. ``ADAPTIVE`` starts with the
        deterministic/OCR bootstrap, measures reusable-template quality, and
        escalates to AI refinement or full AI generation only when needed.
        """
        extraction: ExtractionResult = self._extract_document(request.document)
        if request.mode == TemplateBuildMode.ADAPTIVE:
            return self._build_template_candidate_adaptive(request, extraction)
        return self._build_template_candidate_standard(request, extraction)

    def _build_template_candidate_standard(
        self, request: TemplateBuildRequest, extraction: ExtractionResult,
    ) -> TemplateBuildResult:
        built: TemplateBuildResult = self._template_feature.build_candidate(request, extraction)
        if request.mode == TemplateBuildMode.DETERMINISTIC \
                or not self._template_options.auto_refine_ai_templates \
                or not self._template_feature.refinement_supported:
            return self._review_template_build(request.document, extraction, built)
        return self._refine_template_build(request, extraction, built)

    def _build_template_candidate_adaptive(
        self, request: TemplateBuildRequest, extraction: ExtractionResult,
    ) -> TemplateBuildResult:
        deterministic_request: TemplateBuildRequest = replace(request, mode=TemplateBuildMode.DETERMINISTIC)
        deterministic: TemplateBuildResult = self._template_feature.build_candidate(
            deterministic_request, extraction, allow_missing_required=True
        )
        quality, refinement_feedback = self._assess_template_candidate(
            request, extraction, deterministic.candidate
        )
        base_diagnostics: tuple[str, ...] = tuple(dict.fromkeys(
            deterministic.diagnostics
            + (
                "adaptive_template_routing_enabled",
                f"adaptive_candidate_quality:{quality.score:.3f}",
            )
            + quality.diagnostics
            + tuple(f"adaptive_hard_failure:{item}" for item in quality.hard_failures)
        ))
        deterministic = replace(deterministic, diagnostics=base_diagnostics, quality=quality)

        must_generate: bool = bool(quality.hard_failures) or (
            quality.score < self._template_options.adaptive_medium_quality_threshold
        )
        if must_generate:
            return self._adaptive_full_generation_or_fallback(
                request, extraction, deterministic, quality
            )

        high_quality: bool = (
            quality.score >= self._template_options.adaptive_high_quality_threshold
            and quality.geometry_precision >= self._template_options.adaptive_minimum_geometry_precision
            and quality.schema_valid_ratio >= self._template_options.adaptive_minimum_schema_valid_ratio
        )
        if high_quality:
            reviewed: TemplateBuildResult = self._review_template_build(
                request.document, extraction, deterministic
            )
            if reviewed.review is not None \
                    and reviewed.review.verdict != TemplateReviewVerdict.PASS \
                    and self._template_options.adaptive_refine_after_reviewer_issue \
                    and self._template_feature.refinement_supported:
                review_feedback = self._template_review_refinement_feedback(
                    reviewed.candidate, reviewed.review
                )
                if review_feedback:
                    refined = self._refine_template_build(
                        request, extraction, deterministic, forced_feedback=review_feedback,
                        skip_initial_pass=True, max_attempts_override=1,
                    )
                    refined_quality, _ = self._assess_template_candidate(
                        request, extraction, refined.candidate
                    )
                    return replace(
                        refined,
                        route=TemplateAdaptiveRoute.DETERMINISTIC_REFINE_REVIEW,
                        quality=refined_quality,
                        diagnostics=tuple(dict.fromkeys(
                            refined.diagnostics + ("adaptive_route:deterministic_refine_review",)
                        )),
                    )
            return replace(
                reviewed,
                route=TemplateAdaptiveRoute.DETERMINISTIC_REVIEW,
                quality=quality,
                diagnostics=tuple(dict.fromkeys(
                    reviewed.diagnostics + ("adaptive_route:deterministic_review",)
                )),
            )

        if self._template_feature.refinement_supported:
            refined: TemplateBuildResult = self._refine_template_build(
                request, extraction, deterministic, forced_feedback=refinement_feedback,
                skip_initial_pass=True,
            )
            if refined.review is not None \
                    and refined.review.verdict != TemplateReviewVerdict.PASS \
                    and self._template_options.adaptive_refine_after_reviewer_issue:
                review_feedback = self._template_review_refinement_feedback(
                    refined.candidate, refined.review
                )
                if review_feedback:
                    refined = self._refine_template_build(
                        request, extraction, refined, forced_feedback=review_feedback,
                        skip_initial_pass=True, max_attempts_override=1,
                    )
                    refined = replace(
                        refined, diagnostics=tuple(dict.fromkeys(
                            refined.diagnostics + ("adaptive_review_issue_refinement_retry",)
                        ))
                    )
            refined_quality, _ = self._assess_template_candidate(
                request, extraction, refined.candidate
            )
            return replace(
                refined,
                route=TemplateAdaptiveRoute.DETERMINISTIC_REFINE_REVIEW,
                quality=refined_quality,
                diagnostics=tuple(dict.fromkeys(
                    refined.diagnostics + ("adaptive_route:deterministic_refine_review",)
                )),
            )

        return self._adaptive_full_generation_or_fallback(
            request, extraction, deterministic, quality
        )

    def _adaptive_full_generation_or_fallback(
        self,
        request: TemplateBuildRequest,
        extraction: ExtractionResult,
        deterministic: TemplateBuildResult,
        quality: TemplateCandidateQuality,
    ) -> TemplateBuildResult:
        if self._template_feature.generation_supported:
            ai_request: TemplateBuildRequest = replace(request, mode=TemplateBuildMode.AI_REQUIRED)
            generated: TemplateBuildResult = self._build_template_candidate_standard(ai_request, extraction)
            return replace(
                generated,
                route=TemplateAdaptiveRoute.AI_GENERATE_REVIEW,
                quality=quality,
                diagnostics=tuple(dict.fromkeys(
                    deterministic.diagnostics
                    + ("adaptive_route:ai_generate_review",)
                    + generated.diagnostics
                )),
            )
        reviewed: TemplateBuildResult = self._review_template_build(
            request.document, extraction, deterministic
        )
        return replace(
            reviewed,
            route=TemplateAdaptiveRoute.DETERMINISTIC_FALLBACK_REVIEW,
            quality=quality,
            diagnostics=tuple(dict.fromkeys(
                reviewed.diagnostics
                + (
                    "adaptive_ai_generator_unavailable",
                    "adaptive_route:deterministic_fallback_review",
                )
            )),
        )

    def _refine_template_build(
        self,
        request: TemplateBuildRequest,
        extraction: ExtractionResult,
        built: TemplateBuildResult,
        *,
        forced_feedback: tuple[TemplateRefinementFieldFeedback, ...] | None = None,
        skip_initial_pass: bool = False,
        max_attempts_override: int | None = None,
    ) -> TemplateBuildResult:
        current: DocumentTemplate = built.candidate
        diagnostics: list[str] = list(built.diagnostics)
        configured_attempts: int = self._template_options.max_refinement_attempts
        max_attempts: int = max(0, configured_attempts if max_attempts_override is None else max_attempts_override)
        pending_feedback: tuple[TemplateRefinementFieldFeedback, ...] | None = forced_feedback
        for attempt in range(1, max_attempts + 1):
            if pending_feedback is not None:
                feedback = pending_feedback
                pending_feedback = None
            else:
                definitions = self._template_feature.verification_definitions(current)
                if not definitions:
                    diagnostics.append("template_geometry_self_validation_no_fields")
                    break
                verification = self._field_verifier.verify(
                    FieldVerificationRequest(request.document, definitions), extraction
                )
                feedback = self._template_refinement_feedback(current, verification)
            failing = tuple(
                item for item in feedback
                if item.status in {
                    "geometry_review_required",
                    "geometry_precision_review_required",
                    "semantic_review_required",
                }
            )
            unavailable = tuple(item for item in feedback if item.status == "provider_unavailable")
            if not failing:
                if not skip_initial_pass or attempt > 1:
                    diagnostics.append("template_geometry_self_validation_passed")
                break
            if unavailable and len(unavailable) == len(feedback):
                diagnostics.append("template_geometry_refinement_skipped_no_field_ocr_provider")
                break
            diagnostics.append(f"template_geometry_refinement_attempt:{attempt}")
            context = TemplateRefinementContext(
                generation=self._template_feature.generation_context(request, extraction),
                attempt=attempt,
                max_attempts=max_attempts,
                feedback=failing,
            )
            try:
                current, refine_diagnostics = self._template_feature.refine_candidate(
                    request, extraction, current, context
                )
            except TemplateExtractionError as exc:
                if exc.code.startswith("TEMPLATE_REFINEMENT_"):
                    raise
                diagnostics.append(f"template_geometry_refinement_provider_failed:{exc.code}")
                break
            diagnostics.extend(refine_diagnostics)
        else:
            diagnostics.append("template_geometry_refinement_attempts_exhausted")

        definitions = self._template_feature.verification_definitions(current)
        if definitions:
            final_verification = self._field_verifier.verify(
                FieldVerificationRequest(request.document, definitions), extraction
            )
            final_feedback = self._template_refinement_feedback(current, final_verification)
            if not any(item.status == "geometry_review_required" for item in final_feedback):
                diagnostics.append("template_geometry_self_validation_passed")
            else:
                diagnostics.append("template_geometry_self_validation_incomplete")
        return self._review_template_build(
            request.document, extraction,
            TemplateBuildResult(current, tuple(dict.fromkeys(diagnostics)))
        )

    def _review_template_build(
        self, document: DocumentInput, extraction: ExtractionResult, built: TemplateBuildResult,
    ) -> TemplateBuildResult:
        try:
            review: TemplateReviewResult | None = self._template_feature.review_candidate(
                document, extraction, built.candidate
            )
        except TemplateExtractionError as exc:
            diagnostics = tuple(dict.fromkeys(
                built.diagnostics + (f"template_ai_review_provider_failed:{exc.code}",)
            ))
            return replace(built, diagnostics=diagnostics, review=None)
        if review is None:
            return built
        verdict_diagnostic = {
            TemplateReviewVerdict.PASS: "template_ai_review_passed",
            TemplateReviewVerdict.REVIEW_REQUIRED: "template_ai_review_requires_human",
            TemplateReviewVerdict.REJECT: "template_ai_review_rejected",
        }[review.verdict]
        diagnostics = tuple(dict.fromkeys(
            built.diagnostics + (verdict_diagnostic,) + review.diagnostics
        ))
        return replace(built, diagnostics=diagnostics, review=review)

    def _assess_template_candidate(
        self,
        request: TemplateBuildRequest,
        extraction: ExtractionResult,
        candidate: DocumentTemplate,
    ) -> tuple[TemplateCandidateQuality, tuple[TemplateRefinementFieldFeedback, ...]]:
        requested: set[str] = {item.strip() for item in request.required_fields if item.strip()}
        candidate_names: set[str] = {field.name for field in candidate.fields}
        missing: tuple[str, ...] = tuple(sorted(requested - candidate_names))
        required_coverage: float = (
            (len(requested) - len(missing)) / len(requested) if requested
            else (1.0 if candidate.fields or candidate.tables else 0.0)
        )

        geometry_scores: list[float] = [
            self._template_bbox_precision(field.normalized_value_bbox)
            for field in candidate.fields
        ]
        geometry_precision: float = (
            sum(geometry_scores) / len(geometry_scores) if geometry_scores else 0.0
        )
        anchor_coverage: float = (
            sum(1 for field in candidate.fields if field.anchors) / len(candidate.fields)
            if candidate.fields else 0.0
        )

        feedback: tuple[TemplateRefinementFieldFeedback, ...] = ()
        schema_valid_ratio: float = 0.0
        definitions = self._template_feature.verification_definitions(candidate)
        if definitions:
            verification = self._field_verifier.verify(
                FieldVerificationRequest(request.document, definitions), extraction
            )
            feedback = self._template_refinement_feedback(candidate, verification)
            checkable = tuple(item for item in feedback if item.status != "geometry_not_checked")
            schema_valid = sum(1 for item in checkable if item.status == "geometry_schema_valid")
            schema_valid_ratio = schema_valid / len(checkable) if checkable else 0.0

        feedback_by_name: dict[str, TemplateRefinementFieldFeedback] = {
            item.field_name: item for item in feedback
        }
        adaptive_feedback: list[TemplateRefinementFieldFeedback] = []
        for field in candidate.fields:
            current = feedback_by_name.get(field.name)
            precision = self._template_bbox_precision(field.normalized_value_bbox)
            if precision < self._template_options.adaptive_minimum_geometry_precision:
                reasons: tuple[str, ...] = ("adaptive_value_bbox_too_broad",)
                if current is not None:
                    reasons = tuple(dict.fromkeys(current.review_reasons + reasons))
                adaptive_feedback.append(TemplateRefinementFieldFeedback(
                    field_name=field.name,
                    page_number=field.page_number,
                    current_value_bbox=field.normalized_value_bbox,
                    current_label_bbox=field.normalized_label_bbox,
                    status="geometry_precision_review_required",
                    candidate_values=current.candidate_values if current else (),
                    schema_valid_values=current.schema_valid_values if current else (),
                    review_reasons=reasons,
                ))
            elif current is not None:
                adaptive_feedback.append(current)

        if candidate.tables:
            table_scores: list[float] = []
            for table in candidate.tables:
                if table.normalized_body_bbox is not None \
                        and table.column_definitions \
                        and len(table.column_definitions) == len(table.columns):
                    table_scores.append(1.0)
                elif table.normalized_bbox is not None and table.columns:
                    table_scores.append(0.60)
                else:
                    table_scores.append(0.0)
            table_structure_ratio = sum(table_scores) / len(table_scores)
        else:
            table_structure_ratio = 1.0

        options = self._template_options
        total_weight: float = (
            options.adaptive_required_field_weight
            + options.adaptive_geometry_weight
            + options.adaptive_anchor_weight
            + options.adaptive_schema_weight
            + options.adaptive_table_weight
        )
        weighted: float = (
            required_coverage * options.adaptive_required_field_weight
            + geometry_precision * options.adaptive_geometry_weight
            + anchor_coverage * options.adaptive_anchor_weight
            + schema_valid_ratio * options.adaptive_schema_weight
            + table_structure_ratio * options.adaptive_table_weight
        )
        score: float = max(0.0, min(1.0, weighted / max(total_weight, 1e-9)))
        hard_failures: list[str] = []
        if missing:
            hard_failures.extend(f"missing_required_field:{name}" for name in missing)
        if not candidate.fields and not candidate.tables:
            hard_failures.append("no_reusable_template_targets")
        diagnostics: tuple[str, ...] = (
            f"adaptive_required_field_coverage:{required_coverage:.3f}",
            f"adaptive_geometry_precision:{geometry_precision:.3f}",
            f"adaptive_anchor_coverage:{anchor_coverage:.3f}",
            f"adaptive_schema_valid_ratio:{schema_valid_ratio:.3f}",
            f"adaptive_table_structure_ratio:{table_structure_ratio:.3f}",
        )
        quality = TemplateCandidateQuality(
            score=score,
            required_field_coverage=required_coverage,
            geometry_precision=geometry_precision,
            anchor_coverage=anchor_coverage,
            schema_valid_ratio=schema_valid_ratio,
            table_structure_ratio=table_structure_ratio,
            hard_failures=tuple(hard_failures),
            diagnostics=diagnostics,
        )
        return quality, tuple(adaptive_feedback)

    @staticmethod
    def _template_bbox_precision(bbox: tuple[float, float, float, float] | None) -> float:
        if bbox is None:
            return 0.0
        width: float = max(0.0, bbox[2] - bbox[0])
        height: float = max(0.0, bbox[3] - bbox[1])
        area: float = width * height
        if area <= 0.08:
            return 1.0
        if area <= 0.20:
            return 0.80
        if area <= 0.40:
            return 0.55
        if area <= 0.65:
            return 0.30
        return 0.10

    @staticmethod
    def _template_review_refinement_feedback(
        candidate: DocumentTemplate, review: TemplateReviewResult,
    ) -> tuple[TemplateRefinementFieldFeedback, ...]:
        fields: dict[str, Any] = {field.name: field for field in candidate.fields}
        output: list[TemplateRefinementFieldFeedback] = []
        for issue in review.issues:
            if issue.scope != "field" or issue.name is None or issue.name not in fields:
                continue
            field = fields[issue.name]
            output.append(TemplateRefinementFieldFeedback(
                field_name=field.name,
                page_number=field.page_number,
                current_value_bbox=field.normalized_value_bbox,
                current_label_bbox=field.normalized_label_bbox,
                status="semantic_review_required",
                review_reasons=(f"{issue.code}:{issue.reason}",),
            ))
        return tuple(output)

    @staticmethod
    def _template_refinement_feedback(
        candidate: DocumentTemplate, verification: FieldVerificationResult,
    ) -> tuple[TemplateRefinementFieldFeedback, ...]:
        by_name: dict[str, list[Any]] = {}
        for item in verification.fields:
            by_name.setdefault(item.field_name, []).append(item)
        feedback: list[TemplateRefinementFieldFeedback] = []
        for field in candidate.fields:
            occurrences = by_name.get(field.name, [])
            candidates = tuple(
                candidate_item.value
                for occurrence in occurrences
                for candidate_item in occurrence.candidates
            )
            schema_valid = tuple(dict.fromkeys(
                candidate_item.value
                for occurrence in occurrences
                for candidate_item in occurrence.candidates
                if candidate_item.schema_valid
            ))
            reasons = tuple(dict.fromkeys(
                reason for occurrence in occurrences for reason in occurrence.review_reasons
            ))
            if any(str(occurrence.status.value) == "provider_unavailable" for occurrence in occurrences) \
                    and not any(occurrence.candidates for occurrence in occurrences):
                status = "provider_unavailable"
            elif schema_valid:
                status = "geometry_schema_valid"
            elif field.required or field.normalized_value_bbox is not None:
                status = "geometry_review_required"
            else:
                status = "geometry_not_checked"
            feedback.append(TemplateRefinementFieldFeedback(
                field_name=field.name,
                page_number=field.page_number,
                current_value_bbox=field.normalized_value_bbox,
                current_label_bbox=field.normalized_label_bbox,
                status=status,
                candidate_values=tuple(dict.fromkeys(candidates)),
                schema_valid_values=schema_valid,
                review_reasons=reasons,
            ))
        return tuple(feedback)

    def approve_template(self, request: TemplateApprovalRequest) -> DocumentTemplate:
        """Explicitly approve a validated template candidate and optionally persist it."""
        return self._template_feature.approve(request)

    def match_template(self, request: TemplateMatchRequest) -> TemplateMatchResult:
        """Match approved templates cheaply first, then use full extraction only if needed."""
        inspected: InspectedDocument = self._inspect_document(request.document)
        manifest: FileManifest = self._inspect_service.build_manifest(inspected)
        preflight = self._template_feature.preflight_match(
            manifest, request.document, request.templates, request.family
        )
        if preflight.status == TemplateMatchStatus.ACCEPTED or preflight.template is None:
            return preflight
        extraction: ExtractionResult = self._extract_document(request.document)
        return self._template_feature.match(extraction, request.templates, request.family, request.document)

    def extract_with_template(self, request: TemplateExtractionRequest) -> TemplateExtractionResult:
        """Use a known template first; fall back to generic extraction when uncertain.

        Accepted preflight matches can skip generic OCR/model extraction and run
        only field verification. A template narrows *where to look*; financial
        values still require native evidence or independent OCR consensus.
        """
        inspected: InspectedDocument = self._inspect_document(request.document)
        manifest: FileManifest = self._inspect_service.build_manifest(inspected)
        preflight: TemplateMatchResult = self._template_feature.preflight_match(
            manifest, request.document, request.templates, request.family
        )
        if preflight.template is not None and preflight.status == TemplateMatchStatus.ACCEPTED \
                and self._template_feature.fast_path_eligible(
                    preflight.template, self._template_table_service.available
                ):
            extraction = self._template_skeleton(manifest)
            definitions = self._template_feature.verification_definitions(preflight.template)
            verification = self._field_verifier.verify(
                FieldVerificationRequest(request.document, definitions), extraction
            ) if definitions else None
            template_tables = self._template_table_service.extract(
                request.document, preflight.template
            ) if preflight.template.tables else ()
            result = self._template_feature.compose_extraction(
                extraction, preflight, verification, template_tables
            )
            extra_diagnostics: tuple[str, ...] = ("template_fast_path_used",)
            if template_tables:
                extra_diagnostics += ("template_table_fast_path_used",)
            return replace(
                result,
                diagnostics=tuple(dict.fromkeys(result.diagnostics + extra_diagnostics)),
                generic_extraction_performed=False,
                generic_extraction_pages=(),
                template_fast_path_pages=preflight.stable_pages or tuple(
                    range(1, preflight.template.fingerprint.page_count + 1)
                ),
            )

        if preflight.template is not None \
                and preflight.changed_pages \
                and self._template_options.selective_page_fallback_enabled \
                and manifest.file_kind.value == "pdf":
            changed_pages: tuple[int, ...] = tuple(sorted(set(preflight.changed_pages)))
            stable_pages: tuple[int, ...] = tuple(sorted(set(preflight.stable_pages)))
            selective_options: ExtractionOptions = self._options.model_copy(
                update={"selected_pdf_pages": changed_pages}
            )
            extraction = self._extract_document_with_options(request.document, selective_options)
            definitions = self._template_feature.verification_definitions(
                preflight.template, stable_pages
            )
            verification = self._field_verifier.verify(
                FieldVerificationRequest(request.document, definitions), extraction
            ) if definitions else None
            stable_table_template = replace(
                preflight.template,
                tables=tuple(
                    table for table in preflight.template.tables
                    if table.page_number in set(stable_pages)
                ),
            )
            template_tables = self._template_table_service.extract(
                request.document, stable_table_template
            ) if stable_table_template.tables else ()
            result = self._template_feature.compose_extraction(
                extraction, preflight, verification, template_tables,
                blocked_pages=changed_pages,
            )
            return replace(
                result,
                diagnostics=tuple(dict.fromkeys(result.diagnostics + (
                    "template_selective_page_fallback_used",
                    "template_fast_pages:" + ",".join(str(value) for value in stable_pages),
                    "template_generic_pages:" + ",".join(str(value) for value in changed_pages),
                ))),
                generic_extraction_performed=True,
                generic_extraction_pages=changed_pages,
                template_fast_path_pages=stable_pages,
            )

        extraction = self._extract_document(request.document)
        match: TemplateMatchResult = self._template_feature.match(
            extraction, request.templates, request.family, request.document
        )
        verification: FieldVerificationResult | None = None
        template_tables = ()
        if match.template is not None and match.status != TemplateMatchStatus.REJECTED:
            definitions = self._template_feature.verification_definitions(match.template)
            if definitions:
                verification = self._field_verifier.verify(
                    FieldVerificationRequest(request.document, definitions), extraction
                )
            if self._template_table_service.available and match.template.tables:
                template_tables = self._template_table_service.extract(request.document, match.template)
        return replace(
            self._template_feature.compose_extraction(
                extraction, match, verification, template_tables
            ),
            generic_extraction_performed=True,
            generic_extraction_pages=tuple(page.number for page in extraction.pages),
            template_fast_path_pages=(),
        )

    @staticmethod
    def _template_skeleton(manifest: FileManifest) -> ExtractionResult:
        pages = tuple(
            PageResult(
                number=page.number, kind=page.kind, label=page.label, content_types=page.content_types,
                regions=(), requires_review=True, warnings=page.warnings, width=page.width, height=page.height,
                reading_order_quality=page.reading_order_quality, reading_order_region_ids=page.reading_order_region_ids,
                layout_provider_name=page.layout_provider_name, layout_confidence=page.layout_confidence,
                layout_model_id=page.layout_model_id, content_complete=False, completeness_ratio=0.0,
                extraction_status=ExtractionStatus.FAILED, text_coverage_complete=False, text_coverage_ratio=0.0,
            )
            for page in manifest.pages
        )
        return ExtractionResult(
            manifest=manifest, pages=pages, ready_for_publication=False, total_vision_calls=0,
            diagnostics=("template_fast_path_no_generic_extraction",), document_order_region_ids=manifest.document_order_region_ids,
            content_complete=False, completeness_ratio=0.0, extraction_status=ExtractionStatus.FAILED,
            text_coverage_complete=False, text_coverage_ratio=0.0, semantic_complete=None, semantic_status="not_applicable",
            field_coverage_ratio=0.0, verified_field_coverage_ratio=0.0, semantic_gap_count=0,
        )

