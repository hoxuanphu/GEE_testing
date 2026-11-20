"""Request-local timings for GEE searches; never log inputs or provider URLs."""
from __future__ import annotations

import logging
import time
import uuid
from contextvars import ContextVar
from typing import Callable

from starlette.datastructures import MutableHeaders

_logger = logging.getLogger("uvicorn.error")
_timings: ContextVar[dict[str, float] | None] = ContextVar("search_timings", default=None)
_PATHS = {"/api/scene", "/api/composite-plan"}


def measure_call(name: str, function: Callable, *args, **kwargs):
    timings = _timings.get()
    if timings is None:
        return function(*args, **kwargs)
    start = time.perf_counter()
    try:
        return function(*args, **kwargs)
    finally:
        timings[name] = timings.get(name, 0.0) + (time.perf_counter() - start) * 1000


class SearchTimingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] not in _PATHS or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        timings: dict[str, float] = {}
        token = _timings.set(timings)
        start = time.perf_counter()
        request_id = uuid.uuid4().hex[:12]
        status = 500

        async def timed_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                timings["total"] = (time.perf_counter() - start) * 1000
                headers = MutableHeaders(scope=message)
                headers.append("Server-Timing", ", ".join(
                    f"{name};dur={duration:.2f}" for name, duration in timings.items()
                ))
                headers["X-Search-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, timed_send)
        finally:
            if "total" not in timings:
                timings["total"] = (time.perf_counter() - start) * 1000
            _logger.info(
                "search_timing request_id=%s route=%s status=%s %s",
                request_id, scope["path"], status,
                " ".join(f"{name}_ms={duration:.2f}" for name, duration in timings.items()),
            )
            _timings.reset(token)
