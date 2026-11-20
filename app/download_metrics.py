"""Request-local export measurements. Reports never accept arbitrary metadata."""
from __future__ import annotations

import csv
import ctypes
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

_current = ContextVar('download_metrics', default=None)
_LABEL = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')


def _label(value):
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise ValueError('Metrics require a safe identifier')
    return value


def resource_usage():
    """Process high water mark and CPU seconds; no extra monitoring package."""
    if os.name == 'nt':
        from ctypes import wintypes
        class Counters(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ('PeakWorkingSetSize', 'WorkingSetSize',
                'QuotaPeakPagedPoolUsage', 'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
                'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage')]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        peak = counters.PeakWorkingSetSize
    else:
        import resource
        import sys
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
    return dict(rss_peak_bytes=peak, cpu_s=time.process_time())


class DownloadMetrics:
    def __init__(self, job_id):
        self.job_id = _label(job_id)
        self._phases = {}
        self._failed = []
        self._tiles = []
        self._lock = threading.Lock()

    @contextmanager
    def phase(self, name):
        _label(name)
        start = time.perf_counter()
        try:
            yield
        except BaseException:
            with self._lock:
                self._failed.append(name)
            raise
        finally:
            duration = time.perf_counter() - start
            with self._lock:
                self._phases[name] = self._phases.get(name, 0.) + duration

    def record_tile(self, tile_id, *, event, bytes_count=0, elapsed_s=0, attempt=0):
        entry = dict(tile_id=_label(tile_id), event=_label(event), bytes_count=int(bytes_count),
                     elapsed_s=float(elapsed_s), attempt=int(attempt))
        with self._lock:
            self._tiles.append(entry)

    def report(self):
        with self._lock:
            return dict(schema_version=1, job_id=self.job_id, phases_s=dict(self._phases),
                        failed_phases=list(self._failed), tiles=list(self._tiles), resources=resource_usage())

    def write_report(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        report = self.report()
        temporary = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(report, indent=2, allow_nan=False), encoding='utf-8')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        with path.with_suffix('.csv').open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['tile_id', 'event', 'bytes_count', 'elapsed_s', 'attempt'])
            writer.writeheader()
            writer.writerows(report['tiles'])


@contextmanager
def phase(name):
    metrics = _current.get()
    if metrics is None:
        yield
    else:
        with metrics.phase(name):
            yield


def measure(name, function, *args, **kwargs):
    with phase(name):
        return function(*args, **kwargs)


class DownloadTimingMiddleware:
    """Record server preparation separately from ASGI browser delivery."""
    def __init__(self, app, directory):
        self.app, self.directory = app, Path(directory)

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or not scope['path'].startswith('/api/download/'):
            return await self.app(scope, receive, send)
        metrics = DownloadMetrics(uuid.uuid4().hex)
        token = _current.set(metrics)
        started = time.perf_counter()
        response_start = None

        async def timed_send(message):
            nonlocal response_start
            if message['type'] == 'http.response.start':
                response_start = time.perf_counter()
                from starlette.datastructures import MutableHeaders
                MutableHeaders(scope=message)['X-Download-Request-ID'] = metrics.job_id
            await send(message)

        try:
            with metrics.phase('total'):
                await self.app(scope, receive, timed_send)
        finally:
            if response_start is not None:
                with metrics._lock:
                    metrics._phases['preparation'] = response_start - started
                    metrics._phases['delivery'] = time.perf_counter() - response_start
            _current.reset(token)
            try:
                metrics.write_report(self.directory / (metrics.job_id + '.json'))
            except OSError:
                # Instrumentation cannot turn a successful export into an error.
                import logging
                logging.getLogger(__name__).warning('Cannot write download metrics report')
