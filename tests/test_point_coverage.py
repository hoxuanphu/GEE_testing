import unittest
from unittest import mock

from fastapi.testclient import TestClient
from app import imagery, main
from app.geo import Bbox


class PointCoverageTests(unittest.TestCase):
    def test_coverage_counts_missing_pixels_and_all_export_bands(self):
        image = mock.Mock()
        with mock.patch.object(imagery, 'ee') as ee:
            imagery._coverage_fraction(image, 'sentinel2', 'roi')
        image.select.assert_called_once_with(['B2', 'B3', 'B4', 'B8', 'B11', 'B12'])
        valid = image.select.return_value.mask.return_value.reduce.return_value.rename.return_value
        valid.unmask.assert_called_once_with(0, sameFootprint=False)
        self.assertEqual(valid.unmask.return_value.reduceRegion.call_args.kwargs['geometry'], 'roi')
        self.assertNotIn('bestEffort', valid.unmask.return_value.reduceRegion.call_args.kwargs)

    def test_single_scene_rejects_insufficient_coverage(self):
        collection = mock.Mock()
        collection.filterBounds.return_value.filter.return_value = collection
        collection.size.return_value.getInfo.return_value = 2
        collection.map.return_value.filter.return_value.size.return_value.getInfo.return_value = 0
        with mock.patch.object(imagery, '_optical_collection', return_value=(collection, 'cloud', 10)), \
             mock.patch.object(imagery, 'ee') as ee:
            with self.assertRaises(imagery.InsufficientCoverageError):
                imagery._point_optical_scene('sentinel2', 'roi', '2026-01-01', '2026-02-01', 60, False)
        ee.Filter.gte.assert_called_once_with('roi_coverage', 0.99)

    def test_point_routes_use_same_merge_flag_and_return_coverage(self):
        scene = imagery.SceneInfo('sentinel2', 'collection', 'scene', '2026-01-01', -1, mock.Mock(),
                                  scene_count=2, coverage_pct=100)
        body = dict(dataset='sentinel2', lon=104, lat=21, radius_m=3000,
                    start_date='2026-01-01', end_date='2026-02-01', merge_scenes=True)
        with mock.patch.object(main, '_ensure_ee'), \
             mock.patch.object(main, '_resolve_selection', return_value=('roi', Bbox(103, 20, 105, 22), None)), \
             mock.patch.object(main, 'best_scene', return_value=scene) as best, \
             mock.patch.object(main, 'preview_rgb', return_value='preview'), \
             TestClient(main.app) as client:
            response = client.post('/api/scene', json=body)
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertTrue(data['composite'])
            self.assertEqual(data['coverage_pct'], 100)
            self.assertIn('merge_scenes=true', data['download_url'])
            self.assertTrue(best.call_args.kwargs['merge_scenes'])
            best.side_effect = imagery.InsufficientCoverageError('Bật ghép nhiều cảnh')
            self.assertEqual(client.get(data['download_url']).status_code, 404)
            self.assertTrue(best.call_args.kwargs['merge_scenes'])
            self.assertEqual(client.post('/api/scene', json=body).status_code, 404)

    def test_merge_prefers_clear_pixels_and_requires_coverage(self):
        collection = mock.Mock()
        collection.filterBounds.return_value.filter.return_value = collection
        collection.size.return_value.getInfo.return_value = 2
        ranked = collection.map.return_value.sort.return_value
        ranked.aggregate_array.side_effect = [mock.Mock(getInfo=lambda: ['a', 'b']),
                                               mock.Mock(getInfo=lambda: [1767225600000, 1767312000000])]
        coverage = mock.Mock()
        coverage.getInfo.return_value = 1.0
        with mock.patch.object(imagery, '_optical_collection', return_value=(collection, 'cloud', 10)), \
             mock.patch.object(imagery, '_coverage_fraction', return_value=coverage), \
             mock.patch.object(imagery, 'ee'):
            result = imagery._point_optical_scene('sentinel2', 'roi', '2026-01-01', '2026-02-01', 60, True)
            self.assertEqual(result.scene_count, 2)
            self.assertEqual(result.coverage_pct, 100)
            collection.map.return_value.sort.assert_called_once_with('roi_clear_fraction')
            ranked.mosaic.return_value.blend.assert_called_once_with(ranked.map.return_value.mosaic.return_value)
            ranked.map.assert_called_once_with(imagery._mask_s2_clouds)
            coverage.getInfo.return_value = 0.15
            with self.assertRaises(imagery.InsufficientCoverageError):
                imagery._point_optical_scene('sentinel2', 'roi', '2026-01-01', '2026-02-01', 60, True)

    def test_best_scene_routes_optical_only(self):
        with mock.patch.object(imagery, 'bbox_to_ee_geom', return_value='roi'), \
             mock.patch.object(imagery, '_point_optical_scene', return_value='scene') as select:
            self.assertEqual(imagery.best_scene('landsat', 104, 21, 3000, '2026-01-01', '2026-02-01', merge_scenes=True), 'scene')
            self.assertTrue(select.call_args.args[-1])


if __name__ == '__main__':
    unittest.main()
