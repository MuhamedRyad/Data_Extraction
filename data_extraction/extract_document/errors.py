from __future__ import annotations

from ..shared.errors import Error, ErrorType

INVALID_EXTRACT_DOCUMENT_REQUEST = Error(
    ErrorType.VALIDATION,
    "INVALID_EXTRACT_DOCUMENT_REQUEST",
    "The extract-document request is invalid",
)
