"""Rotated output grids, synthetic rasters and mocked backend integration."""
from pathlib import Path
import json
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

import numpy as np
import rasterio
from affine import Affine
from fastapi.testclient import TestClient
from rasterio.transform import from_origin
from rasterio.warp import transform

from app import main, aws_stac
from app.geo import point_roi
from app.raster_grid import build_rotated_grid, warp_to_grid
from app.timeseries import align_timeseries_raster, TimeSeriesStore


class RasterCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent / "downloads" / ("test-rotation-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.grid = build_rotated_grid(107, 10, 100, 30, 10)

    def tearDown(self):
        shutil.rmtree(self.root)

    def source(self, name="source.tif", value=7, hole=False, crs=None, size=60, pixel=10):
        path = self.root / name
        arr = np.full((size, size), value, dtype="float32")
        with rasterio.open(path, "w", driver="GTiff", width=size, height=size, count=1,
                           dtype="float32", crs=crs or self.grid["crs"],
                           transform=from_origin(-size*pixel/2, size*pixel/2, pixel, pixel)) as dst:
            dst.write(arr, 1)
            if hole:
                mask = np.full(arr.shape, 255, dtype="uint8")
                mask[size//2-3:size//2+3, size//2-3:size//2+3] = 0
                dst.write_mask(mask)
            dst.set_band_description(1, "reflectance")
            dst.update_tags(test_tag="preserved")
        return path


class TestRotatedGrid(RasterCase):
    def test_serialization_corners_and_fixed_footprint(self):
        for angle in (30, 45, 90, 135):
            grid = build_rotated_grid(107, 10, 103, angle, 10)
            self.assertEqual(grid["width"], 21)
            self.assertEqual(grid["height"], 21)
            self.assertAlmostEqual(grid["actual_pixel_size_m"], 206/21)
            self.assertEqual(grid, json.loads(json.dumps(grid)))
            affine = Affine(*grid["transform"])
            xy = [affine * p for p in ((0, 21), (21, 21), (21, 0), (0, 0))]
            lon, lat = transform(grid["crs"], "EPSG:4326", [p[0] for p in xy], [p[1] for p in xy])
            ring = point_roi(107, 10, 103, angle)[0]["coordinates"][0]
            for actual, expected in zip(zip(lon, lat), ring):
                self.assertAlmostEqual(actual[0], expected[0], places=9)
                self.assertAlmostEqual(actual[1], expected[1], places=9)
        for scale in (0, -1, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                build_rotated_grid(107, 10, 100, 30, scale)

    def test_constant_sources_have_no_artificial_empty_corners(self):
        for angle in (30, 45, 90, 135):
            grid = build_rotated_grid(107, 10, 100, angle, 10)
            source = self.source()
            target = self.root / "target.tif"
            warp_to_grid(source, target, grid)
            with rasterio.open(target) as dst:
                self.assertTrue(np.all(dst.read_masks(1) == 255))
                np.testing.assert_allclose(dst.read(1), 7)
                self.assertEqual(dst.transform, Affine(*grid["transform"]))
                self.assertEqual(dst.tags()["test_tag"], "preserved")
                self.assertEqual(dst.descriptions, ("reflectance",))

    def test_valid_zero_source_mask_and_categories(self):
        target = self.root / "target.tif"
        warp_to_grid(self.source(value=0), target, self.grid)
        with rasterio.open(target) as dst:
            self.assertTrue(np.all(dst.read_masks(1) == 255))
            self.assertTrue(np.all(dst.read(1) == 0))
        warp_to_grid(self.source(hole=True), target, self.grid)
        with rasterio.open(target) as dst:
            self.assertTrue(np.any(dst.read_masks(1) == 0))
            self.assertTrue(np.any(dst.read_masks(1) == 255))
        source = self.source(value=0)
        with rasterio.open(source, "r+") as src:
            arr = src.read(1)
            arr[:, 30:] = 8
            src.write(arr, 1)
        warp_to_grid(source, target, self.grid, categorical=True)
        with rasterio.open(target) as dst:
            self.assertEqual(set(np.unique(dst.read(1))), {0, 8})
            self.assertEqual(dst.nodata, 255)

    def test_timeseries_uses_snapshot_not_first_source_grid(self):
        for name, size, pixel in (("a.tif", 60, 10), ("b.tif", 80, 8)):
            path = self.source(name, size=size, pixel=pixel)
            target = self.root / ("aligned-"+name)
            result = align_timeseries_raster(path, target, self.grid, bands=["B2"])
            self.assertEqual(result, self.grid)
            with rasterio.open(target) as dst:
                self.assertEqual(dst.shape, (20, 20))
                self.assertEqual(dst.transform, Affine(*self.grid["transform"]))
                self.assertTrue(np.all(dst.read_masks(1) == 255))
                self.assertEqual(dst.descriptions, ("B2",))

    def test_aws_native_bands_single_warp_and_scl_nearest(self):
        def read(url, bbox, **options):
            self.assertEqual(options, {"halo": 1, "masked": True})
            size, pixel = (30, 20) if url == "scl" else (60, 10)
            arr = np.ma.array(np.full((size, size), 8 if url == "scl" else 1000, dtype="float32"))
            return arr, from_origin(-300, 300, pixel, pixel), self.grid["crs"]
        target = self.root / "aws.tif"
        with mock.patch.object(aws_stac, "read_band_window", side_effect=read):
            bands = aws_stac.merge_all_bands({"B02": "blue", "SCL": "scl"}, target, (1, 2, 3, 4), self.grid)
        self.assertEqual(bands, ["B02", "SCL"])
        with rasterio.open(target) as dst:
            self.assertTrue(np.all(dst.read_masks() == 255))
            np.testing.assert_allclose(dst.read(1), 0)
            self.assertEqual(set(np.unique(dst.read(2))), {8})


class TestRotationAPI(RasterCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(main.app)
        self.body = dict(lon=107, lat=10, radius_m=100, rotation_deg=30,
                         start_date="2026-01-01", end_date="2026-01-02")

    def tearDown(self):
        self.client.close()
        super().tearDown()

    def test_schema_validation_and_non_point_rejection(self):
        for schema in (main.SceneRequest, main.S1SceneRequest):
            self.assertEqual(schema(**{**self.body, "rotation_deg": 360}).rotation_deg, 0)
            for value in (-1, 361, float("nan"), float("inf")):
                with self.assertRaises(ValueError):
                    schema(**{**self.body, "rotation_deg": value})
            with self.assertRaises(ValueError):
                schema(**{**self.body, "selection_mode": "province", "province_id": "x"})
        with mock.patch.object(main, "aws_get_item") as get:
            response = self.client.get("/api/s2/aws/download", params={"product_id": "item", "rotation_deg": 30, "province_id": "x"})
            self.assertEqual(response.status_code, 400)
            get.assert_not_called()

    def test_point_resolver_uses_nongeodesic_polygon(self):
        with mock.patch("ee.Geometry.Polygon", return_value="roi") as polygon:
            roi, bbox, province = main._resolve_selection("point", 107, 10, 100, None, rotation_deg=30)
        self.assertEqual(roi, "roi")
        self.assertFalse(polygon.call_args.kwargs["geodesic"])
        self.assertEqual(polygon.call_args.args[0], point_roi(107, 10, 100, 30)[0]["coordinates"])

    def test_scene_response_polygon_url_and_preview(self):
        geometry, bbox = point_roi(107, 10, 100, 30)
        scene = SimpleNamespace(ee_object=mock.Mock(), scene_id="scene", date="2026-01-01", cloud_pct=0,
                                coverage_pct=100, scene_count=None, scenes=(), estimated_seconds=None)
        with mock.patch.object(main, "_ensure_ee"), mock.patch.object(main, "_resolve_selection", return_value=("roi", bbox, None)), \
             mock.patch.object(main, "best_scene", return_value=scene) as best, \
             mock.patch.object(main, "preview_rgb", return_value="preview") as preview:
            response = self.client.post("/api/scene", json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["roi_geometry"], geometry)
        self.assertIn("rotation_deg=30", response.json()["download_url"])
        self.assertEqual(best.call_args.kwargs["roi"], "roi")
        self.assertEqual(preview.call_args.args[3], "roi")

    def test_gee_download_warps_once_with_halo_without_polygon_clip(self):
        geometry, bbox = point_roi(107, 10, 100, 30)
        scene = SimpleNamespace(ee_object=object(), scene_id="scene", date="2026-01-01")
        paths = []
        def merge(urls, destination, **kwargs):
            paths.append(Path(destination))
            shutil.copyfile(self.source(), destination)
        with mock.patch.object(main, "_ensure_ee"), mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "_resolve_selection", return_value=("roi", bbox, None)), \
             mock.patch.object(main, "best_scene", return_value=scene), \
             mock.patch.object(main, "download_geotiff", return_value=[]) as download, \
             mock.patch.object(main, "merge_band_tiffs", side_effect=merge), \
             mock.patch.object(main, "warp_to_grid", wraps=warp_to_grid) as warp:
            response = self.client.get("/api/download/sentinel2", params=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        warp.assert_called_once()
        self.assertIsNone(download.call_args.args[4])
        self.assertLess(download.call_args.args[2].west, bbox.west)
        self.assertFalse(paths[0].exists())

    def test_aws_api_grid_and_unique_paths(self):
        paths = []
        def merge(assets, destination, bbox, output_grid):
            paths.append(destination)
            warp_to_grid(self.source(), destination, output_grid)
        with mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "aws_get_item", return_value={"assets": {"B02": "url"}}), \
             mock.patch.object(main, "aws_merge_all_bands", side_effect=merge):
            params = dict(product_id="item", lon=107, lat=10, radius_m=100, rotation_deg=30)
            for _ in range(2):
                response = self.client.get("/api/s2/aws/download", params=params)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn("_a30.tif", response.headers["content-disposition"])
        self.assertNotEqual(paths[0], paths[1])

    def test_rotated_catalogue_queries_envelope_and_returns_polygon(self):
        for source, method in (("aws", "aws_search_s2_l2a_bbox"), ("cdse", "search_s2_area")):
            with mock.patch.object(main, method, return_value=[{"id": "scene"}]) as search:
                response = self.client.get("/api/s2/scenes", params={**self.body, "source": source})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["roi_geometry"], point_roi(107, 10, 100, 30)[0])
            envelope = point_roi(107, 10, 100, 30)[1]
            self.assertEqual(search.call_args.args[0], (envelope.west, envelope.south, envelope.east, envelope.north))
        with mock.patch.object(main, "aws_search_s2_l2a_bbox") as search:
            response = self.client.get("/api/s2/scenes", params={**self.body, "rotation_deg": "NaN"})
        self.assertEqual(response.status_code, 400)
        search.assert_not_called()

    def test_timeseries_snapshot_records_full_grid_geometry_and_rotation(self):
        store = TimeSeriesStore(self.root / "timeseries")
        _, bbox = point_roi(107, 10, 100, 30)
        with mock.patch.object(main, "_timeseries_store", store), mock.patch.object(main, "_ensure_ee"), \
             mock.patch.object(main, "_resolve_selection", return_value=("roi", bbox, None)), \
             mock.patch.object(main, "list_timeseries_scenes", return_value=[]) as listing:
            response = self.client.post("/api/timeseries/search", json={**self.body, "dataset": "sentinel2"})
        self.assertEqual(response.status_code, 200, response.text)
        parameters, _ = store._snapshot(response.json()["search_id"])
        self.assertEqual(parameters["rotation_deg"], 30)
        self.assertEqual(parameters["output_grid"], self.grid)
        self.assertEqual(parameters["roi_geometry"], point_roi(107, 10, 100, 30)[0])
        self.assertEqual(listing.call_args.kwargs["roi"], "roi")
