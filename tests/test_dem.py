"""Elevation routing, ROI export and static product tests (no network)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient
from app import main, dem, imagery
from app.geo import Bbox


class DEMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.image = MagicMock()
        self.image.reduceRegion.return_value.get.return_value.getInfo.return_value = 100
        self.roi = MagicMock()
        self.bbox = Bbox(106.99, 10.99, 107.01, 11.01)
        for p in [patch.object(main, '_ensure_ee'),
                  patch.object(main, '_DOWNLOADS_DIR', Path(self.tmp.name)),
                  patch.object(main, '_resolve_selection', return_value=(self.roi, self.bbox, None)),
                  patch.object(dem, 'elevation_image', return_value=self.image),
                  patch.object(dem, 'finalize_elevation'),
                  patch('ee.Reducer.count', return_value=object())]:
            p.start()
            self.addCleanup(p.stop)

    def test_all_sources_download_elevation_without_dates(self):
        def merge(urls, path, **kwargs):
            path.write_bytes(b'tiff-test')
        for dataset in dem.DEM_SOURCES:
            with self.subTest(dataset=dataset), patch.object(main, 'download_geotiff', return_value=[]) as download, patch.object(main, 'merge_band_tiffs', side_effect=merge):
                response = self.client.get('/api/dem/download', params=dict(dataset=dataset, lon=107, lat=11))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.content, b'tiff-test')
                self.assertEqual(download.call_args.args[1:4], ('dem', self.bbox, 30))
                self.assertIs(download.call_args.args[4], self.roi)
                self.assertIn(dataset, response.headers['content-disposition'])

    def test_repeated_downloads_have_unique_roi_filenames(self):
        def merge(urls, path, **kwargs):
            path.write_bytes(b'tiff-test')
        with patch.object(main, 'download_geotiff', return_value=[]), patch.object(main, 'merge_band_tiffs', side_effect=merge):
            responses = [self.client.get('/api/dem/download', params=dict(dataset='alos', lon=107, lat=11, radius_m=1000)) for _ in range(2)]
        names = [r.headers['content-disposition'].split('filename="')[1].rstrip('"') for r in responses]
        self.assertNotEqual(*names)
        for name in names:
            self.assertRegex(name, r'^alos_elevation_30m_pt_lon107\.00000_lat11\.00000_r1000_a0_\d{8}T\d{12}Z_[a-f0-9]{12}\.tif$')
            self.assertTrue((Path(self.tmp.name) / name).is_file())

    def test_invalid_source_and_rectangle(self):
        self.assertEqual(self.client.get('/api/dem/download?dataset=bad').status_code, 422)
        self.assertEqual(self.client.get('/api/dem/download?selection_mode=rectangle').status_code, 400)

    def test_administrative_modes_keep_polygon_clip(self):
        def merge(urls, path, **kwargs):
            path.write_bytes(b'tiff-test')
        for mode, key in [('province', 'province_id'), ('commune', 'commune_id')]:
            with self.subTest(mode=mode), patch.object(main, 'download_geotiff', return_value=[]) as download, patch.object(main, 'merge_band_tiffs', side_effect=merge):
                response = self.client.get('/api/dem/download', params={'selection_mode': mode, key: 'test'})
                self.assertEqual(response.status_code, 200)
                self.assertIs(download.call_args.args[4], self.roi)

    def test_srtm_outside_coverage(self):
        with patch.object(main, '_resolve_selection', return_value=(self.roi, Bbox(10, 65, 11, 66), None)):
            response = self.client.get('/api/dem/download?dataset=srtm&lon=10&lat=65')
        self.assertEqual(response.status_code, 400)

    def test_no_data(self):
        self.image.reduceRegion.return_value.get.return_value.getInfo.return_value = 0
        response = self.client.get('/api/dem/download?lon=107&lat=11')
        self.assertEqual(response.status_code, 404)

    def test_rotated_rectangle_warps_and_cleans_source(self):
        def merge(urls, path, **kwargs):
            path.write_bytes(b'raw')
        def warp(raw, out, grid):
            out.write_bytes(b'rotated')
        with patch.object(main, 'download_geotiff', return_value=[]) as download, patch.object(main, 'merge_band_tiffs', side_effect=merge), patch.object(main, 'warp_to_grid', side_effect=warp) as warped:
            response = self.client.get('/api/dem/download', params=dict(selection_mode='rectangle', center_lon=107, center_lat=11, width_m=3000, height_m=1000, rotation_deg=25))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, b'rotated')
        self.assertIsNone(download.call_args.args[4])
        warped.assert_called_once()
        self.assertFalse(list(Path(self.tmp.name).glob('*.source.tif')))

    def test_export_failure_cleans_files(self):
        def fail(urls, path, **kwargs):
            path.write_bytes(b'partial')
            raise RuntimeError('download failed')
        with patch.object(main, 'download_geotiff', return_value=[]), patch.object(main, 'merge_band_tiffs', side_effect=fail):
            response = self.client.get('/api/dem/download?lon=107&lat=11')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])


class DEMImageTests(unittest.TestCase):
    def test_finalize_normalizes_nodata_without_losing_zero_or_negative_heights(self):
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'dem.tif'
            with rasterio.open(path, 'w', driver='GTiff', width=4, height=1,
                               count=1, dtype='float32', nodata=float('nan'),
                               transform=from_origin(107, 11, 0.001, 0.001)) as dst:
                dst.write(np.array([[0, -15, np.nan, -np.inf]], dtype='float32'), 1)
            dem.finalize_elevation(path)
            with rasterio.open(path) as src:
                self.assertEqual(src.nodata, -9999)
                self.assertEqual(src.read(1).tolist(), [[0, -15, -9999, -9999]])
                self.assertEqual(src.read(1, masked=True).count(), 2)
                self.assertEqual(src.descriptions, ('elevation',))

    def test_collection_products_select_and_mosaic_without_dates(self):
        for dataset in ('alos', 'copernicus'):
            with self.subTest(dataset=dataset), patch.object(dem.ee, 'ImageCollection') as collection:
                tiles = collection.return_value.filterBounds.return_value.select.return_value
                tiles.size.return_value.getInfo.return_value = 1
                image = dem.elevation_image(dataset, 'roi')
                collection.assert_called_once_with(dem.DEM_SOURCES[dataset]['asset'])
                tiles.mosaic.assert_called_once()
                tiles.filterDate.assert_not_called()
                self.assertIs(image, tiles.mosaic.return_value.rename.return_value.toFloat.return_value)

    def test_srtm_is_single_image(self):
        with patch.object(dem.ee, 'Image') as image:
            dem.elevation_image('srtm', 'roi')
            image.assert_called_once_with('USGS/SRTMGL1_003')
            image.return_value.select.assert_called_once_with('elevation')

    def test_download_uses_elevation_and_explicit_nodata(self):
        image = MagicMock()
        with patch.object(imagery, '_DownloadURL') as url, patch.object(imagery.ee.Geometry, 'Rectangle'):
            result = imagery.download_geotiff(image, 'dem', Bbox(107, 11, 107.001, 11.001), 30)
        self.assertEqual(result[0][0], 'elevation')
        image.select.assert_called_once_with(['elevation'])
        image.select.return_value.multiply.return_value.toFloat.return_value.unmask.assert_called_once_with(-9999, sameFootprint=False)
        self.assertEqual(url.call_args.args[1]['formatOptions']['noData'], -9999)
