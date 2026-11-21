"""Offline cloud-range listing and exact-scene download regression tests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError

from app import main
from app.scene_catalog import register_scene_catalog

FOOTPRINT = {'type': 'Polygon', 'coordinates': [[[105, 9], [107, 9],
    [107, 11], [105, 11], [105, 9]]]}


class TestSceneCatalog(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        app = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', Path(self.temp.name)):
            self.store = register_scene_catalog(app)
        self.client = TestClient(app)
        self.body = dict(dataset='s2aws', lon=106, lat=10, start_date='2026-01-01',
                         end_date='2026-01-31', cloud_min=20, cloud_max=50)

    def test_validation_rejects_invalid_ranges_before_network(self):
        with patch.object(main, 'aws_search_s2_l2a_bbox') as search:
            for values in ({'cloud_min': -1}, {'cloud_max': 101},
                           {'cloud_min': 60, 'cloud_max': 50}, {'cloud_min': 'NaN'},
                           {'start_date': '2026-02-01'}, {'end_date': '9999-12-31'}):
                with self.subTest(values=values):
                    self.assertEqual(self.client.post('/api/scene-catalog', json={**self.body, **values}).status_code, 422)
            search.assert_not_called()

    def test_gee_full_scene_uses_the_paged_asset_and_band_preset_without_roi(self):
        asset = 'COPERNICUS/S2_SR_HARMONIZED/EXACT'
        snapshot = self.store.create_search(
            {'source': 'gee', 'dataset': 'sentinel2', 'selection_mode': 'rectangle',
             's2_band_preset': 'rgbnir'},
            [{'asset_id': 'COPERNICUS/S2_SR_HARMONIZED/OTHER'}, {'asset_id': asset}],
        )
        search_id = snapshot['search_id']
        page = self.client.get(f'/api/scene-catalog/{search_id}?offset=1&limit=1').json()
        scene = page['scenes'][0]
        self.assertEqual(scene['download_url'], f'/api/scene-catalog/{search_id}/download/1')
        self.assertEqual(scene['full_scene_download_url'], scene['download_url'] + '?extent=scene')
        def export(asset_id, dataset, path, **options):
            path.write_bytes(b'full-scene-fixture')
        with patch.object(main, '_DOWNLOADS_DIR', Path(self.temp.name)), \
             patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection') as resolve_roi, \
             patch('app.scene_catalog.export_full_scene', create=True, side_effect=export) as full:
            response = self.client.get(scene['full_scene_download_url'])
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, b'full-scene-fixture')
            self.assertIn('sentinel2_EXACT_rgbnir_full_scene.tif', response.headers['content-disposition'])
            self.assertEqual(full.call_args.args[:2], (asset, 'sentinel2'))
            self.assertEqual(full.call_args.kwargs, {'s2_band_preset': 'rgbnir'})
            resolve_roi.assert_not_called()
            self.assertFalse(full.call_args.args[2].exists())

    def test_external_row_cannot_use_the_gee_full_scene_export(self):
        data = self.store.create_search({'source': 'aws', 'selection_mode': 'point'}, [{'id': 'item'}])
        page = self.client.get(f'/api/scene-catalog/{data["search_id"]}').json()
        scene = page['scenes'][0]
        self.assertNotIn('full_scene_download_url', scene)
        url = scene['download_url'] + '?extent=scene'
        self.assertEqual(self.client.get(url, follow_redirects=False).status_code, 409)

    def test_provider_filter_before_snapshot_pagination_and_unknown_cloud(self):
        rows = [dict(id=f'S2A_{i}', date='2026-01-05', cloud_pct=30) for i in range(61)]
        rows += [dict(id='unknown', cloud_pct=None), dict(id='below', cloud_pct=5)]
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=rows) as search:
            response = self.client.post('/api/scene-catalog', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data['total'], 61)
        self.assertEqual(len(data['scenes']), 50)
        self.assertEqual(search.call_args.kwargs['cloud_min'], 20)
        self.assertEqual(search.call_args.kwargs['cloud_max'], 50)
        second = self.client.get(f'/api/scene-catalog/{data["search_id"]}?offset=50').json()
        self.assertEqual(len(second['scenes']), 11)
        self.assertTrue(second['scenes'][0]['download_url'].endswith('/50'))
        self.assertEqual(self.client.get(f'/api/scene-catalog/{data["search_id"]}?limit=51').status_code, 400)

    def test_public_aws_thumbnail_survives_catalogue_snapshot_sanitizer(self):
        preview = ('https://sentinel-cogs.s3.us-west-2.amazonaws.com/'
                   'sentinel-s2-l2a-cogs/48/P/ZC/2026/11/scene/preview.jpg')
        row = dict(id='scene', date='2026-01-05', cloud_pct=30, thumbnail_url=preview)
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=[row]):
            data = self.client.post('/api/scene-catalog', json=self.body).json()
        page = self.client.get(f'/api/scene-catalog/{data["search_id"]}').json()
        self.assertEqual(page['scenes'][0]['thumbnail_url'], preview)
        _, stored = self.store._snapshot(data['search_id'])
        self.assertNotIn('thumbnail_url', stored[0])

        identifier = 'S2C_48PZC_20261117_0_L2A'
        old = self.store.create_search({'source': 'aws', 'selection_mode': 'point'},
                                       [{'id': identifier}])
        url = self.client.get(f'/api/scene-catalog/{old["search_id"]}').json()['scenes'][0]['thumbnail_url']
        with patch('app.aws_stac.get_item', return_value={'id': identifier, 'thumbnail_url': preview}) as get_item:
            result = self.client.get(url, follow_redirects=False)
        get_item.assert_called_once_with(identifier, timeout=8)
        self.assertEqual(result.headers['location'], preview)

    def test_aws_exact_product_download_uses_snapshot_rotation(self):
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=[dict(id='S2B_EXACT', date='2026-01-01', cloud_pct=25)]):
            data = self.client.post('/api/scene-catalog', json={**self.body, 'rotation_deg': 30}).json()
        response = self.client.get(data['scenes'][0]['download_url'], follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn('product_id=S2B_EXACT', response.headers['location'])
        self.assertIn('rotation_deg=30', response.headers['location'])
        self.assertEqual(self.client.get(f'/api/scene-catalog/{data["search_id"]}/download/3').status_code, 404)

    def test_page_keeps_scene_footprint_and_omits_invalid_provider_geometry(self):
        broken = {'type': 'Polygon', 'coordinates': [[[105, 9], [107, 9], [107, 11]]]}
        rows = [dict(id='valid', date='2026-01-01', cloud_pct=25, footprint=FOOTPRINT),
                dict(id='invalid', date='2026-01-02', cloud_pct=30, footprint=broken)]
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=rows):
            data = self.client.post('/api/scene-catalog', json=self.body).json()
        self.assertEqual(data['total'], 2)
        self.assertIsNone(data['scenes'][0].get('footprint'))
        page = self.client.get(f'/api/scene-catalog/{data["search_id"]}?offset=1&limit=1').json()
        self.assertEqual(page['scenes'][0]['id'], 'valid')
        self.assertEqual(page['scenes'][0]['footprint'], FOOTPRINT)
        self.assertTrue(page['scenes'][0]['download_url'].endswith('/1'))

    def test_thumbnail_uses_the_scene_and_roi_from_its_catalogue_page(self):
        asset = 'COPERNICUS/S2_SR_HARMONIZED/EXACT_C'
        stored = self.store.create_search(
            {'source': 'gee', 'dataset': 'sentinel2', 'selection_mode': 'rectangle',
             'roi_geometry': FOOTPRINT},
            [{'asset_id': 'COPERNICUS/S2_SR_HARMONIZED/OTHER'}, {'asset_id': asset}],
        )
        search_id = stored['search_id']
        with patch.object(main, '_ensure_ee'), \
             patch('app.imagery.render_catalog_thumbnail', return_value=b'fixture-png') as render:
            page = self.client.get(f'/api/scene-catalog/{search_id}?offset=1&limit=1').json()
            self.assertIn('thumbnail_url', page['scenes'][0])
            url = page['scenes'][0]['thumbnail_url']
            self.assertEqual(url, f'/api/scene-catalog/{search_id}/thumbnail/1')
            render.assert_not_called()
            for _ in range(2):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'image/png')
                self.assertEqual(response.content, b'fixture-png')
            render.assert_called_once()
            args = render.call_args.args
            self.assertEqual(args[:2], (asset, 'sentinel2'))
            self.assertEqual(json.loads(args[2]), FOOTPRINT)
        self.assertEqual(self.client.get(f'/api/scene-catalog/{search_id}/thumbnail/2').status_code, 404)

    def test_gee_inclusive_dates_roi_and_exact_asset_download(self):
        asset = 'COPERNICUS/S2_SR_HARMONIZED/EXACT'
        with patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=('roi', main.Bbox(105, 9, 107, 11), None)), \
             patch.object(main, 'list_timeseries_scenes', return_value=[dict(asset_id=asset, date='2026-01-31', cloud_pct=35)]) as listing:
            data = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 'sentinel2'}).json()
            self.assertEqual(listing.call_args.args[4], '2026-02-01')
            self.assertEqual(listing.call_args.kwargs['roi'], 'roi')
            self.assertTrue(listing.call_args.kwargs.get('include_footprint'))
            with patch('app.imagery.load_timeseries_scene', return_value='exact-image') as load, \
                 patch.object(main, 'download_geotiff', return_value=[]) as urls, \
                 patch.object(main, '_DOWNLOADS_DIR', Path(self.temp.name)), \
                 patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kwargs: path.write_bytes(b'fixture')):
                response = self.client.get(data['scenes'][0]['download_url'])
            self.assertEqual(response.status_code, 200)
            load.assert_called_once_with(asset, 'sentinel2')
            self.assertEqual(urls.call_args.args[0], 'exact-image')

    def test_gee_timeout_has_safe_message_and_creates_no_snapshot(self):
        with patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection',
                return_value=('roi', main.Bbox(105, 9, 107, 11), None)), \
                patch.object(main, 'list_timeseries_scenes', side_effect=TimeoutError('private URL')), \
                self.assertLogs('uvicorn.error', level='ERROR') as logs:
            response = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 'sentinel2'})
        self.assertEqual(response.status_code, 504, response.text)
        self.assertNotIn('private', response.text + '\n'.join(logs.output))
        self.assertIn(response.headers['X-Search-Request-ID'], '\n'.join(logs.output))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM searches').fetchone()[0], 0)

    def test_gee_initial_auth_failure_is_sanitized_and_correlated(self):
        with patch.object(main, '_ee_ready', False), \
                patch.object(main, 'init_ee', side_effect=RefreshError('private credentials')), \
                self.assertLogs('uvicorn.error', level='ERROR') as logs:
            response = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 'sentinel2'})
        self.assertEqual(response.status_code, 502, response.text)
        text = response.text + '\n'.join(logs.output)
        self.assertNotIn('private', text)
        self.assertIn('stage=initialization', text)
        self.assertIn(response.headers['X-Search-Request-ID'], text)

    def test_gee_selected_platform_reaches_listing_and_exact_download_snapshot(self):
        asset = 'COPERNICUS/S2_SR_HARMONIZED/EXACT_C'
        with patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection',
                return_value=('roi', main.Bbox(105, 9, 107, 11), None)), \
                patch.object(main, 'list_timeseries_scenes', return_value=[dict(asset_id=asset,
                    date='2026-01-31', cloud_pct=35, source_metadata={'SPACECRAFT_NAME': 'Sentinel-2C'})]) as listing:
            response = self.client.post('/api/scene-catalog', json={**self.body,
                'dataset': 'sentinel2', 'platform': 'C'})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(listing.call_args.kwargs.get('platform'), 'C')
            self.assertEqual(data['parameters']['platform'], 'C')
            self.assertEqual(data['scenes'][0]['platform'], 'Sentinel-2C')
            with patch('app.imagery.load_timeseries_scene', return_value='exact-image') as load, \
                    patch.object(main, 'download_geotiff', return_value=[]), \
                    patch.object(main, '_DOWNLOADS_DIR', Path(self.temp.name)), \
                    patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kwargs: path.write_bytes(b'fixture')):
                result = self.client.get(data['scenes'][0]['download_url'])
            self.assertEqual(result.status_code, 200, result.text)
            load.assert_called_once_with(asset, 'sentinel2')

    def test_selected_platform_snapshot_mismatch_cannot_download(self):
        asset = 'COPERNICUS/S2_SR_HARMONIZED/WRONG_A'
        with patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection',
                return_value=('roi', main.Bbox(105, 9, 107, 11), None)), \
                patch.object(main, 'list_timeseries_scenes', return_value=[dict(asset_id=asset,
                    date='2026-01-31', cloud_pct=35, source_metadata={'SPACECRAFT_NAME': 'Sentinel-2A'})]):
            data = self.client.post('/api/scene-catalog', json={**self.body,
                'dataset': 'sentinel2', 'platform': 'C'}).json()
        with patch.object(main, '_ensure_ee') as ensure, patch('app.imagery.load_timeseries_scene') as load:
            result = self.client.get(data['scenes'][0]['download_url'])
        self.assertEqual(result.status_code, 409)
        ensure.assert_not_called()
        load.assert_not_called()

    def test_rotated_gee_exact_asset_warps_snapshot_grid(self):
        asset = 'COPERNICUS/S2_SR_HARMONIZED/ROTATED'
        with patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=('roi', main.Bbox(105, 9, 107, 11), None)), \
             patch.object(main, 'list_timeseries_scenes', return_value=[dict(asset_id=asset, date='2026-01-31', cloud_pct=35)]):
            data = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 'sentinel2', 'rotation_deg': 45}).json()
            with patch('app.imagery.load_timeseries_scene', return_value='exact'), \
                 patch.object(main, 'download_geotiff', return_value=[]), \
                 patch.object(main, '_DOWNLOADS_DIR', Path(self.temp.name)), \
                 patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kwargs: path.write_bytes(b'source')), \
                 patch.object(main, 'warp_to_grid', side_effect=lambda src, dst, grid: dst.write_bytes(b'rotated')) as warp:
                response = self.client.get(data['scenes'][0]['download_url'])
            self.assertEqual(response.content, b'rotated')
            grid = warp.call_args.args[2]
            self.assertEqual(grid, main.build_rotated_grid(106, 10, 2000, 45, main.S2_SCALE))
            self.assertFalse(list(Path(self.temp.name).glob('*.source.tif')))

    def test_landsat_administrative_list_is_not_a_composite(self):
        with patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=('province-roi', main.Bbox(105, 9, 107, 11), object())), \
             patch.object(main, 'list_timeseries_scenes', return_value=[]) as listing, \
             patch.object(main, 'best_scene') as best:
            response = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 'landsat', 'selection_mode': 'province', 'province_id': 'test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(listing.call_args.args[0], 'landsat')
        self.assertEqual(listing.call_args.kwargs['roi'], 'province-roi')
        best.assert_not_called()

    def test_cdse_exact_product_preserves_commune_selection(self):
        with patch.object(main, 'get_commune', return_value=Mock(bbox=main.Bbox(105, 9, 107, 11))), \
             patch.object(main, 'search_s2_area', return_value=[dict(id='exact-product', date='2026-01-01', cloud_pct=25)]):
            data = self.client.post('/api/scene-catalog', json={**self.body, 'dataset': 's2', 'selection_mode': 'commune', 'commune_id': 'test'}).json()
        response = self.client.get(data['scenes'][0]['download_url'], follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertIn('product_id=exact-product', response.headers['location'])
        self.assertIn('commune_id=test', response.headers['location'])

    def test_zero_cloud_is_valid_and_empty_results_are_success(self):
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=[dict(id='zero', cloud_pct=0)]):
            data = self.client.post('/api/scene-catalog', json={**self.body, 'cloud_min': 0, 'cloud_max': 0}).json()
            self.assertEqual(data['total'], 1)
        with patch.object(main, 'aws_search_s2_l2a_bbox', return_value=[]):
            response = self.client.post('/api/scene-catalog', json=self.body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['total'], 0)


class TestProviderCloudRanges(unittest.TestCase):
    def test_cdse_odata_range_and_expanded_metadata(self):
        from app import cdse
        rows = [dict(Id='id', Name='S2A_MSIL2A_T48PWS_fixture', GeoFootprint=FOOTPRINT,
                     Attributes=[{'Name': 'cloudCover', 'Value': 40}])]
        with patch.object(cdse, '_ensure_env'), patch.object(cdse, '_fetch_products', return_value=rows) as fetch:
            result = cdse.search_s2_area((105, 9, 107, 11), '2026-01-01', '2026-01-31', cloud_min=20, cloud_max=50)
        query = fetch.call_args.args[0]['$filter']
        self.assertIn('Attributes/OData.CSC.DoubleAttribute/any', query)
        self.assertIn('d/OData.CSC.DoubleAttribute/Value ge 20.0', query)
        self.assertIn('d/OData.CSC.DoubleAttribute/Value le 50.0', query)
        self.assertTrue(fetch.call_args.kwargs['expand'])
        self.assertEqual(result[0]['cloud_pct'], 40)
        self.assertEqual(result[0]['footprint'], FOOTPRINT)

    def test_aws_stac_cloud_range(self):
        from app import aws_stac
        features = [dict(id='native-id', geometry=FOOTPRINT, properties={'eo:cloud_cover': 40})]
        with patch.object(aws_stac, '_ensure_cas'), patch.object(aws_stac, '_search_stac', return_value=features) as search:
            result = aws_stac.search_s2_l2a_bbox((105, 9, 107, 11), '2026-01-01', '2026-01-31', cloud_min=20, cloud_max=50)
        self.assertEqual(search.call_args.args[0]['query']['eo:cloud_cover'], {'gte': 20.0, 'lte': 50.0})
        self.assertEqual(result[0]['footprint'], FOOTPRINT)
