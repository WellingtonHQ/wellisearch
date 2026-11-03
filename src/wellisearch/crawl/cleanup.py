"""Keep resource cleanup owned until it completes, even under repeated cancellation."""
from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TypeVar

T = TypeVar("T")
_pending_operations: set[asyncio.Task] = set()


async def complete_cleanup(operation: Coroutine[object, object, T]) -> T:
    """Finish bounded cleanup before propagating cancellation to its caller.

    Shield alone is insufficient: its caller can leave before cleanup finishes,
    allowing a replacement browser to race the old browser's process reaper.
    Operations passed here must enforce their own I/O timeouts.
    """
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    return task.result()


async def bounded_operation(
    operation: Coroutine[object, object, T],
    timeout: float,
) -> T:
    """Enforce a hard deadline even when the operation's cancellation can hang.

    wait_for() waits for cancellation acknowledgement; a wedged browser driver
    can withhold that forever. Keep a strong reference to the cancelled task
    while the caller reaps its browser/driver to unblock the pending operation.
    """
    task = asyncio.create_task(operation)
    _pending_operations.add(task)
    task.add_done_callback(_operation_done)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            raise TimeoutError("browser operation exceeded its deadline")
        return task.result()
    finally:
        if not task.done():
            task.cancel()


def _operation_done(task: asyncio.Task) -> None:
    """Release a finished operation and consume errors after its caller leaves."""
    _pending_operations.discard(task)
    if not task.cancelled():
        task.exception()
