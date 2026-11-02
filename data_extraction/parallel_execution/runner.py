from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


def run_bounded_ordered(
    items: Iterable[T],
    operation: Callable[[T], R],
    *,
    max_workers: int,
    enabled: bool,
) -> tuple[R, ...]:
    """Execute independent work concurrently while preserving input order.

    Exceptions are fail-closed: the first observed worker failure is re-raised
    and outstanding futures are cancelled where possible. Output ordering is
    always the original item order, never completion order.
    """

    values: tuple[T, ...] = tuple(items)
    if not values:
        return ()
    if not enabled or max_workers <= 1 or len(values) == 1:
        return tuple(operation(item) for item in values)
    if max_workers < 1:
        raise ValueError("max_workers must be >= 1")

    executor: ThreadPoolExecutor = ThreadPoolExecutor(
        max_workers=min(max_workers, len(values)),
        thread_name_prefix="data-extraction",
    )
    futures: tuple[Future[R], ...] = tuple(executor.submit(operation, item) for item in values)
    try:
        return tuple(future.result() for future in futures)
    except BaseException:
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
