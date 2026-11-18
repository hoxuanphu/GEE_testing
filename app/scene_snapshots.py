"""Small process-local cache freezing calibrated preview/export EE graphs."""
from collections import OrderedDict
from secrets import token_urlsafe
from threading import Lock
from time import monotonic


class SceneSnapshots:
    """Opaque capability tokens, bounded memory and explicit expiry (one worker)."""
    def __init__(self, capacity=32, ttl_seconds=3600):
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self._items = OrderedDict()
        self._lock = Lock()

    def put(self, key, scene):
        with self._lock:
            now = monotonic()
            for token in list(self._items):
                if self._items[token][0] <= now:
                    del self._items[token]
            token = token_urlsafe(24)
            self._items[token] = (now + self.ttl_seconds, key, scene)
            while len(self._items) > self.capacity:
                self._items.popitem(last=False)
            return token

    def get(self, token, key):
        with self._lock:
            value = self._items.get(token)
            if value is None or value[0] <= monotonic():
                self._items.pop(token, None)
                raise LookupError('Lựa chọn ảnh đã hết hạn; hãy tìm ảnh lại để tạo link tải mới.')
            if value[1] != key:
                raise ValueError('Link tải không khớp vùng/ngày của ảnh xem trước; hãy tìm ảnh lại.')
            return value[2]
