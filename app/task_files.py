"""Coordinate task-file removal with responses currently streaming artifacts."""
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
import shutil
import threading

from fastapi.responses import FileResponse, JSONResponse

_lock = threading.RLock()
_leases = Counter()


@contextmanager
def artifact_lease(directory):
    directory = Path(directory).absolute()
    with _lock:
        if not directory.is_dir():
            raise FileNotFoundError('Thư mục kết quả không còn tồn tại.')
        _leases[directory] += 1
    try:
        yield
    finally:
        with _lock:
            _leases[directory] -= 1
            if not _leases[directory]:
                del _leases[directory]


def remove_task_directory(directory, parent):
    directory = Path(directory).absolute()
    parent = Path(parent).resolve()
    if directory.parent.resolve() != parent or directory.is_symlink():
        raise ValueError('Thư mục tác vụ không hợp lệ.')
    with _lock:
        if _leases[directory]:
            raise RuntimeError('Đang tải tệp kết quả. Hãy đợi lượt tải hoàn tất.')
        if directory.exists():
            shutil.rmtree(directory)


class TaskFileResponse(FileResponse):
    def __init__(self, *args, task_directory, **kwargs):
        super().__init__(*args, **kwargs)
        self.task_directory = task_directory

    async def __call__(self, scope, receive, send):
        lease = artifact_lease(self.task_directory)
        try:
            lease.__enter__()
        except FileNotFoundError:
            await JSONResponse({'detail': 'Kết quả đã bị xóa.'}, status_code=410)(scope, receive, send)
            return
        try:
            await super().__call__(scope, receive, send)
        finally:
            lease.__exit__(None, None, None)
