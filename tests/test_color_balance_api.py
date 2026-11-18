import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import numpy as np
import rasterio
from rasterio.transform import from_origin
from fastapi.testclient import TestClient

from app import imagery, main
from app.admin_boundaries import Province
from app.commune_boundaries import Commune
from app.vector_api import VectorArea
from app.geo import Bbox
from app.scene_snapshots import SceneSnapshots


REPORT = {'method': 'clear_overlap_affine_v1', 'spectral_values_modified': True,
          'scenes': [{'scene_id': 'a', 'status': 'reference'},
                     {'scene_id': 'b', 'status': 'applied', 'bands': {
                         'B2': {'status': 'applied', 'gain': 1.2, 'offset': .02}}}]}
BODY = dict(dataset='sentinel2', lon=107, lat=10, radius_m=100,
            start_date='2026-01-01', end_date='2026-01-31', merge_scenes=True, color_balance=True)


class BalanceAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(mock.patch.object(main, '_DOWNLOADS_DIR', self.root))
        self.stack.enter_context(mock.patch.object(main, '_ensure_ee'))
        self.bbox = Bbox(106, 9, 108, 11)
        self.geometry = {'type': 'Polygon', 'coordinates': [[[106, 9], [108, 9], [108, 11], [106, 11], [106, 9]]]}
        self.resolve = self.stack.enter_context(mock.patch.object(main, '_resolve_selection', return_value=('roi', self.bbox, None)))
        self.image = mock.Mock(name='balanced-image')
        self.scene = imagery.SceneInfo('sentinel2', 'collection', 'raw-balanced', '2026-01-01', 10, self.image,
                                       scene_count=2, scenes=(), color_balance=REPORT)
        self.search = self.stack.enter_context(mock.patch.object(main, 'best_scene', return_value=self.scene))
        self.admin = self.stack.enter_context(mock.patch.object(main, 'best_scene_for_roi', return_value=self.scene))
        self.preview = self.stack.enter_context(mock.patch.object(main, 'preview_rgb', return_value='preview'))
        self.export = self.stack.enter_context(mock.patch.object(main, 'download_geotiff', return_value=[]))
        def write(urls, path, **kwargs):
            with rasterio.open(path, 'w', driver='GTiff', width=2, height=2, count=1, dtype='float32',
                               crs='EPSG:4326', transform=from_origin(106, 11, 1, 1), nodata=np.nan) as dst:
                dst.write(np.array([[.2, np.nan], [.3, .4]], dtype='float32'), 1)
        self.merge = self.stack.enter_context(mock.patch.object(main, 'merge_band_tiffs', side_effect=write))
        self.client = self.stack.enter_context(TestClient(main.app))

    def test_preview_download_freeze_same_image_and_write_tiff_provenance(self):
        result = self.client.post('/api/scene', json=BODY)
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertEqual(data['color_balance']['method'], REPORT['method'])
        self.assertTrue(self.search.call_args.kwargs['color_balance'])
        self.preview.assert_called_once()
        self.assertIs(self.preview.call_args.args[0], self.image)
        params = parse_qs(urlsplit(data['download_url']).query)
        self.assertEqual(params['color_balance'], ['true'])
        self.assertIn('color_snapshot', params)
        self.search.side_effect = AssertionError('Download must reuse the preview graph and coefficients')
        response = self.client.get(data['download_url'])
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
        self.assertIs(self.export.call_args.args[0], self.image)
        self.assertTrue(self.merge.call_args.kwargs['preserve_masks'])
        self.assertIn('_balanced.tif', response.headers['content-disposition'])
        with rasterio.MemoryFile(response.content) as memory, memory.open() as src:
            self.assertEqual(json.loads(src.tags()['COLOR_BALANCE']), REPORT)
            self.assertTrue(src.read(1, masked=True).mask[0, 1])

    def test_admin_rectangle_and_vector_propagate_setting(self):
        selections = [
            (dict(selection_mode='province', province_id='VN01'), Province('VN01', 'Province', self.geometry, self.bbox)),
            (dict(selection_mode='commune', commune_id='00001'), Commune('00001', 'Commune', 'VN01', 'Province', self.geometry, self.bbox)),
            (dict(selection_mode='vector', vector_roi_id='00000000-0000-0000-0000-000000000001'), VectorArea('00000000-0000-0000-0000-000000000001', 'Vector', self.geometry, self.bbox)),
            (dict(selection_mode='rectangle', center_lon=107, center_lat=10, width_m=2000, height_m=1000), None),
        ]
        for fields, area in selections:
            with self.subTest(mode=fields['selection_mode']):
                body = {**BODY, **fields}
                if fields['selection_mode'] in ('rectangle', 'vector'):
                    body.pop('lon')
                    body.pop('lat')
                if fields['selection_mode'] == 'vector':
                    body.pop('radius_m')
                self.resolve.return_value = ('roi', self.bbox, area)
                response = self.client.post('/api/scene', json=body)
                self.assertEqual(response.status_code, 200, response.text)
                search = self.admin if area else self.search
                self.assertTrue(search.call_args.kwargs['color_balance'])
                self.assertIn('color_balance=true', response.json()['download_url'])
                downloaded = self.client.get(response.json()['download_url'])
                self.assertEqual(downloaded.status_code, 200,
                    downloaded.text if downloaded.status_code != 200 else '')

    def test_default_is_off_and_unsupported_enabled_requests_are_rejected_before_ee(self):
        self.assertFalse(main.SceneRequest(**{k: v for k, v in BODY.items() if k != 'color_balance'}).color_balance)
        for changes in [dict(merge_scenes=False), dict(dataset='nicfi'),
                        dict(selection_mode='province', province_id='VN01', composite_method='median')]:
            with self.subTest(changes=changes), mock.patch.object(main, '_ensure_ee') as ensure:
                response = self.client.post('/api/scene', json={**BODY, **changes})
                self.assertEqual(response.status_code, 422, response.text)
                ensure.assert_not_called()
        response = self.client.get('/api/download/nicfi', params={**BODY, 'dataset': 'nicfi'})
        self.assertEqual(response.status_code, 422)

    def test_snapshot_mismatch_and_missing_token_do_not_recompute(self):
        result = self.client.post('/api/scene', json=BODY).json()
        url = result['download_url']
        self.search.side_effect = AssertionError('No recalibration after a snapshot error')
        mismatch = self.client.get(url.replace('lon=107.0', 'lon=108.0'))
        self.assertEqual(mismatch.status_code, 409, mismatch.text)
        query = parse_qs(urlsplit(url).query)
        token = query['color_snapshot'][0]
        expired = self.client.get(url.replace(token, 'missing-token'))
        self.assertEqual(expired.status_code, 410, expired.text)


class SnapshotTests(unittest.TestCase):
    def test_capacity_expiry_and_mismatch_preserve_original_entry(self):
        cache = SceneSnapshots(capacity=2, ttl_seconds=10)
        with mock.patch('app.scene_snapshots.monotonic', return_value=100):
            first = cache.put('key', 'first')
            second = cache.put('key', 'second')
            with self.assertRaises(ValueError):
                cache.get(first, 'tampered')
            self.assertEqual(cache.get(first, 'key'), 'first')
            cache.put('key', 'third')
            with self.assertRaises(LookupError):
                cache.get(first, 'key')
        with mock.patch('app.scene_snapshots.monotonic', return_value=111):
            with self.assertRaises(LookupError):
                cache.get(second, 'key')


class EntryPointTests(unittest.TestCase):
    def test_raw_point_and_admin_keep_per_band_report(self):
        detail = REPORT['scenes'][1]
        picked = [{'scene_id': 'b', 'date': '2026-01-01', 'cloud': 10, 'color_balance': detail}]
        collection, image = mock.Mock(), mock.Mock()
        collection.filterBounds.return_value.filter.return_value = collection
        collection.size.return_value.getInfo.return_value = 2
        for mode in ('point', 'province'):
            with self.subTest(mode=mode), \
                 mock.patch.object(imagery, '_optical_collection', return_value=(collection, 'cloud', 10)), \
                 mock.patch.object(imagery.ee.Filter, 'lt', return_value='filter'), \
                 mock.patch.object(imagery, '_least_cloudy_per_tile', return_value=(image, picked)) as picker, \
                 mock.patch.object(imagery, '_coverage_fraction', return_value=mock.Mock(getInfo=lambda: 1.)):
                if mode == 'point':
                    result = imagery.best_scene('sentinel2', 107, 10, 2000, '2026-01-01', '2026-02-01',
                        merge_scenes=True, roi='roi', color_balance=True)
                else:
                    result = imagery.best_scene_for_roi('sentinel2', 'roi', '2026-01-01', '2026-02-01',
                        composite=True, composite_method='raw', color_balance=True)
                self.assertTrue(picker.call_args.kwargs['color_balance'])
                self.assertEqual(result.color_balance['scenes'], [detail])
                self.assertEqual(result.scenes[0]['color_balance'], detail)


if __name__ == '__main__':
    unittest.main()
