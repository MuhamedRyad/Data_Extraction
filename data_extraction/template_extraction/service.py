from __future__ import annotations

import hashlib
import math

from dataclasses import replace

from ..shared.models import BoundingBox, DocumentInput, ExtractedRegion, ExtractionMethod, ExtractionResult, FileKind, FileManifest, PageResult, RegionKind
from ..structured_documents.contracts import StructuredFieldEvidence
from ..verify_fields.request import FieldDefinition
from ..verify_fields.response import FieldVerificationResult, FieldVerificationStatus, VerifiedField
from .contracts import (
    DocumentTemplate,
    TemplateAnchor,
    TemplateFieldDefinition,
    TemplateFieldMode,
    TemplateGenerationContext,
    TemplateGenerationProposal,
    TemplateRefinementContext,
    TemplateReviewContext,
    TemplateReviewResult,
    TemplateReviewVerdict,
    TemplateLayoutFingerprint,
    TemplateMatchStatus,
    TemplateStatus,
    TemplateTableDefinition,
)
from .errors import TemplateExtractionError
from .options import TemplateExtractionOptions
from .provider import TemplateGenerationProvider, TemplateReviewProvider
from .request import TemplateApprovalRequest, TemplateBuildMode, TemplateBuildRequest
from .response import (
    TemplateBuildResult,
    TemplateExtractionResult,
    TemplateFieldResult,
    TemplateMatchEvidence,
    TemplateMatchResult,
    TemplatePageMatch,
    TemplateTableResult,
)
from .store import InMemoryTemplateStore, TemplateStore
from .validation import template_content_hash, validate_template
from .helpers import bbox_distance as _bbox_distance
from .helpers import center as _center
from .helpers import document_kind as _document_kind
from .helpers import norm as _norm
from .helpers import normalize_bbox as _normalize_bbox
from .helpers import page_text as _page_text
from .helpers import safe_template_id as _safe_template_id
from .matching import TemplateMatchingService
from .visual_fingerprints import (
    anchor_visual_scores,
    page_visual_hashes,
    visual_hash,
    visual_similarity,
    with_anchor_visual_hashes,
)


class TemplateExtractionFeatureService:
    def __init__(
        self,
        generator: TemplateGenerationProvider | None = None,
        reviewer: TemplateReviewProvider | None = None,
        store: TemplateStore | None = None,
        options: TemplateExtractionOptions | None = None,
    ) -> None:
        self._generator = generator
        self._reviewer = reviewer
        self._store = store or InMemoryTemplateStore()
        self._reviews_by_hash: dict[str, TemplateReviewResult] = {}
        self._options = options or TemplateExtractionOptions()
        self._matching = TemplateMatchingService(self._options)

    @property
    def store(self) -> TemplateStore:
        return self._store

    @property
    def generation_supported(self) -> bool:
        return callable(getattr(self._generator, "generate_template", None))

    @property
    def refinement_supported(self) -> bool:
        return callable(getattr(self._generator, "refine_template", None))

    @property
    def review_supported(self) -> bool:
        return callable(getattr(self._reviewer, "review_template", None))

    def review_candidate(
        self,
        document: DocumentInput,
        extraction: ExtractionResult,
        candidate: DocumentTemplate,
    ) -> TemplateReviewResult | None:
        if not self._options.auto_review_template_candidates or not self.review_supported:
            return None
        if candidate.status != TemplateStatus.CANDIDATE:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_NOT_CANDIDATE", "Only candidate templates can be reviewed"
            )
        if template_content_hash(candidate) != candidate.content_hash:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_HASH_MISMATCH", "Template candidate content hash does not match payload"
            )
        context = TemplateReviewContext(
            family=candidate.family,
            source_id=extraction.manifest.source_id,
            source_sha256=extraction.manifest.sha256,
            candidate_hash=candidate.content_hash,
            page_text=_page_text(extraction),
            page_sizes=tuple((page.width, page.height) for page in extraction.pages),
        )
        review_fn = getattr(self._reviewer, "review_template")
        review: TemplateReviewResult = review_fn(document, candidate, context)
        try:
            verdict = TemplateReviewVerdict(str(review.verdict))
            confidence = float(review.confidence)
        except (TypeError, ValueError) as exc:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_INVALID_RESULT", "AI reviewer returned an invalid verdict/confidence"
            ) from exc
        if not math.isfinite(confidence) or confidence < 0.0 or confidence > 1.0:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_INVALID_RESULT", "AI reviewer confidence must be finite and between 0 and 1"
            )
        for issue in review.issues:
            issue_confidence = float(issue.confidence)
            if issue.scope not in {"template", "field", "table"} \
                    or not issue.code.strip() or not issue.reason.strip() \
                    or not math.isfinite(issue_confidence) \
                    or issue_confidence < 0.0 or issue_confidence > 1.0:
                raise TemplateExtractionError(
                    "TEMPLATE_REVIEW_INVALID_RESULT", "AI reviewer returned an invalid issue contract"
                )
        diagnostics = list(review.diagnostics)
        if review.reviewed_candidate_hash not in ("", candidate.content_hash):
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_HASH_MISMATCH", "AI review was produced for a different candidate hash"
            )
        if verdict == TemplateReviewVerdict.PASS and confidence < self._options.minimum_ai_review_confidence:
            verdict = TemplateReviewVerdict.REVIEW_REQUIRED
            diagnostics.append("template_ai_review_low_confidence_downgraded")
        if verdict == TemplateReviewVerdict.PASS and review.issues:
            verdict = TemplateReviewVerdict.REVIEW_REQUIRED
            diagnostics.append("template_ai_review_pass_with_issues_downgraded")
        normalized = replace(
            review,
            verdict=verdict,
            confidence=confidence,
            reviewed_candidate_hash=candidate.content_hash,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )
        self._reviews_by_hash[candidate.content_hash] = normalized
        return normalized

    @staticmethod
    def proposal_from_candidate(candidate: DocumentTemplate) -> TemplateGenerationProposal:
        return TemplateGenerationProposal(
            document_kind=candidate.document_kind,
            fields=candidate.fields,
            tables=candidate.tables,
            anchors=candidate.fingerprint.anchors,
            diagnostics=(),
        )

    def refine_candidate(
        self,
        request: TemplateBuildRequest,
        extraction: ExtractionResult,
        candidate: DocumentTemplate,
        refinement_context: TemplateRefinementContext,
    ) -> tuple[DocumentTemplate, tuple[str, ...]]:
        refine = getattr(self._generator, "refine_template", None)
        if not callable(refine):
            return candidate, ("template_refinement_provider_unavailable",)
        before: TemplateGenerationProposal = self.proposal_from_candidate(candidate)
        after: TemplateGenerationProposal = refine(request.document, before, refinement_context)
        self._validate_refinement_invariants(before, after)
        field_anchors: list[TemplateAnchor] = []
        for field in after.fields:
            for anchor_text in field.anchors:
                field_anchors.append(TemplateAnchor(
                    anchor_text, field.page_number, field.normalized_label_bbox, field.required
                ))
        combined_anchors: tuple[TemplateAnchor, ...] = tuple(dict.fromkeys(
            before.anchors + tuple(field_anchors)
        ))
        fingerprint = self._fingerprint(extraction, combined_anchors, request.document)
        refined = replace(
            candidate,
            fields=after.fields,
            tables=after.tables,
            fingerprint=fingerprint,
            content_hash="",
        )
        refined = replace(refined, content_hash=template_content_hash(refined))
        validate_template(refined, self._options)
        diagnostics = tuple(dict.fromkeys((
            "template_geometry_refined_by_ai_provider",
        ) + after.diagnostics))
        return refined, diagnostics

    @staticmethod
    def _validate_refinement_invariants(
        before: TemplateGenerationProposal, after: TemplateGenerationProposal,
    ) -> None:
        if before.document_kind != after.document_kind:
            raise TemplateExtractionError(
                "TEMPLATE_REFINEMENT_SEMANTIC_CHANGE",
                "Template refinement may not change document kind",
            )
        if before.tables != after.tables:
            raise TemplateExtractionError(
                "TEMPLATE_REFINEMENT_TABLE_CHANGE",
                "Template refinement may not change table semantics",
            )
        before_map = {field.name: field for field in before.fields}
        after_map = {field.name: field for field in after.fields}
        if before_map.keys() != after_map.keys():
            raise TemplateExtractionError(
                "TEMPLATE_REFINEMENT_FIELD_SET_CHANGE",
                "Template refinement may not add or remove fields",
            )
        for name, first in before_map.items():
            second = after_map[name]
            immutable_first = (
                first.name, first.field_kind, first.required, first.page_number, first.anchors,
                first.pattern, first.mode, first.template, first.minimum_confidence,
            )
            immutable_second = (
                second.name, second.field_kind, second.required, second.page_number, second.anchors,
                second.pattern, second.mode, second.template, second.minimum_confidence,
            )
            if immutable_first != immutable_second:
                raise TemplateExtractionError(
                    "TEMPLATE_REFINEMENT_SEMANTIC_CHANGE",
                    f"Template refinement may only adjust geometry for field '{name}'",
                )

    def generation_context(
        self, request: TemplateBuildRequest, extraction: ExtractionResult,
    ) -> TemplateGenerationContext:
        family = request.family.strip()
        if not family:
            raise TemplateExtractionError("TEMPLATE_FAMILY_REQUIRED", "Template family is required")
        return TemplateGenerationContext(
            family=family,
            source_id=extraction.manifest.source_id,
            source_sha256=extraction.manifest.sha256,
            required_fields=request.required_fields,
            page_count=len(extraction.pages),
            document_kind=_document_kind(extraction),
            page_text=_page_text(extraction),
            page_sizes=tuple((page.width, page.height) for page in extraction.pages),
        )

    def build_candidate(
        self,
        request: TemplateBuildRequest,
        extraction: ExtractionResult,
        *,
        allow_missing_required: bool = False,
    ) -> TemplateBuildResult:
        family = request.family.strip()
        if not family:
            raise TemplateExtractionError("TEMPLATE_FAMILY_REQUIRED", "Template family is required")
        template_id = request.template_id or _safe_template_id(family, extraction.manifest.sha256)
        version = self._store.next_version(template_id)
        context = self.generation_context(request, extraction)
        diagnostics: list[str] = []
        use_ai: bool = request.mode != TemplateBuildMode.DETERMINISTIC and self._generator is not None
        if request.mode == TemplateBuildMode.AI_REQUIRED and self._generator is None:
            raise TemplateExtractionError(
                "TEMPLATE_AI_PROVIDER_REQUIRED",
                "AI template generation was required but no TemplateGenerationProvider was configured",
            )
        if use_ai:
            proposal: TemplateGenerationProposal = self._generator.generate_template(request.document, context)
            diagnostics.append("template_generated_by_ai_provider_requires_approval")
            generator_name: str = type(self._generator).__name__
        else:
            proposal = self._bootstrap_proposal(extraction, request.required_fields)
            diagnostics.append("template_bootstrap_without_ai_requires_approval")
            generator_name = "deterministic_bootstrap"
        requested_required = {name.strip() for name in request.required_fields if name.strip()}
        proposed_names = {field.name for field in proposal.fields}
        missing_required = sorted(requested_required - proposed_names)
        if missing_required and not allow_missing_required:
            raise TemplateExtractionError(
                "TEMPLATE_REQUIRED_FIELD_MISSING",
                f"Generated template omitted required fields: {', '.join(missing_required)}",
            )
        if missing_required and allow_missing_required:
            diagnostics.append("template_required_fields_missing_for_adaptive_routing")
        if requested_required:
            proposal = replace(
                proposal,
                fields=tuple(
                    replace(field, required=True) if field.name in requested_required else field
                    for field in proposal.fields
                ),
            )
        field_anchors: list[TemplateAnchor] = []
        for field in proposal.fields:
            for anchor_text in field.anchors:
                field_anchors.append(TemplateAnchor(
                    anchor_text, field.page_number, field.normalized_label_bbox, field.required
                ))
        table_anchors: list[TemplateAnchor] = []
        for table in proposal.tables:
            if table.normalized_header_bbox is None or not table.header_anchors:
                continue
            table_anchors.append(TemplateAnchor(
                table.header_anchors[0], table.page_number,
                table.normalized_header_bbox, table.required,
            ))
        combined_anchors: tuple[TemplateAnchor, ...] = tuple(dict.fromkeys(
            proposal.anchors + tuple(field_anchors) + tuple(table_anchors)
        ))
        fingerprint = self._fingerprint(extraction, combined_anchors, request.document)
        template = DocumentTemplate(
            template_id=template_id,
            family=family,
            version=version,
            status=TemplateStatus.CANDIDATE,
            document_kind=proposal.document_kind or context.document_kind,
            fields=proposal.fields,
            tables=proposal.tables,
            fingerprint=fingerprint,
            source_sha256=extraction.manifest.sha256,
            source_id=extraction.manifest.source_id,
            generator_name=generator_name,
            content_hash="",
        )
        template = replace(template, content_hash=template_content_hash(template))
        validate_template(template, self._options)
        diagnostics.extend(proposal.diagnostics)
        return TemplateBuildResult(template, tuple(dict.fromkeys(diagnostics)))

    def approve(self, request: TemplateApprovalRequest) -> DocumentTemplate:
        candidate = request.candidate
        validate_template(candidate, self._options)
        if candidate.status != TemplateStatus.CANDIDATE:
            raise TemplateExtractionError("TEMPLATE_NOT_CANDIDATE", "Only candidate templates can be approved")
        if template_content_hash(candidate) != candidate.content_hash:
            raise TemplateExtractionError("TEMPLATE_HASH_MISMATCH", "Template candidate content hash does not match payload")
        review = request.review or self._reviews_by_hash.get(candidate.content_hash)
        if request.review is not None and request.review.reviewed_candidate_hash != candidate.content_hash:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_HASH_MISMATCH", "Approval review does not match the candidate content hash"
            )
        if self.review_supported and self._options.require_review_when_reviewer_configured and review is None:
            raise TemplateExtractionError(
                "TEMPLATE_AI_REVIEW_REQUIRED", "A semantic AI review is required before approving this candidate"
            )
        if review is not None and review.verdict == TemplateReviewVerdict.REJECT:
            raise TemplateExtractionError(
                "TEMPLATE_AI_REVIEW_REJECTED", "AI semantic review rejected this template candidate"
            )
        approved = replace(candidate, status=TemplateStatus.APPROVED, approved_by=request.approved_by.strip() or "human")
        if request.save:
            self._store.save(approved)
        return approved

    def match(
        self,
        extraction: ExtractionResult,
        templates: tuple[DocumentTemplate, ...] = (),
        family: str | None = None,
        document: DocumentInput | None = None,
    ) -> TemplateMatchResult:
        pool = templates or self._store.list_approved(family)
        pool = tuple(item for item in pool if item.status == TemplateStatus.APPROVED)
        if family is not None:
            pool = tuple(item for item in pool if item.family == family)
        if not pool:
            return TemplateMatchResult(None, 0.0, TemplateMatchStatus.REJECTED, False, (), ("no_approved_template_available",))
        if len(pool) > self._options.max_templates_to_match:
            raise TemplateExtractionError("TEMPLATE_MATCH_LIMIT_EXCEEDED", "Too many templates supplied for matching")
        scored = [self._matching.score_template(extraction, template, document) for template in pool]
        scored.sort(key=lambda item: item.score, reverse=True)
        best = scored[0]
        if len(scored) > 1 and best.score - scored[1].score <= self._options.ambiguity_margin:
            return replace(
                best,
                status=TemplateMatchStatus.REVIEW_REQUIRED,
                diagnostics=tuple(dict.fromkeys(best.diagnostics + ("template_match_ambiguous_between_versions_or_families",))),
            )
        return best

    def preflight_match(
        self,
        manifest: FileManifest,
        document: DocumentInput,
        templates: tuple[DocumentTemplate, ...] = (),
        family: str | None = None,
    ) -> TemplateMatchResult:
        """Cheap page-aware match using inspect metadata + per-page visual hashes.

        v0.22 deliberately fails closed for a templated page that visually drifts.
        Raster pages no longer inherit trust from page 1. Native previews/anchors
        remain useful evidence, but each targeted page receives its own verdict.
        """
        pool = templates or self._store.list_approved(family)
        pool = tuple(item for item in pool if item.status == TemplateStatus.APPROVED)
        if family is not None:
            pool = tuple(item for item in pool if item.family == family)
        if not pool:
            return TemplateMatchResult(None, 0.0, TemplateMatchStatus.REJECTED, False, (), ("no_approved_template_available",))
        current_visual_hashes = page_visual_hashes(document, manifest.file_kind)
        results: list[TemplateMatchResult] = []
        for template in pool[:self._options.max_templates_to_match]:
            anchor_scores_by_anchor = anchor_visual_scores(
                document, manifest.file_kind, template.fingerprint.anchors
            )
            page_count_score = 1.0 if manifest.page_count == template.fingerprint.page_count else max(
                0.0, 1.0 - abs(manifest.page_count - template.fingerprint.page_count) / max(1, template.fingerprint.page_count)
            )
            target_pages = self._template_target_pages(template)
            page_matches = tuple(
                self._preflight_page_match(
                    manifest, template, page_number, current_visual_hashes,
                    page_number in target_pages, anchor_scores_by_anchor,
                )
                for page_number in range(1, max(manifest.page_count, template.fingerprint.page_count) + 1)
            )
            changed_pages = tuple(
                item.page_number for item in page_matches if item.targeted and item.status != TemplateMatchStatus.ACCEPTED
            )
            stable_pages = tuple(
                item.page_number for item in page_matches if item.targeted and item.status == TemplateMatchStatus.ACCEPTED
            )

            # Preserve aggregate evidence for backward-compatible ranking, but
            # targeted page verdicts are the safety gate for ACCEPTED.
            targeted_scores = [item.score for item in page_matches if item.targeted]
            page_score = sum(targeted_scores) / len(targeted_scores) if targeted_scores else 0.0
            components: list[tuple[str, float, float]] = [
                ("page_count", page_count_score, self._options.page_count_weight),
                ("page_matches", page_score, max(0.01, 1.0 - self._options.page_count_weight)),
            ]
            evidence_weight = sum(weight for _, _, weight in components)
            score = sum(value * weight for _, value, weight in components) / max(evidence_weight, 1e-9)

            if manifest.page_count != template.fingerprint.page_count:
                status = TemplateMatchStatus.REVIEW_REQUIRED if score >= self._options.review_match_threshold else TemplateMatchStatus.REJECTED
            elif changed_pages:
                changed_statuses = [
                    item.status for item in page_matches if item.page_number in changed_pages
                ]
                status = (
                    TemplateMatchStatus.REJECTED
                    if all(item == TemplateMatchStatus.REJECTED for item in changed_statuses)
                    else TemplateMatchStatus.REVIEW_REQUIRED
                )
            elif score >= self._options.accepted_match_threshold:
                status = TemplateMatchStatus.ACCEPTED
            elif score >= self._options.review_match_threshold:
                status = TemplateMatchStatus.REVIEW_REQUIRED
            else:
                status = TemplateMatchStatus.REJECTED

            if family is None and status == TemplateMatchStatus.ACCEPTED:
                # Across families, cheap visual/layout evidence is not identity.
                has_native_anchor_evidence = any(
                    item.anchor_score is not None for item in page_matches if item.targeted
                )
                if not has_native_anchor_evidence:
                    status = TemplateMatchStatus.REVIEW_REQUIRED

            drift = bool(changed_pages) or manifest.page_count != template.fingerprint.page_count
            evidence = [
                TemplateMatchEvidence("page_count", page_count_score, "preflight page-count evidence"),
                TemplateMatchEvidence("page_matches", page_score, f"{len(stable_pages)}/{len(target_pages)} targeted pages accepted"),
            ]
            for item in page_matches:
                if not item.targeted:
                    continue
                evidence.append(TemplateMatchEvidence(
                    f"page_{item.page_number}", item.score,
                    f"status={item.status.value}; visual={item.visual_score}; anchors={item.anchor_score}; geometry={item.geometry_score}"
                ))
            diagnostics = ["template_preflight_match", "template_page_aware_preflight"]
            if changed_pages:
                diagnostics.append("template_page_drift_detected")
                diagnostics.append("template_changed_pages:" + ",".join(str(value) for value in changed_pages))
            if status == TemplateMatchStatus.REVIEW_REQUIRED:
                diagnostics.append("template_preflight_requires_selective_or_full_extraction")
            if status == TemplateMatchStatus.REJECTED:
                diagnostics.append("template_preflight_rejected")
            if drift:
                diagnostics.append("possible_template_drift")
            results.append(TemplateMatchResult(
                template, score, status, drift, tuple(evidence), tuple(diagnostics),
                page_matches=page_matches, changed_pages=changed_pages, stable_pages=stable_pages,
            ))
        results.sort(key=lambda item: item.score, reverse=True)
        best = results[0]
        if len(results) > 1 and best.score - results[1].score <= self._options.ambiguity_margin:
            return replace(
                best,
                status=TemplateMatchStatus.REVIEW_REQUIRED,
                diagnostics=tuple(dict.fromkeys(best.diagnostics + ("template_preflight_ambiguous",))),
            )
        return best

    def _template_target_pages(self, template: DocumentTemplate) -> set[int]:
        pages = {field.page_number for field in template.fields}
        pages.update(table.page_number for table in template.tables)
        pages.update(anchor.page_number for anchor in template.fingerprint.anchors if anchor.required)
        if not pages:
            pages.update(range(1, template.fingerprint.page_count + 1))
        return {page for page in pages if page >= 1}

    def _preflight_page_match(
        self,
        manifest: FileManifest,
        template: DocumentTemplate,
        page_number: int,
        current_visual_hashes: tuple[str | None, ...],
        targeted: bool,
        anchor_visual_scores: dict[TemplateAnchor, float],
    ) -> TemplatePageMatch:
        if page_number < 1 or page_number > len(manifest.pages) or page_number > template.fingerprint.page_count:
            return TemplatePageMatch(
                page_number, 0.0, TemplateMatchStatus.REJECTED, True, None, None, None, None, targeted,
                ("page_missing_from_current_or_template",),
            )
        page = manifest.pages[page_number - 1]
        expected_aspect = (
            template.fingerprint.page_aspect_ratios[page_number - 1]
            if page_number - 1 < len(template.fingerprint.page_aspect_ratios) else None
        )
        shape_score: float | None = None
        if expected_aspect is not None and page.width and page.height:
            actual = page.width / page.height
            shape_score = max(0.0, 1.0 - abs(actual - expected_aspect) / max(expected_aspect, 0.01))

        expected_hashes = template.fingerprint.page_visual_hashes
        expected_visual = (
            expected_hashes[page_number - 1]
            if page_number - 1 < len(expected_hashes)
            else (template.fingerprint.visual_hash if page_number == 1 else None)
        )
        current_visual = (
            current_visual_hashes[page_number - 1]
            if page_number - 1 < len(current_visual_hashes) else None
        )
        visual_score = visual_similarity(expected_visual, current_visual)

        anchors = [anchor for anchor in template.fingerprint.anchors if anchor.page_number == page_number]
        preview_regions = [region for region in page.regions if region.preview]
        anchor_values: list[float] = []
        geometry_values: list[float] = []
        for anchor in anchors:
            visual_anchor_score = anchor_visual_scores.get(anchor)
            if not preview_regions:
                if visual_anchor_score is not None:
                    anchor_values.append(visual_anchor_score)
                continue
            matches = [region for region in preview_regions if _norm(anchor.text) in _norm(region.preview or "")]
            if not matches:
                anchor_values.append(0.0 if anchor.required else 0.5)
                continue
            text_score = 1.0
            anchor_values.append(
                min(text_score, visual_anchor_score)
                if visual_anchor_score is not None else text_score
            )
            if anchor.normalized_bbox is not None and page.width and page.height:
                best = 0.0
                for region in matches:
                    normalized = _normalize_bbox(region.bbox, page.width, page.height)
                    if normalized is None:
                        continue
                    best = max(best, max(
                        0.0,
                        1.0 - _bbox_distance(anchor.normalized_bbox, normalized) / max(self._options.geometry_tolerance, 0.01),
                    ))
                geometry_values.append(best)
        anchor_score = sum(anchor_values) / len(anchor_values) if anchor_values else None
        geometry_score = sum(geometry_values) / len(geometry_values) if geometry_values else None

        components: list[tuple[float, float]] = []
        if visual_score is not None:
            components.append((visual_score, self._options.page_visual_weight))
        if anchor_score is not None:
            components.append((anchor_score, self._options.page_anchor_weight))
        if geometry_score is not None:
            components.append((geometry_score, self._options.page_geometry_weight))
        if shape_score is not None:
            components.append((shape_score, self._options.page_shape_weight))
        evidence_weight = sum(weight for _, weight in components)
        score = sum(value * weight for value, weight in components) / max(evidence_weight, 1e-9) if components else 0.0

        # On raster/no-preview pages, visual fingerprint is the only content/layout
        # evidence. A low visual score must never be averaged away by page shape.
        raster_visual_failure = (
            targeted and not preview_regions and visual_score is not None
            and visual_score < self._options.preflight_visual_minimum
        )
        required_field_on_page = any(
            field.required and field.page_number == page_number for field in template.fields
        )
        required_anchor_visuals = [
            anchor for anchor in anchors if anchor.required and anchor.visual_hash is not None
        ]
        insufficient_raster_semantic_evidence = (
            targeted
            and template.fingerprint.page_count > 1
            and not preview_regions
            and required_field_on_page
            and not required_anchor_visuals
        )
        missing_page_visual_evidence = (
            targeted and not preview_regions and expected_visual is None
        )
        required_anchor_visual_failure = any(
            anchor.required and anchor.visual_hash is not None
            and anchor_visual_scores.get(anchor, 0.0) < self._options.preflight_anchor_visual_minimum
            for anchor in anchors
        )
        if not components:
            status = TemplateMatchStatus.REJECTED
        elif (
            raster_visual_failure
            or required_anchor_visual_failure
            or insufficient_raster_semantic_evidence
            or missing_page_visual_evidence
        ):
            status = (
                TemplateMatchStatus.REVIEW_REQUIRED
                if score >= self._options.page_match_review_threshold else TemplateMatchStatus.REJECTED
            )
        elif score >= self._options.page_match_accepted_threshold:
            status = TemplateMatchStatus.ACCEPTED
        elif score >= self._options.page_match_review_threshold:
            status = TemplateMatchStatus.REVIEW_REQUIRED
        else:
            status = TemplateMatchStatus.REJECTED
        drift = targeted and status != TemplateMatchStatus.ACCEPTED
        diagnostics: list[str] = []
        if raster_visual_failure:
            diagnostics.append("page_visual_drift")
        if required_anchor_visual_failure:
            diagnostics.append("page_anchor_visual_drift")
        if insufficient_raster_semantic_evidence:
            diagnostics.append("page_required_anchor_visual_evidence_missing")
        if missing_page_visual_evidence:
            diagnostics.append("page_visual_fingerprint_missing")
        if anchor_score is not None and anchor_score < 0.75:
            diagnostics.append("page_anchor_drift")
        if geometry_score is not None and geometry_score < 0.50:
            diagnostics.append("page_geometry_drift")
        return TemplatePageMatch(
            page_number, score, status, drift, visual_score, anchor_score, geometry_score, shape_score, targeted,
            tuple(diagnostics),
        )

    def fast_path_eligible(
        self, template: DocumentTemplate, template_table_ocr_available: bool = False,
    ) -> bool:
        """Allow field and template-aware repeating-table targets on the cheap path."""
        required_fields = [field for field in template.fields if field.required]
        required_tables = [table for table in template.tables if table.required]
        fields_ready: bool = all(
            field.anchors or field.normalized_value_bbox is not None for field in required_fields
        )
        tables_ready: bool = all(
            table.repeating_rows
            and table.normalized_body_bbox is not None
            and table.column_definitions
            and len(table.column_definitions) == len(table.columns)
            for table in required_tables
        )
        if required_tables and not template_table_ocr_available:
            return False
        return bool(required_fields or required_tables) and fields_ready and tables_ready

    def compose_extraction(
        self,
        extraction: ExtractionResult,
        match: TemplateMatchResult,
        verification: FieldVerificationResult | None,
        template_table_results: tuple[TemplateTableResult, ...] | None = None,
        blocked_pages: tuple[int, ...] = (),
    ) -> TemplateExtractionResult:
        if match.template is None or match.status == TemplateMatchStatus.REJECTED:
            return TemplateExtractionResult(
                extraction, match, (), (), False, 0.0, 0.0, True, match.drift_detected,
                tuple(dict.fromkeys(match.diagnostics + ("template_not_used",))),
            )
        template = match.template
        verified_map: dict[str, list[VerifiedField]] = {}
        if verification is not None:
            for item in verification.fields:
                verified_map.setdefault(item.field_name, []).append(item)
        output: list[TemplateFieldResult] = []
        for definition in template.fields:
            if definition.page_number in blocked_pages:
                output.append(TemplateFieldResult(
                    definition.name, None, definition.page_number, None, False,
                    "template_page_drift", "template_blocked_page", definition.normalized_value_bbox,
                    ("template_page_drift_requires_new_layout_review",),
                ))
                continue
            candidates = verified_map.get(definition.name, [])
            chosen = self._choose_verified(definition, candidates)
            if chosen is not None:
                value = chosen.suggested_value or chosen.original_value
                verified = chosen.status in {FieldVerificationStatus.VERIFIED_SOURCE, FieldVerificationStatus.VERIFIED_CONSENSUS}
                output.append(TemplateFieldResult(
                    definition.name, value, chosen.page_number, chosen.confidence, verified,
                    chosen.status.value, "field_verification", chosen.value_bbox, chosen.review_reasons,
                ))
                continue
            fallback = self._geometry_candidate(extraction, definition)
            output.append(fallback)
        supplied_tables: dict[str, TemplateTableResult] = {
            item.name: item for item in (template_table_results or ())
            if item.status not in {"template_geometry_unavailable", "provider_unavailable"}
        }
        table_results = tuple(
            TemplateTableResult(
                table.name, table.page_number, (), "template_page_drift", False, None,
                ("template_page_drift_requires_new_layout_review",),
            ) if table.page_number in blocked_pages else (
                supplied_tables.get(table.name) or self._table_candidate(extraction, table)
            )
            for table in template.tables
        )
        required_tables = [table for table in template.tables if table.required]
        found_required_tables = sum(
            1 for table in required_tables
            if any(
                result.name == table.name
                and bool(result.rows)
                and result.status not in {"page_not_found", "not_found", "crop_failed", "provider_unavailable", "template_geometry_unavailable", "template_table_incomplete"}
                for result in table_results
            )
        )
        table_ratio = found_required_tables / len(required_tables) if required_tables else 1.0
        required_tables_verified = all(
            any(result.name == table.name and result.verified for result in table_results)
            for table in required_tables
        ) if required_tables else True
        required = [field for field in template.fields if field.required]
        verified_required = sum(
            1 for field in required
            if any(item.name == field.name and item.verified for item in output)
        )
        ratio = verified_required / len(required) if required else 1.0
        has_required_targets: bool = bool(required or required_tables)
        semantic_complete = has_required_targets and ratio == 1.0 and table_ratio == 1.0 and required_tables_verified and match.status == TemplateMatchStatus.ACCEPTED and not match.drift_detected
        requires_review = (
            not semantic_complete
            or match.status != TemplateMatchStatus.ACCEPTED
            or match.drift_detected
            or any(not item.verified for item in output if any(f.name == item.name and f.required for f in required))
            or table_ratio < 1.0
            or not required_tables_verified
        )
        diagnostics = list(match.diagnostics)
        if match.drift_detected:
            diagnostics.append("template_drift_detected")
        if ratio < 1.0:
            diagnostics.append("template_required_fields_not_verified")
        if table_ratio < 1.0:
            diagnostics.append("template_required_tables_missing")
        elif not required_tables_verified:
            diagnostics.append("template_required_table_values_not_verified")
        return TemplateExtractionResult(
            extraction, match, tuple(output), table_results, semantic_complete, ratio, table_ratio, requires_review,
            match.drift_detected, tuple(dict.fromkeys(diagnostics)),
        )

    def verification_definitions(
        self, template: DocumentTemplate, page_numbers: tuple[int, ...] | None = None,
    ) -> tuple[FieldDefinition, ...]:
        allowed_pages = set(page_numbers) if page_numbers is not None else None
        return tuple(
            FieldDefinition(
                name=field.name,
                labels=field.anchors,
                pattern=field.pattern,
                minimum_confidence=field.minimum_confidence,
                template=field.template,
                page_number=field.page_number,
                normalized_value_bbox=field.normalized_value_bbox,
                normalized_label_bbox=field.normalized_label_bbox,
            )
            for field in template.fields
            if (field.anchors or field.normalized_value_bbox is not None)
            and (allowed_pages is None or field.page_number in allowed_pages)
        )

    def _bootstrap_proposal(
        self,
        extraction: ExtractionResult,
        required_fields: tuple[str, ...],
    ) -> TemplateGenerationProposal:
        fields: list[TemplateFieldDefinition] = []
        required = set(required_fields)
        for page in extraction.pages:
            for item in page.structured_fields:
                if not isinstance(item, StructuredFieldEvidence):
                    continue
                pattern = self._pattern_for(item.field_kind, item.value)
                anchors = self._anchors_for_structured_field(page, item)
                fields.append(TemplateFieldDefinition(
                    name=item.field_kind,
                    field_kind=item.field_kind,
                    required=(item.field_kind in required) if required else item.field_kind in {"invoice_number", "date", "total"},
                    page_number=page.number,
                    anchors=anchors,
                    pattern=pattern,
                    mode=TemplateFieldMode.LABEL_RELATIVE if anchors else TemplateFieldMode.REGION,
                    normalized_value_bbox=_normalize_bbox(item.bbox, page.width, page.height),
                    minimum_confidence=0.90 if item.field_kind in {"total", "tax", "subtotal", "invoice_number", "date"} else 0.85,
                ))
        unique: dict[str, TemplateFieldDefinition] = {}
        for field in fields:
            unique.setdefault(field.name, field)
        tables: list[TemplateTableDefinition] = []
        for page in extraction.pages:
            for index, region in enumerate(page.regions, start=1):
                if region.kind != RegionKind.TABLE or region.table is None:
                    continue
                header = tuple(cell or "" for cell in (region.table[0] if region.table else []) if (cell or "").strip())
                tables.append(TemplateTableDefinition(
                    name=f"table_{page.number}_{index}",
                    page_number=page.number,
                    normalized_bbox=_normalize_bbox(region.bbox, page.width, page.height),
                    header_anchors=header,
                    columns=header,
                    repeating_rows=True,
                ))
        return TemplateGenerationProposal(
            document_kind=_document_kind(extraction),
            fields=tuple(unique.values()),
            tables=tuple(tables),
            anchors=tuple(self._default_anchors(extraction)),
            diagnostics=("deterministic_template_may_need_ai_geometry_refinement",),
        )

    @staticmethod
    def _pattern_for(kind: str, value: str) -> str:
        if kind in {"total", "subtotal", "tax", "amount"}:
            return r"(?:\d{1,3}(?:[, ]\d{3})+|\d+)[.,]\d{2}"
        if kind == "date":
            return r"(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|\d{4}[./-]\d{1,2}[./-]\d{1,2})"
        if kind == "invoice_number":
            return r"[A-Za-z0-9][A-Za-z0-9./_-]{2,}"
        if kind in {"merchant_name", "address"}:
            return r".{3,}"
        escaped = re.escape(value.strip())
        return escaped if escaped else r".+"

    @staticmethod
    def _anchors_for_structured_field(page: PageResult, field: StructuredFieldEvidence) -> tuple[str, ...]:
        labels: dict[str, tuple[str, ...]] = {
            "total": ("TOTAL", "Grand Total", "الإجمالي", "الاجمالي"),
            "subtotal": ("Subtotal", "Sub Total", "المجموع الفرعي"),
            "tax": ("VAT", "Tax", "ضريبة", "الضريبة"),
            "invoice_number": ("Invoice Number", "Invoice No", "رقم الفاتورة"),
            "date": ("Date", "التاريخ"),
            "merchant_name": ("Seller Name", "Merchant Name", "اسم البائع", "اسم التاجر"),
            "address": ("Address", "العنوان"),
        }
        return labels.get(field.field_kind, ())

    @staticmethod
    def _default_anchors(extraction: ExtractionResult) -> list[TemplateAnchor]:
        anchors: list[TemplateAnchor] = []
        for page in extraction.pages:
            for region in page.regions[:12]:
                text = (region.text or "").strip().splitlines()
                if not text:
                    continue
                candidate = text[0].strip()
                if 2 <= len(candidate) <= 80:
                    anchors.append(TemplateAnchor(candidate, page.number, _normalize_bbox(region.bbox, page.width, page.height), False))
                    if len(anchors) >= 12:
                        return anchors
        return anchors

    def _fingerprint(
        self, extraction: ExtractionResult, anchors: tuple[TemplateAnchor, ...], document: DocumentInput | None = None
    ) -> TemplateLayoutFingerprint:
        effective_anchors = anchors or tuple(self._default_anchors(extraction))
        if document is not None:
            effective_anchors = with_anchor_visual_hashes(
                document, extraction.manifest.file_kind, effective_anchors
            )
        aspects: list[float | None] = []
        region_counts: list[int] = []
        table_counts: list[int] = []
        for page in extraction.pages:
            aspects.append((page.width / page.height) if page.width and page.height else None)
            region_counts.append(len(page.regions))
            table_counts.append(sum(1 for region in page.regions if region.kind == RegionKind.TABLE and region.table is not None))
        raw = "|".join(
            [str(len(extraction.pages))]
            + [f"{value:.4f}" if value is not None else "?" for value in aspects]
            + [str(value) for value in region_counts]
            + [_norm(anchor.text) for anchor in effective_anchors]
        )
        signature = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return TemplateLayoutFingerprint(
            len(extraction.pages), tuple(aspects), tuple(region_counts), tuple(table_counts), tuple(effective_anchors), signature,
            visual_hash(document, extraction.manifest.file_kind) if document is not None else None,
            page_visual_hashes(document, extraction.manifest.file_kind) if document is not None else (),
        )

    @staticmethod
    def _table_candidate(extraction: ExtractionResult, definition: TemplateTableDefinition) -> TemplateTableResult:
        if definition.page_number < 1 or definition.page_number > len(extraction.pages):
            return TemplateTableResult(definition.name, definition.page_number, (), "page_not_found", False, None, ("template_table_page_not_found",))
        page = extraction.pages[definition.page_number - 1]
        candidates = [region for region in page.regions if region.kind == RegionKind.TABLE and region.table is not None]
        if not candidates:
            return TemplateTableResult(definition.name, definition.page_number, (), "not_found", False, None, ("template_table_not_found",))
        def score(region: ExtractedRegion) -> tuple[int, float]:
            header = _norm(" ".join(cell or "" for cell in (region.table[0] if region.table else [])))
            header_hits = sum(1 for anchor in definition.header_anchors if _norm(anchor) in header)
            geometry = 1.0
            if definition.normalized_bbox is not None and page.width and page.height and region.bbox is not None:
                normalized = _normalize_bbox(region.bbox, page.width, page.height)
                geometry = 1.0 - min(1.0, _bbox_distance(definition.normalized_bbox, normalized)) if normalized else 0.0
            return (header_hits, geometry)
        chosen = max(candidates, key=score)
        rows = tuple(tuple(cell for cell in row) for row in (chosen.table or []))
        verified = chosen.method == ExtractionMethod.NATIVE and not chosen.requires_review
        return TemplateTableResult(
            definition.name, definition.page_number, rows,
            "verified_source" if verified else "structure_matched_review", verified, chosen.region_id,
            () if verified else ("template_table_values_require_independent_verification",),
        )

    @staticmethod
    def _choose_verified(definition: TemplateFieldDefinition, candidates: list[VerifiedField]) -> VerifiedField | None:
        if not candidates:
            return None
        same_page = [item for item in candidates if item.page_number == definition.page_number]
        pool = same_page or candidates
        rank = {
            FieldVerificationStatus.VERIFIED_CONSENSUS: 4,
            FieldVerificationStatus.VERIFIED_SOURCE: 3,
            FieldVerificationStatus.REVIEW_REQUIRED: 2,
            FieldVerificationStatus.VALUE_REGION_NOT_FOUND: 1,
            FieldVerificationStatus.LABEL_NOT_FOUND: 0,
            FieldVerificationStatus.PROVIDER_UNAVAILABLE: 0,
        }
        return max(pool, key=lambda item: (rank[item.status], item.confidence or -1.0))

    @staticmethod
    def _geometry_candidate(extraction: ExtractionResult, definition: TemplateFieldDefinition) -> TemplateFieldResult:
        if definition.page_number < 1 or definition.page_number > len(extraction.pages):
            return TemplateFieldResult(definition.name, None, None, None, False, "page_not_found", "template_geometry", None, ("template_field_page_not_found",))
        page = extraction.pages[definition.page_number - 1]
        target = definition.normalized_value_bbox
        if target is None or not page.width or not page.height:
            return TemplateFieldResult(definition.name, None, definition.page_number, None, False, "not_found", "template_geometry", None, ("template_field_not_verified",))
        candidates = []
        for region in page.regions:
            if not (region.text or "").strip() or region.bbox is None:
                continue
            normalized = _normalize_bbox(region.bbox, page.width, page.height)
            if normalized is None:
                continue
            candidates.append((_bbox_distance(target, normalized), region))
        if not candidates:
            return TemplateFieldResult(definition.name, None, definition.page_number, None, False, "not_found", "template_geometry", None, ("template_geometry_value_not_found",))
        _, region = min(candidates, key=lambda item: item[0])
        value = (region.text or "").strip()
        schema_valid = re.fullmatch(definition.pattern, value) is not None
        return TemplateFieldResult(
            definition.name, value or None, definition.page_number, region.confidence, False,
            "candidate_only" if schema_valid else "schema_invalid_candidate", "template_geometry",
            region.bbox, ("template_geometry_candidate_requires_verification",),
        )
