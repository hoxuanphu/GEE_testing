import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
from PIL import Image
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from shapely.geometry import MultiPolygon, box, mapping

from app.umbra_api import register_umbra
from app.umbra_catalog import Object, PREFIXES
from app.task_center import DownloadTransferMiddleware, DownloadTransfers
from app.umbra_transfer import _chunks, download, thumbnail


class UmbraAPITests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        app = FastAPI()
        self.catalog, self.store = register_umbra(app, Path(temp.name), None)
        self.client = TestClient(app)
        self.request = dict(selection_mode='rectangle', center_lon=0, center_lat=0,
                            width_m=2000, height_m=2000, rotation_deg=45,
                            start_date='2026-01-13', end_date='2026-01-13')

    def populate(self, names=('selected', 'second', 'bbox-only'), footprints=None):
        paths = {name: f'{PREFIXES[0]}{name}/scene' for name in names}
        footprints = footprints or {
            'selected': box(-.001, -.001, .001, .001),
            'second': box(-.002, -.002, .002, .002),
            'bbox-only': box(.011, .011, .012, .012),
        }

        def metadata(obj, **_):
            name = obj.path.split('/')[1]
            return {'type': 'Feature', 'id': name, 'geometry': mapping(footprints[name]),
                    'properties': {'datetime': '2026-01-13T23:59:59.999Z' if name == 'selected'
                                   else '2026-01-13T15:00:00Z', 'platform': 'Umbra-07'}}

        objects = [Object(p + suffix, 4, '"v"')
                   for p in paths.values() for suffix in ('.stac.v2.json', '_GEC.tif')]
        with (patch('app.umbra_catalog.list_objects', side_effect=lambda _, p: objects if p == PREFIXES[0] else []),
              patch('app.umbra_catalog.get_sidecar', side_effect=metadata)):
            self.assertTrue(self.catalog.refresh())

    def test_exact_rotated_roi_inclusive_end_and_frozen_download(self):
        self.assertEqual(self.client.post('/api/umbra/search', json=self.request).status_code, 503)
        self.populate()
        result = self.client.post('/api/umbra/search', json=self.request).json()
        self.assertEqual([s['item_id'] for s in result['scenes']], ['selected', 'second'])
        selected, second = result['scenes']
        self.populate(('selected',))
        page = self.client.get(f"/api/umbra/search/{result['search_id']}?offset=1&limit=1").json()
        self.assertEqual(page['scenes'], [second])
        obj = Mock(status_code=200, headers={'Content-Length': '4', 'ETag': '"v"'})
        obj.iter_content.return_value = iter([b'data'])
        with patch('app.umbra_transfer.requests.get', return_value=obj) as get:
            response = self.client.get(second['download_url'])
        self.assertEqual(response.content, b'data')
        self.assertIn('open-data/second/scene_GEC.tif', get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs['headers']['If-Match'], '"v"')
        obj.close.assert_called()
        response = self.client.get(selected['metadata_url'])
        self.assertIn('CC BY 4.0', response.json()['attribution'])
        self.assertIn('selected/scene_GEC.tif', response.json()['download_source'])
        self.assertEqual(self.client.get('/api/umbra/search/missing').status_code, 404)
        self.store.cleanup_expired_jobs(now=10**11)
        self.assertEqual(self.client.get(second['download_url']).status_code, 404)

    def test_changed_version_or_wrong_length_fails_before_streaming(self):
        self.populate(('selected',))
        scene, = self.client.post('/api/umbra/search', json=self.request).json()['scenes']
        for code, headers, expected in [(412, {}, 409), (404, {}, 409),
                (200, {'Content-Length': '4', 'ETag': '"changed"'}, 409),
                (200, {'Content-Length': '3', 'ETag': '"v"'}, 502)]:
            obj = Mock(status_code=code, headers=headers)
            with patch('app.umbra_transfer.requests.get', return_value=obj):
                self.assertEqual(self.client.get(scene['download_url']).status_code, expected)
            obj.close.assert_called_once()

    def test_truncated_transfer_raises_instead_of_finishing(self):
        response = Mock()
        response.iter_content.return_value = iter([b'12'])
        with self.assertRaises(OSError):
            list(_chunks(response, 4))

    def test_empty_and_stale_catalog_are_distinct(self):
        self.populate(('selected',))
        result = self.client.post('/api/umbra/search', json=dict(self.request, center_lon=5)).json()
        self.assertEqual(result['total'], 0)
        self.assertEqual(result['parameters']['catalog_state'], 'ready')
        with (patch('app.umbra_catalog.list_objects', side_effect=OSError('network')),
              self.assertLogs('app.umbra_catalog', 'ERROR')):
            self.assertFalse(self.catalog.refresh())
        self.assertEqual(self.client.get('/api/umbra/catalog').json()['state'], 'stale')

    def test_vector_multipolygon_preserves_both_sides_of_the_dateline(self):
        self.populate(footprints={'selected': box(179.3, 10, 179.7, 11),
                                  'second': box(-179.8, 10, -179.5, 11),
                                  'bbox-only': box(-.1, 10, .1, 11)})
        roi = mapping(MultiPolygon([box(179, 9, 180, 12), box(-180, 9, -179, 12)]))
        with patch('app.umbra_api.vector_area', return_value=SimpleNamespace(geometry=roi)):
            response = self.client.post('/api/umbra/search', json={
                'selection_mode': 'vector', 'vector_roi_id': '12345678-1234-1234-1234-123456789abc',
                'start_date': '2026-01-13', 'end_date': '2026-01-13',
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual([scene['item_id'] for scene in response.json()['scenes']], ['selected', 'second'])

    def test_thumbnail_reads_ranges_instead_of_entire_image(self):
        with MemoryFile() as memory:
            with memory.open(driver='GTiff', width=2048, height=2048, count=1, dtype='uint8',
                             tiled=True, blockxsize=256, blockysize=256) as dataset:
                dataset.write(np.random.default_rng(3).integers(0, 256, (2048, 2048), dtype=np.uint8), 1)
                dataset.build_overviews([2, 4, 8], Resampling.average)
            payload = memory.read()
        ranges = []

        def get(_, *, headers, **kwargs):
            start, stop = map(int, headers['Range'].removeprefix('bytes=').split('-'))
            body = payload[start:stop + 1]
            ranges.append(len(body))
            response = Mock(status_code=206, headers={'ETag': '"v"',
                'Content-Length': str(len(body)), 'Content-Range': f'bytes {start}-{stop}/{len(payload)}'})
            response.iter_content.return_value = iter([body])
            context = Mock()
            context.__enter__ = Mock(return_value=response)
            context.__exit__ = Mock()
            return context

        with patch('app.umbra_transfer.requests.Session') as session:
            session.return_value.__enter__.return_value.get.side_effect = get
            result = thumbnail('open-data/test/overview_GEC.tif', '"v"', len(payload))
        self.assertEqual(Image.open(BytesIO(result)).size, (512, 512))
        self.assertTrue(0 < sum(ranges) < len(payload) // 2)


class UmbraMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_stops_native_transfer_and_closes_s3_response(self):
        obj = Mock(status_code=200, headers={'Content-Length': '4', 'ETag': '"v"'})
        obj.iter_content.return_value = iter([b'12', b'34'])
        scene = {'object_path': 'open-data/test/scene_GEC.tif', 'size_bytes': 4,
                 'etag': '"v"', 'filename': 'scene_GEC.tif'}
        transfers = DownloadTransfers()
        events = []

        async def receive():
            raise AssertionError('Transfer with ASGI 2.4 does not need a receive loop')

        async def send(message):
            events.append(message)
            if message.get('body') == b'12':
                transfers.cancel(transfers.snapshot()[0]['id'])

        async def app(scope, receive, send):
            await download(scene)(scope, receive, send)

        middleware = DownloadTransferMiddleware(app, transfers)
        scope = {'type': 'http', 'method': 'GET', 'asgi': {'spec_version': '2.4'},
                 'path': '/api/umbra/search/snapshot/scenes/0/download',
                 'query_string': b'attempt=' + b'a' * 32}
        with patch('app.umbra_transfer.requests.get', return_value=obj) as upstream:
            await middleware(scope, receive, send)
            self.assertEqual(transfers.snapshot()[0]['status'], 'cancelled')
            self.assertEqual(transfers.snapshot()[0]['bytes_sent'], 2)
            self.assertEqual([m.get('body') for m in events[1:]], [b'12'])
            events.clear()
            await middleware(scope, receive, send)
            self.assertEqual(events[0]['status'], 409)
            upstream.assert_called_once()
            obj.close.assert_called_once()
            obj.iter_content.return_value = iter([b'data'])
            events.clear()
            await middleware(dict(scope, query_string=b'attempt=' + b'b' * 32), receive, send)
            self.assertEqual(events[0]['status'], 200)
            self.assertEqual([item['status'] for item in transfers.snapshot()], ['cancelled', 'completed'])
