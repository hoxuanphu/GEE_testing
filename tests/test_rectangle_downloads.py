"""Rectangle time-series and provider exports with offline raster fixtures."""
import json
import shutil
import zipfile
from unittest import mock

import numpy as np
import rasterio
from affine import Affine
from rasterio.transform import from_origin
from fastapi.testclient import TestClient

from app import main, timeseries as ts, aws_stac
from app.geo import RectangleROI
from tests.test_rotated_roi import RasterCase


class TestRectangleDownloads(RasterCase):
    def rectangle(self, angle=30):
        return RectangleROI(center_lon=107, center_lat=10, width_m=200, height_m=100, rotation_deg=angle)

    def parameters(self, angle=30):
        return {'selection_mode': 'rectangle', **self.rectangle(angle).model_dump()}

    def test_download_models_reject_bad_rectangles_before_auth_or_network(self):
        with mock.patch.object(main, 'aws_get_item') as get, \
                mock.patch.object(main, '_ensure_cdse_creds') as auth, TestClient(main.app) as client:
            for route in ('/api/s2/aws/download', '/api/s2/bands'):
                for changes in ({'width_m': 0}, {'height_m': None}, {'lon': 107},
                                {'radius_m': 1}, {'province_id': 'test'}, {'rotation_deg': 361}):
                    params = {'product_id': 'product-123', **self.parameters(), **changes}
                    params = {k: v for k, v in params.items() if v is not None}
                    with self.subTest(route=route, changes=changes):
                        response = client.get(route, params=params)
                        self.assertEqual(response.status_code, 400, response.text)
            get.assert_not_called()
            auth.assert_not_called()

    def test_aws_actual_rectangular_raster_zero_and_rotated(self):
        def read(url, bbox, **kwargs):
            self.assertEqual(kwargs, {'halo': 1, 'masked': True})
            arr = np.ma.array(np.full((60, 60), 8 if url == 'scl' else 1000, dtype='float32'), mask=False)
            return arr, from_origin(-300, 300, 10, 10), self.grid['crs']
        for angle in (0, 30):
            with self.subTest(angle=angle), \
                    mock.patch.object(main, '_DOWNLOADS_DIR', self.root), \
                    mock.patch.object(main, 'aws_get_item', return_value={'assets': {'B02': 'blue', 'SCL': 'scl'}}), \
                    mock.patch.object(aws_stac, 'read_band_window', side_effect=read), \
                    mock.patch.object(aws_stac, '_ensure_cas'):
                response = main.s2_aws_download('native-id', **self.parameters(angle))
                with rasterio.open(response.path) as dst:
                    self.assertEqual(dst.shape, (10, 20))
                    self.assertEqual(dst.transform, Affine(*self.rectangle(angle).output_grid(10)['transform']))
                    np.testing.assert_allclose(dst.read(1), 0)
                    self.assertEqual(set(np.unique(dst.read(2))), {8})
                    self.assertTrue(np.all(dst.read_masks() == 255))
                self.assertIn('_w200_h100_', response.filename)

    def test_cdse_actual_rectangular_raster_preserves_scl_and_zero(self):
        info = {'safe': 'S2_T48PXS_20260101T000000.SAFE', 'resolutions': {
            'R10m': [{'name': 'native_B02_10m.jp2'}], 'R20m': [{'name': 'native_SCL_20m.jp2'}]}}
        def download(product, resolution, name, destination):
            size, pixel = (30, 20) if 'SCL' in name else (60, 10)
            with rasterio.open(destination, 'w', driver='GTiff', width=size, height=size, count=1,
                               dtype='uint16', crs=self.grid['crs'], transform=from_origin(-300, 300, pixel, pixel)) as dst:
                dst.write(np.full((size, size), 8 if 'SCL' in name else 1000, dtype='uint16'), 1)
        for angle in (0, 30):
            with self.subTest(angle=angle), \
                    mock.patch.object(main, '_DOWNLOADS_DIR', self.root), \
                    mock.patch.object(main, '_ensure_cdse_creds'), \
                    mock.patch.object(main, 'list_s2_bands', return_value=info), \
                    mock.patch.object(main, 'download_s2_band_file', side_effect=download):
                response = main.s2_bands('product-123', action='download', bands='B02,SCL', **self.parameters(angle))
            with rasterio.open(response.path) as dst:
                self.assertEqual(dst.shape, (10, 20))
                self.assertEqual(dst.transform, Affine(*self.rectangle(angle).output_grid(10)['transform']))
                np.testing.assert_allclose(dst.read(1), 0)
                self.assertEqual(set(np.unique(dst.read(2))), {8})
                self.assertEqual(dst.descriptions, ('B02', 'SCL'))
            self.assertFalse(list(self.root.glob('_nodes_*')))

    def test_s1_rectangle_single_and_mosaic_use_requested_grid(self):
        files = [{'rel': 'measurement/s1-grd-vv-data.tiff', 'key': 'measurement', 'size': 1}]
        def mosaic(product, region, pols, meas, creds, transform, width, height, out, directory, **kwargs):
            self.assertEqual(kwargs['dst_crs'], self.rectangle().output_grid(20)['crs'])
            out[:] = 7
        with mock.patch.object(main, '_DOWNLOADS_DIR', self.root), \
                mock.patch.object(main, 'get_s3_credentials', return_value=('access', 'secret')), \
                mock.patch.object(main, 's3_list_product_files', return_value=files), \
                mock.patch.object(main, 'get_product_s3_path', return_value='/eodata/S1_20260101T000000.SAFE'), \
                mock.patch.object(main, '_s1_grd_mosaic_product', side_effect=mosaic), \
                mock.patch.object(main, 'get_province', side_effect=AssertionError('not a province')):
            for function, argument in ((main.s1_crop, 'product-123'), (main.s1_crop_mosaic, 'product-123,product-456')):
                response = function(argument, **self.parameters())
                with rasterio.open(response.path) as dst:
                    self.assertEqual(dst.shape, (5, 10))
                    self.assertEqual(dst.transform, Affine(*self.rectangle().output_grid(20)['transform']))
                    np.testing.assert_allclose(dst.read(1), 7)
                self.assertIn('_w200_h100_', response.filename)

    def test_s1_gcp_measurement_reprojects_to_rectangle_crs(self):
        from rasterio.control import GroundControlPoint
        roi = self.rectangle()
        grid = roi.output_grid(20)
        gcps = [GroundControlPoint(row=row, col=col, x=lon, y=lat)
                for row, col, lon, lat in ((0, 0, 106.99, 10.01), (0, 40, 107.01, 10.01),
                                           (40, 0, 106.99, 9.99), (40, 40, 107.01, 9.99))]
        with rasterio.MemoryFile() as mem:
            with mem.open(driver='GTiff', width=40, height=40, count=1, dtype='uint16',
                          gcps=gcps, crs='EPSG:4326') as dst:
                dst.write(np.full((40, 40), 7, dtype='uint16'), 1)
            body = mem.read()
        out = np.full((1, grid['height'], grid['width']), np.nan, dtype='float32')
        files = {'vv': [{'key': 'object', 'rel': 'measurement.tiff', 'size': len(body)}]}
        with mock.patch.object(main, 'iter_s3_object', return_value=iter([body])):
            main._s1_grd_mosaic_product('product-123', roi, ['vv'], files, ('access', 'secret'),
                Affine(*grid['transform']), grid['width'], grid['height'], out, self.root,
                dst_crs=grid['crs'])
        np.testing.assert_allclose(out, 7)
        self.assertFalse(list(self.root.glob('product-*.tiff')))

    def test_timeseries_search_snapshot_contains_polygon_and_grid(self):
        store = ts.TimeSeriesStore(self.root / 'timeseries')
        body = dict(dataset='sentinel2', start_date='2026-01-01', end_date='2026-01-31', **self.parameters())
        with mock.patch.object(main, '_timeseries_store', store), \
                mock.patch.object(main, '_ensure_ee'), \
                mock.patch('ee.Geometry.Polygon', return_value='polygon') as polygon, \
                mock.patch.object(main, 'list_timeseries_scenes', return_value=[]) as listing, \
                TestClient(main.app) as client:
            response = client.post('/api/timeseries/search', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            saved = response.json()['parameters']
            polygon.assert_called_once_with(self.rectangle().geometry['coordinates'], geodesic=False)
            self.assertEqual(listing.call_args.kwargs['roi'], 'polygon')
            self.assertEqual(listing.call_args.args[4], '2026-02-01')
            self.assertEqual(saved['rectangle'], self.rectangle().model_dump())
            self.assertEqual(saved['roi_geometry'], self.rectangle().geometry)
            self.assertEqual(saved['output_grid'], json.loads(json.dumps(self.rectangle().output_grid(10))))
            for changes in ({'width_m': None}, {'lon': 107}, {'radius_m': 100}):
                self.assertEqual(client.post('/api/timeseries/search', json={**body, **changes}).status_code, 422)
            self.assertEqual(client.post('/api/timeseries/search', json={
                'dataset': 'sentinel2', 'start_date': '2026-01-01', 'end_date': '2026-01-31'}).status_code, 422)

    def test_timeseries_worker_keeps_persisted_rectangle_after_failed_first_scene(self):
        roi = self.rectangle()
        grid = roi.output_grid(10)
        root = self.root / 'worker'
        store = ts.TimeSeriesStore(root)
        parameters = dict(dataset='sentinel2', selection_mode='rectangle', rectangle=roi.model_dump(),
                          center_lat=10, bbox=vars(roi.bbox), roi_geometry=roi.geometry,
                          output_grid=grid, scale_m=10, bands=['B2'])
        scenes = [dict(asset_id='asset/'+str(i), date='2026-01-01', timestamp='2026-01-01T00:00:00Z') for i in range(3)]
        search = store.create_search(parameters, scenes)
        parameters['rectangle']['width_m'] = 500
        store = ts.TimeSeriesStore(root)
        job = store.create_job(search['search_id'])
        service = ts.TimeSeriesService(store)
        def load(asset, *args):
            if asset == 'asset/0':
                raise ValueError('unavailable')
            return asset
        def merge(urls, dest, **kwargs):
            self.assertTrue(kwargs['preserve_masks'])
            shutil.copyfile(self.source(name='worker-source.tif', size=60), dest)
        try:
            with mock.patch.object(ts, 'load_timeseries_scene', side_effect=load), \
                    mock.patch.object(ts, 'download_geotiff', return_value=[]) as download, \
                    mock.patch.object(ts, 'merge_band_tiffs', side_effect=merge):
                service.run_timeseries_job(job['job_id'])
            self.assertEqual(store.get_job(job['job_id'])['status'], 'partial')
            self.assertLess(download.call_args.args[2].west, roi.bbox.west)
            with zipfile.ZipFile(store.download_path(job['job_id'])) as archive:
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['parameters']['rectangle']['width_m'], 200)
                for scene in manifest['scenes']:
                    if scene['file']:
                        with rasterio.MemoryFile(archive.read(scene['file'])) as mem, mem.open() as dst:
                            self.assertEqual(dst.shape, (10, 20))
                            self.assertEqual(dst.transform, Affine(*grid['transform']))
        finally:
            service.shutdown()
