"""Local immutable snapshots and atomic state transitions for frame jobs."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

from .frame_models import FrameError, TTL_SECONDS

ACTIVE = ('queued', 'running', 'cancelling')
TERMINAL = ('completed', 'partial', 'failed', 'interrupted', 'cancelled')


def identifier(value) -> str:
    try:
        parsed = str(uuid.UUID(str(value)))
        if parsed != str(value):
            raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise FrameError('invalid_id', 'Mã tham chiếu không hợp lệ.', 404) from None


def encode(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(',', ':'))


class FrameStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.leases = {}
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS objects (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
                    created REAL NOT NULL, activity REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, preflight_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
                    data TEXT NOT NULL, created REAL NOT NULL, activity REAL NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.root / 'frames.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, kind: str, value: dict) -> dict:
        key, now = str(uuid.uuid4()), time.time()
        with self.lock, self.connect() as db:
            db.execute('INSERT INTO objects VALUES (?,?,?,?,?)', (key, kind, encode(value), now, now))
        return {**value, f'{kind}_id': key}

    def get(self, kind: str, key) -> dict:
        key = identifier(key)
        with self.lock, self.connect() as db:
            row = db.execute('SELECT * FROM objects WHERE id=? AND kind=?', (key, kind)).fetchone()
            if row is None or row['activity'] < time.time() - TTL_SECONDS:
                raise FrameError('not_found', 'Tham chiếu không tồn tại hoặc đã hết hạn.', 404)
            db.execute('UPDATE objects SET activity=? WHERE id=?', (time.time(), key))
        return {**json.loads(row['data']), f'{kind}_id': key}

    def save_reference(self, metadata):
        return self.save('reference', {'metadata': metadata})

    def get_reference(self, key):
        return self.get('reference', key)

    def save_search(self, parameters, scenes):
        return self.save('search', {'parameters': parameters, 'scenes': scenes})

    def get_search(self, key):
        return self.get('search', key)

    def save_preflight(self, data):
        return self.save('preflight', data)

    def get_preflight(self, key):
        return self.get('preflight', key)

    def create_job(self, preflight):
        now, key = time.time(), str(uuid.uuid4())
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE preflight_id=?', (preflight['preflight_id'],)).fetchone()
            if existing is not None:
                return json.loads(existing['data'])
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running','cancelling')").fetchone():
                raise FrameError('busy', 'Một tác vụ đồng khung đang chạy; chờ hoàn tất.', 409)
            selected = preflight['scenes']
            if preflight['parameters'].get('merge_scenes'):
                selected = [{**selected[0], 'sources': selected}]
            scenes = [{**scene, 'status': 'queued', 'report': None, 'error': None,
                       'file_token': None, 'filename': None} for scene in selected]
            data = {'job_id': key, 'preflight_id': preflight['preflight_id'], 'status': 'queued',
                    'preflight_digest': preflight['digest'],
                    'total': len(scenes), 'processed': 0, 'succeeded': 0, 'failed': 0,
                    'stage': 'queued', 'scenes': scenes, 'error': None}
            db.execute('INSERT INTO jobs VALUES (?,?,?,?,?,?)',
                       (key, preflight['preflight_id'], 'queued', encode(data), now, now))
        return data

    def get_job(self, key):
        key = identifier(key)
        with self.lock, self.connect() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (key,)).fetchone()
            if row is None or (row['status'] not in ACTIVE and row['activity'] < time.time() - TTL_SECONDS):
                raise FrameError('not_found', 'Tác vụ không tồn tại hoặc đã hết hạn.', 404)
            now = time.time()
            db.execute('UPDATE jobs SET activity=? WHERE id=?', (now, key))
            db.execute('UPDATE objects SET activity=? WHERE id=?', (now, row['preflight_id']))
        return json.loads(row['data'])

    def update_job(self, key, **updates):
        with self.lock, self.connect() as db:
            row = db.execute('SELECT data FROM jobs WHERE id=?', (identifier(key),)).fetchone()
            if row is None:
                raise FrameError('not_found', 'Không tìm thấy tác vụ.', 404)
            data = {**json.loads(row['data']), **updates}
            db.execute('UPDATE jobs SET status=?,data=?,activity=? WHERE id=?',
                       (data['status'], encode(data), time.time(), str(key)))
        return data

    def claim(self, key):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT data FROM jobs WHERE id=? AND status='queued'", (identifier(key),)).fetchone()
            if row is None:
                return False
            data = {**json.loads(row['data']), 'status': 'running', 'stage': 'export'}
            db.execute('UPDATE jobs SET status=?,data=?,activity=? WHERE id=?',
                       ('running', encode(data), time.time(), str(key)))
        return True

    def cancel_job(self, key):
        with self.lock:
            job = self.get_job(key)
            if job['status'] == 'queued':
                return self.update_job(key, status='cancelled', stage='cancelled')
            if job['status'] in ('running', 'cancelling'):
                return self.update_job(key, status='cancelling', stage='cancelling')
            return job

    def finish_if_cancelled(self, key):
        with self.lock:
            job = self.get_job(key)
            if job['status'] != 'cancelling':
                return False
            self.update_job(key, status='cancelled', stage='cancelled')
            return True

    def finish_job(self, key, status):
        with self.lock:
            if not self.finish_if_cancelled(key):
                self.update_job(key, status=status, stage=status)

    def job_dir(self, key):
        return self.root / 'jobs' / identifier(key)

    def result_path(self, key, token=None):
        job = self.get_job(key)
        directory = self.job_dir(key)
        if token is None:
            if job['status'] not in ('completed', 'partial'):
                raise FrameError('not_ready', 'Tệp ZIP chưa sẵn sàng.', 409)
            path, name = directory / 'result.zip', f'matched_frame_{key}.zip'
        else:
            token = identifier(token)
            scene = next((s for s in job['scenes'] if s.get('file_token') == token and s['status'] == 'succeeded'), None)
            if scene is None:
                raise FrameError('not_found', 'Không tìm thấy kết quả đã kiểm tra.', 404)
            path, name = directory / f'{token}.tif', scene['filename']
        if not path.is_file():
            raise FrameError('not_ready', 'Tệp chưa sẵn sàng hoặc đã hết hạn.', 409)
        return path, name

    @contextmanager
    def lease(self, key):
        key = identifier(key)
        with self.lock:
            self.leases[key] = self.leases.get(key, 0) + 1
        try:
            yield
        finally:
            with self.lock:
                self.leases[key] -= 1
                if self.leases[key] == 0:
                    del self.leases[key]

    def recover(self):
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT id,data FROM jobs WHERE status IN ('queued','running','cancelling')").fetchall()
            for row in rows:
                data = json.loads(row['data'])
                for scene in data['scenes']:
                    if scene['status'] in ACTIVE:
                        scene.update(status='interrupted', error='Ứng dụng đã khởi động lại.')
                data.update(status='interrupted', stage='interrupted', error='Ứng dụng đã khởi động lại; tạo lại tác vụ để thử lại.')
                db.execute('UPDATE jobs SET status=?,data=?,activity=? WHERE id=?',
                           ('interrupted', encode(data), time.time(), row['id']))
            for path in self.root.glob('upload/*.tif'):
                path.unlink(missing_ok=True)
            return len(rows)

    def cleanup(self, now=None):
        cutoff = (time.time() if now is None else now) - TTL_SECONDS
        with self.lock, self.connect() as db:
            rows = db.execute("SELECT id FROM jobs WHERE activity<? AND status NOT IN ('queued','running','cancelling')", (cutoff,)).fetchall()
            for row in rows:
                if row['id'] not in self.leases:
                    shutil.rmtree(self.job_dir(row['id']), ignore_errors=True)
                    db.execute('DELETE FROM jobs WHERE id=?', (row['id'],))
            expired_searches = db.execute("SELECT id FROM objects WHERE kind='search' AND activity<?", (cutoff,)).fetchall()
            for row in expired_searches:
                shutil.rmtree(self.root / 'previews' / identifier(row['id']), ignore_errors=True)
            expired_preflights = db.execute("SELECT id FROM objects WHERE kind='preflight' AND activity<?", (cutoff,)).fetchall()
            for row in expired_preflights:
                if not self.leases.get(row['id']):
                    shutil.rmtree(self.root / 'preflight_previews' / identifier(row['id']), ignore_errors=True)
            db.execute('DELETE FROM objects WHERE activity<? AND id NOT IN (SELECT preflight_id FROM jobs)', (cutoff,))
        return len(rows)
