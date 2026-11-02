from __future__ import annotations

import base64
import io
import json
import os
from dataclasses import asdict, dataclass, replace
from typing import Any

import fitz
from PIL import Image

from ...shared.models import BoundingBox, DocumentInput
from ...template_extraction.contracts import (
    TemplateAnchor,
    TemplateFieldDefinition,
    TemplateFieldMode,
    TemplateGenerationContext,
    TemplateGenerationProposal,
    TemplateRefinementContext,
    TemplateReviewContext,
    TemplateReviewIssue,
    TemplateReviewResult,
    TemplateReviewVerdict,
    DocumentTemplate,
    TemplateTableColumnDefinition,
    TemplateTableDefinition,
)
from ...template_extraction.errors import TemplateExtractionError
from ...template_extraction.page_batching import plan_page_batches
from ...parallel_execution import run_bounded_ordered


@dataclass(frozen=True, slots=True)
class OpenAIResponsesTemplateOptions:
    model: str = "gpt-6.1-sol"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 90.0
    max_visual_pages: int = 3
    # v0.22: ``max_visual_pages`` is retained for compatibility with callers
    # using the low-level visual helper. Generation/review/refinement use the
    # bounded batch settings below and can therefore cover the whole document.
    max_pages_per_batch: int = 4
    max_ai_batches_per_document: int = 25
    # v0.23: top-level service enables this when bounded parallel execution is on.
    parallel_ai_batches_enabled: bool = False
    max_parallel_ai_batches: int = 2
    max_visual_dimension: int = 1800
    pdf_render_dpi: int = 144
    image_detail: str = "high"
    max_page_text_chars: int = 8000
    max_output_tokens: int = 6000
    store_response: bool = False


class OpenAIResponsesTemplateProvider:
    """Optional OpenAI Responses API adapter for first-seen document layouts.

    This adapter is intentionally outside the template core. It proposes a
    TemplateGenerationProposal from the original visual document plus extracted
    text context. Core validation and explicit human approval remain mandatory.
    """

    def __init__(
        self,
        api_key: str | None = None,
        options: OpenAIResponsesTemplateOptions | None = None,
        http_client: Any | None = None,
    ) -> None:
        self._api_key: str | None = api_key or os.environ.get("OPENAI_API_KEY")
        self._options: OpenAIResponsesTemplateOptions = options or OpenAIResponsesTemplateOptions()
        self._http_client: Any | None = http_client

    def generate_template(
        self,
        document: DocumentInput,
        context: TemplateGenerationContext,
    ) -> TemplateGenerationProposal:
        api_key: str | None = self._api_key
        if not api_key:
            raise TemplateExtractionError(
                "TEMPLATE_AI_CREDENTIALS_MISSING",
                "OPENAI_API_KEY or an explicit api_key is required for OpenAI template generation",
            )
        page_numbers = self._document_page_numbers(document, context.page_count)
        batches = plan_page_batches(
            page_numbers,
            max_pages_per_batch=self._options.max_pages_per_batch,
            max_batches=self._options.max_ai_batches_per_document,
            reason="template_generation",
        )
        proposals: tuple[TemplateGenerationProposal, ...] = run_bounded_ordered(
            batches,
            lambda batch: self._generate_template_batch(
                document, context, batch.page_numbers, api_key
            ),
            max_workers=self._options.max_parallel_ai_batches,
            enabled=self._options.parallel_ai_batches_enabled,
        )
        if not proposals:
            raise TemplateExtractionError(
                "TEMPLATE_AI_NO_VISUAL_PAGES", "No visual pages were available for template generation"
            )
        return self._merge_generation_batches(proposals)

    def _generate_template_batch(
        self,
        document: DocumentInput,
        context: TemplateGenerationContext,
        page_numbers: tuple[int, ...],
        api_key: str,
    ) -> TemplateGenerationProposal:
        content: list[dict[str, Any]] = [{
            "type": "input_text",
            "text": self._build_user_prompt(context) +
                    "\n\nV0.22 PAGE BATCH: Analyze ONLY visual pages " +
                    ", ".join(str(value) for value in page_numbers) +
                    ". Return fields, tables, and anchors only for these original page numbers. "
                    "Do not renumber pages inside the batch.",
        }]
        visual_urls = self._visual_data_urls(document, page_numbers)
        for page_number, visual_url in zip(page_numbers, visual_urls, strict=False):
            content.append({"type": "input_text", "text": f"VISUAL PAGE {page_number}"})
            content.append({
                "type": "input_image",
                "image_url": visual_url,
                "detail": self._options.image_detail,
            })
        request_payload: dict[str, Any] = {
            "model": self._options.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": self._system_prompt()}],
                },
                {"role": "user", "content": content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "document_template_candidate",
                    "strict": True,
                    "schema": self._proposal_schema(),
                }
            },
            "max_output_tokens": self._options.max_output_tokens,
            "store": self._options.store_response,
        }
        response_payload = self._post_response(request_payload, api_key)
        proposal = self._proposal_from_response(response_payload, "generation")
        invalid_pages = [
            field.page_number for field in proposal.fields
            if field.page_number <= context.page_count and field.page_number not in page_numbers
        ] + [
            table.page_number for table in proposal.tables
            if table.page_number <= context.page_count and table.page_number not in page_numbers
        ] + [
            anchor.page_number for anchor in proposal.anchors
            if anchor.page_number <= context.page_count and anchor.page_number not in page_numbers
        ]
        if invalid_pages:
            raise TemplateExtractionError(
                "TEMPLATE_AI_BATCH_PAGE_ESCAPE",
                "AI template batch returned geometry for a page outside the supplied batch",
            )
        return replace(
            proposal,
            diagnostics=tuple(dict.fromkeys(
                proposal.diagnostics +
                ("ai_page_batch:" + ",".join(str(value) for value in page_numbers),)
            )),
        )

    def _proposal_from_response(
        self, response_payload: dict[str, Any], operation: str,
    ) -> TemplateGenerationProposal:
        output_text = self._extract_output_text(response_payload)
        try:
            proposal_payload: Any = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise TemplateExtractionError(
                "TEMPLATE_AI_INVALID_JSON", f"AI template {operation} returned invalid JSON"
            ) from exc
        if not isinstance(proposal_payload, dict):
            raise TemplateExtractionError(
                "TEMPLATE_AI_INVALID_PAYLOAD", f"AI template {operation} must return a JSON object"
            )
        proposal = self._proposal_from_payload(proposal_payload)
        response_id: Any = response_payload.get("id")
        provenance: tuple[str, ...] = (f"openai_model:{self._options.model}",)
        if isinstance(response_id, str) and response_id.strip():
            provenance += (f"openai_response_id:{response_id}",)
        return replace(
            proposal, diagnostics=tuple(dict.fromkeys(proposal.diagnostics + provenance))
        )

    @staticmethod
    def _merge_generation_batches(
        proposals: tuple[TemplateGenerationProposal, ...],
    ) -> TemplateGenerationProposal:
        document_kinds = [item.document_kind for item in proposals if item.document_kind and item.document_kind != "general"]
        document_kind = document_kinds[0] if document_kinds else proposals[0].document_kind
        if any(value != document_kind for value in document_kinds):
            raise TemplateExtractionError(
                "TEMPLATE_AI_BATCH_DOCUMENT_KIND_CONFLICT",
                "AI template page batches disagreed on document kind",
            )
        fields: dict[str, TemplateFieldDefinition] = {}
        tables: dict[str, TemplateTableDefinition] = {}
        anchors: list[TemplateAnchor] = []
        diagnostics: list[str] = ["ai_multi_page_batches_merged"]
        for proposal in proposals:
            for field in proposal.fields:
                existing = fields.get(field.name)
                if existing is not None and existing != field:
                    raise TemplateExtractionError(
                        "TEMPLATE_AI_BATCH_FIELD_CONFLICT",
                        f"AI page batches returned conflicting definitions for field '{field.name}'",
                    )
                fields[field.name] = field
            for table in proposal.tables:
                existing = tables.get(table.name)
                if existing is not None and existing != table:
                    raise TemplateExtractionError(
                        "TEMPLATE_AI_BATCH_TABLE_CONFLICT",
                        f"AI page batches returned conflicting definitions for table '{table.name}'",
                    )
                tables[table.name] = table
            anchors.extend(proposal.anchors)
            diagnostics.extend(proposal.diagnostics)
        return TemplateGenerationProposal(
            document_kind=document_kind,
            fields=tuple(fields.values()),
            tables=tuple(tables.values()),
            anchors=tuple(dict.fromkeys(anchors)),
            diagnostics=tuple(dict.fromkeys(diagnostics)),
        )

    def refine_template(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context: TemplateRefinementContext,
    ) -> TemplateGenerationProposal:
        """Refine field geometry in bounded page batches only where feedback exists."""
        api_key: str | None = self._api_key
        if not api_key:
            raise TemplateExtractionError(
                "TEMPLATE_AI_CREDENTIALS_MISSING",
                "OPENAI_API_KEY or an explicit api_key is required for OpenAI template refinement",
            )
        feedback_pages = tuple(sorted({item.page_number for item in context.feedback if item.page_number >= 1}))
        if not feedback_pages:
            return replace(
                proposal, diagnostics=tuple(dict.fromkeys(proposal.diagnostics + ("ai_refinement_no_feedback_pages",)))
            )
        batches = plan_page_batches(
            feedback_pages,
            max_pages_per_batch=self._options.max_pages_per_batch,
            max_batches=self._options.max_ai_batches_per_document,
            reason="template_refinement",
        )
        fields_by_name: dict[str, TemplateFieldDefinition] = {
            field.name: field for field in proposal.fields
        }
        diagnostics: list[str] = list(proposal.diagnostics)
        work: list[tuple[tuple[int, ...], TemplateGenerationProposal, TemplateRefinementContext, tuple[TemplateFieldDefinition, ...]]] = []
        for batch in batches:
            page_set: set[int] = set(batch.page_numbers)
            sub_fields: tuple[TemplateFieldDefinition, ...] = tuple(
                field for field in proposal.fields if field.page_number in page_set
            )
            if not sub_fields:
                continue
            sub_feedback = tuple(item for item in context.feedback if item.page_number in page_set)
            subproposal: TemplateGenerationProposal = replace(proposal, fields=sub_fields, tables=())
            subcontext: TemplateRefinementContext = replace(context, feedback=sub_feedback)
            work.append((batch.page_numbers, subproposal, subcontext, sub_fields))

        def refine_work(
            item: tuple[tuple[int, ...], TemplateGenerationProposal, TemplateRefinementContext, tuple[TemplateFieldDefinition, ...]]
        ) -> tuple[TemplateGenerationProposal, tuple[TemplateFieldDefinition, ...]]:
            page_numbers, subproposal, subcontext, sub_fields = item
            refined: TemplateGenerationProposal = self._refine_template_batch(
                document, subproposal, subcontext, page_numbers, api_key
            )
            return refined, sub_fields

        refined_batches = run_bounded_ordered(
            work,
            refine_work,
            max_workers=self._options.max_parallel_ai_batches,
            enabled=self._options.parallel_ai_batches_enabled,
        )
        for refined, sub_fields in refined_batches:
            if {field.name for field in refined.fields} != {field.name for field in sub_fields}:
                raise TemplateExtractionError(
                    "TEMPLATE_AI_BATCH_FIELD_CONFLICT",
                    "AI refinement batch changed the field set",
                )
            for field in refined.fields:
                fields_by_name[field.name] = field
            diagnostics.extend(refined.diagnostics)
        return replace(
            proposal,
            fields=tuple(fields_by_name[field.name] for field in proposal.fields),
            diagnostics=tuple(dict.fromkeys(diagnostics + ["ai_refinement_batches_merged"])),
        )

    def _refine_template_batch(
        self,
        document: DocumentInput,
        proposal: TemplateGenerationProposal,
        context: TemplateRefinementContext,
        page_numbers: tuple[int, ...],
        api_key: str,
    ) -> TemplateGenerationProposal:
        proposal_json = json.dumps(asdict(proposal), ensure_ascii=False, default=str)
        feedback_json = json.dumps(
            [asdict(item) for item in context.feedback], ensure_ascii=False, default=str
        )
        content: list[dict[str, Any]] = [{
            "type": "input_text",
            "text": self._build_refinement_prompt(context, proposal_json, feedback_json) +
                    "\n\nV0.22 PAGE BATCH: Refine only original pages " +
                    ", ".join(str(value) for value in page_numbers) + ".",
        }]
        visual_urls = self._visual_data_urls(document, page_numbers)
        for page_number, visual_url in zip(page_numbers, visual_urls, strict=False):
            content.append({"type": "input_text", "text": f"VISUAL PAGE {page_number}"})
            content.append({
                "type": "input_image", "image_url": visual_url, "detail": self._options.image_detail,
            })
        request_payload: dict[str, Any] = {
            "model": self._options.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": self._refinement_system_prompt()}],
                },
                {"role": "user", "content": content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "document_template_geometry_refinement",
                    "strict": True,
                    "schema": self._proposal_schema(),
                }
            },
            "max_output_tokens": self._options.max_output_tokens,
            "store": self._options.store_response,
        }
        response_payload = self._post_response(request_payload, api_key)
        refined = self._proposal_from_response(response_payload, "refinement")
        return replace(
            refined,
            diagnostics=tuple(dict.fromkeys(
                refined.diagnostics +
                ("ai_geometry_refinement_proposal", "ai_refinement_page_batch:" + ",".join(str(v) for v in page_numbers))
            )),
        )

    def review_template(
        self,
        document: DocumentInput,
        candidate: DocumentTemplate,
        context: TemplateReviewContext,
    ) -> TemplateReviewResult:
        """Independently falsify semantic mappings in bounded page batches."""
        api_key: str | None = self._api_key
        if not api_key:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_AI_CREDENTIALS_MISSING",
                "OPENAI_API_KEY or an explicit api_key is required for AI template review",
            )
        target_pages = {field.page_number for field in candidate.fields}
        target_pages.update(table.page_number for table in candidate.tables)
        target_pages.update(anchor.page_number for anchor in candidate.fingerprint.anchors if anchor.required)
        if not target_pages:
            target_pages.update(range(1, candidate.fingerprint.page_count + 1))
        batches = plan_page_batches(
            tuple(sorted(target_pages)),
            max_pages_per_batch=self._options.max_pages_per_batch,
            max_batches=self._options.max_ai_batches_per_document,
            reason="template_review",
        )
        reviews: tuple[TemplateReviewResult, ...] = run_bounded_ordered(
            batches,
            lambda batch: self._review_template_batch(
                document, candidate, context, batch.page_numbers, api_key
            ),
            max_workers=self._options.max_parallel_ai_batches,
            enabled=self._options.parallel_ai_batches_enabled,
        )
        if not reviews:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_AI_NO_VISUAL_PAGES", "No visual pages were available for template review"
            )
        rank = {
            TemplateReviewVerdict.PASS: 0,
            TemplateReviewVerdict.REVIEW_REQUIRED: 1,
            TemplateReviewVerdict.REJECT: 2,
        }
        verdict = max((item.verdict for item in reviews), key=lambda item: rank[item])
        confidence = min(item.confidence for item in reviews)
        issues = tuple(issue for item in reviews for issue in item.issues)
        diagnostics = tuple(dict.fromkeys(
            ("ai_review_batches_merged",) + tuple(
                value for item in reviews for value in item.diagnostics
            )
        ))
        return TemplateReviewResult(
            verdict=verdict,
            confidence=confidence,
            issues=issues,
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name=type(self).__name__,
            diagnostics=diagnostics,
        )

    def _review_template_batch(
        self,
        document: DocumentInput,
        candidate: DocumentTemplate,
        context: TemplateReviewContext,
        page_numbers: tuple[int, ...],
        api_key: str,
    ) -> TemplateReviewResult:
        page_set = set(page_numbers)
        candidate_subset = replace(
            candidate,
            fields=tuple(field for field in candidate.fields if field.page_number in page_set),
            tables=tuple(table for table in candidate.tables if table.page_number in page_set),
            fingerprint=replace(
                candidate.fingerprint,
                anchors=tuple(anchor for anchor in candidate.fingerprint.anchors if anchor.page_number in page_set),
            ),
        )
        candidate_json = json.dumps(asdict(candidate_subset), ensure_ascii=False, default=str)
        text_context = "\n\n".join(
            f"--- PAGE {page_number} EXTRACTED TEXT CONTEXT ---\n"
            f"{context.page_text[page_number - 1][: self._options.max_page_text_chars] if page_number - 1 < len(context.page_text) else ''}"
            for page_number in page_numbers
        )
        content: list[dict[str, Any]] = [{
            "type": "input_text",
            "text": self._build_review_prompt(context, candidate_json, text_context) +
                    "\n\nV0.22 PAGE BATCH: Review only original pages " +
                    ", ".join(str(value) for value in page_numbers) + ".",
        }]
        visual_urls = self._visual_data_urls(document, page_numbers)
        for page_number, visual_url in zip(page_numbers, visual_urls, strict=False):
            content.append({"type": "input_text", "text": f"VISUAL PAGE {page_number}"})
            content.append({
                "type": "input_image", "image_url": visual_url, "detail": self._options.image_detail,
            })
        request_payload: dict[str, Any] = {
            "model": self._options.model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": self._review_system_prompt()}],
                },
                {"role": "user", "content": content},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "document_template_semantic_review",
                    "strict": True,
                    "schema": self._review_schema(),
                }
            },
            "max_output_tokens": min(self._options.max_output_tokens, 4000),
            "store": self._options.store_response,
        }
        response_payload = self._post_response(request_payload, api_key)
        output_text = self._extract_output_text(response_payload)
        try:
            payload: Any = json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_AI_INVALID_JSON", "AI template reviewer returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_AI_INVALID_PAYLOAD", "AI template reviewer must return a JSON object"
            )
        try:
            verdict = TemplateReviewVerdict(str(payload["verdict"]))
            confidence = float(payload["confidence"])
            raw_issues = payload["issues"]
            if not isinstance(raw_issues, list):
                raise TypeError("issues must be an array")
            issues = tuple(
                TemplateReviewIssue(
                    scope=str(item["scope"]),
                    name=str(item["name"]) if item.get("name") is not None else None,
                    code=str(item["code"]),
                    reason=str(item["reason"]),
                    confidence=float(item["confidence"]),
                )
                for item in raw_issues
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TemplateExtractionError(
                "TEMPLATE_REVIEW_AI_INVALID_PAYLOAD", "AI template reviewer returned an invalid review contract"
            ) from exc
        response_id: Any = response_payload.get("id")
        diagnostics: tuple[str, ...] = (
            f"openai_review_model:{self._options.model}",
            "ai_review_page_batch:" + ",".join(str(value) for value in page_numbers),
        )
        if isinstance(response_id, str) and response_id.strip():
            diagnostics += (f"openai_review_response_id:{response_id}",)
        return TemplateReviewResult(
            verdict=verdict,
            confidence=confidence,
            issues=issues,
            reviewed_candidate_hash=context.candidate_hash,
            reviewer_name=type(self).__name__,
            diagnostics=diagnostics,
        )

    @staticmethod
    def _review_system_prompt() -> str:
        return (
            "You are an independent adversarial reviewer of a document template proposed by another AI. "
            "Do not trust the generator. Your job is to falsify semantic mappings against the supplied visual. "
            "Verify that every field name matches its visible label, that each value bbox points to the value for "
            "that exact label, and especially that same-type values such as Subtotal, VAT, and TOTAL are not swapped. "
            "Verify table header/body/column geometry and that summary rows are excluded from repeating line items. "
            "Do not edit the template and do not extract new business truth. Return PASS only when no semantic mapping "
            "problem is visible; REVIEW_REQUIRED when uncertain; REJECT when a concrete wrong mapping is visible."
        )

    @staticmethod
    def _build_review_prompt(context: TemplateReviewContext, candidate_json: str, text_context: str) -> str:
        return (
            f"Template family: {context.family}\n"
            f"Candidate hash: {context.candidate_hash}\n"
            "Review the complete candidate against the original visual document. Check every field and table. "
            "A schema-valid numeric crop is NOT enough: confirm the crop belongs to the named semantic label. "
            "List only concrete or material issues.\n\n"
            f"CANDIDATE TEMPLATE:\n{candidate_json}\n\n"
            f"EXTRACTED TEXT CONTEXT (supporting evidence only):\n{text_context}"
        )

    @staticmethod
    def _review_schema() -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "verdict": {"type": "string", "enum": [item.value for item in TemplateReviewVerdict]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "issues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "scope": {"type": "string", "enum": ["template", "field", "table"]},
                            "name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                            "code": {"type": "string"},
                            "reason": {"type": "string"},
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        },
                        "required": ["scope", "name", "code", "reason", "confidence"],
                    },
                },
            },
            "required": ["verdict", "confidence", "issues"],
        }

    def _build_refinement_prompt(
        self, context: TemplateRefinementContext, proposal_json: str, feedback_json: str,
    ) -> str:
        generation = context.generation
        return (
            f"Template family: {generation.family}\n"
            f"Refinement attempt: {context.attempt}/{context.max_attempts}\n"
            "The core already generated and crop-tested this template. Some field crops failed to produce "
            "a schema-valid value. Refine ONLY normalized_value_bbox and normalized_label_bbox for the "
            "fields listed in feedback. Preserve document_kind, field names, field kinds, required flags, "
            "page numbers, anchors, regex patterns, modes, templates, confidence thresholds, tables, and "
            "stable anchor identity exactly. Do not extract or rewrite transaction values. Use the visual "
            "document to tighten/shift the value boxes so they fully contain the variable value but avoid "
            "neighboring labels or punctuation. Return the complete template proposal, not a patch.\n\n"
            f"CURRENT TEMPLATE:\n{proposal_json}\n\n"
            f"CORE CROP/SCHEMA FEEDBACK:\n{feedback_json}"
        )

    @staticmethod
    def _refinement_system_prompt() -> str:
        return (
            "You are refining document-template geometry after deterministic crop validation. "
            "Change geometry only. Never alter semantics, regex, required fields, anchors, tables, or values. "
            "A wrong reusable crop is worse than leaving the geometry unchanged."
        )

    def _post_response(self, request_payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        client: Any = self._http_client
        close_client: bool = False
        if client is None:
            try:
                import httpx
            except ImportError as exc:
                raise TemplateExtractionError(
                    "TEMPLATE_AI_HTTP_DEPENDENCY_MISSING",
                    "Install the 'openai' optional dependency to use OpenAI template generation",
                ) from exc
            client = httpx.Client(timeout=self._options.timeout_seconds)
            close_client = True
        try:
            response: Any = client.post(
                f"{self._options.base_url.rstrip('/')}/responses",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=request_payload,
            )
            response.raise_for_status()
            payload: Any = response.json()
        except TemplateExtractionError:
            raise
        except Exception as exc:
            raise TemplateExtractionError(
                "TEMPLATE_AI_REQUEST_FAILED", "OpenAI Responses template-generation request failed"
            ) from exc
        finally:
            if close_client:
                client.close()
        if not isinstance(payload, dict):
            raise TemplateExtractionError(
                "TEMPLATE_AI_INVALID_RESPONSE", "OpenAI Responses API returned a non-object payload"
            )
        status: Any = payload.get("status")
        if status not in (None, "completed"):
            raise TemplateExtractionError(
                "TEMPLATE_AI_INCOMPLETE_RESPONSE", f"OpenAI response status was {status!r}"
            )
        return payload

    @staticmethod
    def _extract_output_text(payload: dict[str, Any]) -> str:
        direct: Any = payload.get("output_text")
        if isinstance(direct, str) and direct.strip():
            return direct
        output: Any = payload.get("output", [])
        if not isinstance(output, list):
            output = []
        texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            content: Any = item.get("content", [])
            if not isinstance(content, list):
                continue
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "refusal":
                    raise TemplateExtractionError(
                        "TEMPLATE_AI_REFUSED", "AI provider refused template generation"
                    )
                text: Any = part.get("text")
                if part.get("type") == "output_text" and isinstance(text, str):
                    texts.append(text)
        joined: str = "".join(texts).strip()
        if not joined:
            raise TemplateExtractionError(
                "TEMPLATE_AI_EMPTY_RESPONSE", "AI template provider returned no structured output text"
            )
        return joined

    def _document_page_numbers(
        self, document: DocumentInput, fallback_page_count: int,
    ) -> tuple[int, ...]:
        if document.content.startswith(b"%PDF-"):
            try:
                with fitz.open(stream=document.content, filetype="pdf") as pdf:
                    return tuple(range(1, len(pdf) + 1))
            except Exception:
                return tuple(range(1, max(1, fallback_page_count) + 1))
        return (1,)

    def _visual_data_urls(
        self, document: DocumentInput, page_numbers: tuple[int, ...] | None = None,
    ) -> tuple[str, ...]:
        content: bytes = document.content
        if content.startswith(b"%PDF-"):
            return self._pdf_data_urls(content, page_numbers)
        try:
            with Image.open(io.BytesIO(content)) as image:
                normalized: bytes = self._normalize_image(image)
        except Exception:
            return ()
        return (self._to_data_url(normalized, "image/jpeg"),)

    def _pdf_data_urls(
        self, content: bytes, page_numbers: tuple[int, ...] | None = None,
    ) -> tuple[str, ...]:
        urls: list[str] = []
        try:
            with fitz.open(stream=content, filetype="pdf") as pdf:
                if page_numbers is None:
                    selected = tuple(range(1, min(len(pdf), self._options.max_visual_pages) + 1))
                else:
                    selected = tuple(
                        value for value in page_numbers if 1 <= value <= len(pdf)
                    )
                for page_number in selected:
                    page_index = page_number - 1
                    page: fitz.Page = pdf[page_index]
                    scale: float = self._options.pdf_render_dpi / 72.0
                    pix: fitz.Pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                    image: Image.Image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
                    normalized: bytes = self._normalize_image(image)
                    urls.append(self._to_data_url(normalized, "image/jpeg"))
        except Exception as exc:
            raise TemplateExtractionError(
                "TEMPLATE_AI_PDF_RENDER_FAILED", "Could not render PDF pages for AI template generation"
            ) from exc
        return tuple(urls)

    def _normalize_image(self, image: Image.Image) -> bytes:
        normalized: Image.Image = image.convert("RGB")
        max_dimension: int = self._options.max_visual_dimension
        if max(normalized.size) > max_dimension:
            normalized.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
        buffer: io.BytesIO = io.BytesIO()
        normalized.save(buffer, format="JPEG", quality=88, optimize=True)
        return buffer.getvalue()

    @staticmethod
    def _to_data_url(content: bytes, mime_type: str) -> str:
        encoded: str = base64.b64encode(content).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _build_user_prompt(self, context: TemplateGenerationContext) -> str:
        required: str = ", ".join(context.required_fields) if context.required_fields else "none explicitly required"
        text_parts: list[str] = []
        for index, page_text in enumerate(context.page_text, start=1):
            clipped: str = page_text[: self._options.max_page_text_chars]
            text_parts.append(f"--- PAGE {index} EXTRACTED TEXT CONTEXT ---\n{clipped}")
        page_context: str = "\n\n".join(text_parts)
        return (
            f"Template family: {context.family}\n"
            f"Current coarse document kind: {context.document_kind}\n"
            f"Page count: {context.page_count}\n"
            f"Caller-required fields: {required}\n\n"
            "Create a reusable template for FUTURE documents with the same visual layout. "
            "Use normalized coordinates in [0,1]. Variable document values must NOT become anchors. "
            "Prefer stable labels, headings, logos, and table headers as anchors. "
            "For each field, place normalized_value_bbox tightly around the variable value region, not the label. "
            "Do not map two required fields to the same value region. "
            "For repeating line-item tables, describe the full table region plus a normalized_body_bbox containing ONLY repeating data rows (exclude header and Subtotal/VAT/TOTAL summary rows). "
            "For every semantic column provide its left/right normalized x range relative to the BODY CROP, a generic value_kind such as text/integer/decimal/identifier, and whether the cell is required. "
            "Provide stop_anchors for summary/footer labels that terminate repeating rows, such as Subtotal or TOTAL when present. "
            "Regex patterns should validate the field shape, not copy the current value literally. "
            "If a requested field is not visibly locatable, still include it only if you can provide a safe reusable location; "
            "otherwise the candidate should fail core validation rather than inventing coordinates.\n\n"
            f"{page_context}"
        )

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are a document-template engineer. Analyze the supplied invoice/receipt/form visuals and return ONLY "
            "the structured template candidate required by the JSON schema. Your job is to describe reusable layout, "
            "not to extract transaction values. Distinguish labels from values (for example TOTAL vs Subtotal, "
            "Seller's Name label vs seller value). For line-item tables, separate the header, repeating-row body, and summary/footer; summary values are NOT line items. "
            "Use page numbers starting at 1. Page bboxes are normalized [x0,y0,x1,y1]; table column x ranges are normalized to the repeating-row body crop. "
            "Be conservative: a wrong reusable region is worse than a review-required template."
        )

    @staticmethod
    def _bbox_schema() -> dict[str, Any]:
        return {
            "anyOf": [
                {
                    "type": "array",
                    "items": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "minItems": 4,
                    "maxItems": 4,
                },
                {"type": "null"},
            ]
        }

    @classmethod
    def _proposal_schema(cls) -> dict[str, Any]:
        bbox: dict[str, Any] = cls._bbox_schema()
        field_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "field_kind": {"type": "string"},
                "required": {"type": "boolean"},
                "page_number": {"type": "integer", "minimum": 1},
                "anchors": {"type": "array", "items": {"type": "string"}},
                "pattern": {"type": "string"},
                "mode": {"type": "string", "enum": [item.value for item in TemplateFieldMode]},
                "normalized_value_bbox": bbox,
                "normalized_label_bbox": bbox,
                "template": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "minimum_confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            },
            "required": [
                "name", "field_kind", "required", "page_number", "anchors", "pattern", "mode",
                "normalized_value_bbox", "normalized_label_bbox", "template", "minimum_confidence",
            ],
        }
        table_column_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "normalized_x0": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "normalized_x1": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "value_kind": {"type": "string"},
                "required": {"type": "boolean"},
            },
            "required": ["name", "normalized_x0", "normalized_x1", "value_kind", "required"],
        }
        table_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string"},
                "page_number": {"type": "integer", "minimum": 1},
                "normalized_bbox": bbox,
                "header_anchors": {"type": "array", "items": {"type": "string"}},
                "columns": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "repeating_rows": {"type": "boolean"},
                "required": {"type": "boolean"},
                "normalized_header_bbox": bbox,
                "normalized_body_bbox": bbox,
                "column_definitions": {"type": "array", "items": table_column_schema},
                "stop_anchors": {"type": "array", "items": {"type": "string"}},
                "minimum_rows": {"type": "integer", "minimum": 1},
            },
            "required": [
                "name", "page_number", "normalized_bbox", "header_anchors", "columns", "repeating_rows", "required",
                "normalized_header_bbox", "normalized_body_bbox", "column_definitions", "stop_anchors", "minimum_rows"
            ],
        }
        anchor_schema: dict[str, Any] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "text": {"type": "string"},
                "page_number": {"type": "integer", "minimum": 1},
                "normalized_bbox": bbox,
                "required": {"type": "boolean"},
            },
            "required": ["text", "page_number", "normalized_bbox", "required"],
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "document_kind": {"type": "string"},
                "fields": {"type": "array", "items": field_schema},
                "tables": {"type": "array", "items": table_schema},
                "anchors": {"type": "array", "items": anchor_schema},
                "diagnostics": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["document_kind", "fields", "tables", "anchors", "diagnostics"],
        }

    @staticmethod
    def _bbox(value: Any) -> BoundingBox | None:
        if value is None:
            return None
        if not isinstance(value, list) or len(value) != 4:
            raise TemplateExtractionError("TEMPLATE_AI_INVALID_BBOX", "AI template bbox must contain four numbers")
        try:
            return tuple(float(item) for item in value)  # type: ignore[return-value]
        except (TypeError, ValueError) as exc:
            raise TemplateExtractionError("TEMPLATE_AI_INVALID_BBOX", "AI template bbox contains non-numeric values") from exc

    @classmethod
    def _proposal_from_payload(cls, payload: dict[str, Any]) -> TemplateGenerationProposal:
        try:
            raw_fields: Any = payload["fields"]
            raw_tables: Any = payload["tables"]
            raw_anchors: Any = payload["anchors"]
            raw_diagnostics: Any = payload["diagnostics"]
            if not all(isinstance(item, list) for item in (raw_fields, raw_tables, raw_anchors, raw_diagnostics)):
                raise TypeError("proposal collections must be arrays")
            fields: tuple[TemplateFieldDefinition, ...] = tuple(
                TemplateFieldDefinition(
                    name=str(item["name"]),
                    field_kind=str(item["field_kind"]),
                    required=bool(item["required"]),
                    page_number=int(item["page_number"]),
                    anchors=tuple(str(value) for value in item["anchors"]),
                    pattern=str(item["pattern"]),
                    mode=TemplateFieldMode(str(item["mode"])),
                    normalized_value_bbox=cls._bbox(item.get("normalized_value_bbox")),
                    normalized_label_bbox=cls._bbox(item.get("normalized_label_bbox")),
                    template=str(item["template"]) if item.get("template") is not None else None,
                    minimum_confidence=float(item["minimum_confidence"]),
                )
                for item in raw_fields
            )
            tables: tuple[TemplateTableDefinition, ...] = tuple(
                TemplateTableDefinition(
                    name=str(item["name"]),
                    page_number=int(item["page_number"]),
                    normalized_bbox=cls._bbox(item.get("normalized_bbox")),
                    header_anchors=tuple(str(value) for value in item["header_anchors"]),
                    columns=tuple(str(value) for value in item["columns"]),
                    repeating_rows=bool(item["repeating_rows"]),
                    required=bool(item["required"]),
                    normalized_header_bbox=cls._bbox(item.get("normalized_header_bbox")),
                    normalized_body_bbox=cls._bbox(item.get("normalized_body_bbox")),
                    column_definitions=tuple(
                        TemplateTableColumnDefinition(
                            name=str(column["name"]),
                            normalized_x0=float(column["normalized_x0"]),
                            normalized_x1=float(column["normalized_x1"]),
                            value_kind=str(column.get("value_kind") or "text"),
                            required=bool(column.get("required", True)),
                        )
                        for column in item.get("column_definitions", [])
                    ),
                    stop_anchors=tuple(str(value) for value in item.get("stop_anchors", [])),
                    minimum_rows=int(item.get("minimum_rows", 1)),
                )
                for item in raw_tables
            )
            anchors: tuple[TemplateAnchor, ...] = tuple(
                TemplateAnchor(
                    text=str(item["text"]),
                    page_number=int(item["page_number"]),
                    normalized_bbox=cls._bbox(item.get("normalized_bbox")),
                    required=bool(item["required"]),
                )
                for item in raw_anchors
            )
            diagnostics: tuple[str, ...] = tuple(str(item) for item in raw_diagnostics)
            document_kind: str = str(payload["document_kind"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TemplateExtractionError(
                "TEMPLATE_AI_INVALID_PAYLOAD", "AI template payload does not match the expected contract"
            ) from exc
        return TemplateGenerationProposal(
            document_kind=document_kind,
            fields=fields,
            tables=tables,
            anchors=anchors,
            diagnostics=tuple(dict.fromkeys(("ai_visual_template_proposal",) + diagnostics)),
        )


class OpenAIResponsesTemplateReviewer(OpenAIResponsesTemplateProvider):
    """Separate adapter identity for independent semantic review calls.

    It intentionally uses a separate instance/request from the generator even
    when both use the same configured model.
    """

    pass
