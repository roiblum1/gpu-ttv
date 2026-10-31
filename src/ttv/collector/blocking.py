"""
Run an uncancellable blocking SDK call on a daemon thread the interpreter never joins.

`ucscsdk` has no timeout and `ucsmsdk`'s bounds one socket operation, so a wedged call must be
abandonable without stalling shutdown; why a daemon thread and not `asyncio.to_thread` or a
`ThreadPoolExecutor` is in server-scan's `backend/app/infrastructure/blocking.py@defe172`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from typing import Any


# Copied from server-scan `backend/app/infrastructure/blocking.py@defe172`.
async def run_abandonable[T](func: Callable[..., T], /, *args: Any, name: str) -> T:
    """
    Run `func(*args)` on a daemon thread and await its result.

    The caller imposes the deadline; a call that never returns leaks its thread on purpose, so
    the caller must not reuse the SDK handle afterwards.

    Args:
        func (Callable[..., T]): The blocking, uncancellable callable.
        *args (Any): Positional arguments forwarded to `func`.
        name (str): Thread name, so a wedged call is identifiable in a thread dump.

    Returns:
        T: Whatever `func` returned.

    Raises:
        BaseException: Whatever `func` raised, re-raised on the loop.
    """
    bridge: concurrent.futures.Future[T] = concurrent.futures.Future()

    def _runner() -> None:
        """Run the call unless the awaiting side already gave up on it."""
        if not bridge.set_running_or_notify_cancel():
            return
        try:
            bridge.set_result(func(*args))
        except BaseException as exc:
            bridge.set_exception(exc)

    threading.Thread(target=_runner, name=name, daemon=True).start()
    return await asyncio.wrap_future(bridge)
