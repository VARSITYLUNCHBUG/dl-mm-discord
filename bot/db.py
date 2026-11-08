"""The one async-to-sync boundary. Services stay synchronous because Django has no async atomic().

Every database call runs on one shared thread, which keeps transactions simple at this scale.
A long-lived process must also retire stale connections itself; Django only does that for you
inside the request cycle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from asgiref.sync import sync_to_async
from django.db import close_old_connections


async def db[**P, T](fn: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    def call() -> T:
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()

    wrapped: Callable[[], Any] = sync_to_async(call, thread_sensitive=True)
    return await wrapped()
