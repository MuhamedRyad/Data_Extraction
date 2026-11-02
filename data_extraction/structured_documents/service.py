"""Conservative semantic coverage for receipts, invoices and forms.

This feature never turns an OCR guess into an authoritative business fact.
Its main purpose is to stop raster OCR text coverage from being confused with
semantic completeness of structured financial/form documents.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..shared.models import ExtractedRegion, ExtractionMethod, PageResult, RegionKind
from .options import StructuredDocumentAnalysisOptions
from .contracts import (
    StructuredDocumentKind,
    StructuredFieldEvidence,
    StructuredPageAnalysis,
    StructuredSemanticStatus,
)

_DATE_RE = re.compile(
    r"(?<!\d)(?:20\d{2}|19\d{2})[./-](?:0?[1-9]|1[0-2])[./-](?:0?[1-9]|[12]\d|3[01])"
    r"|(?<!\d)(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])[./-](?:20\d{2}|19\d{2})(?!\d)"
)
_AMOUNT_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:[, ]\d{3})+|\d+)[.,]\d{2}(?!\d)")
_INVOICE_NO_RE = re.compile(
    r"(?:invoice\s*(?:no\.?|number|#)|receipt\s*(?:no\.?|number|#)|document\s*(?:no\.?|number|#)|doc\s*(?:no\.?|number|#)|bill\s*(?:no\.?|number|#)|رقم\s*الفاتور[ةه]|رقم\s*الإيصال|رقم\s*الايصال)\s*[:#-]?\s*([A-Z0-9][A-Z0-9./_-]{2,})",
    re.IGNORECASE,
)
_TOTAL_LABELS = (
    "grand total", "total amount", "amount due", "net total", "total", "الإجمالي", "الاجمالي", "المجموع",
)
_SUBTOTAL_LABELS = ("subtotal", "sub total", "المجموع الفرعي")
_TAX_LABELS = ("vat", "tax", "ضريبة", "الضريبة")
_INVOICE_HINTS = (
    "invoice", "tax invoice", "فاتورة", "الفاتورة", "seller", "buyer", "vat", "ضريبة",
)
_RECEIPT_HINTS = (
    "receipt", "cash", "change", "thank you", "tel", "phone", "store", "total",
    "إيصال", "ايصال", "نقد", "الباقي", "الإجمالي", "الاجمالي",
)
_FORM_HINTS = (
    "name:", "date:", "address:", "signature", "الاسم", "التاريخ", "العنوان", "التوقيع",
)
_ADDRESS_HINTS = (
    "street", "st.", "road", "rd.", "jalan", "jln", "taman", "lot", "no.", "address",
    "شارع", "طريق", "منطقة", "صندوق",
)
_MERCHANT_LABEL_LINES = (
    "seller's name", "seller name", "merchant name", "vendor name", "supplier name",
    "seller information", "buyer information", "issuance", "issue date",
    "اسم البائع", "اسم التاجر", "معلومات البائع", "معلومات المشتري",
)


def _is_generic_field_label(value: str) -> bool:
    lowered: str = value.casefold().strip(" :#-–—")
    if any(lowered == label.casefold() for label in _MERCHANT_LABEL_LINES):
        return True
    return lowered in {
        "invoice", "tax invoice", "simplified tax invoice", "receipt", "date",
        "total", "subtotal", "vat", "tax", "address", "name",
        "فاتورة", "فاتورة ضريبية", "إيصال", "ايصال", "التاريخ", "الإجمالي", "الاجمالي",
    }


def _plausible_merchant_line(value: str) -> bool:
    cleaned: str = value.strip()
    if len(cleaned) < 3 or _is_generic_field_label(cleaned):
        return False
    if _DATE_RE.search(_normalize_digits(cleaned)) or _AMOUNT_RE.search(_normalize_digits(cleaned)):
        return False
    alpha: int = sum(character.isalpha() for character in cleaned)
    if alpha < 3 or alpha / max(1, len(cleaned)) < 0.55:
        return False
    # OCR garbage such as `Ad>odige` should not be promoted to a business name.
    if any(character in cleaned for character in "<>[]{}|\\"):
        return False
    lowered: str = cleaned.casefold()
    if any(token in lowered for token in ("invoice", "receipt", "issuance", "seller information", "buyer information")):
        return False
    if any(hint in lowered for hint in _ADDRESS_HINTS):
        return False
    if re.match(r"^(?:no\.?|lot)\s*\d", lowered):
        return False
    return True


def _structured_suspicion(kind: StructuredDocumentKind, text: str, table_count: int) -> bool:
    invoice_hits: int = _contains_any(text, _INVOICE_HINTS)
    receipt_hits: int = _contains_any(text, _RECEIPT_HINTS)
    form_hits: int = _contains_any(text, _FORM_HINTS)
    amounts: int = len(_AMOUNT_RE.findall(_normalize_digits(text)))
    if kind == StructuredDocumentKind.INVOICE:
        return invoice_hits >= 1 and (amounts >= 1 or _INVOICE_NO_RE.search(text) is not None)
    if kind == StructuredDocumentKind.RECEIPT:
        return receipt_hits >= 1 and amounts >= 1
    if kind == StructuredDocumentKind.FORM:
        return form_hits >= 2 or (form_hits >= 1 and (text.count(":") >= 2 or table_count > 0))
    return False


def _normalize_digits(value: str) -> str:
    output: list[str] = []
    for character in value:
        if character.isdecimal():
            try:
                output.append(str(unicodedata.digit(character)))
            except (TypeError, ValueError):
                output.append(character)
        else:
            output.append(character)
    return "".join(output)


def _clean(value: str) -> str:
    return " ".join(_normalize_digits(value).replace("\u200f", " ").replace("\u200e", " ").split())


def _contains_any(value: str, hints: tuple[str, ...]) -> int:
    lowered: str = value.casefold()
    return sum(1 for hint in hints if hint.casefold() in lowered)


def _last_amount(line: str) -> str | None:
    matches: list[str] = _AMOUNT_RE.findall(_normalize_digits(line))
    if not matches:
        return None
    return matches[-1].replace(" ", "")


def _field_from_labeled_line(
    lines: list[tuple[str, ExtractedRegion]], labels: tuple[str, ...], kind: str,
    page_number: int, excluded_labels: tuple[str, ...] = (),
) -> StructuredFieldEvidence | None:
    for line, region in reversed(lines):
        lowered: str = line.casefold()
        if excluded_labels and any(label.casefold() in lowered for label in excluded_labels):
            continue
        if not any(label.casefold() in lowered for label in labels):
            continue
        amount: str | None = _last_amount(line)
        if amount is None:
            continue
        reasons: list[str] = ["single_observation_requires_verification"]
        if region.confidence is None or region.confidence < 0.90:
            reasons.append("field_source_confidence_below_high_assurance")
        verified: bool = region.method == ExtractionMethod.NATIVE
        return StructuredFieldEvidence(
            kind, amount, page_number, region.region_id, region.bbox,
            region.confidence, f"extraction:{region.method.value}", verified,
            () if verified else tuple(reasons),
        )
    return None


def looks_like_financial_document(text: str) -> bool:
    """Cheap conservative trigger for optional OCR geometry recovery."""
    cleaned: str = _clean(text)
    if not cleaned:
        return False
    invoice_hits: int = _contains_any(cleaned, _INVOICE_HINTS)
    receipt_hits: int = _contains_any(cleaned, _RECEIPT_HINTS)
    amounts: int = len(_AMOUNT_RE.findall(_normalize_digits(cleaned)))
    return invoice_hits >= 2 or (receipt_hits >= 2 and amounts >= 1)


def looks_like_structured_document(text: str) -> bool:
    """Conservative trigger for selective AI on flat structured raster documents."""
    cleaned: str = _clean(text)
    if not cleaned:
        return False
    if looks_like_financial_document(cleaned):
        return True
    form_hits: int = _contains_any(cleaned, _FORM_HINTS)
    return form_hits >= 2 or (form_hits >= 1 and cleaned.count(":") >= 3)


@dataclass(frozen=True, slots=True)
class StructuredDocumentAnalysisService:
    """Deterministic classifier + candidate extractor.

    `semantic_complete=True` is intentionally hard to reach for raster OCR. A
    single OCR observation can establish candidate coverage, not authoritative
    correctness of sensitive values such as totals or invoice numbers.
    """

    options: StructuredDocumentAnalysisOptions = StructuredDocumentAnalysisOptions()

    def analyze_page(self, page: PageResult) -> StructuredPageAnalysis:
        text_regions: list[ExtractedRegion] = [
            region for region in page.regions
            if (region.text or "").strip()
        ]
        table_regions: list[ExtractedRegion] = [
            region for region in page.regions
            if region.kind == RegionKind.TABLE and region.table is not None
        ]
        lines: list[tuple[str, ExtractedRegion]] = []
        for region in text_regions:
            for raw_line in (region.text or "").splitlines():
                line: str = _clean(raw_line)
                if line:
                    lines.append((line, region))
        # Candidate tables are review-only, but their row relationships are
        # better evidence than a flattened OCR block. Feed rows into field
        # candidate discovery without making them authoritative.
        for region in table_regions:
            for row in region.table or []:
                row_text: str = _clean(" ".join(cell or "" for cell in row))
                if row_text:
                    lines.append((row_text, region))
        joined: str = "\n".join(line for line, _ in lines)
        kind, confidence = self._classify(joined, len(lines), len(table_regions))
        if kind == StructuredDocumentKind.GENERAL:
            return StructuredPageAnalysis(
                StructuredDocumentKind.GENERAL, confidence,
                StructuredSemanticStatus.NOT_APPLICABLE, None, 1.0, 1.0, 0,
                (), (), (), False, (),
            )
        below_acceptance_threshold: bool = confidence < self.options.minimum_kind_confidence
        if below_acceptance_threshold and (
            confidence < self.options.minimum_suspicion_confidence
            or not _structured_suspicion(kind, joined, len(table_regions))
        ):
            return StructuredPageAnalysis(
                StructuredDocumentKind.GENERAL, confidence,
                StructuredSemanticStatus.NOT_APPLICABLE, None, 1.0, 1.0, 0,
                (), (), (), False, (),
            )

        fields: list[StructuredFieldEvidence] = []
        if kind in (StructuredDocumentKind.RECEIPT, StructuredDocumentKind.INVOICE):
            fields.extend(self._financial_fields(page.number, lines))
            required: tuple[str, ...] = (
                ("invoice_number", "date", "total")
                if kind == StructuredDocumentKind.INVOICE else ("merchant_name", "address", "date", "total")
            )
        else:
            required = ("key_value_structure",)
            # A form with only a flat OCR block has text coverage but not
            # question/answer relationship coverage.
            if table_regions:
                fields.append(StructuredFieldEvidence(
                    "key_value_structure", "structured_table_present", page.number,
                    table_regions[0].region_id, table_regions[0].bbox,
                    table_regions[0].confidence, "structured_table", False,
                    ("form_relationships_require_verification",),
                ))

        found: tuple[str, ...] = tuple(dict.fromkeys(field.field_kind for field in fields))
        coverage: float = (
            sum(1 for name in required if name in found) / len(required) if required else 1.0
        )
        verified_coverage: float = (
            sum(1 for name in required if any(field.field_kind == name and field.verified for field in fields)) / len(required)
            if required else 1.0
        )
        all_verified: bool = bool(required) and verified_coverage == 1.0
        # Candidate presence is not enough for semantic completeness. For raster
        # OCR, a one-character amount/date error can be business-significant.
        semantic_gap_count: int = sum(
            1 for name in required
            if not any(field.field_kind == name and field.verified for field in fields)
        )
        semantic_complete: bool = all_verified
        if semantic_complete:
            status = StructuredSemanticStatus.COMPLETE
        elif coverage > 0:
            status = StructuredSemanticStatus.PARTIAL
        else:
            status = StructuredSemanticStatus.UNKNOWN
        reasons: list[str] = []
        if below_acceptance_threshold:
            reasons.append("structured_kind_suspected_below_acceptance_threshold")
        if coverage < 1.0:
            reasons.append("structured_required_fields_missing")
        if fields and not all_verified:
            reasons.append("structured_fields_not_independently_verified")
        if kind in (StructuredDocumentKind.RECEIPT, StructuredDocumentKind.INVOICE) and not table_regions:
            reasons.append("financial_document_flat_ocr_without_structure")
        if any(
            region.method == ExtractionMethod.OCR and region.confidence is not None and region.confidence >= 0.90
            for region in text_regions
        ) and coverage < 1.0:
            reasons.append("high_ocr_confidence_does_not_prove_field_coverage")
        return StructuredPageAnalysis(
            kind, confidence, status, semantic_complete, coverage, verified_coverage, semantic_gap_count,
            required, found, tuple(fields), True, tuple(dict.fromkeys(reasons)),
        )

    @staticmethod
    def _classify(text: str, line_count: int, table_count: int) -> tuple[StructuredDocumentKind, float]:
        if not text:
            return StructuredDocumentKind.GENERAL, 0.0
        invoice_hits: int = _contains_any(text, _INVOICE_HINTS)
        receipt_hits: int = _contains_any(text, _RECEIPT_HINTS)
        form_hits: int = _contains_any(text, _FORM_HINTS)
        amounts: int = len(_AMOUNT_RE.findall(_normalize_digits(text)))
        dates: int = len(_DATE_RE.findall(_normalize_digits(text)))
        invoice_score: float = min(1.0, invoice_hits * 0.22 + min(amounts, 4) * 0.10 + (0.12 if dates else 0.0))
        receipt_score: float = min(1.0, receipt_hits * 0.16 + min(amounts, 4) * 0.11 + (0.10 if dates else 0.0) + (0.05 if line_count >= 5 else 0.0))
        form_score: float = min(1.0, form_hits * 0.18 + (0.20 if text.count(":") >= 3 else 0.0) + (0.08 if table_count else 0.0))
        best = max(
            (invoice_score, StructuredDocumentKind.INVOICE),
            (receipt_score, StructuredDocumentKind.RECEIPT),
            (form_score, StructuredDocumentKind.FORM),
            key=lambda item: item[0],
        )
        return best[1], best[0]

    @staticmethod
    def _financial_fields(page_number: int, lines: list[tuple[str, ExtractedRegion]]) -> list[StructuredFieldEvidence]:
        output: list[StructuredFieldEvidence] = []

        merchant_index: int | None = None
        merchant_value: str | None = None
        merchant_region: ExtractedRegion | None = None
        merchant_reason: str = "merchant_name_heuristic_candidate_review"

        # Prefer an explicit seller/merchant label followed by a plausible value.
        for index, (line, _) in enumerate(lines[:12]):
            lowered: str = line.casefold().strip(" :#-–—")
            if not any(lowered == label.casefold() for label in _MERCHANT_LABEL_LINES):
                continue
            for next_index in range(index + 1, min(len(lines), index + 4)):
                candidate_line, candidate_region = lines[next_index]
                if _plausible_merchant_line(candidate_line):
                    merchant_index = next_index
                    merchant_value = candidate_line
                    merchant_region = candidate_region
                    merchant_reason = "merchant_name_label_adjacent_candidate_review"
                    break
            if merchant_value is not None:
                break

        # Receipts often have no label. Only promote a clean header-like line;
        # do not emit the first alphabetic OCR fragment blindly.
        if merchant_value is None:
            for index, (line, region) in enumerate(lines[:8]):
                if not _plausible_merchant_line(line):
                    continue
                lowered: str = line.casefold()
                if any(label.casefold() in lowered for label in _TOTAL_LABELS + _SUBTOTAL_LABELS + _TAX_LABELS):
                    continue
                merchant_index = index
                merchant_value = line
                merchant_region = region
                break

        if merchant_value is not None and merchant_region is not None:
            output.append(StructuredFieldEvidence(
                "merchant_name", merchant_value, page_number, merchant_region.region_id, merchant_region.bbox,
                merchant_region.confidence, f"extraction:{merchant_region.method.value}", False,
                (merchant_reason,),
            ))

        # Address candidates require actual address evidence. Arbitrary lines after
        # the merchant name are no longer concatenated into an address.
        if merchant_index is not None:
            address_parts: list[str] = []
            address_region: ExtractedRegion | None = None
            for line, region in lines[merchant_index + 1:merchant_index + 6]:
                lowered: str = line.casefold()
                if _DATE_RE.search(_normalize_digits(line)) or _AMOUNT_RE.search(_normalize_digits(line)):
                    break
                if _INVOICE_NO_RE.search(line):
                    continue
                if _is_generic_field_label(line):
                    continue
                has_address_hint: bool = any(hint in lowered for hint in _ADDRESS_HINTS)
                has_street_number: bool = any(character.isdigit() for character in line) and len(line.split()) >= 2
                if has_address_hint or has_street_number:
                    address_parts.append(line)
                    address_region = address_region or region
                elif address_parts:
                    break
            if address_parts and address_region is not None:
                output.append(StructuredFieldEvidence(
                    "address", " | ".join(address_parts), page_number,
                    address_region.region_id, address_region.bbox, address_region.confidence,
                    f"extraction:{address_region.method.value}", False,
                    ("address_geometry_candidate_review",),
                ))
        total = _field_from_labeled_line(
            lines, _TOTAL_LABELS, "total", page_number, excluded_labels=_SUBTOTAL_LABELS
        )
        if total is not None:
            output.append(total)
        subtotal = _field_from_labeled_line(lines, _SUBTOTAL_LABELS, "subtotal", page_number)
        if subtotal is not None:
            output.append(subtotal)
        tax = _field_from_labeled_line(lines, _TAX_LABELS, "tax", page_number)
        if tax is not None:
            output.append(tax)
        for line, region in lines:
            invoice_match = _INVOICE_NO_RE.search(line)
            if invoice_match:
                output.append(StructuredFieldEvidence(
                    "invoice_number", invoice_match.group(1), page_number,
                    region.region_id, region.bbox, region.confidence,
                    f"extraction:{region.method.value}", region.method == ExtractionMethod.NATIVE,
                    () if region.method == ExtractionMethod.NATIVE else ("single_observation_requires_verification",),
                ))
                break
        for line, region in lines:
            match = _DATE_RE.search(_normalize_digits(line))
            if match:
                output.append(StructuredFieldEvidence(
                    "date", match.group(0), page_number,
                    region.region_id, region.bbox, region.confidence,
                    f"extraction:{region.method.value}", region.method == ExtractionMethod.NATIVE,
                    () if region.method == ExtractionMethod.NATIVE else ("single_observation_requires_verification",),
                ))
                break
        return output
