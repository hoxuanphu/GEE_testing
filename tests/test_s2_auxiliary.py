"""Optional Sentinel-2 export layers across direct and snapshot downloads."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import imagery, main
from app.geo import Bbox
from app.timeseries import TimeSeriesStore, TimeSeriesService


class TestS2Auxiliary(unittest.TestCase):
    body = dict(dataset='sentinel2', lon=107, lat=10,
                start_date='2026-01-01', end_date='2026-01-02')

    def test_rgbnir_plans_only_four_band_downloads(self):
        image = mock.Mock()
        with mock.patch.object(imagery, '_DownloadURL'), \
             mock.patch.object(imagery.ee.Geometry, 'Rectangle', return_value='roi'):
            urls = imagery.download_geotiff(image, 'sentinel2', Bbox(107, 10, 107.01, 10.01),
                                           10, s2_band_preset='rgbnir')
        expected = ['B2', 'B3', 'B4', 'B8']
        self.assertEqual([band for band, _ in urls], expected)
        self.assertEqual(image.select.call_args_list, [mock.call([band]) for band in expected])

    def test_export_only_selected_auxiliary_layers_in_canonical_order(self):
        for selected in ([], ['QA60', 'AOT']):
            with self.subTest(selected=selected):
                image = mock.Mock()
                with mock.patch.object(imagery, '_DownloadURL'), \
                     mock.patch.object(imagery.ee.Geometry, 'Rectangle', return_value='roi'):
                    urls = imagery.download_geotiff(image, 'sentinel2', Bbox(107, 10, 107.01, 10.01),
                                                   10, s2_auxiliary_bands=selected)
                expected = imagery.S2_SPECTRAL_BANDS + (['AOT', 'QA60'] if selected else [])
                self.assertEqual([band for band, _ in urls], expected)
                self.assertEqual(image.select.call_args_list, [mock.call([band]) for band in expected])

    def test_invalid_selections_rejected_before_network(self):
        with TestClient(main.app) as client, mock.patch.object(main, '_ensure_ee') as auth:
            for route in ('/api/scene', '/api/timeseries/search', '/api/scene-catalog'):
                for selected in (['B2'], ['unknown'], ['AOT', 'AOT']):
                    response = client.post(route, json={**self.body, 's2_auxiliary_bands': selected})
                    self.assertEqual(response.status_code, 422, response.text)
                for invalid in ({'s2_band_preset': 'unknown'},
                                {'dataset': 'landsat', 's2_band_preset': 'rgbnir'},
                                {'s2_band_preset': 'rgbnir', 's2_auxiliary_bands': ['SCL']}):
                    response = client.post(route, json={**self.body, **invalid})
                    self.assertEqual(response.status_code, 422, response.text)
            response = client.get('/api/download/sentinel2', params={
                **self.body, 's2_auxiliary_bands': 'B2'})
            self.assertEqual(response.status_code, 422, response.text)
            auth.assert_not_called()

    def test_scene_link_and_download_retain_empty_or_partial_selection(self):
        scene = imagery.SceneInfo('sentinel2', 'fixture', 'exact', '2026-01-01', 0, 'image')
        with tempfile.TemporaryDirectory() as temp, TestClient(main.app) as client, \
             mock.patch.object(main, '_ensure_ee'), \
             mock.patch.object(main, '_resolve_selection', return_value=('roi', Bbox(107, 10, 107.01, 10.01), None)), \
             mock.patch.object(main, 'best_scene', return_value=scene), \
             mock.patch.object(main, 'preview_rgb', return_value=None), \
             mock.patch.object(main, '_DOWNLOADS_DIR', Path(temp)), \
             mock.patch.object(main, 'download_geotiff', return_value=[]) as export, \
             mock.patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: path.write_bytes(b'tiff')):
            for options, bands in (({'s2_auxiliary_bands': []}, imagery.S2_SPECTRAL_BANDS),
                                   ({'s2_auxiliary_bands': ['AOT', 'QA60']}, imagery.S2_SPECTRAL_BANDS + ['AOT', 'QA60']),
                                   ({'s2_band_preset': 'rgbnir'}, ['B2', 'B3', 'B4', 'B8'])):
                response = client.post('/api/scene', json={**self.body, **options})
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data['bands'], bands)
                response = client.get(data['download_url'])
                self.assertEqual(response.status_code, 200, response.text)
                for key, value in options.items():
                    self.assertIn(f'{key}=', data['download_url'])
                    self.assertEqual(export.call_args.kwargs[key], value)

    def test_timeseries_snapshot_metadata_contains_only_selected_bands(self):
        with tempfile.TemporaryDirectory() as temp, TestClient(main.app) as client, \
             mock.patch.object(main, '_timeseries_store', TimeSeriesStore(Path(temp))), \
             mock.patch.object(main, '_ensure_ee'), \
             mock.patch.object(main, '_resolve_selection', return_value=('roi', Bbox(107, 10, 107.01, 10.01), None)), \
             mock.patch.object(main, 'list_timeseries_scenes', return_value=[]):
            for options, bands in (({'s2_auxiliary_bands': []}, imagery.S2_SPECTRAL_BANDS),
                                   ({'s2_auxiliary_bands': ['SCL']}, imagery.S2_SPECTRAL_BANDS + ['SCL']),
                                   ({'s2_band_preset': 'rgbnir'}, ['B2', 'B3', 'B4', 'B8'])):
                response = client.post('/api/timeseries/search', json={**self.body, **options})
                self.assertEqual(response.status_code, 200, response.text)
                params = response.json()['parameters']
                for key, value in options.items():
                    self.assertEqual(params[key], value)
                self.assertEqual(params['bands'], bands)
                self.assertEqual(list(params['band_metadata']), params['bands'])

    def test_catalog_download_uses_selection_from_immutable_snapshot(self):
        from fastapi import FastAPI
        from app.scene_catalog import register_scene_catalog

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(main, '_DOWNLOADS_DIR', Path(temp)):
            api = FastAPI()
            register_scene_catalog(api)
            with TestClient(api) as client, mock.patch.object(main, '_ensure_ee'), \
                 mock.patch.object(main, '_resolve_selection', return_value=('roi', Bbox(107, 10, 107.01, 10.01), None)), \
                 mock.patch.object(main, 'list_timeseries_scenes', return_value=[dict(
                     asset_id='COPERNICUS/S2_SR_HARMONIZED/EXACT', date='2026-01-01', cloud_pct=0)]), \
                 mock.patch.object(imagery, 'load_timeseries_scene', return_value='image'), \
                 mock.patch.object(main, 'download_geotiff', return_value=[]) as export, \
                 mock.patch.object(main, 'merge_band_tiffs', side_effect=lambda urls, path, **kw: path.write_bytes(b'tiff')):
                for options in ({'s2_auxiliary_bands': []}, {'s2_auxiliary_bands': ['SCL', 'QA60']},
                                {'s2_band_preset': 'rgbnir'}):
                    response = client.post('/api/scene-catalog', json={**self.body, **options})
                    self.assertEqual(response.status_code, 200, response.text)
                    data = response.json()
                    # A later search must not change this link's export layers.
                    client.post('/api/scene-catalog', json={**self.body, 's2_auxiliary_bands': ['WVP']})
                    response = client.get(data['scenes'][0]['download_url'])
                    self.assertEqual(response.status_code, 200, response.text)
                    for key, value in options.items():
                        self.assertEqual(export.call_args.kwargs.get(key), value)

    def test_timeseries_worker_exports_selected_bands_and_retains_band_order(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        for options, bands in (({'s2_auxiliary_bands': []}, imagery.S2_SPECTRAL_BANDS),
                               ({'s2_auxiliary_bands': ['SCL']}, imagery.S2_SPECTRAL_BANDS + ['SCL']),
                               ({'s2_band_preset': 'rgbnir'}, ['B2', 'B3', 'B4', 'B8'])):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temp:
                store = TimeSeriesStore(Path(temp))
                service = TimeSeriesService(store)
                try:
                    params = {**self.body, **options, 'bands': bands, 'scale_m': 10,
                              'bbox': vars(Bbox(107, 10, 107.01, 10.01))}
                    scene = dict(asset_id='COPERNICUS/S2_SR_HARMONIZED/EXACT',
                                 date='2026-01-01', timestamp='2026-01-01T00:00:00Z')
                    snapshot = store.create_search(params, [scene])
                    job = store.create_job(snapshot['search_id'])

                    def merge(urls, path, **kwargs):
                        with rasterio.open(path, 'w', driver='GTiff', width=4, height=4,
                                           count=len(params['bands']), dtype='float32', nodata=-9999,
                                           crs='EPSG:4326', transform=from_origin(107, 10.01, .0001, .0001)) as dst:
                            dst.write(np.ones((len(params['bands']), 4, 4), dtype='float32'))
                            dst.descriptions = tuple(params['bands'])

                    with mock.patch('app.timeseries.load_timeseries_scene', return_value='image'), \
                         mock.patch('app.timeseries.download_geotiff', return_value=[]) as export, \
                         mock.patch('app.timeseries.merge_band_tiffs', side_effect=merge):
                        service.run_timeseries_job(job['job_id'])
                    result = store.get_job(job['job_id'])
                    self.assertEqual(result['status'], 'completed', result)
                    for key, value in options.items():
                        self.assertEqual(export.call_args.kwargs.get(key), value)
                    with rasterio.open(next(store._job_dir(job['job_id']).glob('*.tif'))) as raster:
                        self.assertEqual(list(raster.descriptions), params['bands'])
                finally:
                    service.shutdown()
