"""Real raster regressions for bounded, atomic large-file exports."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app import imagery, raster_grid


class RasterOutputs(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def source(self, name="source.tif", width=300, height=280):
        path = self.root / name
        with rasterio.open(path, "w", driver="GTiff", width=width, height=height,
                           count=2, dtype="float32", nodata=float("nan"),
                           crs="EPSG:3857", transform=from_origin(0, height, 1, 1)) as dst:
            data = np.zeros((height, width), dtype="float32")
            data[2:4, 2:4] = np.nan
            dst.write(data, 1)
            dst.write(np.full_like(data, 8), 2)
            dst.descriptions = ("B2", "SCL")
            dst.update_tags(origin="fixture")
            dst.update_tags(1, wavelength="blue")
        return path

    def grid(self, width=300, height=280):
        return dict(crs="EPSG:3857", transform=list(from_origin(0, height, 1, 1))[:6],
                    width=width, height=height)

    def tile(self, data, left=0, top=280, nodata=float("nan"), pixel=1):
        with MemoryFile() as mem:
            with mem.open(driver="GTiff", width=data.shape[1], height=data.shape[0], count=1,
                          dtype=str(data.dtype), crs="EPSG:3857",
                          transform=from_origin(left, top, pixel, pixel), nodata=nodata) as dst:
                dst.write(data, 1)
            return mem.read()

    def merge(self, payloads, out, **options):
        urls = []
        for name in payloads:
            url = SimpleNamespace(url=lambda n=name: n, refresh=lambda n=name: n)
            urls.append((name, url))
        with mock.patch.object(imagery, "_fetch_with_retry", side_effect=payloads.__getitem__):
            imagery.merge_band_tiffs(urls, out, **options)

    def test_warp_publishes_bigtiff_with_masks_metadata_and_bounded_blocks(self):
        out = self.root / "warped.tif"
        raster_grid.warp_to_grid(self.source(), out, self.grid())
        with out.open("rb") as f:
            self.assertEqual(f.read(4), b"II+\x00")
        with rasterio.open(out) as src:
            self.assertEqual(src.tags(ns="IMAGE_STRUCTURE")["INTERLEAVE"], "BAND")
            self.assertLessEqual(max(src.block_shapes[0]), 256)
            self.assertEqual(src.descriptions, ("B2", "SCL"))
            self.assertEqual(src.tags()["origin"], "fixture")
            self.assertEqual(src.tags(1)["wavelength"], "blue")
            self.assertTrue(np.isnan(src.read(1)[2:4, 2:4]).all())
            self.assertEqual(src.read(1)[279, 299], 0)
            self.assertEqual(src.read_masks(1)[279, 299], 255)
            self.assertEqual(set(np.unique(src.read(2))), {8})

    def test_failed_warp_preserves_existing_result_and_removes_attempt(self):
        source = self.source()
        for exists in (False, True):
            with self.subTest(existing=exists):
                out = self.root / "result.tif"
                if exists:
                    out.write_bytes(b"previous-complete-result")
                original = raster_grid.write_grid_band

                def fail_after_band(*args, **kwargs):
                    original(*args, **kwargs)
                    raise rasterio.errors.RasterioIOError("disk write failed")

                with mock.patch.object(raster_grid, "write_grid_band", side_effect=fail_after_band):
                    with self.assertRaises(rasterio.errors.RasterioIOError):
                        raster_grid.warp_to_grid(source, out, self.grid())
                if exists:
                    self.assertEqual(out.read_bytes(), b"previous-complete-result")
                else:
                    self.assertFalse(out.exists())
                self.assertEqual(set(self.root.iterdir()), {source, out} if exists else {source})

    def test_mosaic_uses_bounded_windows_and_preserves_overlap_zero_and_holes(self):
        left = np.zeros((280, 400), dtype="float32")
        left[10:20, 310:320] = np.nan
        left[30:40, 10:20] = np.nan
        right = np.full((280, 400), 8, dtype="float32")
        payloads = {"B2_t00": self.tile(left), "B2_t01": self.tile(right, left=300)}
        out = self.root / "merged.tif"
        self.merge(payloads, out, preserve_masks=True)
        expected = np.zeros((280, 700), dtype="float32")
        expected[:, 400:] = 8
        expected[10:20, 310:320] = 8
        expected[30:40, 10:20] = np.nan
        with rasterio.open(out) as src:
            np.testing.assert_array_equal(src.read(1), expected)
            self.assertEqual(src.transform, from_origin(0, 280, 1, 1))
            self.assertEqual(src.descriptions, ("B2",))

    def test_stack_never_reads_a_whole_band_and_preserves_sentinel_nodata(self):
        data = np.full((280, 300), 0.1, dtype="float32")
        data[3, 4] = -9999
        payloads = {"B2_t00": self.tile(data, nodata=-9999),
                    "B2_t01": self.tile(data, nodata=-9999),
                    "B3": self.tile(data + 1, nodata=-9998)}
        out = self.root / "stack.tif"
        real_open = rasterio.open

        class WindowReader:
            def __init__(self, dataset):
                self.dataset = dataset

            def __getattr__(self, key):
                return getattr(self.dataset, key)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.dataset.__exit__(*args)

            def read(self, *args, **kwargs):
                if kwargs.get("window") is None:
                    raise AssertionError("Full-band reads are unbounded")
                shape = kwargs.get("out_shape")
                if shape is None:
                    win = kwargs["window"]
                    shape = (win.height, win.width)
                if max(shape[-2:]) > 256:
                    raise AssertionError("Raster reads must fit an output block")
                return self.dataset.read(*args, **kwargs)

        def window_open(path, mode="r", **kwargs):
            src = real_open(path, mode, **kwargs)
            return WindowReader(src) if mode == "r" else src

        with mock.patch("rasterio.open", side_effect=window_open):
            self.merge(payloads, out, preserve_masks=True)
        with out.open("rb") as f:
            self.assertEqual(f.read(4), b"II+\x00")
        with real_open(out) as src:
            self.assertEqual(src.descriptions, ("B2", "B3"))
            self.assertTrue(np.isnan(src.read(1)[3, 4]))
            self.assertTrue(np.isnan(src.read(2)[3, 4]))
            self.assertAlmostEqual(float(src.read(1)[279, 299]), 0.1)

    def test_shifted_band_grids_are_rejected_without_replacing_result(self):
        data = np.ones((16, 16), dtype="float32")
        out = self.root / "result.tif"
        out.write_bytes(b"previous-result")
        with self.assertRaisesRegex(RuntimeError, "grid"):
            self.merge({"B2": self.tile(data), "B3": self.tile(data, left=1)}, out)
        self.assertEqual(out.read_bytes(), b"previous-result")
        self.assertEqual(list(self.root.iterdir()), [out])

    def test_failed_merge_write_preserves_result_and_removes_attempt(self):
        data = np.ones((280, 300), dtype="float32")
        payloads = {"B2": self.tile(data)}
        out = self.root / "result.tif"
        out.write_bytes(b"previous-result")
        real_open = rasterio.open

        class FailedWriter:
            def __init__(self, dataset):
                self.dataset = dataset

            def __getattr__(self, key):
                return getattr(self.dataset, key)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.dataset.__exit__(*args)

            def write(self, *args, **kwargs):
                self.dataset.write(*args, **kwargs)
                raise rasterio.errors.RasterioIOError("write failed")

        def failing_open(path, mode="r", **kwargs):
            dataset = real_open(path, mode, **kwargs)
            return FailedWriter(dataset) if mode == "w" else dataset

        with mock.patch("rasterio.open", side_effect=failing_open):
            with self.assertRaises(rasterio.errors.RasterioIOError):
                self.merge(payloads, out, preserve_masks=True)
        self.assertEqual(out.read_bytes(), b"previous-result")
        self.assertEqual(list(self.root.iterdir()), [out])

    def test_empty_merge_fails_before_creating_output(self):
        out = self.root / "result.tif"
        with self.assertRaisesRegex(ValueError, "band"):
            imagery.merge_band_tiffs([], out)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_polygon_mask_keeps_bigtiff_and_valid_zero(self):
        from app.vector_mask import mask_raster_to_polygon
        source = self.source()
        out = self.root / "masked.tif"
        raster_grid.warp_to_grid(source, out, self.grid())
        polygon = {"type": "Polygon", "coordinates": [[[0, 0], [0.003, 0],
                    [0.003, 0.003], [0, 0.003], [0, 0]]]}
        mask_raster_to_polygon(out, polygon)
        with out.open("rb") as f:
            self.assertEqual(f.read(4), b"II+\x00")
        with rasterio.open(out) as src:
            self.assertEqual(src.read(1)[279, 299], 0)
            self.assertEqual(src.read_masks(1)[279, 299], 255)


    def test_local_write_error_returns_detail_and_cleans_result(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.geo import point_roi

        _, bbox = point_roi(107, 10, 100, 30)
        scene = SimpleNamespace(ee_object=object(), scene_id="fixture", date="2026-01-01")

        def merge(urls, destination, **kwargs):
            Path(destination).write_bytes(b"raw")

        def fail_warp(raw, destination, grid, **kwargs):
            Path(destination).write_bytes(b"partial")
            raise rasterio.errors.RasterioIOError("disk full")

        with mock.patch.object(main, "_ensure_ee"), \
             mock.patch.object(main, "_DOWNLOADS_DIR", self.root), \
             mock.patch.object(main, "_resolve_selection", return_value=("roi", bbox, None)), \
             mock.patch.object(main, "best_scene", return_value=scene), \
             mock.patch.object(main, "merge_band_tiffs", side_effect=merge), \
             mock.patch.object(main, "download_geotiff", return_value=[]), \
             mock.patch.object(main, "warp_to_grid", side_effect=fail_warp):
            client = TestClient(main.app, raise_server_exceptions=False)
            response = client.get("/api/download/sentinel2", params=dict(
                start_date="2026-01-01", end_date="2026-01-02", lon=107, lat=10,
                radius_m=100, rotation_deg=30))
        self.assertEqual(response.status_code, 500)
        self.assertIn("application/json", response.headers.get("content-type", ""))
        self.assertIn("write", response.json()["detail"].lower())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_download_can_use_more_than_eight_tiles_per_axis(self):
        from app.geo import Bbox
        image = mock.Mock()
        selected = image.select.return_value
        selected.unmask.return_value.toUint8.return_value = selected
        with mock.patch.object(imagery, "_DownloadURL", return_value=mock.Mock()), \
             mock.patch.object(imagery.ee.Geometry, "Rectangle", return_value=object()):
            urls = imagery.download_geotiff(image, "dynamic_world", Bbox(0, 0, 0.009, 0.009),
                                           10, max_tile_pixels=125)
        self.assertEqual(len(urls), 81)

    def test_default_large_download_pretiles_beyond_eight_by_eight(self):
        from app.geo import Bbox
        image = mock.Mock()
        selected = image.select.return_value
        selected.unmask.return_value.toUint8.return_value = selected
        with mock.patch.object(imagery, "_DownloadURL", return_value=mock.Mock()), \
             mock.patch.object(imagery.ee.Geometry, "Rectangle", return_value=object()):
            # Approximately 522 million pixels: 8x8 exceeds the conservative
            # default of eight million pixels per Float32 download tile.
            urls = imagery.download_geotiff(image, "dem", Bbox(0, 0, 2.6, 1.62), 10)
        self.assertEqual(len(urls), 81)

    def test_fractional_tile_origins_match_original_full_merge_without_seams(self):
        from rasterio.merge import merge
        for left, top, pixel in ((300.7, 280, 1), (300.7, 280.3, 0.8)):
            with self.subTest(left=left, top=top, pixel=pixel):
                a = np.full((280, 400), np.nan, dtype="float32")
                b = np.tile(np.arange(400, dtype="float32"), (280, 1))
                payloads = {"B2_t00": self.tile(a),
                            "B2_t01": self.tile(b, left=left, top=top, pixel=pixel)}
                with MemoryFile(payloads["B2_t00"]) as ma, MemoryFile(payloads["B2_t01"]) as mb:
                    with ma.open() as sa, mb.open() as sb:
                        expected, transform = merge([sa, sb], nodata=np.nan, dtype="float32")
                out = self.root / "fractional.tif"
                self.merge(payloads, out, preserve_masks=True)
                with rasterio.open(out) as result:
                    self.assertEqual(result.transform, transform)
                    np.testing.assert_array_equal(result.read(), expected)
