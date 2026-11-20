"""Offline matched-frame search and transport contract tests."""
import json
import time
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import ee
import numpy as np
from ee.apitestcase import ApiTestCase

from app import frame_search as search
from app.frame_models import FrameError, FrameSearchRequest
from app.reference_frame import reference_roi_identity


def request(**kwargs):
    return FrameSearchRequest(reference_id=uuid4(), start_date=date(2026, 1, 1),
                              end_date=date(2026, 1, 31), **kwargs)


FRAME = {"crs": "EPSG:32648", "transform": [10, 2, 398000, 1, -10, 2400000],
         "width": 8, "height": 5}


def raw_scene(asset="scene", **kwargs):
    return {"asset_id": search.COLLECTIONS["sentinel2"] + "/" + asset,
            "time": 1767225600000, "platform": "Sentinel-2A", "tile": "48PXS",
            "orbit_pass": "DESCENDING", "relative_orbit": 12,
            "covers_roi": True, "band_coverage_pct": 100, "roi_cloud_shadow_pct": 5,
            "tile_cloud_pct": 90, **kwargs}


class RankingTests(unittest.TestCase):
    def test_cloud_coverage_distance_timestamp_asset_ties_and_null_last(self):
        req = request(reference_date=date(2026, 1, 2))
        def scene(name, day, cloud, coverage):
            return {"asset_id": name, "date": "2026-01-" + day,
                    "timestamp": "2026-01-" + day + "T00:00:00Z",
                    "roi_cloud_shadow_pct": cloud, "band_coverage_pct": coverage,
                    "tile_cloud_pct": 0 if name == "z" else 100}
        scenes = [scene("z", "02", 0, 99), scene("b", "02", 0, 100),
                  scene("a", "02", 0, 100), scene("x", "01", 0, 100),
                  scene("n", "02", None, 100)]
        self.assertEqual([s["asset_id"] for s in search.rank_frame_scenes(scenes, req)], ["a", "b", "x", "z", "n"])
        self.assertEqual(scenes[0]["asset_id"], "z")

    def test_nearest_date_first_and_required(self):
        req = request(sort="nearest_date", reference_date=date(2026, 1, 2))
        scenes = [{"asset_id": "a", "date": "2026-01-01", "timestamp": "a", "roi_cloud_shadow_pct": 0,
                   "band_coverage_pct": 100},
                  {"asset_id": "b", "date": "2026-01-02", "timestamp": "b", "roi_cloud_shadow_pct": 95,
                   "band_coverage_pct": 100}]
        self.assertEqual(search.rank_frame_scenes(scenes, req)[0]["asset_id"], "b")
        with self.assertRaises(FrameError):
            search.rank_frame_scenes([], {"sort": "nearest_date"})

    def test_sar_reference_track_precedes_nearest_without_excluding_other_tracks(self):
        req = request(dataset="s1gee", reference_date=date(2026, 1, 2), prefer_reference_orbit=True,
                      reference_orbit_pass="DESCENDING", reference_relative_orbit=12)
        scenes = [{"asset_id": "near", "date": "2026-01-02", "timestamp": "a", "orbit_pass": "ASCENDING", "relative_orbit": 12},
                  {"asset_id": "match", "date": "2026-01-20", "timestamp": "b", "orbit_pass": "DESCENDING", "relative_orbit": 12}]
        self.assertEqual([s["asset_id"] for s in search.rank_frame_scenes(scenes, req)], ["match", "near"])
        self.assertFalse(search._orbit_match(scenes[1], {"prefer_reference_orbit": True}))

    def test_sar_without_reference_defaults_to_latest(self):
        req = request(dataset='s1gee')
        scenes = [{'asset_id': 'old', 'date': '2026-01-01', 'timestamp': '2026-01-01T00:00:00Z'},
                  {'asset_id': 'latest', 'date': '2026-01-25', 'timestamp': '2026-01-25T00:00:00Z'}]
        self.assertEqual(search.rank_frame_scenes(scenes, req)[0]['asset_id'], 'latest')

    def test_snapshot_eligibility_null_and_unique_satellites(self):
        a = search._scene_row(raw_scene("a"), request())
        b = search._scene_row(raw_scene("b", platform="Sentinel-2B"), request())
        self.assertNotEqual(a["scene_token"], b["scene_token"])
        self.assertEqual(a["timestamp"], "2026-01-01T00:00:00Z")
        self.assertIn("scl_does_not_guarantee_haze_free", a["warnings"])
        self.assertFalse(search._scene_row(raw_scene(band_coverage_pct=None), request())["eligible"])
        self.assertFalse(search._scene_row(raw_scene(covers_roi=False), request())["eligible"])
        self.assertTrue(search._scene_row(raw_scene(covers_roi=False), request(require_full_coverage=False))["eligible"])
        self.assertFalse(search._scene_row(raw_scene(), request(max_cloud_shadow_pct=4))["eligible"])

    def test_cloud_range_coverage_threshold_and_mosaic_partial_sources(self):
        self.assertTrue(search._scene_row(raw_scene(band_coverage_pct=80), request(
            require_full_coverage=False, min_coverage_pct=75))['eligible'])
        self.assertFalse(search._scene_row(raw_scene(band_coverage_pct=70), request(
            require_full_coverage=False, min_coverage_pct=75))['eligible'])
        self.assertFalse(search._scene_row(raw_scene(), request(min_cloud_shadow_pct=6))['eligible'])
        self.assertTrue(search._scene_row(raw_scene(), request(
            min_cloud_shadow_pct=5, max_cloud_shadow_pct=5))['eligible'])
        self.assertTrue(search._scene_row(raw_scene(covers_roi=False, band_coverage_pct=50), request(
            merge_scenes=True))['eligible'])
        self.assertFalse(search._scene_row(raw_scene(band_coverage_pct=0), request(merge_scenes=True))['eligible'])
        with self.assertRaises(ValueError):
            request(min_cloud_shadow_pct=60, max_cloud_shadow_pct=20)

    def test_zero_threshold_requires_known_positive_coverage(self):
        for dataset in ('sentinel2', 's1gee'):
            for merging in (False, True):
                req = request(dataset=dataset, require_full_coverage=False, min_coverage_pct=0, merge_scenes=merging)
                for coverage in (None, 0, float('nan'), float('inf'), -1, 101, '100', True):
                    with self.subTest(dataset=dataset, merging=merging, coverage=coverage):
                        self.assertFalse(search._scene_row(raw_scene(band_coverage_pct=coverage), req)['eligible'])
                self.assertTrue(search._scene_row(raw_scene(band_coverage_pct=.01), req)['eligible'])

    def test_coverage_threshold_boundaries_and_invalid_explicit_thresholds(self):
        from app import frame_models
        for threshold in (0, 95, 99.9, 100):
            req = request(require_full_coverage=False, min_coverage_pct=threshold)
            self.assertTrue(search._scene_row(raw_scene(band_coverage_pct=max(.01, threshold)), req)['eligible'])
            if threshold:
                self.assertFalse(search._scene_row(raw_scene(band_coverage_pct=threshold-.01), req)['eligible'])
        self.assertTrue(hasattr(frame_models, 'effective_min_coverage'))
        self.assertEqual(frame_models.effective_min_coverage({'require_full_coverage': True, 'min_coverage_pct': 0}), 99.9)
        for invalid in (None, float('nan'), float('inf'), -1, 101, '0', False):
            for full in (False, True):
                with self.subTest(invalid=invalid, full=full), self.assertRaises(FrameError):
                    frame_models.effective_min_coverage({'require_full_coverage': full, 'min_coverage_pct': invalid})

    def test_bounded_retry_and_safe_permanent_errors(self):
        end = time.monotonic() + 30
        with mock.patch.object(search.time, "sleep") as sleep:
            operation = mock.Mock(side_effect=[search._TransientError(), search._TransientError(), "ok"])
            self.assertEqual(search._retry(operation, end), "ok")
            self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
            operation = mock.Mock(side_effect=ValueError("secret signed URL"))
            with self.assertRaises(search.FrameUpstreamError) as caught:
                search._retry(operation, end)
            self.assertNotIn("secret", str(caught.exception))
            self.assertEqual(operation.call_count, 1)
        with self.assertRaises(search._DeadlineError):
            search._retry(lambda: "late", time.monotonic() - 1)

    def test_concurrent_search_rejected_before_initialization(self):
        search._SEARCH_LOCK.acquire()
        try:
            with self.assertRaises(FrameError) as caught:
                search.search_frame_scenes(request(), FRAME)
            self.assertEqual(caught.exception.status, 409)
        finally:
            search._SEARCH_LOCK.release()

    def test_cold_search_enters_initialized_rpc_scope_before_building_roi(self):
        for dataset in ('sentinel2', 's1gee'):
            with self.subTest(dataset=dataset):
                entered = False
                rpc = mock.Mock()
                rpc.compute.return_value = []
                class Scope:
                    def __enter__(scope):
                        nonlocal entered
                        entered = True
                        return rpc
                    def __exit__(scope, *args):
                        return False
                def roi(frame):
                    self.assertTrue(entered, 'EE geometry constructed before scoped EE initialization')
                    self.assertEqual(frame, FRAME)
                    return 'canonical-roi'
                with mock.patch.object(search, '_RPC', return_value=Scope()), \
                        mock.patch.object(search, '_frame_roi', side_effect=roi), \
                        mock.patch.object(search, 'ee'):
                    self.assertEqual(search.search_frame_scenes(request(dataset=dataset), FRAME), [])
                rpc.compute.assert_called_once()


class GraphTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, "_install_cloud_api_resource", install)

    def test_source_polygon_not_bbox_and_end_date_inclusive(self):
        rpc = mock.Mock()
        rpc.compute.side_effect = [[raw_scene()["asset_id"]], [raw_scene()]]
        with mock.patch.object(search, "_RPC") as scoped:
            scoped.return_value.__enter__.return_value = rpc
            scenes = search.search_frame_scenes(request(platform="A"), FRAME)
        graph = rpc.compute.call_args_list[0].args[0].serialize()
        self.assertIn("2026-02-01", graph)
        self.assertIn("2026-01-01", graph)
        self.assertIn("Sentinel-2A", graph)
        self.assertIn("EPSG:4326", graph)
        self.assertNotIn("EPSG:32648", graph)
        self.assertIn("104.", graph)
        self.assertEqual(scenes[0]["asset_id"], raw_scene()["asset_id"])
        self.assertEqual(scoped.call_args.args[0], 120)

    def test_custom_eqc_roi_uses_same_wgs84_footprint_as_map_and_preview(self):
        from app.reference_frame import reference_footprint
        frame = dict(crs='+proj=eqc +lat_ts=21.7423783969593 +lat_0=21.7423783969593 '
                         '+lon_0=104.035720835673 +R=6378166.17539632 +units=m',
                     transform=[10, 0, -8065, 0, -10, 8095], width=1614, height=1619)
        footprint = reference_footprint(frame)['footprint_geojson']
        polygon = search._frame_roi(frame)
        encoded = polygon.serialize()
        self.assertNotIn('Equirectangular', encoded)
        self.assertIn('EPSG:4326', encoded)
        latitudes = [p[1] for p in footprint['coordinates'][0]]
        self.assertGreater(min(latitudes), 21.6)
        self.assertLess(max(latitudes), 21.9)
        self.assertEqual(polygon.args['coordinates'].serialize(), ee.List(footprint['coordinates']).serialize())

    def test_empty_and_overflow_do_not_compute_quality(self):
        for ids, expected in (([], None), (["x"] * 501, "too_many_scenes")):
            rpc = mock.Mock()
            rpc.compute.return_value = ids
            with mock.patch.object(search, "_RPC") as scoped:
                scoped.return_value.__enter__.return_value = rpc
                if expected:
                    with self.assertRaises(FrameError) as caught:
                        search.search_frame_scenes(request(), FRAME)
                    self.assertEqual(caught.exception.code, expected)
                else:
                    self.assertEqual(search.search_frame_scenes(request(), FRAME), [])
            self.assertEqual(rpc.compute.call_count, 1)

    def test_sar_filters_raw_iw_and_every_requested_pol(self):
        rpc = mock.Mock()
        rpc.compute.return_value = []
        with mock.patch.object(search, "_RPC") as scoped:
            scoped.return_value.__enter__.return_value = rpc
            search.search_frame_scenes(request(dataset="s1gee", orbit_pass="DESCENDING", relative_orbit=12), FRAME)
        graph = rpc.compute.call_args.args[0].serialize()
        for text in ("COPERNICUS/S1_GRD", "instrumentMode", "IW", "VV", "VH", "orbitProperties_pass", "relativeOrbitNumber_start"):
            self.assertIn(text, graph)
        self.assertNotIn("clamp", graph)

    def test_metric_graphs_have_explicit_scale_and_no_best_effort(self):
        for optical, scale in ((True, 20), (False, 100)):
            with mock.patch.object(search, "_area_sums", wraps=search._area_sums) as sums:
                expression = (search.s2_roi_metrics if optical else search.s1_roi_metrics)(
                    ee.Image("asset"), search._frame_roi(FRAME), ["B4"] if optical else ["VV"])
            self.assertEqual(sums.call_args.args[-1], scale)
            graph = expression.serialize()
            self.assertIn('"maxPixels"', graph)
            self.assertIn('10000000', graph)
            self.assertIn('"bestEffort"', graph)
            self.assertIn('false', graph)
            self.assertNotIn('Image.eqZero', graph)

    def test_preview_is_separate_and_returns_only_png_bytes(self):
        rpc = mock.Mock()
        rpc.thumbnail_once.return_value = b"\x89PNG\r\n\x1a\nabc"
        with mock.patch.object(search, "_RPC") as scoped:
            scoped.return_value.__enter__.return_value = rpc
            result = search.make_frame_preview(raw_scene(), {"dataset": "sentinel2", "bands": ["B4"], "reference": FRAME, **reference_roi_identity(FRAME)})
        args, kwargs = rpc.thumbnail_once.call_args
        self.assertIsInstance(result, bytes)
        self.assertTrue(kwargs["preview"])
        self.assertEqual(args[1]["bands"], ["B4", "B3", "B2"])
        self.assertEqual(args[1]["dimensions"], 512)
        self.assertEqual(args[1]["max"], 0.3)
        self.assertIn("0.0001", args[0].serialize())
        self.assertNotIn("visualize", args[0].serialize())

    def test_transport_does_not_modify_global_deadline_or_cloud_resource(self):
        state = SimpleNamespace(cloud_api_base_url="https://earthengine.googleapis.com",
                                cloud_api_user_project="offline", credentials=object())
        with mock.patch.object(search, "init_ee"), mock.patch.object(ee.data, "_get_state", return_value=state), \
                mock.patch.object(search, "AuthorizedSession") as session, mock.patch.object(ee.data, "setDeadline") as deadline:
            response = session.return_value.request.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.return_value = [b'{"result": []}']
            with search._RPC(120, time.monotonic() + 300) as rpc:
                self.assertEqual(rpc.compute(ee.List([])), [])
            deadline.assert_not_called()
            self.assertEqual(session.call_args.kwargs["max_refresh_attempts"], 0)
            self.assertEqual(session.return_value.request.call_args.kwargs["timeout"] <= 120, True)
            self.assertEqual(session.return_value.request.call_count, 1)


class NumericMetricTests(unittest.TestCase):
    """Evaluate the actual metric expression with a tiny masked-array EE fake."""
    class Image:
        def __init__(self, values):
            self.values = np.ma.asarray(values)
        def select(self, bands):
            return self
        def mask(self):
            return type(self)((~np.ma.getmaskarray(self.values)).astype(float))
        def gt(self, value):
            return type(self)(self.values > value)
        def neq(self, value):
            return type(self)(self.values != value)
        def eq(self, value):
            return type(self)(self.values == value)
        def And(self, other):
            return type(self)(np.ma.logical_and(self.values, other.values))
        def Or(self, other):
            return type(self)(np.ma.logical_or(self.values, other.values))
        def unmask(self, value, **kwargs):
            return type(self)(self.values.filled(value))
        def reduce(self, reducer):
            return self
        def projection(self):
            return mock.Mock()
        def reproject(self, *args):
            return self

    def test_scl_denominator_zero_native_masks_and_missing_roi(self):
        fake_ee = mock.Mock()
        fake_ee.Dictionary.side_effect = lambda value: value
        fake_ee.Image.constant.side_effect = lambda value: self.Image(np.ones(10))
        fake_ee.Image.cat.side_effect = lambda images: images[0]
        scl = self.Image(np.ma.array([0, 1, 3, 4, 8, 9, 10, 11, 4, 4], mask=[0]*8 + [1, 1]))
        # Reflectance zero is valid. The final two pixels are outside the source footprint.
        band = self.Image(np.ma.array(np.zeros(10), mask=[0]*8 + [1, 1]))
        source = mock.Mock()
        source.select.side_effect = lambda bands: scl if bands == "SCL" else band
        def sums(layers, names, *args):
            return dict(zip(names, [float(layer.values.sum()) for layer in layers]))
        def ratio(numerator, denominator):
            return numerator / denominator * 100 if denominator else None
        with mock.patch.object(search, "ee", fake_ee), mock.patch.object(search, "_area_sums", side_effect=sums), \
                mock.patch.object(search, "_ratio", side_effect=ratio):
            result = search.s2_roi_metrics(source, "roi", ["B4"])
        self.assertEqual(result["roi_cloud_pct"], 50)
        self.assertAlmostEqual(result["roi_shadow_pct"], 100 / 6)
        self.assertAlmostEqual(result["roi_cloud_shadow_pct"], 400 / 6)
        self.assertEqual(result["scl_valid_pct"], 60)
        self.assertEqual(result["band_coverage_pct"], 80)


if __name__ == "__main__":
    unittest.main()
