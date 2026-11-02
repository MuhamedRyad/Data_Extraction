from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class ErrorType(StrEnum):
    VALIDATION = "validation"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    PROVIDER = "provider"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    SECURITY = "security"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class Error:
    type: ErrorType
    code: str
    description: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("Error code is required")
        if not self.description.strip():
            raise ValueError("Error description is required")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class ExtractionError(Exception):
    """Backward-compatible exception wrapper around the canonical Error contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        error_type: ErrorType = ErrorType.VALIDATION,
        metadata: Mapping[str, object] | None = None,
    ) -> None:
        self.error: Error = Error(
            type=error_type,
            code=code,
            description=message,
            metadata=metadata or {},
        )
        self.code: str = code
        self.message: str = message
        super().__init__(message)


def error_from_exception(exception: BaseException) -> Error:
    """Map expected feature exceptions to a safe Error without leaking input content."""

    if isinstance(exception, ExtractionError):
        return exception.error
    if isinstance(exception, TimeoutError):
        return Error(ErrorType.TIMEOUT, "OPERATION_TIMEOUT", "The operation timed out")
    return Error(ErrorType.INTERNAL, "UNEXPECTED_ERROR", "An unexpected internal error occurred")
