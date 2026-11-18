"""Metadata-only inventory: geometry, complete pagination and immutable CSV."""
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.geo import RectangleROI, point_roi


class InventoryAPITests(unittest.TestCase):
    def setUp(self):
        from app import scene_inventory
        self.inventory = scene_inventory
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        api = FastAPI()
        with mock.patch.object(main, '_DOWNLOADS_DIR', Path(temporary.name)):
            self.store = scene_inventory.register_scene_inventory(api)
        self.client = TestClient(api)
        self.addCleanup(self.client.close)
        self.body = dict(dataset='sentinel2', selection_mode='point', lon=107, lat=10,
                         radius_m=2000, start_date='2026-11-01', end_date='2026-11-02')
        self.rows = [dict(asset_id=f'collection/scene-{i}', name=f'Ảnh {i}, "ROI"',
                          date='2026-11-02', timestamp='2026-11-02T23:59:00Z',
                          cloud_pct=None if i % 2 else 100) for i in range(123)]

    def search(self, body=None, rows=None):
        with mock.patch.object(self.inventory.sources, 'list_scenes', return_value=self.rows if rows is None else rows) as query:
            response = self.client.post('/api/scene-inventory', json=body or self.body)
        return response, query

    def test_count_and_csv_use_same_all_cloud_snapshot_and_all_rows(self):
        with mock.patch.object(main, 'download_geotiff') as download:
            response, query = self.search()
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
            self.assertEqual(result['total'], 123)
            self.assertEqual(len(result['scenes']), 50)
            self.assertEqual(query.call_args.kwargs['geometry'], point_roi(107, 10, 2000)[0])
            self.assertEqual(query.call_args.kwargs['end'], '2026-11-02')
            self.rows[0]['name'] = 'changed after query'
            with mock.patch.object(self.inventory.sources, 'list_scenes', side_effect=AssertionError('CSV must not requery')):
                exported = self.client.get(result['csv_url'])
            download.assert_not_called()
        self.assertEqual(exported.status_code, 200)
        self.assertTrue(exported.content.startswith(b'\xef\xbb\xbf'))
        rows = list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
        self.assertEqual(len(rows), result['total'])
        self.assertEqual(rows[0]['name'], 'Ảnh 0, "ROI"')
        self.assertEqual(rows[0]['date'], '2026-11-02')
        self.assertEqual(rows[0]['cloud_pct'], '100')
        self.assertEqual(rows[1]['cloud_pct'], '')
        self.assertNotIn('token', exported.text)

    def test_exact_rotated_rectangle_platform_and_source(self):
        rect = dict(center_lon=107, center_lat=10, width_m=4000, height_m=2000, rotation_deg=37)
        body = dict(dataset='sentinel2', selection_mode='rectangle', **rect,
                    platform='C', start_date='2026-11-01', end_date='2026-11-02')
        response, query = self.search(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(query.call_args.kwargs['geometry'], RectangleROI(**rect).geometry)
        self.assertEqual(query.call_args.kwargs['platform'], 'C')
        self.assertEqual(response.json()['parameters']['source'], 'gee')
        body.update(dataset='s2', source='aws', platform=None)
        response, query = self.search(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(query.call_args.kwargs['source'], 'aws')

    def test_reference_is_resolved_from_server_grid(self):
        from app.reference_frame import reference_footprint
        frame = dict(crs='EPSG:4326', transform=[0.001, 0.0001, 107, 0.0001, -0.001, 10], width=12, height=10)
        body = dict(dataset='sentinel2', selection_mode='reference', reference_id='not-a-file-path',
                    start_date='2026-11-01', end_date='2026-11-02')
        with mock.patch.object(main._frame_store, 'get_reference', return_value={'metadata': frame}):
            response, query = self.search(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(query.call_args.kwargs['geometry'], reference_footprint(frame)['footprint_geojson'])

    def test_administrative_selection_uses_polygon_without_gee_access(self):
        geometry = {'type': 'Polygon', 'coordinates': [[[107, 10], [108, 10], [107, 11], [107, 10]]]}
        body = dict(dataset='s2', source='cdse', selection_mode='province', province_id='test',
                    start_date='2026-11-01', end_date='2026-11-02')
        with mock.patch.object(main, 'get_province', return_value=mock.Mock(geometry=geometry)), \
                mock.patch.object(main, '_ensure_ee', side_effect=AssertionError('public catalogue')):
            response, query = self.search(body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(query.call_args.kwargs['geometry'], geometry)

    def test_empty_snapshot_can_be_exported_with_header(self):
        response, _ = self.search(rows=[])
        self.assertEqual(response.json()['total'], 0)
        exported = self.client.get(response.json()['csv_url'])
        self.assertEqual(len(exported.content.decode('utf-8-sig').splitlines()), 1)

    def test_invalid_interval_geometry_and_cloud_fields_fail_before_query(self):
        for changes in ({'start_date': '2026-11-03'}, {'end_date': '9999-12-31'},
                        {'start_date': 'bad'}, {'lon': None}, {'platform': 'D'},
                        {'cloud_max': 10}, {'dataset': 'dem_srtm'},
                        {'selection_mode': 'reference'}, {'commune_id': 'wrong-mode'}):
            with self.subTest(changes=changes):
                response, query = self.search(dict(self.body, **changes))
                self.assertEqual(response.status_code, 422, response.text)
                query.assert_not_called()

    def test_upstream_error_does_not_create_snapshot_or_leak_credentials(self):
        with mock.patch.object(self.inventory.sources, 'list_scenes', side_effect=RuntimeError('secret token https://private')), \
                mock.patch.object(self.store, 'create_search') as create:
            response = self.client.post('/api/scene-inventory', json=self.body)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('secret', response.text)
        create.assert_not_called()
        self.assertEqual(self.client.get('/api/scene-inventory/missing/csv').status_code, 404)

    def test_csv_escapes_spreadsheet_formulas(self):
        response, _ = self.search(rows=[dict(self.rows[0], name='=HYPERLINK("bad")')])
        exported = self.client.get(response.json()['csv_url'])
        rows = list(csv.DictReader(io.StringIO(exported.content.decode('utf-8-sig'))))
        self.assertEqual(rows[0]['name'], '\'=HYPERLINK("bad")')

    def test_gee_timeout_is_actionable_without_exposing_upstream_details(self):
        from app.frame_search import FrameUpstreamError
        error = FrameUpstreamError('timeout', 'GEE hết thời gian phản hồi.')
        with mock.patch.object(self.inventory.sources, 'list_scenes', side_effect=error):
            response = self.client.post('/api/scene-inventory', json=self.body)
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()['detail']['code'], 'timeout')
        self.assertEqual(response.json()['detail']['source'], 'gee')
        self.assertIn('GEE', response.json()['detail']['message'])


class InventorySourceTests(unittest.TestCase):
    def setUp(self):
        from app import inventory_sources
        self.sources = inventory_sources
        self.geometry = RectangleROI(center_lon=107, center_lat=10, width_m=2000,
                                      height_m=4000, rotation_deg=30).geometry

    def query(self, dataset='s2', source='cdse', **kw):
        return self.sources.list_scenes(dataset=dataset, source=source, geometry=self.geometry,
                                       start='2026-11-01', end='2026-11-02', **kw)

    def test_cdse_follows_pages_no_cloud_filter_and_normalizes_name_date(self):
        pages = [{'value': [{'Id': '1', 'Name': 'cloudy', 'ContentDate': {'Start': '2026-11-02T23:59:00Z'}}],
                  '@odata.nextLink': self.sources.cdse._CATALOG_URL + '?$skip=1'},
                 {'value': [{'Id': '2', 'Name': 'unknown', 'ContentDate': {'Start': '2026-11-01T01:00:00Z'}}]}]
        with mock.patch.object(self.sources.requests, 'get') as get:
            get.return_value.json.side_effect = pages
            rows = self.query()
        by_id = {row['asset_id']: row for row in rows}
        self.assertEqual(by_id['1']['name'], 'cloudy')
        self.assertEqual(by_id['2']['name'], 'unknown')
        self.assertEqual(by_id['1']['date'], '2026-11-02')
        params = get.call_args_list[0].kwargs['params']
        self.assertNotIn('cloudCover', params['$filter'])
        self.assertIn('2026-11-03T00:00:00', params['$filter'])
        self.assertNotIn('$top', get.call_args_list[1].kwargs.get('params') or {})

    def test_cdse_refuses_missing_and_repeated_pages(self):
        for page in ({'value': [], '@odata.count': 3},
                     {'value': [{'Id': '1', 'Name': 'a', 'ContentDate': {'Start': '2026-11-01T00:00:00Z'}}],
                      '@odata.nextLink': self.sources.cdse._CATALOG_URL + '?$skip=0'},
                     {'value': [], '@odata.nextLink': 'https://unexpected.example/private'}):
            with self.subTest(page=page), mock.patch.object(self.sources.requests, 'get') as get:
                get.return_value.json.return_value = page
                with self.assertRaises(self.sources.InventoryIncomplete):
                    self.query()

    def test_aws_uses_intersects_and_all_next_pages_including_null_cloud(self):
        item = lambda value, cloud: {'id': value, 'properties': {'datetime': '2026-11-02T23:59:00Z', 'eo:cloud_cover': cloud}}
        pages = [{'features': [item('a', 100)], 'links': [{'rel': 'next', 'href': self.sources.aws_stac._STAC_URL,
                   'method': 'POST', 'body': {'token': 'next'}}]},
                 {'features': [item('b', None)], 'links': []}]
        with mock.patch.object(self.sources.requests, 'post') as post:
            post.return_value.json.side_effect = pages
            rows = self.query(source='aws')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['cloud_pct'], None)
        body = post.call_args_list[0].kwargs['json']
        self.assertEqual(body['intersects'], self.geometry)
        self.assertNotIn('query', body)
        self.assertIn('2026-11-02T23:59:59', body['datetime'])
        self.assertEqual(post.call_args_list[1].kwargs['json']['token'], 'next')

    def test_repeated_terminal_page_cannot_publish_an_exact_total_without_provider_count(self):
        aws_item = {'id': 'same', 'properties': {'datetime': '2026-11-01T00:00:00Z'}}
        cdse_item = {'Id': 'same', 'Name': 'repeated', 'ContentDate': {'Start': '2026-11-01T00:00:00Z'}}
        for source, method, pages in (
            ('aws', 'post', [{'features': [aws_item], 'links': [{'rel': 'next', 'href': self.sources.aws_stac._STAC_URL,
               'method': 'POST', 'body': {'token': 'next'}}]}, {'features': [aws_item], 'links': []}]),
            ('cdse', 'get', [{'value': [cdse_item], '@odata.nextLink': self.sources.cdse._CATALOG_URL + '?$skip=1'},
                            {'value': [cdse_item]}])):
            with self.subTest(source=source), mock.patch.object(self.sources.requests, method) as request:
                request.return_value.json.side_effect = pages
                with self.assertRaises(self.sources.InventoryIncomplete):
                    self.query(source=source)

    def test_gee_enumeration_keeps_null_and_full_cloud_and_end_day(self):
        collection = mock.Mock()
        collection.filterBounds.return_value = collection
        collection.filterDate.return_value = collection
        collection.map.return_value = collection
        values = [
            {'asset_id': 'a', 'timestamp_ms': 1793663940000, 'cloud_pct': 100},
            {'asset_id': 'b', 'timestamp_ms': 1793663940000, 'cloud_pct': None}]
        with mock.patch.object(main, '_ensure_ee'), \
                mock.patch.object(self.sources.ee, 'ImageCollection', return_value=collection), \
                mock.patch.object(self.sources, '_compute_gee', return_value=values), \
                mock.patch.object(self.sources, 'apply_platform_filter', return_value=collection) as platform_filter, \
                mock.patch.object(self.sources.ee, 'Geometry', return_value='exact-geometry'):
            rows = self.query(dataset='sentinel2', source='gee', platform='C')
        self.assertEqual(len(rows), 2)
        self.assertEqual([r['cloud_pct'] for r in rows], [100, None])
        collection.filterDate.assert_called_once_with('2026-11-01', '2026-11-03')
        platform_filter.assert_called_once_with(collection, 'sentinel2', 'C')
        collection.filterBounds.assert_called_once_with('exact-geometry')

    def test_s1_reference_filters_remain_in_inventory(self):
        collection = mock.Mock()
        collection.filter.return_value = collection
        collection.filterBounds.return_value = collection
        collection.filterDate.return_value = collection
        collection.map.return_value = collection
        with mock.patch.object(main, '_ensure_ee'), \
                mock.patch.object(self.sources.ee, 'ImageCollection', return_value=collection), \
                mock.patch.object(self.sources, '_compute_gee', return_value=[]), \
                mock.patch.object(self.sources.ee, 'Geometry'), \
                mock.patch.object(self.sources.ee.Filter, 'listContains', side_effect=lambda a, b: (a, b)), \
                mock.patch.object(self.sources.ee.Filter, 'eq', side_effect=lambda a, b: (a, b)):
            rows = self.query(dataset='s1gee', source='gee', polarization='hh',
                              orbit_pass='DESCENDING', relative_orbit=42)
        self.assertEqual(rows, [])
        self.assertEqual(collection.filter.call_args_list, [mock.call(('instrumentMode', 'IW')),
                                                           mock.call(('transmitterReceiverPolarisation', 'HH')),
                                                           mock.call(('orbitProperties_pass', 'DESCENDING')),
                                                           mock.call(('relativeOrbitNumber_start', 42))])


if __name__ == '__main__':
    unittest.main()
