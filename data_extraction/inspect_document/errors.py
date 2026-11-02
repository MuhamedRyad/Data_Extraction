from __future__ import annotations

from ..shared.errors import Error, ErrorType

INVALID_INSPECT_DOCUMENT_REQUEST = Error(
    ErrorType.VALIDATION,
    "INVALID_INSPECT_DOCUMENT_REQUEST",
    "The inspect-document request is invalid",
)
