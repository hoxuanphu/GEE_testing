"""Exact saved polygon export contracts using real small GeoTIFFs, offline."""
import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import rasterio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from app import main, s5p, timeseries
from app.scene_catalog import register_scene_catalog


# A hole and a disconnected piece, with pixels outside both in the export bbox.
GEOMETRY = {'type': 'MultiPolygon', 'coordinates': [
    [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
     [[1, 1], [1, 3], [3, 3], [3, 1], [1, 1]]],
    [[[5, 0], [6, 0], [6, 1], [5, 1], [5, 0]]]]}
BBOX = {'west': 0, 'south': 0, 'east': 6, 'north': 4}


def write_raster(path):
    with rasterio.open(path, 'w', driver='GTiff', width=6, height=4, count=1,
                       dtype='float32', crs='EPSG:4326', transform=from_origin(0, 4, 1, 1),
                       nodata=-9999) as target:
        target.write(np.ones((4, 6), dtype='float32'), 1)


def assert_mask(test, path):
    with rasterio.open(path) as source:
        data = source.read(1, masked=True)
        test.assertTrue(data.mask[1, 1], 'polygon hole must be NoData')
        test.assertTrue(data.mask[0, 5], 'outside disconnected pieces must be NoData')
        test.assertFalse(data.mask[0, 0])
        test.assertFalse(data.mask[3, 5], 'disconnected polygon must remain valid')


class VectorExports(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.params = dict(selection_mode='vector', vector_roi_id='saved-roi',
                           dataset='sentinel2', source='gee', roi_geometry=copy.deepcopy(GEOMETRY),
                           bbox=BBOX, scale_m=10, bands=['B2'], nodata=-9999,
                           s2_auxiliary_bands=[], rotation_deg=0)
        self.scene = dict(asset_id='COPERNICUS/S2_SR_HARMONIZED/EXACT',
                          date='2026-01-01', timestamp='2026-01-01T00:00:00Z')

    def test_catalog_search_freezes_trusted_geometry_before_query(self):
        api = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            register_scene_catalog(api)
        geometry = copy.deepcopy(GEOMETRY)
        area = SimpleNamespace(geometry=geometry)
        body = dict(selection_mode='vector', vector_roi_id=str(uuid.uuid4()), dataset='sentinel2',
                    start_date='2026-01-01', end_date='2026-01-01')
        def listing(*args, **kw):
            geometry['coordinates'].clear()
            return [{**self.scene, 'cloud_pct': 12}]
        with TestClient(api) as client, patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', return_value=('exact-roi', main.Bbox(**BBOX), area)) as resolve, \
                patch.object(main, 'list_timeseries_scenes', side_effect=listing):
            response = client.post('/api/scene-catalog', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['parameters']['roi_geometry'], GEOMETRY)
        self.assertEqual(resolve.call_args.kwargs['vector_roi_id'], body['vector_roi_id'])

    def test_external_vector_search_is_rejected_before_any_provider_call(self):
        api = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            register_scene_catalog(api)
        for dataset in ('s2', 's2aws'):
            with TestClient(api) as client, patch.object(main, 'search_s2_area') as cdse, \
                    patch.object(main, 'aws_search_s2_l2a_bbox') as aws:
                response = client.post('/api/scene-catalog', json=dict(selection_mode='vector',
                    vector_roi_id=str(uuid.uuid4()), dataset=dataset,
                    start_date='2026-01-01', end_date='2026-01-01'))
            self.assertEqual(response.status_code, 409, response.text)
            cdse.assert_not_called()
            aws.assert_not_called()

    def test_s5p_search_freezes_trusted_geometry_without_gee_evaluation(self):
        api = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            s5p.register_s5p(api)
        geometry = copy.deepcopy(GEOMETRY)
        roi = MagicMock()
        roi.getInfo.side_effect = AssertionError('trusted raw geometry requires no evaluation')
        body = dict(selection_mode='vector', vector_roi_id=str(uuid.uuid4()),
                    start_date='2026-01-01', end_date='2026-01-01')
        def listing(*args):
            geometry['coordinates'].clear()
            return [], False
        with TestClient(api) as client, patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', return_value=(roi, main.Bbox(**BBOX), SimpleNamespace(geometry=geometry))) as resolve, \
                patch.object(s5p, 'list_scenes', side_effect=listing):
            response = client.post('/api/s5p/search', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['parameters']['roi_geometry'], GEOMETRY)
        self.assertEqual(resolve.call_args.kwargs['vector_roi_id'], body['vector_roi_id'])

    def test_catalog_download_uses_snapshot_after_roi_removed_and_final_mask(self):
        api = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            store = register_scene_catalog(api)
        result = store.create_search(self.params, [self.scene])
        with TestClient(api) as client, patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', side_effect=AssertionError('snapshot only')), \
                patch('ee.Geometry', return_value='saved-geometry') as geometry, \
                patch('app.imagery.load_timeseries_scene', return_value='image'), \
                patch.object(main, '_DOWNLOADS_DIR', self.root), \
                patch.object(main, 'download_geotiff', return_value=[]) as export, \
                patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: write_raster(path)):
            response = client.get(f'/api/scene-catalog/{result["search_id"]}/download/0')
        self.assertEqual(response.status_code, 200, response.text)
        geometry.assert_called_with(GEOMETRY, geodesic=False)
        self.assertEqual(export.call_args.kwargs['clip_region'], 'saved-geometry')
        with rasterio.MemoryFile(response.content) as memory:
            assert_mask(self, memory.name)

    def test_external_catalog_snapshot_cannot_redirect_vector_to_point_or_bbox(self):
        api = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            store = register_scene_catalog(api)
        for source in ('aws', 'cdse', 'eodag'):
            result = store.create_search({**self.params, 'source': source}, [dict(id='scene')])
            with TestClient(api, raise_server_exceptions=False) as client:
                response = client.get(f'/api/scene-catalog/{result["search_id"]}/download/0', follow_redirects=False)
            self.assertEqual(response.status_code, 409, response.text)

    def test_worker_masks_after_final_alignment_without_resolving_live_roi(self):
        store = timeseries.TimeSeriesStore(self.root)
        service = timeseries.TimeSeriesService(store)
        self.addCleanup(service.shutdown)
        result = store.create_search(self.params, [self.scene])
        job = store.create_job(result['search_id'])
        # Simulate interpolation writing valid values across the whole envelope.
        def align(source, target, grid, **kw):
            write_raster(target)
            return dict(crs='EPSG:4326', transform=[1, 0, 0, 0, -1, 4], width=6, height=4)
        with patch.object(timeseries, 'load_timeseries_scene', return_value='image'), \
                patch('ee.Geometry', return_value='saved-geometry') as geometry, \
                patch.object(timeseries, 'download_geotiff', return_value=[]) as export, \
                patch.object(timeseries, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: write_raster(path)) as merge, \
                patch.object(timeseries, 'align_timeseries_raster', side_effect=align):
            service.run_timeseries_job(job['job_id'])
        status = store.get_job(job['job_id'])
        self.assertEqual(status['status'], 'completed', status)
        geometry.assert_called_with(GEOMETRY, geodesic=False)
        self.assertEqual(export.call_args.kwargs['clip_region'], 'saved-geometry')
        self.assertTrue(merge.call_args.kwargs['preserve_masks'])
        assert_mask(self, next(store._job_dir(job['job_id']).glob('*.tif')))

    def test_s5p_saved_vector_download_masks_exact_polygon(self):
        api = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.root):
            store = s5p.register_s5p(api)
        params = {**self.params, **s5p.product_metadata('NO2')}
        scene = dict(scene_id='EXACT', asset_id='COPERNICUS/S5P/OFFL/L3_NO2/EXACT',
                     timestamp=self.scene['timestamp'])
        result = store.create_search(params, [scene])
        with TestClient(api) as client, patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', side_effect=AssertionError('snapshot only')), \
                patch.object(s5p.ee, 'Geometry', return_value='saved-geometry') as geometry, \
                patch.object(s5p.ee, 'Image', return_value=MagicMock()), \
                patch.object(main, '_DOWNLOADS_DIR', self.root), \
                patch.object(main, 'download_geotiff', return_value=[]), \
                patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: write_raster(path)):
            response = client.get(f'/api/s5p/search/{result["search_id"]}/scenes/0/download')
        self.assertEqual(response.status_code, 200, response.text)
        geometry.assert_called_with(GEOMETRY, geodesic=False)
        with rasterio.MemoryFile(response.content) as memory:
            assert_mask(self, memory.name)
