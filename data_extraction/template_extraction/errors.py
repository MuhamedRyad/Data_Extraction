from __future__ import annotations

from ..shared.errors import ErrorType, ExtractionError


class TemplateExtractionError(ExtractionError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, error_type=ErrorType.VALIDATION)
