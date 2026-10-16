"""Synthetic download and persisted time-series rotation regressions."""
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
from unittest import mock
import zipfile

import numpy as np
import rasterio
from affine import Affine
from rasterio.transform import from_origin

from app import main, imagery, timeseries as ts
from app.geo import point_roi
from tests.test_rotated_roi import RasterCase


class TestCDSERotation(RasterCase):
    def test_native_mixed_resolution_bands_zero_and_scl(self):
        paths = []
        info = {"safe": "S2_T48PXS_20260101T000000.SAFE", "resolutions": {
            "R10m": [{"name": "native_B02_10m.jp2"}],
            "R20m": [{"name": "native_SCL_20m.jp2"}]}}
        def download(product, resolution, name, destination):
            paths.append(destination)
            size, pixel = (30, 20) if "SCL" in name else (60, 10)
            with rasterio.open(destination, "w", driver="GTiff", width=size, height=size, count=1,
                               dtype="uint16", crs=self.grid["crs"],
                               transform=from_origin(-300, 300, pixel, pixel)) as dst:
                arr = np.full((size, size), 8 if "SCL" in name else 1000, dtype="uint16")
                dst.write(arr, 1)
        with mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "_ensure_cdse_creds"), \
             mock.patch.object(main, "list_s2_bands", return_value=info), \
             mock.patch.object(main, "download_s2_band_file", side_effect=download):
            response = main.s2_bands("product-123", action="download", bands="B02,SCL", lon=107,
                                     lat=10, radius_m=100, rotation_deg=30)
        with rasterio.open(response.path) as dst:
            self.assertEqual(dst.transform, Affine(*self.grid["transform"]))
            self.assertEqual(dst.shape, (20, 20))
            self.assertTrue(np.all(dst.read_masks() == 255))
            np.testing.assert_allclose(dst.read(1), 0)
            self.assertEqual(set(np.unique(dst.read(2))), {8})
            self.assertEqual(dst.descriptions, ("B02", "SCL"))
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertIn("_a30", response.filename)

    def test_download_failure_cleans_request_directory(self):
        info = {"safe": "source.SAFE", "resolutions": {"R10m": [{"name": "native_B02_10m.jp2"}]}}
        with mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "_ensure_cdse_creds"), \
             mock.patch.object(main, "list_s2_bands", return_value=info), \
             mock.patch.object(main, "download_s2_band_file", side_effect=ValueError("failure")):
            with self.assertRaises(main.HTTPException):
                main.s2_bands("product-123", action="download", bands="B02", lon=107,
                              lat=10, radius_m=100, rotation_deg=30)
        self.assertEqual(list(self.root.glob("_nodes_*")), [])


class TestRotatedTimeSeriesWorker(RasterCase):
    def test_snapshot_grid_survives_failed_first_scene_and_is_in_zip(self):
        store = ts.TimeSeriesStore(self.root / "timeseries")
        service = ts.TimeSeriesService(store)
        geometry, bbox = point_roi(107, 10, 100, 30)
        parameters = dict(dataset="sentinel2", polarization="vvvh", lon=107, lat=10,
                          radius_m=100, rotation_deg=30, bbox=vars(bbox), roi_geometry=geometry,
                          output_grid=self.grid, scale_m=10, bands=["B2"])
        scenes = [dict(asset_id="asset/"+str(i), timestamp="2026-01-01T00:00:00Z", date="2026-01-01")
                  for i in range(3)]
        search = store.create_search(parameters, scenes)
        job = store.create_job(search["search_id"])
        parameters["rotation_deg"] = 90
        def load(asset, *args):
            if asset == "asset/0":
                raise ValueError("Unavailable scene")
            return asset
        merges = []
        def merge(urls, destination, **options):
            merges.append(options)
            shutil.copyfile(self.source(name="src"+str(len(merges))+".tif", size=60+len(merges)*2), destination)
        try:
            with mock.patch.object(ts, "load_timeseries_scene", side_effect=load), \
                 mock.patch.object(ts, "download_geotiff", return_value=[]) as download, \
                 mock.patch.object(ts, "merge_band_tiffs", side_effect=merge):
                service.run_timeseries_job(job["job_id"])
            result = store.get_job(job["job_id"])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["succeeded"], 2)
            self.assertTrue(all(options["preserve_masks"] for options in merges))
            self.assertLess(download.call_args.args[2].west, bbox.west)
            with zipfile.ZipFile(store.download_path(job["job_id"])) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["grid"], self.grid)
                self.assertEqual(manifest["parameters"]["rotation_deg"], 30)
                self.assertEqual(manifest["parameters"]["roi_geometry"], geometry)
                for record in manifest["scenes"]:
                    if not record["file"]:
                        continue
                    with rasterio.MemoryFile(archive.read(record["file"])) as memory, memory.open() as dst:
                        self.assertEqual(dst.transform, Affine(*self.grid["transform"]))
                        self.assertEqual(dst.shape, (20, 20))
                        self.assertTrue(np.all(dst.read_masks() == 255))
        finally:
            service.shutdown()


class TestGEERotatedMerge(RasterCase):
    def test_optical_selection_uses_polygon_without_admin_composite(self):
        roi = object()
        with mock.patch.object(imagery, "_point_optical_scene", return_value="scene") as pick:
            result = imagery.best_scene("sentinel2", 107, 10, 100, "2026-01-01", "2026-01-02",
                                        merge_scenes=True, roi=roi)
        self.assertEqual(result, "scene")
        self.assertIs(pick.call_args.args[1], roi)
        self.assertFalse(pick.call_args.kwargs["clip_output"])
        with mock.patch.object(imagery, "_point_optical_scene", return_value="scene") as pick, \
             mock.patch.object(imagery, "bbox_to_ee_geom", return_value=object()):
            imagery.best_scene("sentinel2", 107, 10, 100, "2026-01-01", "2026-01-02")
        self.assertNotIn("clip_output", pick.call_args.kwargs)

    def test_sentinel_and_landsat_downloads_are_really_rotated_tiffs(self):
        from app.raster_grid import warp_to_grid
        _, bbox = point_roi(107, 10, 100, 30)
        scene = SimpleNamespace(ee_object=object(), scene_id='exact', date='2026-01-01')
        for dataset in ('sentinel2', 'landsat'):
            with self.subTest(dataset=dataset):
                def merge(urls, destination, **options):
                    shutil.copyfile(self.source(name=dataset + '.tif'), destination)
                with mock.patch.object(main, '_ensure_ee'), mock.patch.object(main, '_DOWNLOADS_DIR', self.root), \
                     mock.patch.object(main, '_resolve_selection', return_value=('roi', bbox, None)), \
                     mock.patch.object(main, 'best_scene', return_value=scene), \
                     mock.patch.object(main, 'download_geotiff', return_value=[]), \
                     mock.patch.object(main, 'merge_band_tiffs', side_effect=merge), \
                     mock.patch.object(main, 'warp_to_grid', wraps=warp_to_grid) as warp:
                    response = main.download(dataset, '2026-01-01', '2026-01-02', lon=107, lat=10,
                                             radius_m=100, rotation_deg=30)
                warp.assert_called_once()
                with rasterio.open(response.path) as dst:
                    grid = main.build_rotated_grid(107, 10, 100, 30, main._scale_for(dataset))
                    self.assertEqual(dst.transform, Affine(*grid['transform']))
                    self.assertNotEqual(dst.transform.b, 0)
                    self.assertNotEqual(dst.transform.d, 0)
                    self.assertEqual(dst.width, dst.height)
                    self.assertTrue(np.all(dst.read_masks() == 255))

    def test_s1_download_uses_rotated_grid_and_source_envelope(self):
        from app.raster_grid import warp_to_grid
        _, bbox = point_roi(107, 10, 100, 30)
        scene = SimpleNamespace(ee_object=object(), scene_id="scene", date="2026-01-01")
        def merge(urls, destination, **options):
            shutil.copyfile(self.source(), destination)
        with mock.patch.object(main, "_ensure_ee"), mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "_resolve_selection", return_value=("roi", bbox, None)), \
             mock.patch.object(main, "best_s1_scene", return_value=scene) as best, \
             mock.patch.object(main, "download_geotiff", return_value=[]) as download, \
             mock.patch.object(main, "merge_band_tiffs", side_effect=merge), \
             mock.patch.object(main, "warp_to_grid", wraps=warp_to_grid) as warp:
            response = main.s1gee_download("2026-01-01", "2026-01-02", lon=107, lat=10,
                                           radius_m=100, rotation_deg=30)
        self.assertEqual(best.call_args.kwargs["roi"], "roi")
        self.assertIsNone(download.call_args.args[4])
        self.assertLess(download.call_args.args[2].west, bbox.west)
        warp.assert_called_once()
        with rasterio.open(response.path) as dst:
            grid = main.build_rotated_grid(107, 10, 100, 30, main.S1_SCALE)
            self.assertEqual(dst.transform, Affine(*grid["transform"]))
            self.assertTrue(np.all(dst.read_masks() == 255))

    def test_explicit_mask_and_valid_zero_survive_merge_then_warp(self):
        from app.raster_grid import warp_to_grid
        source = self.source(value=0, hole=True)
        payload = source.read_bytes()
        url = mock.Mock()
        url.url.return_value = "https://example.invalid"
        raw, target = self.root / "merged.tif", self.root / "rotated.tif"
        with mock.patch.object(imagery, "_fetch_with_retry", return_value=payload):
            imagery.merge_band_tiffs([("B2", url)], raw, preserve_masks=True)
        warp_to_grid(raw, target, self.grid)
        with rasterio.open(target) as dst:
            values = dst.read(1, masked=True)
            self.assertTrue(np.any(values.mask))
            self.assertTrue(np.any(~values.mask))
            self.assertTrue(np.all(values.compressed() == 0))

    def test_360_gee_download_does_not_warp_or_change_filename(self):
        _, bbox = point_roi(107, 10, 100, 0)
        scene = SimpleNamespace(ee_object=object(), scene_id="scene", date="2026-01-01")
        def merge(urls, destination, **options):
            shutil.copyfile(self.source(), destination)
        with mock.patch.object(main, "_ensure_ee"), mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "_resolve_selection", return_value=("roi", bbox, None)), \
             mock.patch.object(main, "best_scene", return_value=scene), \
             mock.patch.object(main, "download_geotiff", return_value=[]), \
             mock.patch.object(main, "merge_band_tiffs", side_effect=merge), \
             mock.patch.object(main, "warp_to_grid") as warp:
            response = main.download("sentinel2", "2026-01-01", "2026-01-02", lon=107, lat=10,
                                     radius_m=100, rotation_deg=360)
        warp.assert_not_called()
        self.assertEqual(Path(response.path).name, "sentinel2_scene_2026-01-01.tif")
