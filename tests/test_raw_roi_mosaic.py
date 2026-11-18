"""Optical RAW mosaics retain clouds and fill original data gaps across dates."""
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest import mock

import ee
from ee.apitestcase import ApiTestCase
from fastapi.testclient import TestClient

from app import imagery, main
from app.geo import Bbox


class RawGapFillGraphTests(ApiTestCase):
    """Build real EE graphs offline; only external evaluations are replaced."""

    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, "_install_cloud_api_resource", install)

    def test_same_tile_other_date_remains_available_to_fill_original_gaps(self):
        bands = ["B2", "B3", "B4", "B8", "B11", "B12", "QA60"]
        rows = [
            {"system:index": "fallback", "system:time_start": 1767225600000,
             "MGRS_TILE": "48Q", "cloud": 30, "hazeScore": .3},
            {"system:index": "preferred", "system:time_start": 1767312000000,
             "MGRS_TILE": "48Q", "cloud": 1, "hazeScore": .1},
        ]
        sources = [ee.Image.constant([i] * 7).rename(bands).set(row)
                   for i, row in enumerate(rows)]
        collection = ee.ImageCollection.fromImages(sources)
        roi = ee.Geometry.Rectangle([106, 20, 106.01, 20.01])

        def list_info(value):
            graph = value.serialize()
            if "AggregateFeatureCollection.array" in graph:
                return ["48Q"]
            return rows

        with mock.patch.object(imagery, "_haze_rank", return_value=collection), \
             mock.patch.object(ee.List, "getInfo", list_info), \
             mock.patch.object(ee.Image, "getInfo", return_value={"properties": rows[1]}), \
             mock.patch.object(ee.Dictionary, "getInfo",
                               side_effect=[{"-1": 5, "1": 95}, {"0": 5, "1": 95}]):
            image, picked = imagery._least_cloudy_per_tile(
                collection, "sentinel2", "cloud", roi, keep_clouds=True)

        self.assertEqual({row["scene_id"] for row in picked}, {"preferred", "fallback"})
        graph = image.serialize()
        self.assertIn("Image.eq", graph)  # one source owns every band at a pixel
        self.assertIn("Reducer.min", graph)  # original joint spectral validity
        self.assertIn("Image.toFloat", graph)  # native QA and absent Float auxiliaries can mix
        self.assertIn("Image.gt", graph)  # source IDs require binary validity, never fractional alpha
        self.assertNotIn("Image.bitwiseAnd", graph)
        self.assertNotIn("Image.blend", graph)

    def test_stable_mask_switch_keeps_one_scene_per_tile_and_same_source_ids(self):
        rows = [dict({'system:index': name, 'system:time_start': 1767225600000,
                      'MGRS_TILE': name, 'cloud': 10}) for name in ['a', 'b']]
        image = ee.Image.constant([.2] * 6 + [9]).rename(['B2', 'B3', 'B4', 'B8', 'B11', 'B12', 'SCL'])
        collection = ee.ImageCollection.fromImages([image.set(row) for row in rows])
        results = []
        for enabled in (True, False):
            with mock.patch.object(imagery, '_haze_rank', return_value=collection), \
                 mock.patch.object(ee.List, 'getInfo', return_value=['a', 'b']), \
                 mock.patch.object(ee.Image, 'getInfo', side_effect=[{'properties': row} for row in rows]), \
                 mock.patch.object(imagery, '_raw_gap_fill', side_effect=AssertionError('Stable is not RAW gap-fill')):
                result, picked = imagery._least_cloudy_per_tile(collection, 'sentinel2', 'cloud',
                    ee.Geometry.Rectangle([106, 10, 106.1, 10.1]), mosaic_mask_clouds=enabled)
            results.append([row['scene_id'] for row in picked])
            self.assertEqual('Image.neq' in result.serialize(), enabled)
        self.assertEqual(results[0], results[1])

    def test_full_primary_coverage_does_not_build_stack_of_all_dates(self):
        rows = [
            {"system:index": f"scene-{i}", "system:time_start": 1767225600000,
             "MGRS_TILE": "48Q", "cloud": 10, "hazeScore": 1 - i / 100}
            for i in range(80)
        ]
        collection = ee.ImageCollection.fromImages([
            ee.Image.constant([1] * 6).rename(["B2", "B3", "B4", "B8", "B11", "B12"]).set(r)
            for r in rows])
        with mock.patch.object(imagery, "_haze_rank", return_value=collection), \
             mock.patch.object(ee.List, "getInfo", return_value=rows), \
             mock.patch.object(ee.Dictionary, "getInfo", return_value={"79": 100}):
            image, picked = imagery._least_cloudy_per_tile(collection, "sentinel2", "cloud",
                ee.Geometry.Rectangle([106, 20, 106.01, 20.01]), keep_clouds=True)
        # An 80-date collection may remain as a source lookup, but the output
        # must contain only one ownership mask when its original data covers ROI.
        self.assertEqual(image.serialize().count('"functionName": "Image.addBands"'), 1)
        self.assertEqual([r["scene_id"] for r in picked], ["scene-79"])

    def test_unused_dates_are_not_reported_as_contributors(self):
        row = {"system:index": "preferred", "system:time_start": 1767312000000,
               "MGRS_TILE": "48Q", "cloud": 1, "hazeScore": .1}
        other = {**row, "system:index": "unused", "cloud": 30, "hazeScore": .3}
        collection = ee.ImageCollection.fromImages([
            ee.Image.constant([1] * 6).rename(["B2", "B3", "B4", "B8", "B11", "B12"]).set(r)
            for r in [other, row]])
        with mock.patch.object(imagery, "_haze_rank", return_value=collection), \
             mock.patch.object(ee.List, "getInfo", return_value=[other, row]), \
             mock.patch.object(ee.Dictionary, "getInfo", return_value={"1": 100}):
            _, picked = imagery._least_cloudy_per_tile(collection, "sentinel2", "cloud",
                ee.Geometry.Rectangle([106, 20, 106.01, 20.01]), keep_clouds=True)
        self.assertEqual([r["scene_id"] for r in picked], ["preferred"])

    def test_balance_only_receives_used_sources_in_priority_order(self):
        from app import color_balance as cb
        rows = [{"system:index": name, "system:time_start": 1767225600000,
                 "MGRS_TILE": name, "cloud": 10} for name in ['unused', 'fallback', 'primary']]
        sources = [ee.Image.constant([.2] * 12 + [4]).rename(cb.S2_BANDS + ['SCL']).set(row)
                   for row in rows]
        collection = ee.ImageCollection.fromImages(sources)
        reports = [{'scene_id': 'primary', 'status': 'reference'},
                   {'scene_id': 'fallback', 'status': 'skipped', 'reason': 'insufficient_overlap'}]
        def balance(images, dataset, roi, evaluate, scene_ids):
            self.assertEqual(scene_ids, ['primary', 'fallback'])
            self.assertEqual(len(images), 2)
            with mock.patch.object(ee.FeatureCollection, 'getInfo', return_value={'features': []}):
                # Network sample evaluations must be guarded by the common lock.
                class Sample:
                    def getInfo(inner):
                        self.assertTrue(imagery._OPTICAL_AGGREGATION_LOCK.locked())
                        return {'features': []}
                evaluate(Sample())
            return images, {'scenes': reports}
        with mock.patch.object(ee.List, 'getInfo', return_value=rows), \
             mock.patch.object(ee.Dictionary, 'getInfo', return_value={'1': 10, '2': 90}), \
             mock.patch('app.color_balance.balance_sources', side_effect=balance) as calibrate:
            image, picked = imagery._raw_gap_fill(collection, collection, 'sentinel2', 'cloud',
                ee.Geometry.Rectangle([106, 10, 106.1, 10.1]), color_balance=True)
        calibrate.assert_called_once()
        self.assertEqual([row['color_balance']['status'] for row in picked], ['skipped', 'reference'])
        self.assertIn('Image.eq', image.serialize())


class RawROIMosaicTests(unittest.TestCase):
    def test_merge_selects_raw_per_tile_without_per_scene_qa_scoring(self):
        for dataset in ("sentinel2", "landsat"):
            for explicit_roi in (False, True):
                with self.subTest(dataset=dataset, explicit_roi=explicit_roi), ExitStack() as stack:
                    roi = object()
                    collection = mock.Mock()
                    collection.filterBounds.return_value.filter.return_value = collection
                    collection.size.return_value.getInfo.return_value = 80
                    collection.toList.side_effect = AssertionError("RAW must not score 80 scenes in Python")
                    image = mock.Mock()
                    image.clip.return_value = image
                    components = [dict(scene_id="tile-a", date="2026-01-02", cloud=10),
                                  dict(scene_id="tile-b", date="2026-01-03", cloud=20)]
                    source = stack.enter_context(mock.patch.object(imagery, "_optical_collection",
                        return_value=(collection, "cloud", 10)))
                    picker = stack.enter_context(mock.patch.object(imagery, "_least_cloudy_per_tile",
                        return_value=(image, components)))
                    stack.enter_context(mock.patch.object(imagery.ee.Filter, "lt", return_value="filter"))
                    stack.enter_context(mock.patch.object(imagery, "bbox_to_ee_geom", return_value=roi))
                    stack.enter_context(mock.patch.object(imagery, "_coverage_fraction",
                        return_value=SimpleNamespace(getInfo=lambda: 1.0)))
                    for name in ("_s2_clear_fraction", "_landsat_clear_fraction",
                                 "_mask_s2_clouds", "_mask_landsat_clouds"):
                        stack.enter_context(mock.patch.object(imagery, name,
                            side_effect=AssertionError("RAW does not run pixel cloud masking/scoring")))
                    result = imagery.best_scene(dataset, 107, 10, 100, "2026-01-01", "2026-01-31",
                        merge_scenes=True, roi=roi if explicit_roi else None,
                        platform="C" if dataset == "sentinel2" else None)
                    source.assert_called_once_with(dataset, "2026-01-01", "2026-01-31",
                        platform="C" if dataset == "sentinel2" else None)
                    picker.assert_called_once_with(collection, dataset, "cloud", roi, keep_clouds=True)
                    self.assertIs(result.ee_object, image)
                    self.assertEqual(result.scene_count, 2)
                    self.assertEqual(result.scenes, tuple(components))
                    self.assertEqual(result.coverage_pct, 100)
                    self.assertIn("raw", result.scene_id)
                    self.assertEqual(image.clip.call_count, 0 if explicit_roi else 1)

    def test_raw_merge_keeps_data_coverage_gate(self):
        collection = mock.Mock()
        collection.filterBounds.return_value.filter.return_value = collection
        collection.size.return_value.getInfo.return_value = 2
        collection.toList.side_effect = AssertionError("Per-scene scoring is not RAW")
        with mock.patch.object(imagery, "_optical_collection", return_value=(collection, "cloud", 10)), \
             mock.patch.object(imagery.ee.Filter, "lt", return_value="filter"), \
             mock.patch.object(imagery, "_least_cloudy_per_tile", return_value=(mock.Mock(), [])), \
             mock.patch.object(imagery, "_coverage_fraction",
                               return_value=SimpleNamespace(getInfo=lambda: .9)):
            with self.assertRaises(imagery.InsufficientCoverageError):
                imagery.best_scene("sentinel2", 107, 10, 100, "2026-01-01", "2026-01-31",
                                   merge_scenes=True, roi=object())

    def test_default_and_response_download_link_identify_raw(self):
        body = dict(dataset="sentinel2", lon=107, lat=10, radius_m=100,
                    start_date="2026-01-01", end_date="2026-01-31", merge_scenes=True)
        self.assertEqual(main.SceneRequest(**body).composite_method, "raw")
        scene = imagery.SceneInfo("sentinel2", "collection", "raw-scene", "2026-01-01", 10,
                                  mock.Mock(), scene_count=2, coverage_pct=100)
        with mock.patch.object(main, "_ensure_ee"), \
             mock.patch.object(main, "_resolve_selection", return_value=("roi", Bbox(106, 9, 108, 11), None)), \
             mock.patch.object(main, "best_scene", return_value=scene), \
             mock.patch.object(main, "preview_rgb", return_value="preview"), TestClient(main.app) as client:
            response = client.post("/api/scene", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["composite_method"], "raw")
        self.assertIn("composite_method=raw", response.json()["download_url"])
