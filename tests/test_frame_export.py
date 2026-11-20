"""Offline fixed-grid tests; synthetic rasters stay in the owned project scratch dir."""
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

import ee
import numpy as np
import rasterio
from affine import Affine
from ee.apitestcase import ApiTestCase
from rasterio.io import MemoryFile
from rasterio.windows import Window

from app import frame_export as export
from app import frame_search as search
from app.frame_models import S2_BANDS, S2_PRESETS, S1_POLARIZATIONS, bands_for

GRID = {"crs": "EPSG:32648", "transform": [10, 2, 398000, 1, -10, 2400000], "width": 7, "height": 5}


def parameters(dataset="s1gee", **kwargs):
    return {"dataset": dataset, "bands": ["VV"] if dataset == "s1gee" else list(S2_BANDS),
            "output_grid": dict(GRID), "require_full_coverage": True, "reference": dict(GRID),
            "grid_mode": "match_grid", **kwargs}


def scene(dataset="s1gee"):
    return {"scene_token": str(uuid4()), "asset_id": search.COLLECTIONS[dataset] + "/raw_scene",
            "timestamp": "2026-01-01T12:00:00Z", "date": "2026-01-01", "band_coverage_pct": 99.99,
            "warnings": []}


def tiff_bytes(grid, values, dtype="float32"):
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=grid["width"], height=grid["height"], count=values.shape[0],
                         dtype=dtype, crs=grid["crs"], transform=Affine(*grid["transform"])) as output:
            output.write(values.astype(dtype))
        return memory.read()


class TileTests(unittest.TestCase):
    def test_tiling_exact_no_overlap_bounds_and_rotated_transform(self):
        grid = {**GRID, "width": 2051, "height": 2049}
        visits = np.zeros((grid["height"], grid["width"]), dtype=np.uint8)
        for window in export.iter_grid_tiles(grid, 12):
            self.assertLessEqual(window.width, 2048)
            self.assertLessEqual(window.height, 2048)
            self.assertLessEqual(window.width * window.height * 12 * 4, 16 * 1024 * 1024)
            row, col = int(window.row_off), int(window.col_off)
            visits[row:row+int(window.height), col:col+int(window.width)] += 1
            target = export._tile_grid(grid, window)
            self.assertEqual(Affine(*target["transform"]) * (0, 0), Affine(*grid["transform"]) * (col, row))
        self.assertTrue(np.all(visits == 1))

    def test_invalid_grid_and_tile_limits_fail_early(self):
        for grid in ({**GRID, "width": 30001}, {**GRID, "transform": [0]*6}, {**GRID, "width": 1.5},
                     {**GRID, "transform": [1, 0, float("nan"), 0, -1, 1]}):
            with self.assertRaises(export.FrameExportError):
                list(export.iter_grid_tiles(grid, 1))
        with self.assertRaises(export.FrameExportError):
            list(export.iter_grid_tiles(GRID, 12, byte_limit=4))

    def test_exact_asset_allowlist_and_band_order(self):
        for asset, dataset, bands in (("other/id", "s1gee", ["VV"]),
                (search.COLLECTIONS["s1gee"] + "/../scene", "s1gee", ["VV"]),
                (search.COLLECTIONS["s1gee"] + "/scene", "s1gee", ["VV", "HV"]),
                (search.COLLECTIONS["sentinel2"] + "/scene", "sentinel2", ["SCL"])):
            with self.assertRaises(search.FrameSearchError):
                export.load_frame_image(asset, dataset, bands)
        self.assertEqual(S2_PRESETS["rgb"], ["B4", "B3", "B2"])
        self.assertEqual(S2_PRESETS["rgbnir"], ["B2", "B3", "B4", "B8"])
        self.assertEqual(len(bands_for({"dataset": "sentinel2"})), 12)
        self.assertEqual(S1_POLARIZATIONS["hhhv"], ["HH", "HV"])


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent.parent / (".frame-export-test-" + uuid4().hex)
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory)
        self.destination = self.directory / "result.tif"
        self.rpc_patch = mock.patch.object(export, "_RPC")
        self.rpc_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.image_patch = mock.patch.object(export, "load_frame_image", return_value="graph")
        self.image_patch.start()
        self.addCleanup(self.image_patch.stop)

    def run_export(self, values, params=None, source_scene=None):
        params = params or parameters()
        source_scene = source_scene or scene(params["dataset"])
        def tile(image, dataset, bands, grid, window, **kwargs):
            y, x = int(window.row_off), int(window.col_off)
            return values[:, y:y+int(window.height), x:x+int(window.width)]
        with mock.patch.object(export, "fetch_grid_tile", side_effect=tile):
            return export.export_frame_scene(source_scene, params, self.destination)

    def test_all_platform_selection_reports_actual_source_platform(self):
        report = self.run_export(np.ma.array(np.zeros((1, 5, 7), np.float32)),
                                 parameters(platform=None), {**scene(), 'platform': 'C'})
        self.assertEqual(report.get('platform'), 'C')
        manifest = export.build_frame_manifest(parameters(platform=None), [report])
        self.assertIsNone(manifest['platform'])
        self.assertEqual(manifest['reports'][0].get('platform'), 'C')

    def test_failed_source_reports_actual_platform_in_manifest(self):
        for failure in ('all_nodata', 'insufficient_coverage'):
            with self.subTest(failure=failure):
                values = np.ma.array(np.ones((1, 5, 7), np.float32), mask=False)
                if failure == 'all_nodata':
                    values.mask[:] = True
                else:
                    values.mask[0, 0, 0] = True
                with self.assertRaises(export.FrameExportError) as caught:
                    self.run_export(values, parameters(platform=None), {**scene(), 'platform': 'A'})
                report = caught.exception.report
                self.assertEqual(caught.exception.code, failure)
                self.assertFalse(self.destination.exists())
                self.assertEqual(report.get('platform'), 'A')
                manifest = export.build_frame_manifest(parameters(platform=None), [report])
                self.assertEqual(manifest['reports'][0].get('platform'), 'A')

    def test_missing_data_mosaic_preserves_primary_and_reports_sources(self):
        sources = [scene(), scene()]
        sources[1]['asset_id'] = search.COLLECTIONS['s1gee'] + '/fallback_scene'
        primary = np.ma.array(np.full((1, 5, 7), -12, np.float32), mask=False)
        primary[0, 0, 0] = 0
        primary.mask[0, 2:, :] = True
        backup = np.ma.array(np.full((1, 5, 7), -24, np.float32), mask=False)
        def tile(image, dataset, bands, grid, window, **kwargs):
            values = primary if image == sources[0]['asset_id'] else backup
            y, x = int(window.row_off), int(window.col_off)
            return values[:, y:y+int(window.height), x:x+int(window.width)]
        with mock.patch.object(export, 'load_frame_image', side_effect=lambda asset, *args: asset), \
                mock.patch.object(export, 'fetch_grid_tile', side_effect=tile):
            report = export.export_frame_scene({**sources[0], 'sources': sources},
                                               parameters(merge_scenes=True), self.destination)
        with rasterio.open(self.destination) as result:
            expected = primary.filled(-24)
            np.testing.assert_array_equal(result.read(), expected)
            self.assertEqual(result.read(1)[0, 0], 0)
            self.assertEqual(json.loads(result.tags()['SOURCE_SCENES']), [s['asset_id'] for s in sources])
        self.assertEqual(report['actual_coverage_pct'], {'VV': 100})
        self.assertEqual(report['mosaic_policy'], 'primary_first_fill_nodata')
        self.assertEqual(len(export.build_frame_manifest(parameters(), [report])['reports'][0]['sources']), 2)
        self.assertEqual(list(self.directory.glob('*.part.tif')), [])

    def test_mosaic_uses_one_source_for_all_bands_and_keeps_unfilled_nodata(self):
        sources = [scene(), scene()]
        sources[1]['asset_id'] = search.COLLECTIONS['s1gee'] + '/backup'
        primary = np.ma.array(np.full((2, 5, 7), -10, np.float32), mask=False)
        backup = np.ma.array(np.full((2, 5, 7), -20, np.float32), mask=False)
        primary.mask[1, 0, 0] = True
        primary.mask[:, 0, 1] = True
        backup.mask[0, 0, 1] = True
        def tile(image, dataset, bands, grid, window, **kwargs):
            return primary if image == sources[0]['asset_id'] else backup
        with mock.patch.object(export, 'load_frame_image', side_effect=lambda asset, *args: asset), \
                mock.patch.object(export, 'fetch_grid_tile', side_effect=tile):
            report = export.export_frame_scene({**sources[0], 'sources': sources},
                parameters(bands=['VV', 'VH'], merge_scenes=True, require_full_coverage=False), self.destination)
        with rasterio.open(self.destination) as result:
            values = result.read()
            np.testing.assert_array_equal(values[:, 0, 0], [-20, -20])
            self.assertTrue(np.isnan(values[:, 0, 1]).all())
            np.testing.assert_array_equal(values[:, 1, 1], [-10, -10])
        self.assertAlmostEqual(report['actual_coverage_pct']['VV'], 34/35*100)
        self.assertEqual(report['actual_coverage_pct']['VV'], report['actual_coverage_pct']['VH'])

    def test_failed_mosaic_preserves_ordered_platform_and_strategy_provenance(self):
        sources = [{**scene(), 'platform': 'C', 'orbit_pass': 'DESCENDING', 'relative_orbit': 128},
                   {**scene(), 'platform': 'C', 'date': '2026-01-02',
                    'timestamp': '2026-01-02T12:00:00Z', 'orbit_pass': 'DESCENDING', 'relative_orbit': 128}]
        sources[1]['asset_id'] += '_second'
        # Native mask preflight can pass while finite values have additional holes.
        values = np.ma.array(np.ones((1, 5, 7), np.float32), mask=False)
        values.mask[:, :, -1] = True
        with mock.patch.object(export, 'fetch_grid_tile', return_value=values):
            with self.assertRaises(export.FrameExportError) as caught:
                export.export_frame_scene({**sources[0], 'sources': sources},
                    parameters(merge_scenes=True), self.destination)
        report = caught.exception.report
        self.assertEqual(caught.exception.code, 'insufficient_coverage')
        self.assertEqual(report['primary_asset_id'], sources[0]['asset_id'])
        self.assertEqual(report['source_time_range_utc'], [sources[0]['timestamp'], sources[1]['timestamp']])
        self.assertEqual([s['platform'] for s in report['sources']], ['C', 'C'])
        self.assertEqual([s['priority'] for s in report['sources']], [1, 2])
        self.assertTrue(all(s['export_provenance']['export_strategy'] == 'direct' for s in report['sources']))
        manifest = export.build_frame_manifest(parameters(), [report])
        self.assertEqual(manifest['reports'][0]['sources'], report['sources'])
        self.assertEqual(manifest['reports'][0]['primary_asset_id'], sources[0]['asset_id'])
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.directory.glob('*.part.tif')), [])

    def test_custom_minimum_coverage_is_checked_after_download(self):
        values = np.ma.array(np.ones((1, 5, 7), dtype=np.float32), mask=False)
        values.mask[:, 0, :] = True
        report = self.run_export(values, parameters(require_full_coverage=False, min_coverage_pct=80))
        self.assertEqual(report['actual_coverage_pct'], {'VV': 80})
        with self.assertRaises(export.FrameExportError) as caught:
            self.run_export(values, parameters(require_full_coverage=False, min_coverage_pct=81))
        self.assertEqual(caught.exception.code, 'insufficient_coverage')

    def test_native_sar_outside_legacy_range_zero_and_exact_grid(self):
        values = np.resize(np.array([-45, -10, 17, 0], np.float32), (1, 5, 7))
        report = self.run_export(np.ma.array(values))
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["actual_coverage_pct"], {"VV": 100})
        with rasterio.open(self.destination) as result:
            np.testing.assert_array_equal(result.read(), values)
            self.assertEqual(list(result.transform)[:6], GRID["transform"])
            self.assertEqual(result.descriptions, ("VV",))
            self.assertEqual(result.tags()["UNITS"], "dB")
            self.assertTrue(np.isnan(result.nodata))
        self.assertFalse(any(self.directory.glob("*.part.tif")))

    def test_fixed_window_stitch_matches_untiled_synthetic(self):
        grid = {**GRID, "width": 2051, "height": 5}
        values = np.arange(2051*5, dtype=np.float32).reshape(1, 5, 2051)
        self.run_export(np.ma.array(values), parameters(output_grid=grid))
        with rasterio.open(self.destination) as result:
            np.testing.assert_array_equal(result.read(), values)

    def test_full12_float_raw_zero_and_synthetic_scaled_values_persist(self):
        values = np.arange(12*35, dtype=np.float32).reshape(12, 5, 7) * np.float32(.0001)
        self.run_export(np.ma.array(values), parameters("sentinel2"))
        with rasterio.open(self.destination) as result:
            np.testing.assert_array_equal(result.read(), values)
            self.assertEqual(result.descriptions, tuple(S2_BANDS))
            self.assertEqual(result.read_masks(1)[0, 0], 255)
            self.assertEqual(result.tags()["UNITS"], "reflectance")
            self.assertEqual(json.loads(result.tags()["SOURCE_NATIVE_RESOLUTION_M"])["B1"], 60)

    def test_partial_coverage_preserves_holes_with_actual_report(self):
        values = np.ma.array(np.zeros((1, 5, 7), np.float32), mask=False)
        values.mask[0, 2, 3] = True
        report = self.run_export(values, parameters(require_full_coverage=False))
        self.assertAlmostEqual(report["actual_coverage_pct"]["VV"], 34/35*100)
        with rasterio.open(self.destination) as result:
            self.assertTrue(np.isnan(result.read(1)[2, 3]))
            self.assertEqual(result.read_masks(1)[0, 0], 255)
        self.assertEqual(report["estimated_band_coverage_pct"], 99.99)

    def test_incomplete_scene_not_published_and_report_retained(self):
        self.destination.write_bytes(b"existing verified result")
        values = np.ma.array(np.ones((1, 5, 7), np.float32), mask=False)
        values.mask[0, 0, 0] = True
        with self.assertRaises(export.FrameExportError) as caught:
            self.run_export(values)
        self.assertEqual(caught.exception.code, "insufficient_coverage")
        self.assertIn("actual_coverage_pct", caught.exception.report)
        self.assertEqual(self.destination.read_bytes(), b"existing verified result")
        self.assertFalse(any(self.directory.glob("*.part.tif")))

    def test_all_nodata_never_published_even_when_partial_allowed(self):
        with self.assertRaises(export.FrameExportError) as caught:
            self.run_export(np.ma.masked_all((1, 5, 7), np.float32), parameters(require_full_coverage=False))
        self.assertEqual(caught.exception.code, "all_nodata")
        self.assertFalse(self.destination.exists())
        self.assertTrue(caught.exception.report["all_nodata"])

    def test_final_validation_rejects_no_common_pixel_even_with_data_in_every_band(self):
        params = parameters(bands=['VV', 'VH'], require_full_coverage=False)
        self.run_export(np.ma.array(np.ones((2, 5, 7), np.float32)), params)
        with rasterio.open(self.destination, 'r+') as output:
            data = np.ones((2, 5, 7), np.float32)
            data[0, :2, :] = np.nan
            data[1, 2:, :] = np.nan
            output.write(data)
        with self.assertRaises(export.FrameExportError) as caught:
            export.validate_frame_export(self.destination, GRID, ['VV', 'VH'])
        self.assertEqual(caught.exception.code, 'all_nodata')
        self.assertEqual(caught.exception.report['common_valid_pixel_count'], 0)

    def test_final_threshold_uses_common_finite_pixels_across_bands(self):
        params = parameters(bands=['VV', 'VH'], require_full_coverage=False, min_coverage_pct=90)
        self.run_export(np.ma.array(np.ones((2, 5, 7), np.float32)), params)
        with rasterio.open(self.destination, 'r+') as output:
            data = np.ones((2, 5, 7), np.float32)
            data[0, 0, :3] = np.nan
            data[1, 1, :3] = np.nan
            output.write(data)
        report = export.validate_frame_export(self.destination, GRID, ['VV', 'VH'])
        self.assertGreater(min(report['actual_coverage_pct'].values()), 90)
        with self.assertRaises(export.FrameExportError) as caught:
            export._check_coverage(report, params)
        self.assertEqual(caught.exception.code, 'insufficient_coverage')
        self.assertEqual(report['common_valid_pixel_count'], 29)
        self.assertAlmostEqual(report['common_band_coverage_pct'], 29 / 35 * 100)

    def test_metadata_and_affine_mismatch_rejected_on_reopen(self):
        self.run_export(np.ma.array(np.ones((1, 5, 7), np.float32)))
        with self.assertRaises(export.FrameExportError):
            export.validate_frame_export(self.destination, {**GRID, "transform": [10, 2, 398001, 1, -10, 2400000]}, ["VV"])
        with rasterio.open(self.destination, "r+") as result:
            result.update_tags(RESAMPLING="bilinear")
        with self.assertRaises(export.FrameExportError) as caught:
            export.validate_frame_export(self.destination, GRID, ["VV"])
        self.assertEqual(caught.exception.code, "metadata_mismatch")

    def test_atomic_promote_occurs_after_closed_reopened_validation(self):
        original = export.validate_frame_export
        calls = []
        def validate(path, *args):
            self.assertFalse(self.destination.exists())
            calls.append("validate")
            return original(path, *args)
        replace = export.os.replace
        def promote(source, destination):
            self.assertEqual(calls, ["validate"])
            # Opening here catches unclosed output handles on Windows.
            with rasterio.open(source) as persisted:
                self.assertEqual(persisted.count, 1)
            return replace(source, destination)
        with mock.patch.object(export, "validate_frame_export", side_effect=validate), \
                mock.patch.object(export.os, "replace", side_effect=promote):
            self.run_export(np.ma.array(np.ones((1, 5, 7), np.float32)))
        self.assertTrue(self.destination.exists())

    def test_disk_reserve_fails_before_network(self):
        with mock.patch.object(export.shutil, "disk_usage", return_value=mock.Mock(free=1)), \
                mock.patch.object(export, "load_frame_image") as image:
            with self.assertRaises(export.FrameExportError) as caught:
                export.export_frame_scene(scene(), parameters(), self.destination)
        self.assertEqual(caught.exception.code, "disk_full")
        image.assert_not_called()

    def test_manifest_contains_both_coverages_but_not_url_or_server_path(self):
        params = parameters(reference={**GRID, "path": "C:\\secret\\upload.tif", "tags": {"secret": "token"}})
        report = self.run_export(np.ma.array(np.ones((1, 5, 7), np.float32)), params)
        report["url"] = "https://signed.example/?token=secret"
        report["path"] = str(self.destination)
        manifest = export.build_frame_manifest(params, [report])
        serialized = json.dumps(manifest)
        for forbidden in ("signed.example", "token=", "C:\\secret", str(self.directory), '"secret"'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(manifest["reports"][0]["actual_coverage_pct"], {"VV": 100})
        self.assertEqual(manifest["reports"][0]["estimated_band_coverage_pct"], 99.99)
        self.assertEqual(manifest["processing"]["scale_factor"], 1)


class GraphAndFetchTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, "_install_cloud_api_resource", install)

    def test_raw_scaling_once_and_no_sar_log_or_clamp(self):
        for dataset, bands in (("sentinel2", list(S2_BANDS)), ("s1gee", ["VV"])):
            with mock.patch("app.ee_session.init_ee"):
                image = export.load_frame_image(scene(dataset)["asset_id"], dataset, bands)
            graph = image.serialize()
            self.assertEqual(graph.count("0.0001"), 1 if dataset == "sentinel2" else 0)
            self.assertNotIn("clamp", graph)
            self.assertNotIn("log10", graph)
            self.assertNotIn("Image.neq", graph)

    def test_resampling_and_masks_are_separate_without_preclip(self):
        for dataset, bands in (("sentinel2", ["B1", "B2"]), ("s1gee", ["VV", "VH"])):
            graph = export._target_image(ee.Image("raw"), dataset, bands, GRID).serialize()
            self.assertEqual("Image.resample" in graph, dataset == "sentinel2")
            self.assertIn("Image.mask", graph)
            self.assertIn("Image.updateMask", graph)
            self.assertIn(str(export._WIRE_NODATA), graph)
            self.assertNotIn("Image.clip", graph)
            self.assertNotIn("log10", graph)

    def test_tile_grid_direct_wire_sentinel_valid_zero_and_retry_corrupt(self):
        values = np.zeros((1, 5, 7), np.float32)
        values[0, 2, 3] = export._WIRE_NODATA
        values[0, 3, 3] = export._LEGACY_WIRE_NODATA
        self.assertNotEqual(export._WIRE_NODATA, int(export._WIRE_NODATA))
        self.assertEqual(float(np.float32(export._WIRE_NODATA)), export._WIRE_NODATA)
        rpc = mock.Mock()
        rpc.thumbnail_once.side_effect = [b"<html>expired</html>", tiff_bytes(GRID, values)]
        with mock.patch.object(search.time, "sleep") as sleep:
            result = export.fetch_grid_tile(ee.Image("raw"), "s1gee", ["VV"], GRID, Window(0, 0, 7, 5), rpc=rpc)
        self.assertTrue(result.mask[0, 2, 3])
        self.assertTrue(result.mask[0, 3, 3])
        self.assertFalse(result.mask[0, 0, 0])
        self.assertEqual(result.data[0, 0, 0], 0)
        self.assertEqual(rpc.thumbnail_once.call_count, 2)
        sleep.assert_called_once_with(1)
        params = rpc.thumbnail_once.call_args.args[1]
        self.assertEqual(params["crs_transform"], GRID["transform"])
        self.assertEqual(params["dimensions"], [7, 5])
        self.assertNotIn("region", params)
        self.assertNotIn("scale", params)

    def test_bad_tile_header_not_written_or_reprojected_locally(self):
        rpc = mock.Mock()
        rpc.thumbnail_once.return_value = tiff_bytes({**GRID, "width": 8}, np.zeros((1, 5, 8), np.float32))
        with self.assertRaises(export.FrameExportError) as caught:
            export.fetch_grid_tile(ee.Image("raw"), "s1gee", ["VV"], GRID, Window(0, 0, 7, 5), rpc=rpc)
        self.assertEqual(caught.exception.code, "grid_mismatch")
        self.assertEqual(rpc.thumbnail_once.call_count, 1)

    def test_native_hole_remains_masked_even_if_bilinear_value_exists(self):
        values = np.ones((2, 5, 7), np.float32)
        values[1, 2, 3] = export._WIRE_NODATA
        rpc = mock.Mock()
        rpc.thumbnail_once.return_value = tiff_bytes(GRID, values)
        result = export.fetch_grid_tile(ee.Image("raw"), "sentinel2", ["B4", "B3"], GRID,
                                        Window(0, 0, 7, 5), rpc=rpc)
        self.assertTrue(result.mask[:, 2, 3].all())
        self.assertFalse(result.mask[:, 0, 0].any())


if __name__ == "__main__":
    unittest.main()
