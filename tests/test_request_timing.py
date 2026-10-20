"""GEE timing diagnostics preserve existing response payloads and error behavior."""
import asyncio
import unittest
from contextlib import ExitStack
from unittest import mock

from fastapi.testclient import TestClient

from app import main
from app.geo import Bbox
from app.imagery import SceneInfo
from app.request_timing import SearchTimingMiddleware, measure_call


class TestSearchTiming(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(main, '_ensure_ee'))
        self.stack.enter_context(mock.patch.object(main, '_resolve_selection', return_value=(object(), Bbox(106, 9, 108, 11), None)))
        self.scene = SceneInfo('sentinel2', 'collection', 'scene', '2026-10-01', 5, object())
        self.stack.enter_context(mock.patch.object(main, 'best_scene', return_value=self.scene))
        self.preview = self.stack.enter_context(mock.patch.object(main, 'preview_rgb', return_value='https://example.test/preview'))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.body = dict(dataset='sentinel2', lon=107, lat=10, radius_m=2000,
                         start_date='2026-10-01', end_date='2026-10-02')

    def test_search_has_phase_headers_without_changing_payload(self):
        with self.assertLogs('uvicorn.error', level='INFO') as logs:
            response = self.client.post('/api/scene', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        header = response.headers['server-timing']
        for phase in ('ee_init', 'selection', 'scene_search', 'preview_url', 'total'):
            self.assertIn(phase + ';dur=', header)
        self.assertRegex(response.headers['x-search-request-id'], r'^[a-f0-9]{12}$')
        self.assertEqual(response.json()['scene_id'], 'scene')
        self.assertNotIn('timings', response.json())
        self.assertNotIn('example.test', '\n'.join(logs.output))
        self.assertNotIn('preview_url;', '\n'.join(logs.output))

    def test_preview_failure_remains_nonfatal_and_is_measured(self):
        self.preview.side_effect = RuntimeError('private URL or credentials')
        with self.assertLogs('uvicorn.error', level='INFO') as logs:
            response = self.client.post('/api/scene', json=self.body)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()['preview_url'])
        self.assertIn('preview_url;dur=', response.headers['server-timing'])
        self.assertNotIn('private URL', '\n'.join(logs.output))

    def test_query_failure_and_validation_have_total_timing(self):
        with mock.patch.object(main, 'best_scene', side_effect=RuntimeError('failed')):
            response = self.client.post('/api/scene', json=self.body)
        self.assertEqual(response.status_code, 502)
        self.assertIn('scene_search;dur=', response.headers['server-timing'])
        self.assertIn('total;dur=', response.headers['server-timing'])
        response = self.client.post('/api/scene', json={})
        self.assertEqual(response.status_code, 422)
        self.assertIn('total;dur=', response.headers['server-timing'])

    def test_plan_has_separate_stage(self):
        with mock.patch.object(main, 'composite_plan', return_value={'scene_count': 1}):
            response = self.client.post('/api/composite-plan', json={**self.body, 'selection_mode': 'province', 'province_id': 'test'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('composite_plan;dur=', response.headers['server-timing'])
        self.assertNotIn('scene_search;dur=', response.headers['server-timing'])

    def test_unrelated_route_has_no_timing_header(self):
        response = self.client.get('/nonexistent')
        self.assertNotIn('server-timing', response.headers)

    def test_concurrent_requests_do_not_share_metrics(self):
        async def check():
            headers = {}
            async def app(scope, receive, send):
                measure_call(scope['metric'], lambda: None)
                await asyncio.sleep(0)
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b''})
            middleware = SearchTimingMiddleware(app)
            async def run(metric):
                async def receive():
                    return {'type': 'http.request', 'body': b''}
                async def send(message):
                    if message['type'] == 'http.response.start':
                        headers[metric] = dict(message['headers'])[b'server-timing'].decode()
                await middleware({'type': 'http', 'method': 'POST', 'path': '/api/scene', 'metric': metric}, receive, send)
            await asyncio.gather(run('first'), run('second'))
            self.assertIn('first;dur=', headers['first'])
            self.assertNotIn('second;dur=', headers['first'])
            self.assertIn('second;dur=', headers['second'])
        asyncio.run(check())
