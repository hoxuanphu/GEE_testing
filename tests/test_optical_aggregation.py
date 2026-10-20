"""Regression tests for bounded optical ROI aggregation graphs."""
from contextlib import ExitStack
import unittest
from unittest import mock

from app import imagery


class _Value:
    def __init__(self, value):
        self.value = value

    def getInfo(self):
        return self.value


class _Image:
    def __init__(self, name, coverage, clear, cloud=10):
        self.name = name
        self.coverage = coverage
        self.clear = clear
        self.properties = {"system:index": name, "system:time_start": 1767225600000,
                           "cloud": cloud}

    def get(self, key):
        return self.properties.get(key)


class _Candidates:
    def __init__(self, images):
        self.images = images

    def get(self, index):
        return self.images[index]


class OpticalAggregationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.images = [_Image("partial", .98, 1), _Image("cloudy", 1, .2),
                       _Image("clear", .995, .9, cloud=0)]
        self.collection = mock.Mock()
        self.collection.filterBounds.return_value.filter.return_value = self.collection
        self.collection.size.return_value = _Value(len(self.images))
        self.collection.toList.side_effect = lambda count: _Candidates(self.images[:count])
        self.collection.map.side_effect = AssertionError("Unbounded aggregation map")
        self.stack.enter_context(mock.patch.object(imagery, "_optical_collection",
            return_value=(self.collection, "cloud", 10)))
        self.ee = self.stack.enter_context(mock.patch.object(imagery, "ee"))
        self.ee.Image.side_effect = lambda image: image
        self.ee.Dictionary.side_effect = lambda data: _Value({
            key: value.value if isinstance(value, _Value) else value
            for key, value in data.items()})
        self.ee.Date.return_value.format.return_value = "2026-01-01"
        self.coverage = self.stack.enter_context(mock.patch.object(imagery, "_coverage_fraction",
            side_effect=lambda image, *args: _Value(image.coverage)))
        self.clear = self.stack.enter_context(mock.patch.object(imagery, "_s2_clear_fraction",
            side_effect=lambda image, *args: _Value(image.clear)))
        self.landsat_clear = self.stack.enter_context(mock.patch.object(imagery, "_landsat_clear_fraction",
            side_effect=lambda image, *args: _Value(image.clear)))

    def select(self, merge=False, dataset="sentinel2", clip=True):
        return imagery._point_optical_scene(dataset, "roi", "2026-01-01", "2026-02-01",
                                            60, merge, clip_output=clip)

    def test_single_scores_sequentially_and_keeps_roi_ranking(self):
        result = self.select()
        self.assertEqual(result.scene_id, "clear")
        self.assertIs(result.ee_object, self.images[2])
        self.assertEqual(result.cloud_pct, 0)
        self.assertEqual(result.coverage_pct, 99.5)
        self.assertEqual(self.coverage.call_count, 3)
        self.assertEqual([call.args[0].name for call in self.clear.call_args_list], ["cloudy", "clear"])
        self.collection.map.assert_not_called()
        self.ee.ImageCollection.fromImages.assert_not_called()

    def test_landsat_uses_qa_score_and_all_candidates(self):
        self.images.extend(_Image(f"extra-{i}", 1, .1) for i in range(40))
        self.images.append(_Image("winner-last", 1, .99))
        self.collection.size.return_value = _Value(len(self.images))
        result = self.select(dataset="landsat")
        self.assertEqual(result.scene_id, "winner-last")
        self.clear.assert_not_called()
        self.assertEqual(self.landsat_clear.call_count, len(self.images) - 1)

    def test_insufficient_coverage_does_not_compute_clouds(self):
        for image in self.images:
            image.coverage = .98
        with self.assertRaises(imagery.InsufficientCoverageError):
            self.select()
        self.clear.assert_not_called()

    def test_empty_collection_returns_none(self):
        self.collection.size.return_value = _Value(0)
        self.assertIsNone(self.select())
        self.coverage.assert_not_called()

    def test_merge_materializes_order_and_only_checks_final_coverage(self):
        ranked = self.ee.ImageCollection.fromImages.return_value
        merged = ranked.mosaic.return_value.blend.return_value
        merged.coverage = 1
        merged.clip.return_value.coverage = 1
        result = self.select(merge=True, clip=False)
        ordered = self.ee.ImageCollection.fromImages.call_args.args[0]
        self.assertEqual([image.name for image in ordered], ["cloudy", "clear", "partial"])
        self.assertEqual([scene["scene_id"] for scene in result.scenes], ["cloudy", "clear", "partial"])
        self.assertEqual(result.scene_count, 3)
        self.assertEqual(result.coverage_pct, 100)
        self.coverage.assert_called_once_with(merged, "sentinel2", "roi")
        merged.clip.assert_not_called()
        ranked.map.assert_called_once_with(imagery._mask_s2_clouds)
        ranked.mosaic.return_value.blend.assert_called_once_with(ranked.map.return_value.mosaic.return_value)
        ranked.aggregate_array.assert_not_called()

    def test_missing_qa_score_is_not_ranked_as_clear(self):
        self.images[1].clear = None
        result = self.select()
        self.assertEqual(result.scene_id, "clear")

    def test_cloud_reduction_is_evaluated_under_shared_lock(self):
        def clear_score(image, *args):
            self.assertTrue(imagery._OPTICAL_AGGREGATION_LOCK.locked())
            return _Value(image.clear)

        self.clear.side_effect = clear_score
        self.assertEqual(self.select().scene_id, "clear")
        self.assertFalse(imagery._OPTICAL_AGGREGATION_LOCK.locked())

    def test_reductions_share_a_lock_and_release_it_on_failure(self):
        def fail_coverage(*args):
            self.assertTrue(imagery._OPTICAL_AGGREGATION_LOCK.locked())
            raise RuntimeError("EE failed")

        self.coverage.side_effect = fail_coverage
        with self.assertRaisesRegex(RuntimeError, "EE failed"):
            self.select()
        self.assertFalse(imagery._OPTICAL_AGGREGATION_LOCK.locked())

    def test_merge_rejects_incomplete_mosaic(self):
        merged = self.ee.ImageCollection.fromImages.return_value.mosaic.return_value.blend.return_value
        merged.clip.return_value.coverage = .5
        with self.assertRaises(imagery.InsufficientCoverageError):
            self.select(merge=True)


if __name__ == "__main__":
    unittest.main()
