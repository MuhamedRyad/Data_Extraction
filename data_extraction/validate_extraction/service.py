"""Final review gate; never claims accuracy from a mere successful parse."""
from __future__ import annotations

from collections.abc import Sequence
from ..shared.models import PageResult


class ValidateExtractionService:
    @staticmethod
    def is_ready_for_publication(pages: Sequence[PageResult]) -> bool:
        """Keep the v0.6 readiness rule unchanged during structure migration."""
        return all(not page.requires_review for page in pages) and all(
            bool(page.regions) for page in pages
        )
