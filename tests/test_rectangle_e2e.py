"""Full HTTP search-to-download contract with synthetic GEE imagery only."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from affine import Affine
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from app import main
from app.geo import RectangleROI
from app.imagery import SceneInfo


class TestRectangleSearchToDownload(unittest.TestCase):
    def test_ui_shaped_requests_follow_returned_links_to_exact_raster_grid(self):
        with tempfile.TemporaryDirectory() as temp, TestClient(main.app) as client:
            root = Path(temp)
            for dataset in ('sentinel2', 'landsat', 's1gee'):
                for angle in (0, 25, 90, 360):
                    with self.subTest(dataset=dataset, angle=angle):
                        roi = RectangleROI(center_lon=107, center_lat=10,
                                           width_m=200, height_m=100, rotation_deg=angle)
                        grid = roi.output_grid(main.S1_SCALE if dataset == 's1gee' else main._scale_for(dataset))
                        scene = SceneInfo(dataset, 'fixture', 'exact-scene', '2026-01-01', 0,
                                          object(), coverage_pct=100)
                        def merge(urls, destination, **kwargs):
                            self.assertTrue(kwargs['preserve_masks'])
                            with rasterio.open(destination, 'w', driver='GTiff', width=80,
                                               height=80, count=1, dtype='float32',
                                               crs=grid['crs'], transform=from_origin(-400, 400, 10, 10),
                                               nodata=float('nan')) as dst:
                                dst.write(np.zeros((80, 80), dtype='float32'), 1)
                        body = dict(selection_mode='rectangle', **roi.model_dump(),
                                    start_date='2026-01-01', end_date='2026-01-02')
                        if angle == 360:
                            body['rotation_deg'] = 360
                        optical = dataset != 's1gee'
                        if optical:
                            body['dataset'] = dataset
                        endpoint = '/api/scene' if optical else '/api/s1gee/scene'
                        selector = 'best_scene' if optical else 'best_s1_scene'
                        with mock.patch.object(main, '_ensure_ee'), \
                                mock.patch('ee.Geometry.Polygon', return_value='true-polygon'), \
                                mock.patch.object(main, selector, return_value=scene) as search, \
                                mock.patch.object(main, 'preview_rgb', return_value=None), \
                                mock.patch.object(main, '_DOWNLOADS_DIR', root), \
                                mock.patch.object(main, 'download_geotiff', return_value=[]) as export, \
                                mock.patch.object(main, 'merge_band_tiffs', side_effect=merge):
                            found = client.post(endpoint, json=body)
                            self.assertEqual(found.status_code, 200, found.text)
                            data = found.json()
                            self.assertEqual(data['roi_geometry'], roi.geometry)
                            self.assertEqual(search.call_args.kwargs['roi'], 'true-polygon')
                            downloaded = client.get(data['download_url'])
                            self.assertEqual(downloaded.status_code, 200, downloaded.text)
                            self.assertEqual(search.call_args.kwargs['roi'], 'true-polygon')
                            self.assertIsNone(export.call_args.args[4])
                            self.assertLess(export.call_args.args[2].west, roi.bbox.west)
                            with rasterio.MemoryFile(downloaded.content) as mem, mem.open() as dst:
                                self.assertEqual(dst.shape, (grid['height'], grid['width']))
                                self.assertEqual(dst.transform, Affine(*grid['transform']))
                                self.assertEqual(dst.crs, rasterio.crs.CRS.from_wkt(grid['crs']))
                                self.assertTrue(np.all(dst.read_masks() == 255))
                                np.testing.assert_allclose(dst.read(1), 0)
                            self.assertFalse(list(root.glob('*.source.tif')))
