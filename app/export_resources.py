"""Process-local FIFO admission shared by bulk GEE exports, never previews."""
from collections import deque
from contextlib import contextmanager
from functools import wraps
import threading
import time


class QueueTimeout(TimeoutError):
    pass


class ExportResources:
    def __init__(self):
        self._condition = threading.Condition()
        self._queue = deque()
        self._active = False
        self._local = threading.local()

    @contextmanager
    def raster_admission(self, check=lambda: None, wait_deadline=None):
        if getattr(self._local, 'owned', False):
            check()
            if wait_deadline is not None and time.monotonic() >= wait_deadline:
                raise QueueTimeout('queue_timeout')
            yield
            return
        ticket = object()
        deadline = time.monotonic() + 900 if wait_deadline is None else wait_deadline
        acquired = False
        with self._condition:
            self._queue.append(ticket)
        try:
            with self._condition:
                while True:
                    check()
                    if time.monotonic() >= deadline:
                        raise QueueTimeout('queue_timeout')
                    if not self._active and self._queue[0] is ticket:
                        self._queue.popleft()
                        self._active = acquired = self._local.owned = True
                        break
                    self._condition.wait(min(.1, max(.001, deadline-time.monotonic())))
            yield
        finally:
            with self._condition:
                if acquired:
                    self._active = self._local.owned = False
                elif ticket in self._queue:
                    self._queue.remove(ticket)
                self._condition.notify_all()


RESOURCES = ExportResources()


def bulk_export(function):
    @wraps(function)
    def admitted(*args, **kwargs):
        from .download_metrics import phase
        from contextlib import ExitStack
        with ExitStack() as stack:
            with phase('queue_wait'):
                stack.enter_context(RESOURCES.raster_admission(kwargs.get('check', lambda:None)))
            return function(*args, **kwargs)
    return admitted
