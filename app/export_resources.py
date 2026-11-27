"""Process-local FIFO admission shared by bulk GEE exports, never previews."""
from collections import deque
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
import shutil
import threading
import time

from .download_models import DownloadSettings


class QueueTimeout(TimeoutError):
    pass


class TransferCancelled(Exception):
    pass


class ExportDiskSpaceError(OSError):
    pass


class ExportResources:
    def __init__(self):
        self._condition = threading.Condition()
        self._queue = deque()
        self._active = False
        self._local = threading.local()
        self._disk_lock = threading.Lock()
        self._scene_disk = {}

    def reserve_scene_disk(self, job, directory, logical_bytes):
        settings = DownloadSettings()
        headroom = settings.disk_reserve(0)
        demand = settings.disk_reserve(logical_bytes) - headroom
        with self._disk_lock:
            required = headroom + demand + sum(self._scene_disk.values())
            if shutil.disk_usage(directory).free < required:
                raise ExportDiskSpaceError('Insufficient space for scene export')
            self._scene_disk[job] = demand

    def release_scene_disk(self, job):
        with self._disk_lock:
            self._scene_disk.pop(job, None)

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
CURRENT_SCENE = ContextVar('current_scene_export', default=None)


class SceneExport:
    """Request-owned handoff from the GEE gate to the local writer."""

    def __init__(self, job, check, update, acquire_assembly):
        self.job = job
        self.check = check
        self.update = update
        self.acquire_assembly = acquire_assembly
        self.admission = None
        self.reserved = False
        self.result = None
        self.temporary_result = False

    def reserve(self, path, logical_bytes):
        self.check()
        RESOURCES.reserve_scene_disk(self.job, path.parent, logical_bytes)
        self.reserved = True

    def wait_retry(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            self.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def begin_assembly(self):
        self.check()
        # Exit on the same worker that acquired the thread-local GEE gate.
        self.admission.close()
        self.update(stage='waiting_assembly')
        self.acquire_assembly()
        self.check()


@contextmanager
def scene_download_admission():
    scene = CURRENT_SCENE.get()
    if scene is None or scene.admission is not None:
        yield
        return
    from .download_metrics import phase
    with ExitStack() as admission:
        scene.admission = admission
        try:
            with phase('queue_wait'):
                admission.enter_context(RESOURCES.raster_admission(scene.check))
            yield
        finally:
            scene.admission = None


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


def scene_bulk_export(function):
    legacy = bulk_export(function)

    @wraps(function)
    def admitted(*args, **kwargs):
        if CURRENT_SCENE.get() is None:
            return legacy(*args, **kwargs)
        with scene_download_admission():
            return function(*args, **kwargs)

    return admitted
