"""Offline Sentinel-5P individual scene contract tests."""
import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import main, s5p


class S5PTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / 'downloads' / ('test-s5p-' + uuid.uuid4().hex)
        self.store = main.TimeSeriesStore(self.root)
        self.roi = MagicMock()
        self.roi.getInfo.return_value = {'type': 'Polygon', 'coordinates': [[[106, 10], [107, 10], [107, 11], [106, 10]]]}
        self.patches = [patch.object(main, '_DOWNLOADS_DIR', self.root),
                        patch.object(main, '_ensure_ee'),
                        patch.object(main, '_resolve_selection', return_value=(self.roi, main.Bbox(106, 10, 107, 11), None))]
        for p in self.patches:
            p.start()
        # Route closures use the persistent application store; remove only our own snapshots below.
        self.client = TestClient(main.app)
        self.search_ids = []
        self.body = dict(product='NO2', lon=106.5, lat=10.5, start_date='2026-01-01', end_date='2026-01-01')
        self.scene = dict(scene_id='20260101T010000_20260101T024000',
                          asset_id='COPERNICUS/S5P/OFFL/L3_NO2/20260101T010000_20260101T024000',
                          timestamp='2026-01-01T01:00:00Z', date='2026-01-01')

    def tearDown(self):
        for sid in self.search_ids:
            with main._s5p_store._connect() as db:
                db.execute('DELETE FROM searches WHERE id=?', (sid,))
        self.client.close()
        for p in reversed(self.patches):
            p.stop()
        shutil.rmtree(self.root)

    def search(self, body=None, scenes=None):
        with patch.object(s5p, 'list_scenes', return_value=(scenes if scenes is not None else [self.scene], False)) as listing:
            response = self.client.post('/api/s5p/search', json=body or self.body)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.search_ids.append(data['search_id'])
        self.assertEqual(listing.call_args.args[3], '2026-01-02')
        return data

    def test_products_and_strict_validation(self):
        products = self.client.get('/api/s5p/products').json()
        self.assertEqual(len(products), 7)
        for item in products:
            self.assertTrue(item['collection'].startswith('COPERNICUS/S5P/OFFL/L3_'))
        self.assertEqual(s5p.product_metadata('AAI')['collection'], 'COPERNICUS/S5P/OFFL/L3_AER_AI')
        for update in [dict(product='PM25'), dict(asset_id='bad'), dict(start_date='20260101'),
                       dict(end_date='2025-01-01'), dict(end_date='9999-12-31'), dict(lat=91),
                       dict(radius_m=0), dict(selection_mode='province'), dict(selection_mode='reference'),
                       dict(selection_mode='rectangle', center_lon=106, center_lat=10, width_m=10, height_m=10),
                       dict(province_id='01')]:
            with patch.object(main, '_ensure_ee') as auth:
                response = self.client.post('/api/s5p/search', json={**self.body, **update})
                self.assertEqual(response.status_code, 422, update)
                auth.assert_not_called()

    def test_snapshot_individual_ids_and_pagination(self):
        scenes = [{**self.scene, 'scene_id': f'scene_{i}', 'asset_id': f'COPERNICUS/S5P/OFFL/L3_NO2/scene_{i}'} for i in range(53)]
        data = self.search(scenes=scenes)
        self.assertEqual(len(data['scenes']), 50)
        url = '/api/s5p/search/' + data['search_id']
        with patch.object(s5p, 'list_scenes', side_effect=AssertionError('No relisting')):
            page = self.client.get(url + '?offset=50').json()
        self.assertEqual([s['scene_id'] for s in page['scenes']], ['scene_50', 'scene_51', 'scene_52'])
        self.assertEqual(page['scenes'][0]['timestamp'], self.scene['timestamp'])
        self.assertEqual(self.client.get(url + '?offset=-1').status_code, 400)
        self.assertEqual(self.client.get(url + '/scenes/53/download').status_code, 404)
        self.assertEqual(self.client.get(url + '/scenes/-1/download').status_code, 404)
        self.assertEqual(self.client.get('/api/s5p/search/missing').status_code, 404)
        self.assertEqual(self.search(scenes=[])['total'], 0)

    def test_selection_modes_and_grid(self):
        for selection in [dict(selection_mode='province', province_id='01'),
                          dict(selection_mode='commune', commune_id='00001'),
                          dict(selection_mode='rectangle', center_lon=106.5, center_lat=10.5,
                               width_m=2000, height_m=3000, rotation_deg=20)]:
            body = {k: v for k, v in self.body.items() if k not in ('lon', 'lat')}
            data = self.search({**body, **selection})
            self.assertEqual(data['parameters']['roi_geometry'], self.roi.getInfo())
            if selection['selection_mode'] == 'rectangle':
                self.assertIn('output_grid', data['parameters'])

    def test_download_and_preview_only_selected_asset(self):
        data = self.search()
        scene = data['scenes'][0]
        image = MagicMock()
        image.select.return_value.toFloat.return_value.clip.return_value = image
        image.getThumbURL.return_value = 'https://example.invalid/preview.png'
        def merge(urls, path, **kwargs):
            Path(path).write_bytes(b'fake-geotiff')
        with patch.object(s5p.ee, 'Image', return_value=image) as constructor, \
                patch.object(s5p.ee, 'Geometry'), \
                patch.object(main, 'download_geotiff', return_value=[('band', 'url')]) as export, \
                patch.object(main, 'merge_band_tiffs', side_effect=merge):
            response = self.client.get(scene['download_url'])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b'fake-geotiff')
            constructor.assert_called_with(self.scene['asset_id'])
            self.assertEqual(export.call_args.args[1:4], ('s5p_NO2', main.Bbox(106, 10, 107, 11), s5p.SCALE_M))
            self.assertEqual(list(self.root.glob('*.tif')), [])
            preview = self.client.get(scene['preview_url'])
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()['unit'], 'mol/m²')
        image.mean.assert_not_called()
        image.select.assert_called_with('tropospheric_NO2_column_number_density')

    def test_download_failure_and_rotated_warp_cleanup(self):
        data = self.search({**self.body, 'rotation_deg': 30})
        with patch.object(s5p.ee, 'Image'), patch.object(s5p.ee, 'Geometry'), \
                patch.object(main, 'download_geotiff', return_value=[]), \
                patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: Path(path).write_bytes(b'source')), \
                patch.object(main, 'warp_to_grid', side_effect=lambda raw, out, grid: Path(out).write_bytes(b'warped')) as warp:
            response = self.client.get(data['scenes'][0]['download_url'])
            self.assertEqual(response.content, b'warped')
            warp.assert_called_once()
        self.assertEqual(list(self.root.glob('*.tif')), [])
        with patch.object(s5p.ee, 'Image'), patch.object(s5p.ee, 'Geometry'), \
                patch.object(main, 'download_geotiff', side_effect=RuntimeError('unavailable')):
            self.assertEqual(self.client.get(data['scenes'][0]['download_url']).status_code, 502)

    def test_real_export_helper_selects_all_seven_bands_and_nodata(self):
        from app import imagery
        for product in s5p.PRODUCTS:
            image = MagicMock()
            selected = image.clip.return_value.select.return_value
            one_band = selected.multiply.return_value.toFloat.return_value.unmask.return_value
            with patch.object(imagery.ee, 'Geometry'), patch.object(imagery, '_DownloadURL') as url:
                result = imagery.download_geotiff(image, 's5p_' + product,
                    main.Bbox(106, 10, 106.01, 10.01), s5p.SCALE_M, self.roi)
            band = s5p.product_metadata(product)['band']
            image.clip.assert_called_once_with(self.roi)
            image.clip.return_value.select.assert_called_once_with([band])
            selected.multiply.return_value.toFloat.return_value.unmask.assert_called_once_with(-9999, sameFootprint=False)
            self.assertEqual(result[0][0], band)
            self.assertIs(url.call_args.args[0], one_band)
            self.assertEqual(url.call_args.args[1]['scale'], s5p.SCALE_M)

    def test_scene_listing_dates_real_asset_ids(self):
        collection = MagicMock()
        collection.filterBounds.return_value.filterDate.return_value.sort.return_value = collection
        rows = [{'system:index': '20260101T235959', 'system:time_start': 1767311999000},
                {'system:index': '20260102T000001', 'system:time_start': 1767312001000}]
        collection.limit.return_value.toList.return_value.map.return_value.getInfo.return_value = rows
        with patch.object(s5p.ee, 'ImageCollection', return_value=collection) as factory:
            scenes, truncated = s5p.list_scenes('CH4', self.roi, '2026-01-01', '2026-01-03', limit=1)
        factory.assert_called_once_with('COPERNICUS/S5P/OFFL/L3_CH4')
        self.assertTrue(truncated)
        self.assertEqual(scenes[0]['timestamp'], '2026-01-01T23:59:59Z')
        self.assertEqual(scenes[0]['asset_id'], 'COPERNICUS/S5P/OFFL/L3_CH4/20260101T235959')


if __name__ == '__main__':
    unittest.main()
