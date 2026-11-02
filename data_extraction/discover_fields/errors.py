from __future__ import annotations

from ..shared.errors import Error, ErrorType

INVALID_FIELD_DISCOVERY_REQUEST = Error(
    ErrorType.VALIDATION,
    "INVALID_FIELD_DISCOVERY_REQUEST",
    "The field-discovery request is invalid",
)
