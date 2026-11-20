"""Vector selections keep the exact trusted geometry through counts and GEE export."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main, imagery
from app.scene_inventory import register_scene_inventory

GEOMETRY = {'type': 'Polygon', 'coordinates': [
    [[107, 10], [107.04, 10], [107.04, 10.04], [107, 10.04], [107, 10]],
    [[107.01, 10.01], [107.01, 10.02], [107.02, 10.02], [107.02, 10.01], [107.01, 10.01]]]}


class VectorROITests(unittest.TestCase):
    def setUp(self):
        self.roi_id = str(uuid4())
        self.record = dict(roi_id=self.roi_id, name='Vùng nhập', geometry=GEOMETRY,
                           bbox=dict(west=107, south=10, east=107.04, north=10.04))
        self.store = mock.Mock()
        self.store.get_roi.return_value = self.record
        patch = mock.patch.object(main, '_vector_store', self.store, create=True)
        patch.start(); self.addCleanup(patch.stop)
        self.body = dict(dataset='sentinel2', selection_mode='vector', vector_roi_id=self.roi_id,
                         start_date='2025-01-01', end_date='2025-01-07')

    def test_models_accept_only_vector_id_with_vector_mode(self):
        req = main.SceneRequest(**self.body)
        self.assertEqual(req.vector_roi_id, self.roi_id)
        for changes in ({'vector_roi_id': None}, {'vector_roi_id': '../../bad'}, {'lon':107},
                        {'radius_m':1000}, {'rotation_deg':30}, {'province_id':'test'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                main.SceneRequest(**dict(self.body, **changes))
        with self.assertRaises(ValidationError):
            main.SceneRequest(**dict(self.body, selection_mode='point', lon=107, lat=10))

    def test_resolver_preserves_hole_and_never_uses_point_or_rectangle(self):
        with mock.patch('ee.Geometry', return_value='exact-polygon') as geometry, \
                mock.patch.object(main, 'point_roi', side_effect=AssertionError('not a point')):
            roi, bbox, area = main._resolve_selection('vector', None, None, 2000, None,
                                                      vector_roi_id=self.roi_id)
        self.assertEqual(roi, 'exact-polygon')
        self.assertEqual(area.geometry, GEOMETRY)
        self.assertEqual(bbox.west, 107)
        geometry.assert_called_once_with(GEOMETRY, geodesic=False)

    def test_inventory_metadata_uses_trusted_exact_geometry(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        api = FastAPI()
        with mock.patch.object(main, '_DOWNLOADS_DIR', Path(temporary.name)):
            register_scene_inventory(api)
        with TestClient(api) as client, mock.patch('app.inventory_sources.list_scenes', return_value=[]) as query:
            response = client.post('/api/scene-inventory', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(query.call_args.kwargs['geometry'], GEOMETRY)
        self.assertEqual(response.json()['parameters']['roi_geometry'], GEOMETRY)

    def test_scene_link_keeps_vector_roi_id_and_polygon(self):
        scene = imagery.SceneInfo('sentinel2', 'scene', 'scene', '2025-01-01', 10, 'image')
        with mock.patch.object(main, '_ensure_ee'), mock.patch('ee.Geometry', return_value='exact'), \
                mock.patch.object(main, 'best_scene_for_roi', return_value=scene), \
                mock.patch.object(main, 'preview_rgb', return_value='preview'):
            response = TestClient(main.app).post('/api/scene', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result['roi_geometry'], GEOMETRY)
        self.assertEqual(result['selection_mode'], 'vector')
        self.assertIn('selection_mode=vector', result['download_url'])
        self.assertIn('vector_roi_id=' + self.roi_id, result['download_url'])
        self.assertNotIn('province_id=', result['download_url'])

    def test_download_final_mask_uses_same_polygon_after_merge(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        scene = imagery.SceneInfo('sentinel2', 'scene', 'scene', '2025-01-01', 10, 'image')
        def merge(urls, path, **kwargs):
            Path(path).write_bytes(b'fixture')
        with mock.patch.object(main, '_DOWNLOADS_DIR', Path(temporary.name)), \
                mock.patch.object(main, '_ensure_ee'), mock.patch('ee.Geometry', return_value='exact'), \
                mock.patch.object(main, 'best_scene_for_roi', return_value=scene), \
                mock.patch.object(main, 'download_geotiff', return_value=['url']) as export, \
                mock.patch.object(main, 'merge_band_tiffs', side_effect=merge), \
                mock.patch('app.vector_mask.mask_raster_to_polygon') as mask:
            params = {k:v for k,v in self.body.items() if k != 'dataset'}
            response = TestClient(main.app).get('/api/download/sentinel2', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(mask.call_args.args[1], GEOMETRY)
        self.assertEqual(export.call_args.args[4], 'exact')

    def test_vector_timeseries_request_accepts_without_hidden_point_fields(self):
        req = main.TimeSeriesRequest(**self.body)
        self.assertEqual(req.vector_roi_id, self.roi_id)


if __name__ == '__main__':
    unittest.main()
