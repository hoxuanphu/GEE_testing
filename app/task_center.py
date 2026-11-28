"""Unified progress and controls for the application's downloads."""
import asyncio
from email.message import Message
from functools import partial
import json
import logging
import os
import re
from threading import Lock
from time import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import HTTPException, Query
from fastapi.responses import JSONResponse
from anyio import from_thread, to_thread

from .export_resources import CURRENT_SCENE, RESOURCES, SceneExport, TransferCancelled
from .timeseries import BusyError


ACTIVE = {'queued', 'running', 'cancelling', 'pausing'}
SCENE_PATH = re.compile(r'/api/scene-catalog/[^/]+/download/[^/]+')
logger = logging.getLogger(__name__)


def _sar_whole_scene(path, query):
    return path == '/api/s1gee/download' and parse_qs(query).get(b'extent', [])[-1:] == [b'scene']

DOWNLOAD_PATH = re.compile(
    r'/api/(?:download/[^/]+|(?:dem|s1gee|s1|s2|s1/s3|s2/s3|s2/aws)/download'
    r'|s1/crop(?:-mosaic)?|s2/aws/province-mosaic'
    r'|scene-catalog/[^/]+/download/[^/]+|scene-inventory/[^/]+/csv'
    r'|s5p/search/[^/]+/scenes/[^/]+/download'
    r'|(?:timeseries|esri|matched-frame)/jobs/[^/]+/download'
    r'|matched-frame/jobs/[^/]+/files/[^/]+|download-jobs/[^/]+/file'
    r'|timeseries/batches/[^/]+/years/[^/]+/(?:download|csv))'
)


class DownloadTransfers:
    def __init__(self):
        self.items = {}
        self.stops = {}
        self.lock = Lock()

    def snapshot(self):
        with self.lock:
            return [item.copy() for item in self.items.values()]

    def start(self, path, query):
        job_id = uuid4().hex
        stopped = asyncio.Event()
        loop = asyncio.get_running_loop()
        params = parse_qs(query.decode('utf-8', errors='replace'))
        name = next((params[key][0] for key in ('filename', 'scene_ids', 'product_id', 'product_ids')
                     if params.get(key)), None)
        with self.lock:
            self.stops[job_id] = (loop, stopped)
            self.items[job_id] = {
                'id': job_id, 'kind': 'transfer', 'status': 'queued',
                'stage': 'queued', 'created': time(), 'source': path,
                'filename': name[:240] if name else None,
                'bytes_sent': 0, 'bytes_total': None, 'bytes_estimated': False,
            }
        return job_id, stopped

    def cancel(self, job_id):
        with self.lock:
            item = self.items.get(job_id)
            if item is None:
                raise HTTPException(404, 'Không tìm thấy tác vụ.')
            if item['status'] not in {'queued', 'running', 'cancelling'}:
                raise HTTPException(409, 'Lượt tải đã kết thúc.')
            loop, stopped = self.stops[job_id]
            item['status'] = 'cancelling'
            loop.call_soon_threadsafe(stopped.set)
            return item.copy()

    def check_cancelled(self, job_id):
        with self.lock:
            if self.items[job_id]['status'] == 'cancelling':
                raise TransferCancelled

    def begin(self, job_id):
        with self.lock:
            item = self.items[job_id]
            if item['status'] == 'cancelling':
                raise TransferCancelled
            item.update(status='running', stage='preparing')

    def update(self, job_id, **fields):
        with self.lock:
            self.items[job_id].update(fields)

    def finish(self, job_id, status):
        with self.lock:
            item = self.items[job_id]
            self.stops.pop(job_id, None)
            if item['status'] == 'cancelling' and status != 'completed':
                status = 'cancelled'
                item.pop('error', None)
            item['status'] = status
            if status == 'completed':
                item['stage'] = 'completed'
            completed = [key for key, item in self.items.items() if item['status'] not in ACTIVE]
            for key in completed[:-200]:
                self.items.pop(key)

    def hide(self, job_id):
        with self.lock:
            self.items.pop(job_id, None)
            self.stops.pop(job_id, None)


class DownloadTransferMiddleware:
    def __init__(self, app, transfers):
        self.app = app
        self.transfers = transfers
        self.slot = asyncio.Lock()
        self.assembly_slot = asyncio.Lock()

    @staticmethod
    def tracked(path, query):
        return bool(DOWNLOAD_PATH.fullmatch(path)) or (
            path == '/api/s2/bands' and parse_qs(query).get(b'action') == [b'download']
        )

    @staticmethod
    async def acquire_slot(slot, receive, stopped):
        # A waiting request can be abandoned without starting an export.
        if not slot.locked():
            await slot.acquire()
            return True

        async def wait_for_disconnect():
            while (await receive())['type'] != 'http.disconnect':
                pass

        ticket = asyncio.create_task(slot.acquire())
        disconnected = asyncio.create_task(wait_for_disconnect())
        cancelled = asyncio.create_task(stopped.wait())
        admitted = False
        try:
            done, _ = await asyncio.wait((ticket, disconnected, cancelled), return_when=asyncio.FIRST_COMPLETED)
            disconnected.cancel()
            cancelled.cancel()
            await asyncio.gather(disconnected, cancelled, return_exceptions=True)
            admitted = done == {ticket}
            return admitted
        finally:
            disconnected.cancel()
            cancelled.cancel()
            if not admitted:
                if ticket.done() and not ticket.cancelled() and ticket.result():
                    slot.release()
                else:
                    ticket.cancel()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] != 'GET':
            return await self.app(scope, receive, send)
        path = scope['path']
        query = scope.get('query_string', b'')
        if not self.tracked(path, query):
            return await self.app(scope, receive, send)

        job, stopped = self.transfers.start(path, query)
        navigation = dict(scope.get('headers', ())).get(b'sec-fetch-mode') == b'navigate'
        acquired = False
        assembling = False
        scene = None
        # FileResponse must emit chunks for the byte count to be observable.
        scope = {**scope, 'extensions': {key: value for key, value in
                 scope.get('extensions', {}).items() if key != 'http.response.pathsend'}}
        sent = 0
        total = None
        estimated = False
        outcome = 'interrupted'
        accepted = False
        redirect = False
        response_started = False
        destination = None
        disconnected = False

        async def acquire_assembly():
            nonlocal acquired, assembling
            assembling = await self.acquire_slot(self.assembly_slot, receive, stopped)
            if not assembling:
                raise TransferCancelled
            self.transfers.check_cancelled(job)
            self.transfers.update(job, stage='assembling')
            # Keep the fetch slot until the local writer has room for this scene.
            self.slot.release()
            acquired = False

        async def watch_receive():
            nonlocal disconnected
            message = await receive()
            if message['type'] == 'http.disconnect':
                disconnected = True
            return message

        async def watch_send(message):
            nonlocal sent, total, estimated, outcome, redirect, accepted, destination, disconnected, response_started
            self.transfers.check_cancelled(job)
            if destination is not None:
                return
            if message['type'] == 'http.response.start':
                code = message['status']
                headers = dict(message.get('headers', ()))
                redirect = 300 <= code < 400
                if navigation and code in (303, 307, 308):
                    target = urlsplit(headers.get(b'location', b'').decode('ascii', errors='replace'))
                    if (target.path.startswith('/') and not target.netloc and not target.scheme
                            and target.path.isascii() and target.query.isascii()
                            and self.tracked(target.path, target.query.encode('utf-8'))):
                        destination = target
                        return
                accepted = 200 <= code < 300
                if accepted:
                    length = headers.get(b'content-length', b'')
                    estimate = headers.get(b'x-s3-total-bytes', b'')
                    estimated = not length.isdigit() and estimate.isdigit() and int(estimate) > 0
                    if estimated:
                        length = estimate
                    total = int(length) if length.isdigit() else None
                    metadata = Message()
                    metadata['Content-Disposition'] = headers.get(b'content-disposition', b'').decode('latin-1')
                    fields = {'stage': 'transferring', 'bytes_total': total, 'bytes_estimated': estimated}
                    if filename := metadata.get_filename():
                        fields['filename'] = filename
                    self.transfers.update(job, **fields)
                elif not redirect:
                    outcome = 'failed'
                    self.transfers.update(job, error=f'HTTP {code}')
            try:
                await send(message)
                if message['type'] == 'http.response.start':
                    response_started = True
            except OSError:
                disconnected = True
                raise
            if message['type'] == 'http.response.body' and accepted and not disconnected:
                sent += len(message.get('body', b''))
                self.transfers.update(job, bytes_sent=sent)
                if not message.get('more_body', False) and (estimated or total is None or sent == total):
                    outcome = 'completed'

        try:
            acquired = await self.acquire_slot(self.slot, receive, stopped)
            self.transfers.check_cancelled(job)
            if not acquired:
                return
            self.transfers.begin(job)
            next_scope = scope
            for _ in range(4):
                destination = None
                current_scene = None
                current_path = next_scope['path']
                if SCENE_PATH.fullmatch(current_path) or _sar_whole_scene(current_path, next_scope.get('query_string', b'')):
                    scene = current_scene = SceneExport(
                        job, partial(self.transfers.check_cancelled, job),
                        partial(self.transfers.update, job), partial(from_thread.run, acquire_assembly),
                    )
                token = CURRENT_SCENE.set(current_scene)
                try:
                    await self.app(next_scope.copy(), watch_receive, watch_send)
                finally:
                    CURRENT_SCENE.reset(token)
                if destination is None:
                    break
                next_scope = {
                    **scope, 'path': destination.path, 'raw_path': destination.path.encode('ascii'),
                    'query_string': destination.query.encode('ascii'),
                }
            else:
                redirect = False
                outcome = 'failed'
                self.transfers.update(job, error='Không mở được đường dẫn tải tệp.')
                await send({'type': 'http.response.start', 'status': 508, 'headers': []})
                await send({'type': 'http.response.body', 'body': b''})
        except TransferCancelled:
            redirect = False
            outcome = 'cancelled'
            if not response_started:
                await JSONResponse({'detail': 'Lượt tải đã được hủy.'}, status_code=409)(scope, receive, send)
            # An incomplete HTTP response makes the server close the connection.
            # Sending a final chunk could let the browser save a truncated file as complete.
        except Exception:
            if not disconnected:
                outcome = 'failed'
                self.transfers.update(job, error='Không gửi được tệp.')
            raise
        finally:
            try:
                if scene is not None and scene.result is not None and (
                        scene.temporary_result or outcome != 'completed'):
                    await to_thread.run_sync(partial(scene.result.unlink, missing_ok=True))
            except OSError:
                logger.warning('Could not remove scene export for transfer %s', job)
            finally:
                if scene is not None and scene.reserved:
                    RESOURCES.release_scene_disk(job)
                if redirect:
                    self.transfers.hide(job)
                else:
                    self.transfers.finish(job, outcome)
                if assembling:
                    self.assembly_slot.release()
                if acquired:
                    self.slot.release()


def register_task_center(api, series_store, batch_store, frame_store,
                         esri_provider, download_store_provider, batch_provider,
                         download_service_provider):
    transfers = DownloadTransfers()
    api.add_middleware(DownloadTransferMiddleware, transfers=transfers)

    def all_tasks():
        items = transfers.snapshot()
        for job in series_store.list_jobs():
            job_id = job['job_id']
            items.append({
                'id': job_id, 'kind': 'timeseries', 'status': job['status'],
                'stage': job.get('stage'), 'created': job['created'],
                'parameters': {key: job['parameters'].get(key) for key in
                               ('dataset', 'selection_mode', 'start_date', 'end_date')},
                'total': job['total'], 'processed': job['processed'],
                'succeeded': job['succeeded'], 'failed': job['failed'], 'error': job['error'],
                'download_url': f'/api/timeseries/jobs/{job_id}/download'
                if job['status'] in ('completed', 'partial') and
                   (series_store._job_dir(job_id) / 'timeseries.zip').is_file() else None,
            })
        for batch in batch_store.list_batches():
            years = batch['years']
            items.append({
                'id': batch['batch_id'], 'kind': 'annual', 'status': batch['status'],
                'created': batch['created'], 'parameters': {key: batch['parameters'].get(key)
                    for key in ('dataset', 'selection_mode', 'start_date', 'end_date')},
                'stage': str(batch['current_year']) if batch['current_year'] else None,
                'total': sum(year['total'] or 0 for year in years) if all(year['total'] is not None for year in years) else None,
                'processed': sum(year['processed'] for year in years),
                'succeeded': sum(year['succeeded'] for year in years),
                'failed': sum(year['failed'] for year in years),
                'error': batch['error'], 'years': years,
            })
        with frame_store.connect() as db:
            rows = db.execute('SELECT data,created FROM jobs ORDER BY created DESC').fetchall()
        for row in rows:
            job = json.loads(row['data'])
            job_id = job['job_id']
            items.append({
                'id': job_id, 'kind': 'frame', 'status': job['status'],
                'stage': job.get('stage'), 'created': row['created'],
                'total': job['total'], 'processed': job['processed'],
                'succeeded': job['succeeded'], 'failed': job['failed'],
                'error': job.get('error'),
                'download_url': f'/api/matched-frame/jobs/{job_id}/download'
                if job['status'] in ('completed', 'partial') and job['succeeded'] and
                   (frame_store.job_dir(job_id) / 'result.zip').is_file() else None,
            })
        esri = esri_provider()
        with esri._lock:
            jobs = list(esri._jobs.values())
            for job in jobs:
                job_id = job['job_id']
                progress = job.get('progress') or {}
                items.append({
                    'id': job_id, 'kind': 'esri', 'status': job['status'],
                    'created': job.get('created', 0), 'parameters': {'dataset': 'Esri World Imagery'},
                    'total': progress.get('total'), 'processed': progress.get('processed', 0),
                    'succeeded': progress.get('succeeded', 0), 'failed': progress.get('failed', 0),
                    'error': job.get('error'),
                    'download_url': f'/api/esri/jobs/{job_id}/download'
                    if job['status'] in ('completed', 'partial') and
                       (esri.root / job_id / 'imagery.tif').is_file() else None,
                })
        download_store = download_store_provider()
        if download_store is not None:
            with download_store.connect() as db:
                rows = db.execute('SELECT payload FROM download_jobs ORDER BY created_at DESC').fetchall()
            for row in rows:
                job = json.loads(row['payload'])
                job_id = job['id']
                available = (job['state'] == 'completed' and
                             download_store.get_artifact(job.get('artifact_key')) is not None)
                state = 'expired' if job['state'] == 'completed' and not available else job['state']
                items.append({
                    'id': job_id, 'kind': 'sentinel2', 'status': state,
                    'stage': job.get('stage'), 'created': job.get('created_at', 0),
                    'parameters': {'dataset': 'sentinel2'},
                    'total': job.get('tiles_total'), 'processed': job.get('tiles_verified', 0),
                    'succeeded': job.get('tiles_verified', 0), 'failed': 0,
                    'error': job.get('error_code'),
                    'download_url': f'/api/download-jobs/{job_id}/file' if available else None,
                })
        with series_store._connect() as db:
            hidden = {(row['kind'], row['id']) for row in db.execute('SELECT kind,id FROM hidden_tasks')}

        def order(item):
            created = item.get('created') or 0
            if item['kind'] == 'transfer' and item['status'] == 'queued':
                return (1, created)
            return (0 if item['status'] in ACTIVE else 2, -created)

        return sorted((item for item in items if (item['kind'], item['id']) not in hidden),
                      key=order)

    def find(kind, job_id):
        return next((item for item in all_tasks() if item['kind'] == kind and item['id'] == job_id), None)

    @api.get('/api/tasks')
    def list_tasks(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
                   status: str = Query('all', pattern='^(all|active|finished)$')):
        items = all_tasks()
        counts = {'active': sum(item['status'] in ACTIVE for item in items),
                  'finished': sum(item['status'] not in ACTIVE for item in items)}
        if status != 'all':
            items = [item for item in items if (item['status'] in ACTIVE) == (status == 'active')]
        return {'items': items[offset:offset + limit], 'total': len(items), 'offset': offset,
                'limit': limit, 'counts': counts}

    @api.post('/api/tasks/{kind}/{job_id}/cancel')
    def cancel_task(kind: str, job_id: str):
        if kind == 'transfer':
            return transfers.cancel(job_id)
        job = find(kind, job_id)
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        if kind == 'timeseries':
            return series_store.cancel_job(job_id)
        if kind == 'annual':
            return batch_provider().pause(job_id)
        if kind == 'frame':
            return frame_store.cancel_job(job_id)
        if kind == 'esri':
            return esri_provider().cancel(job_id)
        if kind == 'sentinel2':
            return download_service_provider().cancel(job_id)
        raise HTTPException(409, 'Tác vụ này chưa hỗ trợ hủy.')

    @api.get('/api/tasks/{kind}/{job_id}/files')
    def task_file_size(kind: str, job_id: str):
        job = find(kind, job_id)
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        if kind == 'timeseries':
            directory = series_store._job_dir(job_id)
        elif kind == 'annual':
            directory = batch_store.directory(job_id)
        else:
            raise HTTPException(409, 'Loại tác vụ này chưa hỗ trợ xóa tệp kết quả.')
        if directory.is_symlink():
            raise HTTPException(409, 'Thư mục tác vụ không hợp lệ.')
        count = size = 0
        for root, _, names in os.walk(directory, followlinks=False):
            for name in names:
                path = os.path.join(root, name)
                if os.path.islink(path):
                    continue
                try:
                    size += os.path.getsize(path)
                    count += 1
                except FileNotFoundError:
                    continue
        return {'bytes': size, 'files': count}

    @api.delete('/api/tasks/{kind}/{job_id}')
    def hide_task(kind: str, job_id: str, delete_files: bool = False):
        job = find(kind, job_id)
        if job is None:
            raise HTTPException(404, 'Không tìm thấy tác vụ.')
        if job['status'] in ACTIVE:
            raise HTTPException(409, 'Hãy dừng tác vụ trước khi xóa khỏi danh sách.')
        if delete_files and kind not in ('timeseries', 'annual'):
            raise HTTPException(409, 'Loại tác vụ này chưa hỗ trợ xóa tệp kết quả.')
        if kind == 'transfer':
            transfers.hide(job_id)
        elif kind == 'timeseries':
            try:
                series_store.hide_job(job_id, delete_files=delete_files)
            except BusyError as exc:
                raise HTTPException(409, str(exc)) from None
        elif kind == 'annual':
            try:
                batch_store.hide_batch(job_id, delete_files=delete_files)
            except BusyError as exc:
                raise HTTPException(409, str(exc)) from None
        else:
            with series_store._connect() as db:
                db.execute('INSERT OR IGNORE INTO hidden_tasks VALUES (?,?)', (kind, job_id))
        return {'hidden': True}
