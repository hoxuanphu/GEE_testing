"""Rectangle catalogue contracts: polygon search, persisted ROI and safe downloads."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.catalog_models import CatalogResult
from app.geo import RectangleROI
from app.scene_catalog import register_scene_catalog


class TestRectangleCatalog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client, self.store = self.restart()
        self.body = dict(dataset='s2', source='aws', selection_mode='rectangle',
                         center_lon=107.1, center_lat=11.2, width_m=12000, height_m=6000,
                         rotation_deg=25, start_date='2026-01-01', end_date='2026-01-31',
                         cloud_min=0, cloud_max=50)
        self.rectangle = RectangleROI(**{k: self.body[k] for k in
            ('center_lon', 'center_lat', 'width_m', 'height_m', 'rotation_deg')})
        self.rows = [dict(id='native-item', date='2026-01-31', cloud_pct=0,
                          asset_id='COPERNICUS/S2_SR_HARMONIZED/exact-item')]

    def restart(self):
        app = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            store = register_scene_catalog(app)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client, store

    def test_validation_rejects_incomplete_mixed_and_invalid_rectangle(self):
        with patch.object(main, '_ensure_ee') as init, \
                patch.object(main, 'aws_search_s2_l2a_bbox') as search:
            for changes in ({'width_m': None}, {'center_lat': None}, {'height_m': 0},
                            {'lon': 107}, {'lat': 11}, {'radius_m': 2000},
                            {'province_id': 'test'}, {'commune_id': 'test'},
                            {'rotation_deg': 361}):
                with self.subTest(changes=changes):
                    response = self.client.post('/api/scene-catalog', json={**self.body, **changes})
                    self.assertEqual(response.status_code, 422, response.text)
            init.assert_not_called()
            search.assert_not_called()

    def test_external_providers_use_envelope_without_initializing_gee(self):
        bbox = self.rectangle.bbox
        bounds = (bbox.west, bbox.south, bbox.east, bbox.north)
        for source, function in (('aws', 'aws_search_s2_l2a_bbox'), ('cdse', 'search_s2_area')):
            with self.subTest(source=source), \
                    patch.object(main, '_ensure_ee') as init, \
                    patch.object(main, function, return_value=self.rows) as search:
                response = self.client.post('/api/scene-catalog', json={**self.body, 'source': source})
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(search.call_args.args, (bounds, '2026-01-01', '2026-01-31'))
                self.assertEqual(search.call_args.kwargs['cloud_max'], 50)
                self.assertEqual(data['parameters']['roi_geometry'], self.rectangle.geometry)
                self.assertEqual(data['parameters']['rectangle'], self.rectangle.model_dump())
                self.assertEqual(data['parameters']['bbox'], vars(bbox))
                self.assertIn('download_url', data['scenes'][0])
                self.assertEqual(data['scenes'][0]['download_capabilities']['status'], 'supported')
                download = self.client.get(data['scenes'][0]['download_url'], follow_redirects=False)
                self.assertEqual(download.status_code, 303)
                from urllib.parse import urlparse, parse_qs
                query = parse_qs(urlparse(download.headers['location']).query)
                self.assertEqual(query['selection_mode'], ['rectangle'])
                for key, value in self.rectangle.model_dump().items():
                    self.assertEqual(float(query[key][0]), value)
                self.assertNotIn('commune_id', query)
                self.assertNotIn('radius_m', query)
                init.assert_not_called()

    def test_eodag_rectangle_remains_catalogue_only(self):
        with patch('app.eodag_adapter.search', return_value=CatalogResult(self.rows, {'truncated': False})) as search, \
                patch.object(main, '_ensure_ee') as init:
            response = self.client.post('/api/scene-catalog', json={**self.body,
                'source': 'eodag', 'provider': 'earth_search'})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(search.call_args.args[0], tuple(vars(self.rectangle.bbox).values()))
            self.assertEqual(data['parameters']['rectangle'], self.rectangle.model_dump())
            self.assertNotIn('download_url', data['scenes'][0])
            self.assertEqual(data['scenes'][0]['download_capabilities']['reason'], 'eodag_mvp_catalogue_only')
            self.assertEqual(self.client.get(f'/api/scene-catalog/{data["search_id"]}/download/0').status_code, 409)
            init.assert_not_called()

    def test_gee_search_uses_polygon_and_stores_grid_without_compositing(self):
        with patch.object(main, '_ensure_ee'), \
                patch('ee.Geometry.Polygon', return_value='rectangle-polygon') as polygon, \
                patch.object(main, 'list_timeseries_scenes', return_value=self.rows) as listing, \
                patch.object(main, 'best_scene') as best:
            response = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 'sentinel2'})
        self.assertEqual(response.status_code, 200, response.text)
        polygon.assert_called_once_with(self.rectangle.geometry['coordinates'], geodesic=False)
        self.assertEqual(listing.call_args.kwargs['roi'], 'rectangle-polygon')
        self.assertEqual(listing.call_args.args[4], '2026-02-01')
        data = response.json()
        self.assertEqual(data['parameters']['source'], 'gee')
        self.assertEqual(data['parameters']['output_grid'], json.loads(json.dumps(self.rectangle.output_grid(main.S2_SCALE))))
        self.assertIn('download_url', data['scenes'][0])
        best.assert_not_called()

    def test_restart_paging_and_new_search_do_not_change_old_rectangle(self):
        rows = [dict(id=str(i), date='2026-01-01', cloud_pct=10) for i in range(61)]
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=rows):
            first = self.client.post('/api/scene-catalog', json=self.body).json()
            self.client.post('/api/scene-catalog', json={**self.body, 'width_m': 5000, 'rotation_deg': 90})
        client, store = self.restart()
        with patch.object(main, 'aws_search_s2_l2a_bbox', side_effect=AssertionError('must use snapshot')):
            page = client.get(f'/api/scene-catalog/{first["search_id"]}?offset=50').json()
        self.assertEqual(len(page['scenes']), 11)
        self.assertEqual(page['parameters'], first['parameters'])
        self.assertEqual(store._snapshot(first['search_id'])[0]['roi_geometry'], self.rectangle.geometry)
        self.assertIn('download_url', page['scenes'][0])
        redirect = client.get(page['scenes'][0]['download_url'], follow_redirects=False)
        self.assertEqual(redirect.status_code, 303)
        self.assertIn('width_m=12000', redirect.headers['location'])
        self.assertIn('rotation_deg=25', redirect.headers['location'])

    def test_exact_gee_download_uses_saved_polygon_and_grid_after_restart(self):
        for angle in (0, 25):
            for dataset in ('sentinel2', 'landsat'):
                with self.subTest(angle=angle, dataset=dataset):
                    rectangle = self.rectangle.model_copy(update={'rotation_deg': angle})
                    with patch.object(main, '_ensure_ee'), \
                            patch('ee.Geometry.Polygon', return_value='search-polygon'), \
                            patch.object(main, 'list_timeseries_scenes', return_value=self.rows):
                        result = self.client.post('/api/scene-catalog', json={**self.body,
                            'dataset': dataset, 'rotation_deg': angle}).json()
                    client, _ = self.restart()
                    with patch.object(main, '_ensure_ee'), \
                            patch('ee.Geometry.Polygon', return_value='saved-polygon') as polygon, \
                            patch.object(main, '_resolve_selection', side_effect=AssertionError('use saved geometry')), \
                            patch.object(main, '_DOWNLOADS_DIR', self.root), \
                            patch('app.imagery.load_timeseries_scene', return_value='exact-image') as load, \
                            patch.object(main, 'download_geotiff', return_value=[]) as download, \
                            patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: path.write_bytes(b'source')), \
                            patch.object(main, 'warp_to_grid', side_effect=lambda src, dst, grid: dst.write_bytes(b'warped')) as warp:
                        response = client.get(result['scenes'][0]['download_url'])
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.content, b'warped')
                    load.assert_called_once_with(self.rows[0]['asset_id'], dataset)
                    polygon.assert_called_once_with(rectangle.geometry['coordinates'], geodesic=False)
                    self.assertEqual(warp.call_args.args[2], json.loads(json.dumps(rectangle.output_grid(main._scale_for(dataset)))))
                    self.assertIsNone(download.call_args.kwargs['clip_region'])
                    self.assertFalse(list(self.root.glob('*.source.tif')))
