"""Measure group coverage on the output grid using masks rather than percentages."""
import importlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import numpy as np
import rasterio
from affine import Affine
from rasterio.io import MemoryFile


class SelectionValidityTests(unittest.TestCase):
    def setUp(self):
        try:
            self.module = importlib.import_module('app.frame_validity')
        except ModuleNotFoundError:
            self.fail('Output-grid selection mask measurement is not implemented')
        self.grid = {'crs': 'EPSG:32648', 'transform': [10, 0, 400000, 0, -10, 2400000],
                     'width': 10, 'height': 2}
        self.params = {'dataset': 'sentinel2', 'bands': ['B4', 'B3'],
                       'output_grid': self.grid, 'roi_fingerprint': 'test-fingerprint'}
        self.scenes = [{'asset_id': 'COPERNICUS/S2_SR_HARMONIZED/first'},
                       {'asset_id': 'COPERNICUS/S2_SR_HARMONIZED/second'}]

    def measure(self, masks):
        rpc = mock.MagicMock()
        images = [mock.Mock() for _ in masks]
        def download(image, bands, grid, window, scoped, deadline):
            return masks[images.index(image)]
        with mock.patch.object(self.module, '_RPC', return_value=rpc), \
                mock.patch.object(self.module, '_load_mask_image', side_effect=images), \
                mock.patch.object(self.module, '_fetch_mask_tile', side_effect=download):
            return self.module.measure_frame_selection(self.scenes, self.params,
                                                        deadline=time.monotonic() + 30)

    def test_overlapping_sixty_percent_is_sixty_not_one_hundred_twenty(self):
        mask = np.zeros((2, 2, 10), dtype=np.uint8)
        mask[:, :, :6] = 1
        result = self.measure([mask, mask])
        self.assertEqual(result['band_coverage_pct'], 60)
        self.assertEqual(result['valid_pixel_count'], 12)
        self.assertEqual(result['total_pixels'], 20)
        self.assertEqual(result['ordered_asset_ids'], [s['asset_id'] for s in self.scenes])

    def test_complementary_masks_cover_the_whole_grid(self):
        a = np.zeros((2, 2, 10), dtype=np.uint8); a[:, :, :6] = 1
        b = np.zeros_like(a); b[:, :, 4:] = 1
        self.assertEqual(self.measure([a, b])['band_coverage_pct'], 100)

    def test_masks_must_intersect_bands_before_union_of_sources(self):
        a = np.zeros((2, 2, 10), dtype=np.uint8); a[0] = 1
        b = np.zeros_like(a); b[1] = 1
        result = self.measure([a, b])
        self.assertEqual(result['valid_pixel_count'], 0)
        self.assertEqual(result['band_coverage_pct'], 0)

    def test_expired_deadline_stops_before_loading_images(self):
        with mock.patch.object(self.module, '_RPC') as rpc:
            with self.assertRaises(Exception) as caught:
                self.module.measure_frame_selection(self.scenes, self.params, deadline=time.monotonic()-1)
        self.assertEqual(caught.exception.code, 'timeout')
        rpc.assert_not_called()

    def test_eqc_multiband_mask_union_matches_final_export_and_cleans_staging(self):
        self._eqc_multiband_measure_and_export('s1gee')

    def test_eqc_s2_finite_value_loss_keeps_mask_estimate_distinct(self):
        self._eqc_multiband_measure_and_export('sentinel2')

    def _eqc_multiband_measure_and_export(self, dataset):
        from app import frame_export as export
        from app.frame_projection import plan_transport_grid
        grid = dict(crs='+proj=eqc +lat_ts=21.7423783969593 +lat_0=21.7423783969593 '
                         '+lon_0=104.035720835673 +R=6378166.17539632 +units=m',
                    transform=[10, 0, -90, 0, -10, 70], width=19, height=13)
        bands = ['VV', 'VH'] if dataset == 's1gee' else ['B4', 'B3']
        params = {**self.params, 'dataset': dataset, 'bands': bands,
                  'output_grid': grid, 'reference': grid, 'merge_scenes': True,
                  'require_full_coverage': False, 'min_coverage_pct': 0}
        transport = plan_transport_grid(grid, params['dataset'], params['bands'])['grid']
        shape = (2, transport['height'], transport['width'])
        first = np.ones(shape, dtype=np.uint8)
        first[0, :, shape[2] // 2:] = 0
        first[1, :, shape[2] * 2 // 5:] = 0
        second = np.ones(shape, dtype=np.uint8)
        second[0, :, :shape[2] * 3 // 5] = 0
        second[1, :, :shape[2] // 2] = 0
        collection = export.COLLECTIONS[dataset]
        scenes = [{'asset_id': collection + '/' + name, 'date': f'2026-10-0{i+1}',
                   'timestamp': f'2026-10-0{i+1}T00:00:00Z'} for i, name in enumerate(['first', 'second'])]
        masks = dict(zip([s['asset_id'] for s in scenes], [first, second]))
        def mask_tile(image, bands, target, window, *args):
            self.assertEqual(target, transport)
            y, x = int(window.row_off), int(window.col_off)
            return masks[image][:, y:y+int(window.height), x:x+int(window.width)]
        def value_tile(image, dataset, bands, target, window, **kwargs):
            self.assertEqual(dataset, 's1gee')  # Initial transport sampling must be nearest, including S2.
            band_index = params['bands'].index(bands[0])
            mask = mask_tile(image, bands, target, window)[band_index:band_index+1]
            value = 0 if image == scenes[0]['asset_id'] else .25
            return np.ma.array(np.full(mask.shape, value, np.float32), mask=~mask.astype(bool))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, '_RPC'), \
                    mock.patch.object(self.module, '_load_mask_image', side_effect=lambda asset, *args: asset), \
                    mock.patch.object(self.module, '_fetch_mask_tile', side_effect=mask_tile):
                quality = self.module.measure_frame_selection(scenes, params,
                    deadline=time.monotonic()+30, work_directory=root)
            self.assertEqual(list(root.iterdir()), [])
            with mock.patch.object(export, '_RPC'), \
                    mock.patch.object(export, 'load_frame_image', side_effect=lambda asset, *args: asset), \
                    mock.patch.object(export, 'fetch_grid_tile', side_effect=value_tile):
                report = export.export_frame_scene({**scenes[0], 'sources': scenes}, params, root/'result.tif')
            with rasterio.open(root/'result.tif') as output:
                data = output.read()
            common = np.isfinite(data).all(axis=0)
            if dataset == 's1gee':
                self.assertEqual(int(common.sum()), quality['valid_pixel_count'])
                self.assertEqual(report['actual_coverage_pct'][bands[0]], quality['band_coverage_pct'])
            else:
                # GDAL bilinear finite values may lose edge pixels despite nearest native validity.
                # This is why mask preflight remains estimated and final TIFF validation is mandatory.
                self.assertLess(int(common.sum()), quality['valid_pixel_count'])
            self.assertEqual(report['actual_coverage_pct'][bands[0]], report['actual_coverage_pct'][bands[1]])
            self.assertTrue(np.any(data[:, common] == 0))
            self.assertTrue(np.any(data[:, common] == .25))
            self.assertFalse(common.all())
            self.assertEqual([p.name for p in root.iterdir()], ['result.tif'])

    def test_mask_failure_releases_lock_and_cleans_private_staging(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with mock.patch.object(self.module, '_RPC'), \
                    mock.patch.object(self.module, '_load_mask_image', side_effect=RuntimeError('upstream')):
                with self.assertRaises(RuntimeError):
                    self.module.measure_frame_selection(self.scenes, self.params,
                        deadline=time.monotonic()+30, work_directory=root)
            self.assertEqual(list(root.iterdir()), [])
            self.assertFalse(self.module._MEASURE_LOCK.locked())

    def test_mask_decoder_validates_grid_and_values(self):
        grid = self.grid
        with MemoryFile() as memory:
            with memory.open(driver='GTiff', width=10, height=2, count=2, dtype='uint8',
                             crs=grid['crs'], transform=Affine(*grid['transform'])) as target:
                target.write(np.ones((2, 2, 10), dtype=np.uint8))
            content = memory.read()
        rpc = mock.Mock(); rpc.thumbnail_once.return_value = content
        with mock.patch.object(self.module, '_mask_graph', return_value=mock.Mock()):
            result = self.module._fetch_mask_tile(mock.Mock(), ['B4','B3'], grid,
                rasterio.windows.Window(0,0,10,2), rpc, time.monotonic()+30)
        self.assertTrue(result.all())

    def test_shared_mask_warp_keeps_zero_and_all_band_holes(self):
        from app import frame_projection
        grid = self.grid
        data = np.ones((2,2,10), dtype=np.uint8)
        data[0,:,6:] = 0
        with MemoryFile() as memory:
            with memory.open(driver='GTiff', width=10, height=2, count=2, dtype='uint8',
                             crs=grid['crs'], transform=Affine(*grid['transform'])) as source:
                source.write(data)
            with memory.open() as source:
                options = getattr(frame_projection, 'mask_warp_options', None)
                self.assertIsNotNone(options, 'Exporter and preflight need a shared mask warp policy')
                with rasterio.vrt.WarpedVRT(source, **options(grid)) as warped:
                    actual = warped.read(masked=True).filled(0)
        np.testing.assert_array_equal(actual, data)


if __name__ == '__main__':
    unittest.main()
