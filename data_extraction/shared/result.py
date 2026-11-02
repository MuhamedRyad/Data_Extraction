from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar, cast

from .errors import Error

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Result(Generic[T]):
    """Typed success/failure contract used at feature/application boundaries."""

    value: T | None = None
    error: Error | None = None

    def __post_init__(self) -> None:
        has_value: bool = self.value is not None
        has_error: bool = self.error is not None
        if has_value == has_error:
            raise ValueError("Result must contain exactly one of value or error")

    @property
    def is_success(self) -> bool:
        return self.error is None

    @property
    def is_failure(self) -> bool:
        return self.error is not None

    @classmethod
    def Success(cls, value: T) -> Result[T]:
        return cls(value=value)

    @classmethod
    def Failure(cls, error: Error) -> Result[T]:
        return cls(error=error)

    def unwrap(self) -> T:
        if self.error is not None:
            raise RuntimeError(f"Cannot unwrap failure result: {self.error.code}")
        return cast(T, self.value)

    def unwrap_error(self) -> Error:
        if self.error is None:
            raise RuntimeError("Cannot unwrap error from a success result")
        return self.error
