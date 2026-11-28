"""Task history, cancellation and queue contracts using an isolated store."""
import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.task_center import DownloadTransferMiddleware, DownloadTransfers
from app.timeseries import TimeSeriesService, TimeSeriesStore, BusyError
from app.timeseries_batches import BatchStore, BatchService


class DownloadTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_queued_download_without_starting_it(self):
        transfers = DownloadTransfers()
        first_started = asyncio.Event()
        finish_first = asyncio.Event()
        started = []
        emitted = []

        async def app(scope, receive, send):
            started.append(scope['path'])
            first_started.set()
            await finish_first.wait()
            await send({'type': 'http.response.start', 'status': 200})
            await send({'type': 'http.response.body', 'body': b'done'})

        async def receive():
            await asyncio.Future()

        async def send(message):
            emitted.append(message)

        middleware = DownloadTransferMiddleware(app, transfers)
        first = asyncio.create_task(middleware(
            {'type': 'http', 'method': 'GET', 'path': '/api/download/first'}, receive, send))
        await first_started.wait()
        second = asyncio.create_task(middleware(
            {'type': 'http', 'method': 'GET', 'path': '/api/download/second'}, receive, send))
        await asyncio.sleep(0)
        try:
            queued = transfers.snapshot()[1]
            transfers.cancel(queued['id'])
            await asyncio.wait_for(second, 2)
            self.assertEqual(started, ['/api/download/first'])
            self.assertEqual(transfers.snapshot()[1]['status'], 'cancelled')
            self.assertEqual(emitted[0]['status'], 409)
        finally:
            finish_first.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)

    async def test_cancel_stream_never_sends_remaining_bytes(self):
        transfers = DownloadTransfers()
        first_chunk = asyncio.Event()
        next_chunk = asyncio.Event()
        emitted = []

        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200})
            await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
            first_chunk.set()
            await next_chunk.wait()
            await send({'type': 'http.response.body', 'body': b'last'})

        async def send(message):
            emitted.append(message)

        middleware = DownloadTransferMiddleware(app, transfers)
        request = asyncio.create_task(middleware(
            {'type': 'http', 'method': 'GET', 'path': '/api/download/first'}, None, send))
        await first_chunk.wait()
        try:
            job, = transfers.snapshot()
            self.assertEqual(transfers.cancel(job['id'])['status'], 'cancelling')
        finally:
            next_chunk.set()
            await asyncio.wait_for(request, 2)
        self.assertEqual(emitted[-1]['body'], b'first')
        self.assertTrue(emitted[-1]['more_body'])
        job, = transfers.snapshot()
        self.assertEqual((job['status'], job['bytes_sent']), ('cancelled', 5))
        self.assertFalse(middleware.slot.locked())

    async def test_direct_downloads_queue_until_previous_file_is_sent(self):
        transfers = DownloadTransfers()
        first_preparing = asyncio.Event()
        first_transfer = asyncio.Event()
        send_first = asyncio.Event()
        finish_first = asyncio.Event()
        started = []

        async def app(scope, receive, send):
            name = scope['path'].rsplit('/', 1)[1]
            started.append(name)
            if name == 'first':
                first_preparing.set()
                await send_first.wait()
            await send({'type': 'http.response.start', 'status': 200, 'headers': [(b'content-length', b'4')]})
            await send({'type': 'http.response.body', 'body': b'a', 'more_body': True})
            if name == 'first':
                first_transfer.set()
                await finish_first.wait()
            await send({'type': 'http.response.body', 'body': b'bcd'})

        async def receive():
            await asyncio.Future()

        async def send(message):
            pass

        middleware = DownloadTransferMiddleware(app, transfers)
        first = asyncio.create_task(middleware(
            {'type': 'http', 'method': 'GET', 'path': '/api/download/first'}, receive, send))
        await first_preparing.wait()
        second = asyncio.create_task(middleware(
            {'type': 'http', 'method': 'GET', 'path': '/api/download/second'}, receive, send))
        await asyncio.sleep(0)
        self.assertEqual([item['stage'] for item in transfers.snapshot()], ['preparing', 'queued'])
        self.assertEqual(started, ['first'])
        send_first.set()
        await first_transfer.wait()
        self.assertEqual([(item['stage'], item['bytes_sent']) for item in transfers.snapshot()],
                         [('transferring', 1), ('queued', 0)])
        finish_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual(started, ['first', 'second'])
        self.assertEqual([item['stage'] for item in transfers.snapshot()], ['completed', 'completed'])

    async def test_browser_redirect_uses_same_slot_and_reports_stream_total(self):
        transfers = DownloadTransfers()
        calls = []
        emitted = []

        async def app(scope, receive, send):
            calls.append(scope['path'])
            if len(calls) == 1:
                await send({'type': 'http.response.start', 'status': 303,
                            'headers': [(b'location', b'/api/s2/bands?action=download&product_id=sample')]})
                await send({'type': 'http.response.body', 'body': b''})
            else:
                self.assertEqual(len(transfers.snapshot()), 1)
                self.assertEqual(scope['query_string'], b'action=download&product_id=sample')
                await send({'type': 'http.response.start', 'status': 200,
                            'headers': [(b'x-s3-total-bytes', b'2')]})
                await send({'type': 'http.response.body', 'body': b'zip'})

        async def send(message):
            emitted.append(message)

        await DownloadTransferMiddleware(app, transfers)(
            {'type': 'http', 'method': 'GET', 'path': '/api/scene-catalog/id/download/0',
             'headers': [(b'sec-fetch-mode', b'navigate')]}, None, send)
        self.assertEqual(calls, ['/api/scene-catalog/id/download/0', '/api/s2/bands'])
        self.assertEqual(emitted[0]['status'], 200)
        item, = transfers.snapshot()
        self.assertEqual((item['stage'], item['bytes_sent'], item['bytes_total'], item['bytes_estimated']),
                         ('completed', 3, 2, True))

    async def test_bytes_and_stage_are_visible_before_request_returns(self):
        transfers = DownloadTransfers()

        async def app(scope, receive, send):
            self.assertEqual(transfers.snapshot()[0]['stage'], 'preparing')
            self.assertNotIn('http.response.pathsend', scope['extensions'])
            await send({'type': 'http.response.start', 'status': 200,
                        'headers': [(b'content-length', b'5'),
                                    (b'content-disposition', b'attachment; filename="scene.tif"')]})
            await send({'type': 'http.response.body', 'body': b'ab', 'more_body': True})
            item = transfers.snapshot()[0]
            self.assertEqual((item['status'], item['stage'], item['bytes_sent'], item['bytes_total']),
                             ('running', 'transferring', 2, 5))
            self.assertEqual(item['filename'], 'scene.tif')
            await send({'type': 'http.response.body', 'body': b'cde'})

        async def collect(message):
            sent.append(message)

        sent = []
        scope = {'type': 'http', 'method': 'GET', 'path': '/api/download/sentinel2',
                 'extensions': {'http.response.pathsend': {}}}
        await DownloadTransferMiddleware(app, transfers)(scope, None, collect)
        self.assertEqual(transfers.snapshot()[0]['status'], 'completed')
        self.assertEqual(b''.join(message.get('body', b'') for message in sent), b'abcde')

    async def test_preview_and_redirect_do_not_remain_in_history(self):
        transfers = DownloadTransfers()

        async def redirect(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 307, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        async def send(message):
            pass

        middleware = DownloadTransferMiddleware(redirect, transfers)
        await middleware({'type': 'http', 'method': 'GET', 'path': '/api/scene-catalog/id/download/0'}, None, send)
        self.assertEqual(transfers.snapshot(), [])

        async def preview(scope, receive, send):
            self.assertEqual(transfers.snapshot(), [])

        await DownloadTransferMiddleware(preview, transfers)(
            {'type': 'http', 'method': 'GET', 'path': '/api/s2/bands', 'query_string': b'action=list'},
            None, send)


class TaskCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = BatchStore(self.root)
        self.service = TimeSeriesService(self.store)
        self.addCleanup(self.service.shutdown)
        self.batch = BatchService(self.store, self.service)
        self.params = {'dataset': 'sentinel2', 'start_date': '2024-01-01',
                       'end_date': '2024-01-02', 'bands': ['B2'],
                       'scale_m': 10, 'bbox': {'west': 0, 'east': .01, 'south': 0, 'north': .01}}
        self.scenes = [{'asset_id': 'one', 'timestamp': '2024-01-01T00:00:00Z'}]

    def test_queue_spans_regular_and_annual_jobs_and_runs_in_order(self):
        first = self.store.create_search(self.params, self.scenes)
        second = self.store.create_search(self.params, self.scenes)
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        executed = []
        def normal(job_id):
            executed.append(job_id)
            if len(executed) == 1:
                entered.set()
                release.wait(5)
            else:
                finished.set()
        with patch.object(self.service, 'run_timeseries_job', side_effect=normal), \
             patch.object(self.batch, 'run_batch', side_effect=lambda job_id: executed.append(job_id)):
            one = self.service.start_job(first['search_id'])
            self.assertTrue(entered.wait(2))
            annual = self.batch.start(self.params)
            two = self.service.start_job(second['search_id'])
            self.assertEqual(self.store.get_job(two['job_id'])['status'], 'queued')
            release.set()
            self.assertTrue(finished.wait(2))
            self.service.shutdown()
        self.assertEqual(executed, [one['job_id'], annual['batch_id'], two['job_id']])

    def test_cancel_pending_and_remove_from_history_preserves_files(self):
        search = self.store.create_search(self.params, self.scenes)
        job = self.store.create_job(search['search_id'])
        folder = self.store._job_dir(job['job_id'])
        folder.mkdir()
        artifact = folder / 'result.tif'
        artifact.write_bytes(b'kept')
        self.assertEqual(self.store.cancel_job(job['job_id'])['status'], 'cancelled')
        self.store.hide_job(job['job_id'])
        self.assertTrue(artifact.exists())
        self.assertEqual(self.store.list_jobs(), [])

    def test_running_cancellation_remains_active_until_worker_acknowledges(self):
        search = self.store.create_search(self.params, self.scenes)
        job = self.store.create_job(search['search_id'])
        self.store._update_job(job['job_id'], status='running')
        self.assertEqual(self.store.cancel_job(job['job_id'])['status'], 'cancelling')
        with self.assertRaises(BusyError):
            self.store.hide_job(job['job_id'])
        self.assertTrue(self.store.cancel_requested(job['job_id']))

    def test_live_owner_lock_prevents_second_recovery(self):
        from app.task_lock import TaskLock
        owner, other = TaskLock(self.root), TaskLock(self.root)
        try:
            self.assertTrue(owner.acquire())
            self.assertFalse(other.acquire())
        finally:
            owner.release()
            other.release()
        self.assertTrue(other.acquire())
        other.release()

    def test_task_center_lists_and_hides_without_removing_output(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.task_center import register_task_center
        from app.frame_store import FrameStore
        from app.esri import EsriService
        frame = FrameStore(self.root / 'frame')
        esri = EsriService(self.root / 'esri')
        self.addCleanup(esri.shutdown)
        app = FastAPI()
        register_task_center(app, self.store, self.store, frame, lambda: esri,
                             lambda: None, lambda: self.batch, lambda: None)
        client = TestClient(app)
        search = self.store.create_search(self.params, self.scenes)
        job = self.store.create_job(search['search_id'])
        job_id = job['job_id']
        folder = self.store._job_dir(job_id)
        folder.mkdir()
        (folder / 'result.tif').write_bytes(b'data')
        data = client.get('/api/tasks').json()
        self.assertEqual([(row['kind'], row['id']) for row in data['items']], [('timeseries', job_id)])
        self.assertEqual(client.delete('/api/tasks/timeseries/' + job_id).status_code, 409)
        self.assertEqual(client.post('/api/tasks/timeseries/' + job_id + '/cancel').json()['status'], 'cancelled')
        self.assertEqual(client.delete('/api/tasks/timeseries/' + job_id).status_code, 200)
        self.assertEqual(client.get('/api/tasks').json()['items'], [])
        self.assertTrue((folder / 'result.tif').exists())

        from fastapi.responses import StreamingResponse
        app.get('/api/s2/s3/download')(lambda: StreamingResponse(iter([b'da', b'ta'])))
        self.assertEqual(client.get('/api/s2/s3/download').content, b'data')
        transfer, = client.get('/api/tasks?status=finished').json()['items']
        self.assertEqual((transfer['kind'], transfer['status'], transfer['bytes_sent'],
                          transfer['bytes_total']), ('transfer', 'completed', 4, None))
        self.assertEqual(client.delete('/api/tasks/transfer/' + transfer['id']).status_code, 200)
        self.assertEqual(client.get('/api/tasks').json()['items'], [])

    def test_delete_files_requires_terminal_and_no_download_lease(self):
        from app.task_files import artifact_lease
        search = self.store.create_search(self.params, self.scenes)
        job = self.store.create_job(search['search_id'])
        job_id = job['job_id']
        folder = self.store._job_dir(job_id)
        folder.mkdir()
        (folder / 'result.tif').write_bytes(b'data')
        with self.assertRaises(BusyError):
            self.store.hide_job(job_id, delete_files=True)
        self.store.cancel_job(job_id)
        with artifact_lease(folder):
            with self.assertRaises(BusyError):
                self.store.hide_job(job_id, delete_files=True)
        self.assertTrue(folder.exists())
        self.store.hide_job(job_id, delete_files=True)
        self.assertFalse(folder.exists())

    def test_frame_cancellation_waits_for_worker_and_blocks_removal(self):
        from app.frame_store import FrameStore
        frame = FrameStore(self.root / 'frame')
        preflight = {'preflight_id': 'a', 'parameters': {},
                     'scenes': [{'asset_id': 'image'}], 'digest': 'digest'}
        job_id = frame.create_job(preflight)['job_id']
        self.assertTrue(frame.claim(job_id))
        self.assertEqual(frame.cancel_job(job_id)['status'], 'cancelling')
        self.assertTrue(frame.finish_if_cancelled(job_id))
        self.assertEqual(frame.get_job(job_id)['status'], 'cancelled')

    def test_listing_batches_does_not_extend_retention_or_offer_missing_files(self):
        batch = self.store.create_batch(self.params)
        batch_id = batch['batch_id']
        with self.store._connect() as db:
            db.execute('UPDATE ts_batches SET activity=123 WHERE id=?', (batch_id,))
            db.execute('UPDATE ts_years SET zip_ready=1,csv_ready=1 WHERE batch_id=?', (batch_id,))
        listed = self.store.list_batches()[0]
        with self.store._connect() as db:
            activity = db.execute('SELECT activity FROM ts_batches WHERE id=?', (batch_id,)).fetchone()[0]
        self.assertEqual(activity, 123)
        self.assertIsNone(listed['years'][0]['zip_url'])
        self.assertIsNone(listed['years'][0]['csv_url'])

    def test_batch_pause_at_completion_is_not_lost(self):
        batch_id = self.store.create_batch(self.params)['batch_id']
        self.store.update(batch_id, status='running')
        self.store.pause(batch_id)
        self.store.finish_batch(batch_id, 'completed')
        self.assertEqual(self.store.get_batch(batch_id)['status'], 'paused')

    def test_retention_cleanup_does_not_remove_a_streamed_artifact(self):
        from app.task_files import artifact_lease
        search = self.store.create_search(self.params, self.scenes)
        job_id = self.store.create_job(search['search_id'])['job_id']
        self.store.cancel_job(job_id)
        folder = self.store._job_dir(job_id)
        folder.mkdir()
        (folder / 'result').write_bytes(b'data')
        with self.store._connect() as db:
            db.execute('UPDATE jobs SET activity=0 WHERE id=?', (job_id,))
        with artifact_lease(folder):
            self.assertEqual(self.store.cleanup_expired_jobs(now=10**10), 0)
            self.assertTrue(folder.exists())
        self.assertEqual(self.store.cleanup_expired_jobs(now=10**10), 1)

    def test_pausing_queued_batch_then_resuming_runs_once(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def block():
            entered.set()
            release.wait(5)
        def run(batch_id):
            calls.append(batch_id)
            finished.set()
        self.service.submit_task('block', block)
        self.assertTrue(entered.wait(2))
        try:
            with patch.object(self.batch, 'run_batch', side_effect=run):
                batch_id = self.batch.start(self.params)['batch_id']
                self.assertEqual(self.batch.pause(batch_id)['status'], 'paused')
                self.assertEqual(self.batch.resume(batch_id)['status'], 'queued')
                release.set()
                self.assertTrue(finished.wait(2))
                self.service.shutdown()
            self.assertEqual(calls, [batch_id])
        finally:
            release.set()

    def test_annual_cleanup_respects_active_download_lease(self):
        from app.task_files import artifact_lease
        batch_id = self.store.create_batch(self.params)['batch_id']
        self.store.update(batch_id, status='completed')
        folder = self.store.directory(batch_id)
        folder.mkdir(parents=True)
        (folder / 'result').write_bytes(b'data')
        with self.store._connect() as db:
            db.execute('UPDATE ts_batches SET activity=0 WHERE id=?', (batch_id,))
        with artifact_lease(folder):
            self.assertEqual(self.store.cleanup(now=10**10), 0)
            self.assertTrue(folder.exists())
        self.assertEqual(self.store.cleanup(now=10**10), 1)

    def test_restart_queue_order_uses_latest_resume_time(self):
        with patch('app.timeseries_batches.time.time', return_value=100):
            batch_id = self.store.create_batch(self.params)['batch_id']
            self.store.pause(batch_id)
        with patch('app.timeseries.time.time', return_value=200):
            search_id = self.store.create_search(self.params, self.scenes)['search_id']
            job_id = self.store.create_job(search_id)['job_id']
        with patch('app.timeseries_batches.time.time', return_value=300):
            self.store.prepare_resume(batch_id)
        self.assertEqual([(item['kind'], item['id']) for item in self.store.queued_tasks()],
                         [('timeseries', job_id), ('annual', batch_id)])


if __name__ == '__main__':
    unittest.main()
