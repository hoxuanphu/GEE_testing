"""Metadata-only graphs, bounded provider calls and truthful search failures."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ee
from ee.apitestcase import ApiTestCase
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError

from app import imagery, main
from app import frame_search
from app.timeseries import TimeSeriesStore


class MetadataGraphTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)

    def test_search_graph_has_no_pixel_processing_for_any_source(self):
        for dataset in ('sentinel2', 'landsat', 's1gee', 'nicfi', 'dynamic_world'):
            with self.subTest(dataset=dataset), mock.patch.object(
                    imagery, '_compute_timeseries_metadata', return_value=[]) as compute:
                imagery.list_timeseries_scenes(dataset, 105, 20, '2024-01-01', '2024-02-01')
                graph = json.dumps(ee.serializer.encode(compute.call_args.args[0]))
                for operation in ('Image.select', 'Image.addBands', 'Image.multiply',
                                  'Image.divide', 'Image.clamp', 'Image.updateMask'):
                    self.assertNotIn(operation, graph)
                self.assertIn('Geometry.contains', graph)
                self.assertIn('Element.toDictionary', graph)

    def test_rotated_roi_and_catalogue_cloud_filters_survive_metadata_query(self):
        roi = ee.Geometry.Polygon([[[105, 20], [105.001, 20.002], [105.003, 20.001],
                                    [105.002, 19.999], [105, 20]]], geodesic=False)
        with mock.patch.object(imagery, '_compute_timeseries_metadata', return_value=[]) as compute:
            imagery.list_timeseries_scenes('sentinel2', 105, 20, '2024-01-01',
                                           '2024-02-01', roi=roi, cloud_min=0,
                                           cloud_max=40, platform='C')
        graph = json.dumps(ee.serializer.encode(compute.call_args.args[0]))
        for value in ('105.003', '19.999', 'CLOUDY_PIXEL_PERCENTAGE', 'Sentinel-2C',
                      'Filter.not', 'Filter.greaterThan', 'Filter.lessThan'):
            self.assertIn(value, graph)
        self.assertNotIn('Geometry.contains', graph)

    def test_response_cap_requests_one_extra_scene_without_unbounded_size(self):
        with mock.patch.object(imagery, '_compute_timeseries_metadata', return_value=[]) as compute:
            imagery.list_timeseries_scenes('sentinel2', 105, 20, '2019-01-01', '2027-01-01')
        graph = json.dumps(ee.serializer.encode(compute.call_args.args[0]))
        self.assertNotIn('Collection.size', graph)
        self.assertIn('20001', graph)


class MetadataTransportTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)
        self.now = 0
        self.calls = []
        self.effects = []
        self.rpc_class = frame_search._RPC
        owner = self

        class OfflineRPC(owner.rpc_class):
            def __init__(self, timeout, deadline=None):
                self.timeout, self.deadline = timeout, deadline
                self.project = 'projects/offline'
                self.session = mock.Mock()
                self.auth_session = mock.Mock()

            def request_once(self, method, endpoint, **kwargs):
                owner.calls.append((self.deadline, kwargs))
                effect = owner.effects.pop(0)
                if isinstance(effect, Exception):
                    raise effect
                if callable(effect):
                    effect = effect()
                return json.dumps({'result': effect}).encode()

        for patch in (mock.patch.object(frame_search, '_RPC', OfflineRPC),
                      mock.patch('time.monotonic', side_effect=lambda: self.now),
                      mock.patch('time.sleep')):
            patch.start()
            self.addCleanup(patch.stop)

    def compute(self):
        return imagery._compute_timeseries_metadata(ee.List([]))

    def test_transient_retry_uses_one_deadline_and_no_global_ee_mutation(self):
        self.effects = [TimeoutError('private URL'), []]
        with mock.patch.object(ee.data, 'setDeadline') as global_deadline:
            self.assertEqual(self.compute(), [])
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([call[0] for call in self.calls], [120, 120])
        global_deadline.assert_not_called()

    def test_exhausted_retries_timeout_without_exposing_provider_message(self):
        self.effects = [TimeoutError('private URL')] * 3
        with self.assertRaises(Exception) as caught:
            self.compute()
        self.assertEqual(getattr(caught.exception, 'code', None), 'timeout')
        self.assertEqual(getattr(caught.exception, 'status', None), 504)
        self.assertEqual(len(self.calls), 3)
        self.assertNotIn('private', str(caught.exception))

    def test_upstream_gateway_timeout_remains_a_timeout_after_retries(self):
        self.effects = [frame_search._TransientError(504) for _ in range(3)]
        with self.assertRaises(Exception) as caught:
            self.compute()
        self.assertEqual(getattr(caught.exception, 'code', None), 'timeout')
        self.assertEqual(getattr(caught.exception, 'status', None), 504)

    def test_late_result_is_not_published(self):
        def late():
            self.now = 121
            return []
        self.effects = [late]
        with self.assertRaises(Exception) as caught:
            self.compute()
        self.assertEqual(getattr(caught.exception, 'code', None), 'timeout')

    def test_auth_errors_are_not_retried_or_exposed(self):
        for error in (RefreshError('private credentials'),
                      frame_search.FrameUpstreamError('upstream_forbidden', 'private URL', http_status=403)):
            with self.subTest(error=type(error).__name__):
                self.effects = [error]
                self.calls.clear()
                with self.assertRaises(Exception) as caught:
                    self.compute()
                self.assertEqual(getattr(caught.exception, 'code', None), 'upstream_auth')
                self.assertEqual(len(self.calls), 1)
                self.assertNotIn('private', str(caught.exception))

    def test_invalid_or_excessive_result_cannot_become_complete_search(self):
        for result in (None, {}, [None], [{}] * 20001):
            with self.subTest(result_type=type(result).__name__):
                self.effects = [result]
                with self.assertRaises(Exception):
                    self.compute()


class SearchFailureTests(unittest.TestCase):
    def setUp(self):
        self.real_ensure_ee = main._ensure_ee
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TimeSeriesStore(Path(self.temp.name))
        self.client = TestClient(main.app)
        self.body = dict(dataset='sentinel2', lon=105, lat=20,
                         start_date='2024-02-29', end_date='2024-02-29')
        for patch in (mock.patch.object(main, '_ensure_ee'),
                      mock.patch.object(main, '_resolve_selection',
                                        return_value=('roi', main.Bbox(104, 19, 106, 21), None)),
                      mock.patch.object(main, '_timeseries_store', self.store)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_timeout_is_504_and_no_snapshot_is_created(self):
        with mock.patch.object(main, 'list_timeseries_scenes', side_effect=TimeoutError('private URL')), \
                self.assertLogs('uvicorn.error', level='ERROR') as logs:
            response = self.client.post('/api/timeseries/search', json=self.body)
        self.assertEqual(response.status_code, 504, response.text)
        request_id = response.headers['X-Search-Request-ID']
        self.assertIn(request_id, '\n'.join(logs.output))
        self.assertIn('stage=metadata', '\n'.join(logs.output))
        self.assertNotIn('private', response.text + '\n'.join(logs.output))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM searches').fetchone()[0], 0)

    def test_initial_auth_failure_is_sanitized_and_correlated(self):
        with mock.patch.object(main, '_ensure_ee', side_effect=self.real_ensure_ee), \
                mock.patch.object(main, '_ee_ready', False), \
                mock.patch.object(main, 'init_ee', side_effect=RefreshError('private credentials')), \
                self.assertLogs('uvicorn.error', level='ERROR') as logs:
            response = self.client.post('/api/timeseries/search', json=self.body)
        self.assertEqual(response.status_code, 502, response.text)
        text = response.text + '\n'.join(logs.output)
        self.assertNotIn('private', text)
        self.assertIn('stage=initialization', text)
        self.assertIn(response.headers['X-Search-Request-ID'], text)

    def test_storage_failure_is_not_reported_as_gee_access_failure(self):
        with mock.patch.object(main, 'list_timeseries_scenes', return_value=[]), \
                mock.patch.object(self.store, 'create_search', side_effect=OSError('private path')), \
                self.assertLogs('uvicorn.error', level='ERROR') as logs:
            response = self.client.post('/api/timeseries/search', json=self.body)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIn('stage=snapshot', '\n'.join(logs.output))
        self.assertNotIn('private', response.text + '\n'.join(logs.output))
        self.assertNotIn('collection access', response.text)

    def test_invalid_scene_does_not_publish_a_partial_snapshot(self):
        with mock.patch.object(main, 'list_timeseries_scenes', return_value=[{'date': '2024-02-29'}]):
            response = self.client.post('/api/timeseries/search', json=self.body)
        self.assertEqual(response.status_code, 502, response.text)
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM searches').fetchone()[0], 0)
