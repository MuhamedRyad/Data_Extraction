from __future__ import annotations

from ..shared.contracts import StrictContract
from ..shared.models import ExtractionResult


class ExtractDocumentResponse(StrictContract):
    extraction: ExtractionResult
