from __future__ import annotations

from ..shared.models import DocumentInput, ExtractionResult, RegionKind
from .contracts import DocumentTemplate, TemplateAnchor, TemplateMatchStatus
from .helpers import bbox_distance, document_kind, norm, normalize_bbox, page_text
from .options import TemplateExtractionOptions
from .response import TemplateMatchEvidence, TemplateMatchResult
from .visual_fingerprints import page_visual_hashes, visual_similarity


class TemplateMatchingService:
    def __init__(self, options: TemplateExtractionOptions) -> None:
        self._options: TemplateExtractionOptions = options

    def score_template(
        self,
        extraction: ExtractionResult,
        template: DocumentTemplate,
        document: DocumentInput | None = None,
    ) -> TemplateMatchResult:
        page_count_score: float = (
            1.0
            if len(extraction.pages) == template.fingerprint.page_count
            else max(
                0.0,
                1.0
                - abs(len(extraction.pages) - template.fingerprint.page_count)
                / max(1, template.fingerprint.page_count),
            )
        )
        actual_kind: str = document_kind(extraction)
        kind_score: float = 1.0 if actual_kind == template.document_kind else (0.55 if actual_kind == "general" else 0.0)

        shape_scores: list[float] = []
        for index, expected in enumerate(template.fingerprint.page_aspect_ratios):
            if index >= len(extraction.pages) or expected is None:
                continue
            page = extraction.pages[index]
            if not page.width or not page.height:
                continue
            actual: float = page.width / page.height
            shape_scores.append(max(0.0, 1.0 - abs(actual - expected) / max(expected, 0.01)))
        shape_score: float = sum(shape_scores) / len(shape_scores) if shape_scores else 0.5

        page_texts: list[str] = [norm(text) for text in page_text(extraction)]
        anchor_hits: list[float] = []
        geometry_hits: list[float] = []
        for anchor in template.fingerprint.anchors:
            if anchor.page_number < 1 or anchor.page_number > len(page_texts):
                anchor_hits.append(0.0)
                geometry_hits.append(0.0)
                continue
            found: bool = norm(anchor.text) in page_texts[anchor.page_number - 1]
            anchor_hits.append(1.0 if found else (0.0 if anchor.required else 0.5))
            if anchor.normalized_bbox is None:
                geometry_hits.append(0.5 if found else 0.0)
                continue
            geometry_hits.append(self._anchor_geometry_score(extraction, anchor))

        anchor_score: float = sum(anchor_hits) / len(anchor_hits) if anchor_hits else 0.5
        geometry_score: float = sum(geometry_hits) / len(geometry_hits) if geometry_hits else 0.5

        table_scores: list[float] = []
        for index, expected in enumerate(template.fingerprint.page_table_counts):
            if index >= len(extraction.pages):
                table_scores.append(0.0)
                continue
            actual: int = sum(
                1
                for region in extraction.pages[index].regions
                if region.kind == RegionKind.TABLE and region.table is not None
            )
            table_scores.append(
                1.0
                if actual == expected
                else max(0.0, 1.0 - abs(actual - expected) / max(1, expected or 1))
            )
        table_score: float = sum(table_scores) / len(table_scores) if table_scores else 0.5

        current_visual_hashes: tuple[str | None, ...] = (
            page_visual_hashes(document, extraction.manifest.file_kind)
            if document is not None
            else ()
        )
        expected_visual_hashes: tuple[str | None, ...] = template.fingerprint.page_visual_hashes
        visual_scores: list[float] = []
        visual_page_scores: dict[int, float] = {}
        for index in range(min(len(extraction.pages), template.fingerprint.page_count)):
            expected_hash: str | None = (
                expected_visual_hashes[index]
                if index < len(expected_visual_hashes)
                else (template.fingerprint.visual_hash if index == 0 else None)
            )
            current_hash: str | None = (
                current_visual_hashes[index]
                if index < len(current_visual_hashes)
                else None
            )
            value: float | None = visual_similarity(expected_hash, current_hash)
            if value is not None:
                visual_scores.append(value)
                visual_page_scores[index + 1] = value
        visual_score: float | None = (
            sum(visual_scores) / len(visual_scores) if visual_scores else None
        )

        components: list[tuple[float, float]] = [
            (anchor_score, self._options.anchor_weight),
            (geometry_score, self._options.geometry_weight),
            (shape_score, self._options.page_shape_weight),
            (kind_score, self._options.document_kind_weight),
            (table_score, self._options.table_structure_weight),
            (page_count_score, self._options.page_count_weight),
        ]
        if visual_score is not None:
            components.append((visual_score, self._options.visual_fingerprint_weight))
        total_weight: float = sum(weight for _, weight in components)
        weighted: float = sum(score * weight for score, weight in components) / max(total_weight, 1e-9)
        score: float = max(0.0, min(1.0, weighted))

        if score >= self._options.accepted_match_threshold:
            status = TemplateMatchStatus.ACCEPTED
        elif score >= self._options.review_match_threshold:
            status = TemplateMatchStatus.REVIEW_REQUIRED
        else:
            status = TemplateMatchStatus.REJECTED

        target_pages: set[int] = {
            field.page_number for field in template.fields
        } | {
            table.page_number for table in template.tables
        }
        visual_page_drift: bool = any(
            page in target_pages and value < self._options.preflight_visual_minimum
            for page, value in visual_page_scores.items()
        )
        drift: bool = (
            (score < self._options.drift_threshold and anchor_score > 0.0)
            or visual_page_drift
            or (anchor_score >= 0.75 and geometry_score < 0.50)
        )
        if drift and status == TemplateMatchStatus.ACCEPTED:
            status = TemplateMatchStatus.REVIEW_REQUIRED

        evidence: tuple[TemplateMatchEvidence, ...] = (
            TemplateMatchEvidence(
                "anchors",
                anchor_score,
                f"{sum(1 for value in anchor_hits if value >= 1.0)}/{len(anchor_hits)} anchors matched",
            ),
            TemplateMatchEvidence("geometry", geometry_score, "normalized anchor geometry similarity"),
            TemplateMatchEvidence("page_shape", shape_score, "page aspect-ratio similarity"),
            TemplateMatchEvidence(
                "document_kind",
                kind_score,
                f"expected={template.document_kind};actual={actual_kind}",
            ),
            TemplateMatchEvidence("table_structure", table_score, "table-count similarity"),
            TemplateMatchEvidence(
                "page_count",
                page_count_score,
                f"expected={template.fingerprint.page_count};actual={len(extraction.pages)}",
            ),
            TemplateMatchEvidence(
                "visual_fingerprint",
                visual_score if visual_score is not None else 0.0,
                "per-page average-hash similarity" if visual_score is not None else "visual fingerprint unavailable",
            ),
        )
        diagnostics: list[str] = []
        if status == TemplateMatchStatus.REVIEW_REQUIRED:
            diagnostics.append("template_match_requires_review")
        if status == TemplateMatchStatus.REJECTED:
            diagnostics.append("template_match_rejected")
        if drift:
            diagnostics.append("possible_template_drift")
        return TemplateMatchResult(
            template,
            score,
            status,
            drift,
            evidence,
            tuple(diagnostics),
        )

    def _anchor_geometry_score(
        self,
        extraction: ExtractionResult,
        anchor: TemplateAnchor,
    ) -> float:
        page = extraction.pages[anchor.page_number - 1]
        if anchor.normalized_bbox is None or not page.width or not page.height:
            return 0.5
        target = anchor.normalized_bbox
        best: float = 0.0
        for region in page.regions:
            text: str = norm(region.text or "")
            if norm(anchor.text) not in text:
                continue
            normalized = normalize_bbox(region.bbox, page.width, page.height)
            if normalized is None:
                best = max(best, 0.5)
                continue
            distance: float = bbox_distance(target, normalized)
            score: float = max(
                0.0,
                1.0 - distance / max(self._options.geometry_tolerance, 0.01),
            )
            best = max(best, score)
        return best
