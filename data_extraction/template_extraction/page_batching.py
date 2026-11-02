from __future__ import annotations

from .contracts import TemplatePageBatch
from .errors import TemplateExtractionError


def plan_page_batches(
    page_numbers: tuple[int, ...],
    *,
    max_pages_per_batch: int,
    max_batches: int,
    reason: str,
) -> tuple[TemplatePageBatch, ...]:
    """Group sorted page numbers into bounded consecutive batches.

    Non-consecutive page groups are never silently bridged. This keeps the
    visual context local while allowing tables/sections spanning neighboring
    pages to remain in one request when they fit the configured budget.
    """
    if max_pages_per_batch < 1 or max_batches < 1:
        raise TemplateExtractionError(
            "TEMPLATE_BATCH_OPTIONS_INVALID", "AI page batch limits must be positive"
        )
    pages = tuple(sorted(set(value for value in page_numbers if value >= 1)))
    if not pages:
        return ()
    groups: list[list[int]] = []
    current: list[int] = []
    for page in pages:
        if current and page != current[-1] + 1:
            groups.append(current)
            current = []
        current.append(page)
    if current:
        groups.append(current)

    chunks: list[tuple[int, ...]] = []
    for group in groups:
        for start in range(0, len(group), max_pages_per_batch):
            chunks.append(tuple(group[start:start + max_pages_per_batch]))
    if len(chunks) > max_batches:
        raise TemplateExtractionError(
            "TEMPLATE_AI_BATCH_LIMIT_EXCEEDED",
            f"Document requires {len(chunks)} AI page batches; configured maximum is {max_batches}",
        )
    return tuple(
        TemplatePageBatch(index, chunk, reason)
        for index, chunk in enumerate(chunks, start=1)
    )
