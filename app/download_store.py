"""SQLite authority for frozen selections, attempts and immutable tile receipts."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
import json
import os
import re
from pathlib import Path
import sqlite3
import time

from .download_models import FrozenSelection, PIPELINE_VERSION, canonical, TileReceipt, Attempt
from uuid import uuid4


class AttemptBusy(RuntimeError):
    pass


class StaleAttempt(RuntimeError):
    pass


class JobNotFound(LookupError):
    pass


def _process_alive(pid):
    if pid==os.getpid():
        return True
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes=[wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        handle=kernel.OpenProcess(0x1000,False,pid)
        if not handle:
            return ctypes.get_last_error()!=87  # access denied is conservatively alive
        try:
            code=wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value==259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid,0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class SelectionExpired(LookupError):
    pass


class SelectionMismatch(ValueError):
    pass


class SelectionStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / 'state.sqlite3'
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS selections (
                    token TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS selection_pins (
                    owner TEXT PRIMARY KEY, token TEXT NOT NULL REFERENCES selections(token));
                CREATE TABLE IF NOT EXISTS attempts (
                    artifact_key TEXT PRIMARY KEY, number INTEGER NOT NULL,
                    owner TEXT NOT NULL, active INTEGER NOT NULL, lease_until REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS attempt_jobs (
                    owner TEXT PRIMARY KEY, job_id TEXT NOT NULL, generation INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tiles (tile_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tile_references (
                    artifact_key TEXT NOT NULL, tile_key TEXT NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(artifact_key,tile_key));
                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_key TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    expires_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS invalid_artifacts (artifact_key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS publication_intents (artifact_key TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS artifact_leases (
                    owner TEXT PRIMARY KEY, artifact_key TEXT NOT NULL REFERENCES artifacts(artifact_key),
                    pid INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS download_jobs (
                    id TEXT PRIMARY KEY, artifact_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                    generation INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                    payload TEXT NOT NULL);
            ''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, selection):
        with self.connect() as db:
            db.execute('INSERT INTO selections VALUES (?, ?, ?)',
                       (selection.token, canonical(asdict(selection)), selection.expires_at))
        return selection

    def get(self, token, *, request=None, provider_scope=None, now=None, pinned_by=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            row = db.execute('SELECT * FROM selections WHERE token=?', (token,)).fetchone()
            pin = pinned_by and db.execute('SELECT 1 FROM selection_pins WHERE owner=? AND token=?',
                                           (pinned_by, token)).fetchone()
        if not row or (row['expires_at'] <= now and not pin):
            raise SelectionExpired('Lựa chọn ảnh đã hết hạn; hãy tìm ảnh lại.')
        selection = FrozenSelection(**json.loads(row['payload']))
        if request is not None:
            request = dict(request)
            for key, default in (('scene_grouping', 'any'), ('max_scene_span_days', 3),
                                 ('mosaic_mask_clouds', True)):
                if key not in selection.request and request.get(key) == default:
                    request.pop(key, None)
        if (selection.version != PIPELINE_VERSION or
            (request is not None and canonical(request) != canonical(selection.request)) or
            (provider_scope is not None and selection.provider_scope != provider_scope)):
            raise SelectionMismatch('Lựa chọn không khớp vùng, băng, phiên bản hoặc tài khoản GEE.')
        return selection

    def pin(self, token, owner):
        with self.connect() as db:
            db.execute('INSERT INTO selection_pins VALUES (?, ?)', (owner, token))

    def unpin(self, owner):
        with self.connect() as db:
            db.execute('DELETE FROM selection_pins WHERE owner=?', (owner,))

    def cleanup(self, now=None):
        with self.connect() as db:
            clock=time.time() if now is None else now
            db.execute('DELETE FROM selection_pins WHERE owner IN '
                       '(SELECT owner FROM attempts WHERE lease_until<=?)',(clock,))
            db.execute('UPDATE attempts SET active=0 WHERE lease_until<=?',(clock,))
            db.execute('DELETE FROM selections WHERE expires_at<=? AND token NOT IN '
                       '(SELECT token FROM selection_pins)', (time.time() if now is None else now,))

    def claim_attempt(self, artifact_key, selection_token=None, *, job_context=None):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM attempts WHERE artifact_key=?', (artifact_key,)).fetchone()
            if row and row['active'] and row['lease_until'] > now:
                raise AttemptBusy('download_in_progress')
            attempt = Attempt(artifact_key, row['number'] + 1 if row else 1, uuid4().hex)
            if row:
                db.execute('DELETE FROM selection_pins WHERE owner=?', (row['owner'],))
                db.execute('DELETE FROM attempt_jobs WHERE owner=?',(row['owner'],))
            db.execute('INSERT OR REPLACE INTO attempts VALUES (?, ?, ?, 1, ?)',
                       (artifact_key, attempt.number, attempt.owner, now + 1800))
            if selection_token:
                db.execute('INSERT INTO selection_pins VALUES (?, ?)', (attempt.owner, selection_token))
            if job_context:
                db.execute('INSERT INTO attempt_jobs VALUES (?, ?, ?)',(attempt.owner,*job_context))
        return attempt

    def _fence(self, db, attempt):
        row = db.execute('SELECT * FROM attempts WHERE artifact_key=?', (attempt.artifact_key,)).fetchone()
        if not row or not row['active'] or row['number'] != attempt.number or row['owner'] != attempt.owner or row['lease_until'] <= time.time():
            raise StaleAttempt('stale_attempt')
        job=db.execute('SELECT j.state,j.generation,a.generation AS attempt_generation '
                       'FROM attempt_jobs a LEFT JOIN download_jobs j ON j.id=a.job_id WHERE a.owner=?',
                       (attempt.owner,)).fetchone()
        if job and (job['state']!='running' or job['generation']!=job['attempt_generation']):
            raise StaleAttempt('stale_job_attempt')

    def heartbeat(self, attempt):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db, attempt)
            db.execute('UPDATE attempts SET lease_until=? WHERE artifact_key=?', (time.time()+1800, attempt.artifact_key))

    def commit_tile(self, attempt, receipt):
        if not Path(receipt.filename).resolve().is_relative_to(self.root):
            raise ValueError('Tile path outside managed root')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db, attempt)
            db.execute('INSERT OR REPLACE INTO tiles VALUES (?, ?)', (receipt.tile_key, canonical(asdict(receipt))))
            db.execute('INSERT INTO tile_references VALUES (?, ?, ?) ON CONFLICT(artifact_key,tile_key) '
                       'DO UPDATE SET updated_at=excluded.updated_at',(attempt.artifact_key,receipt.tile_key,time.time()))

    def release_attempt(self, attempt):
        with self.connect() as db:
            db.execute('UPDATE attempts SET active=0 WHERE artifact_key=? AND number=? AND owner=?',
                       (attempt.artifact_key, attempt.number, attempt.owner))
            db.execute('DELETE FROM selection_pins WHERE owner=?', (attempt.owner,))
            db.execute('DELETE FROM attempt_jobs WHERE owner=?',(attempt.owner,))

    def get_verified_tile(self, tile_key):
        from .download_transport import verify_receipt
        with self.connect() as db:
            row = db.execute('SELECT payload FROM tiles WHERE tile_key=?', (tile_key,)).fetchone()
        if not row:
            return None
        receipt = TileReceipt(**json.loads(row['payload']))
        if not Path(receipt.filename).resolve().is_relative_to(self.root) or not verify_receipt(receipt):
            # CAS: a delayed verifier cannot remove a newer worker's receipt.
            with self.connect() as db:
                db.execute('DELETE FROM tiles WHERE tile_key=? AND payload=?', (tile_key, row['payload']))
            return None
        return receipt

    def invalidate_tile(self, tile_key):
        with self.connect() as db:
            db.execute('DELETE FROM tiles WHERE tile_key=?', (tile_key,))

    def _artifact_path(self, path):
        path=Path(path).resolve()
        if not path.is_relative_to(self.root) or path.suffix!='.tif':
            raise ValueError('Artifact path outside managed root')
        return path

    def save_artifact(self, attempt, receipt):
        self._artifact_path(receipt.filename)
        if receipt.tile_key!=attempt.artifact_key:
            raise ValueError('artifact_fingerprint_mismatch')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,attempt)
            self._save_artifact(db,receipt)

    def _save_artifact(self, db, receipt):
        now=time.time()
        db.execute('INSERT INTO artifacts VALUES (?, ?, ?, ?) ON CONFLICT(artifact_key) DO UPDATE '
                   'SET payload=excluded.payload,expires_at=excluded.expires_at,updated_at=excluded.updated_at',
                   (receipt.tile_key,canonical(asdict(receipt)),now+72*3600,now))
        db.execute('DELETE FROM invalid_artifacts WHERE artifact_key=?',(receipt.tile_key,))
        db.execute('DELETE FROM publication_intents WHERE artifact_key=?',(receipt.tile_key,))

    def prepare_publication(self,attempt,receipt,destination):
        destination=self._artifact_path(destination)
        if receipt.tile_key!=attempt.artifact_key:
            raise ValueError('artifact_fingerprint_mismatch')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,attempt)
            db.execute('INSERT OR REPLACE INTO publication_intents VALUES (?, ?)',
                       (attempt.artifact_key,canonical(asdict(replace(receipt,filename=str(destination))))))

    def publish_artifact(self, attempt, receipt, destination):
        source=self._artifact_path(receipt.filename)
        destination=self._artifact_path(destination)
        if receipt.tile_key!=attempt.artifact_key:
            raise ValueError('artifact_fingerprint_mismatch')
        # This intent survives a crash/rollback after the filesystem rename.
        self.prepare_publication(attempt,receipt,destination)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._fence(db,attempt)
            # Keep fencing and rename inside one short SQLite writer transaction.
            # If killed after rename, execute_selection revalidates the file.
            os.replace(source,destination)
            receipt=replace(receipt,filename=str(destination))
            self._save_artifact(db,receipt)
        return receipt

    def get_artifact(self, artifact_key):
        receipt=self.get_artifact_record(artifact_key)
        if receipt is None:
            return None
        path=self._artifact_path(receipt.filename)
        if not path.is_file() or path.stat().st_size!=receipt.size:
            return None
        return receipt

    def get_artifact_record(self,artifact_key):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM invalid_artifacts WHERE artifact_key=?',(artifact_key,)).fetchone():
                return None
            row=db.execute('SELECT payload FROM artifacts WHERE artifact_key=?',(artifact_key,)).fetchone()
        if not row:
            return None
        return TileReceipt(**json.loads(row['payload']))

    def get_publication_intent(self,artifact_key):
        with self.connect() as db:
            row=db.execute('SELECT payload FROM publication_intents WHERE artifact_key=?',(artifact_key,)).fetchone()
        return TileReceipt(**json.loads(row['payload'])) if row else None

    def invalidate_artifact(self,artifact_key):
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO invalid_artifacts VALUES (?)',(artifact_key,))

    @contextmanager
    def artifact_lease(self, artifact_key):
        owner=uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM invalid_artifacts WHERE artifact_key=?',(artifact_key,)).fetchone():
                raise FileNotFoundError('artifact_invalid')
            row=db.execute('SELECT payload FROM artifacts WHERE artifact_key=?',(artifact_key,)).fetchone()
            if not row:
                raise FileNotFoundError('artifact_expired')
            receipt=TileReceipt(**json.loads(row['payload']))
            path=self._artifact_path(receipt.filename)
            if not path.is_file() or path.stat().st_size!=receipt.size:
                raise FileNotFoundError('artifact_missing')
            db.execute('INSERT INTO artifact_leases VALUES (?, ?, ?)',(owner,artifact_key,os.getpid()))
        try:
            yield receipt
        finally:
            with self.connect() as db:
                db.execute('DELETE FROM artifact_leases WHERE owner=?',(owner,))

    def cleanup_artifacts(self, *, now=None, quota_bytes=32*1024**3):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT DISTINCT pid FROM artifact_leases').fetchall():
                if not _process_alive(row['pid']):
                    db.execute('DELETE FROM artifact_leases WHERE pid=?',(row['pid'],))
            rows=db.execute('SELECT * FROM artifacts ORDER BY updated_at').fetchall()
            total=sum(json.loads(row['payload'])['size'] for row in rows)
            for row in rows:
                key=row['artifact_key']
                protected=(db.execute('SELECT 1 FROM artifact_leases WHERE artifact_key=?',(key,)).fetchone() or
                    db.execute('SELECT 1 FROM attempts WHERE artifact_key=? AND active=1 AND lease_until>?',(key,now)).fetchone() or
                    db.execute("SELECT 1 FROM download_jobs WHERE artifact_key=? AND expires_at>? AND state NOT IN ('completed','cancelled','failed')",(key,now)).fetchone())
                if protected or (row['expires_at']>now and total<=quota_bytes):
                    continue
                receipt=TileReceipt(**json.loads(row['payload']))
                path=self._artifact_path(receipt.filename)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                db.execute('DELETE FROM artifacts WHERE artifact_key=?',(key,))
                total-=receipt.size

    def create_job(self, snapshot, artifact_key, settings):
        now=time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT payload FROM download_jobs WHERE artifact_key=?',(artifact_key,)).fetchone()
            if existing:
                previous=json.loads(existing['payload'])
                if previous['state']=='cancelled' or (previous['state']=='completed' and self.get_artifact(artifact_key) is None):
                    db.execute('DELETE FROM selection_pins WHERE owner=?',('job-'+previous['id'],))
                    db.execute('DELETE FROM download_jobs WHERE id=?',(previous['id'],))
                else:
                    return previous
            count=db.execute("SELECT COUNT(*) FROM download_jobs WHERE state IN ('queued','running')").fetchone()[0]
            if count>=32:
                raise AttemptBusy('download_queue_full')
            job=dict(id=uuid4().hex,artifact_key=artifact_key,selection_token=snapshot.token,
                     settings=settings,state='queued',stage='queued',generation=0,
                     created_at=now,expires_at=now+7*86400,tiles_total=0,tiles_verified=0,
                     received_bytes=0,verified_bytes=0,blocks_written=0,error_code=None)
            db.execute('INSERT INTO download_jobs VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (job['id'],artifact_key,'queued',0,now,job['expires_at'],canonical(job)))
            db.execute('INSERT INTO selection_pins VALUES (?, ?)',('job-'+job['id'],snapshot.token))
            return job

    def get_job(self, job_id):
        with self.connect() as db:
            row=db.execute('SELECT payload FROM download_jobs WHERE id=?',(job_id,)).fetchone()
        if not row:
            raise JobNotFound('download_job_not_found')
        return json.loads(row['payload'])

    def update_job(self, job_id, *, generation=None, allowed_states=None, **changes):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM download_jobs WHERE id=?',(job_id,)).fetchone()
            if not row:
                raise JobNotFound('download_job_not_found')
            job=json.loads(row['payload'])
            if ((generation is not None and generation!=job['generation']) or
                (allowed_states is not None and job['state'] not in allowed_states)):
                raise StaleAttempt('stale_job_generation')
            if changes.pop('increment_generation',False):
                job['generation']+=1
            job.update(changes)
            db.execute('UPDATE download_jobs SET state=?,generation=?,expires_at=?,payload=? WHERE id=?',
                       (job['state'],job['generation'],job['expires_at'],canonical(job),job_id))
            return job

    def queued_jobs(self):
        with self.connect() as db:
            return [json.loads(row['payload']) for row in db.execute(
                "SELECT payload FROM download_jobs WHERE state='queued' ORDER BY created_at LIMIT 32")]

    def recover_jobs(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT payload FROM download_jobs WHERE state IN ('queued','running')").fetchall():
                job=json.loads(row['payload'])
                job.update(state='interrupted',stage='interrupted',generation=job['generation']+1)
                db.execute('UPDATE download_jobs SET state=?,generation=?,payload=? WHERE id=?',
                           (job['state'],job['generation'],canonical(job),job['id']))
                # Fence the interrupted artifact writer too, before resume.
                db.execute('DELETE FROM selection_pins WHERE owner IN '
                           '(SELECT owner FROM attempt_jobs WHERE job_id=?)',(job['id'],))
                db.execute('UPDATE attempts SET active=0 WHERE owner IN '
                           '(SELECT owner FROM attempt_jobs WHERE job_id=?)',(job['id'],))

    def expire_jobs(self, now=None):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT id FROM download_jobs WHERE expires_at<=? AND state NOT IN ('queued','running')",(now,)).fetchall():
                db.execute('DELETE FROM selection_pins WHERE owner=?',('job-'+row['id'],))
                db.execute('DELETE FROM download_jobs WHERE id=?',(row['id'],))

    def cleanup_tiles(self, *, now=None, quota_bytes=32*1024**3):
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows=db.execute('SELECT t.tile_key,t.payload,MAX(r.updated_at) AS updated_at FROM tiles t '
                            'LEFT JOIN tile_references r ON r.tile_key=t.tile_key GROUP BY t.tile_key '
                            'ORDER BY updated_at').fetchall()
            total=sum(json.loads(row['payload'])['size'] for row in rows)
            for row in rows:
                key=row['tile_key']
                protected=db.execute("SELECT 1 FROM tile_references r WHERE r.tile_key=? AND ("
                    "EXISTS(SELECT 1 FROM attempts a WHERE a.artifact_key=r.artifact_key AND a.active=1 AND a.lease_until>?) OR "
                    "EXISTS(SELECT 1 FROM download_jobs j WHERE j.artifact_key=r.artifact_key AND j.expires_at>? "
                    "AND j.state IN ('queued','running','paused','interrupted'))) LIMIT 1",(key,now,now)).fetchone()
                if protected or (row['updated_at'] is not None and row['updated_at']+7*86400>now and total<=quota_bytes):
                    continue
                receipt=TileReceipt(**json.loads(row['payload']))
                path=Path(receipt.filename).resolve()
                # Only owned, content-addressed tiles are eligible for deletion.
                if path.parent!=(self.root/'tiles').resolve() or path.name!=receipt.sha256+'.tif':
                    continue
                others=[json.loads(other['payload'])['filename'] for other in db.execute(
                    'SELECT payload FROM tiles WHERE tile_key<>?',(key,))]
                if str(path) not in others:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        continue
                db.execute('DELETE FROM tiles WHERE tile_key=?',(key,))
                db.execute('DELETE FROM tile_references WHERE tile_key=?',(key,))
                total-=receipt.size

    def cleanup_orphans(self,now=None):
        """Reap only owned leftovers from a killed attempt, never verified tiles."""
        now=time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            active={row['owner'] for row in db.execute('SELECT owner FROM attempts WHERE active=1 AND lease_until>?',(now,))}
            for row in db.execute('SELECT * FROM publication_intents').fetchall():
                key=row['artifact_key']
                protected=(db.execute('SELECT 1 FROM attempts WHERE artifact_key=? AND active=1 AND lease_until>?',(key,now)).fetchone() or
                    db.execute("SELECT 1 FROM download_jobs WHERE artifact_key=? AND expires_at>? AND state NOT IN ('completed','cancelled')",(key,now)).fetchone() or
                    db.execute('SELECT 1 FROM artifacts WHERE artifact_key=?',(key,)).fetchone())
                if protected:
                    continue
                receipt=TileReceipt(**json.loads(row['payload']));path=Path(receipt.filename).resolve()
                if path.parent!=(self.root/'artifacts').resolve() or path.name!=key+'.tif' or not re.fullmatch('[0-9a-f]{64}',key):
                    continue
                try:
                    if path.exists() and path.stat().st_mtime+72*3600>now:
                        continue
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
                db.execute('DELETE FROM publication_intents WHERE artifact_key=?',(key,))
            for name in ('tiles','artifacts'):
                directory=self.root/name
                if not directory.is_dir():
                    continue
                for candidate in directory.iterdir():
                    match=re.fullmatch(r'\.?([0-9a-f]{32})(?:-[0-9a-f]{64}\.part|\.(?:staging|source)(?:\.[0-9a-f]{32}\.partial)?\.tif)',candidate.name)
                    if not match or match[1] in active:
                        continue
                    path=candidate.resolve()
                    if path.parent!=directory.resolve():
                        continue
                    try:
                        if path.is_file() and path.stat().st_mtime+86400<=now:
                            path.unlink()
                    except OSError:
                        pass
