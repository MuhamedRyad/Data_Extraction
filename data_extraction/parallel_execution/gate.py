from __future__ import annotations

from threading import BoundedSemaphore
from typing import Callable, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class BoundedCallGate:
    """Thread-safe resource gate for provider calls.

    The gate limits simultaneous calls without changing provider contracts.
    It is intentionally synchronous because OCR/Vision providers in this
    package expose synchronous APIs and often wrap native/subprocess work.
    """

    def __init__(self, max_concurrency: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._max_concurrency: int = max_concurrency
        self._semaphore: BoundedSemaphore = BoundedSemaphore(max_concurrency)

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def call(self, operation: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._semaphore:
            return operation(*args, **kwargs)
