"""Fail-closed multipass OCR with independently inspectable attempts.

The old adapter discarded non-selected text. This wrapper never calls a
conflict a successful recognition. Compare() retains *all* provider outputs;
recognize() only returns content when nonempty attempts agree exactly.
"""
from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from ...shared.models import ProviderTextResult
from ..contracts import OCRProvider


@dataclass(frozen=True, slots=True)
class OCRAttempt:
    provider_index: int
    result: ProviderTextResult | None
    failure_type: str | None = None


@dataclass(frozen=True, slots=True)
class OCRComparison:
    attempts: tuple[OCRAttempt, ...]
    consensus: ProviderTextResult | None
    conflict: bool


@dataclass(frozen=True, slots=True)
class MultiPassOCRProvider:
    providers: tuple[OCRProvider, ...]
    comparison_sink: Callable[[OCRComparison], None] | None = None

    def __post_init__(self) -> None:
        if len(self.providers) < 2:
            raise ValueError("At least two OCR providers/profiles are required")

    def compare(self, image_bytes: bytes, mime_type: str) -> OCRComparison:
        attempts: list[OCRAttempt] = []
        for index, provider in enumerate(self.providers):
            try:
                result: ProviderTextResult = provider.recognize(image_bytes, mime_type)
                if not isinstance(result, ProviderTextResult):
                    raise TypeError("Provider did not return ProviderTextResult")
                attempts.append(OCRAttempt(index, result))
            except Exception as error:
                # Keep error type only; never leak provider secrets or raw exception strings.
                attempts.append(OCRAttempt(index, None, type(error).__name__))
        valid: tuple[ProviderTextResult, ...] = tuple(
            entry.result for entry in attempts
            if entry.result is not None and (entry.result.text.strip() or entry.result.structured_table is not None)
        )
        if not valid:
            return OCRComparison(tuple(attempts), None, False)
        signatures: set[tuple[str, str]] = {
            (result.text, repr(result.structured_table)) for result in valid
        }
        conflict: bool = len(signatures) > 1
        fully_agreed: bool = not conflict and len(valid) == len(self.providers)
        if not fully_agreed:
            return OCRComparison(tuple(attempts), None, conflict)
        agreement: ProviderTextResult = valid[0]
        confidence: float | None = (
            min(result.confidence for result in valid if result.confidence is not None)
            if all(result.confidence is not None for result in valid) else None
        )
        return OCRComparison(tuple(attempts), ProviderTextResult(
            agreement.text, confidence, agreement.structured_table,
            provider_name="multi_pass_consensus", model_id=agreement.model_id,
        ), False)

    def recognize(self, image_bytes: bytes, mime_type: str) -> ProviderTextResult:
        compared: OCRComparison = self.compare(image_bytes, mime_type)
        if self.comparison_sink is not None:
            # Optional caller-controlled audit persistence; no hidden mutable last-result.
            self.comparison_sink(compared)
        if compared.consensus is not None:
            return compared.consensus
        # Conflict is explicitly handled in dispatch; never select a partial pass.
        return ProviderTextResult(
            text="", confidence=None,
            provider_name="multi_pass_conflict" if compared.conflict else "multi_pass_no_consensus",
        )
