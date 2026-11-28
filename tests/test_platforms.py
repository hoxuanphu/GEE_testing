"""Platform filtering is applied before ranking, merging and download selection."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app import imagery, main, s1_mosaic
from app.geo import Bbox


class Collection:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, expression):
        operation, key, value = expression
        if operation == 'eq':
            return Collection([row for row in self.rows if row.get(key) == value])
        if operation == 'neq':
            return Collection([row for row in self.rows if row.get(key) != value])
        return self

    def filterDate(self, *args):
        return self

    def filterBounds(self, *args):
        return self

    def map(self, callback):
        return self

    def size(self):
        return MagicMock(getInfo=lambda: len(self.rows))

    def sort(self, *args):
        return self

    def limit(self, count):
        return Collection(self.rows[:count])

    def aggregate_array(self, key):
        return MagicMock(getInfo=lambda: [row.get(key) for row in self.rows])


class PlatformTests(unittest.TestCase):
    def test_validate_rejects_empty_invalid_and_unsupported(self):
        from app.platforms import validate_platform
        for dataset, codes in [('s1gee', 'ABCD'), ('sentinel2', 'ABC')]:
            self.assertIsNone(validate_platform(dataset, None))
            for code in codes:
                self.assertEqual(validate_platform(dataset, code), code)
        for dataset, value in [('s1gee', ''), ('s1gee', 'ALL'), ('s1gee', 'c'),
                               ('s1gee', ' C '), ('sentinel2', 'D'), ('sentinel2', 'Sentinel-2C'),
                               ('landsat', 'A')]:
            with self.subTest(dataset=dataset, value=value), self.assertRaises(ValueError):
                validate_platform(dataset, value)
        self.assertIsNone(validate_platform('landsat', None))

    def test_filter_retains_only_selected_code_and_all_retains_every_row(self):
        from app.platforms import apply_platform_filter
        for dataset, key, prefix, codes in [('s1gee', 'platform_number', '', 'ABCD'),
                                            ('sentinel2', 'SPACECRAFT_NAME', 'Sentinel-2', 'ABC')]:
            collection = Collection([{key: prefix + code} for code in codes])
            with patch('app.platforms.ee.Filter.eq', side_effect=lambda key, val: ('eq', key, val)):
                filtered = apply_platform_filter(collection, dataset, 'C')
            self.assertEqual(filtered.rows, [{key: prefix + 'C'}])
            self.assertIs(apply_platform_filter(collection, dataset, None), collection)

    def test_s1_and_s2_collections_filter_before_ranking(self):
        rows = [{'platform_number': code, 'SPACECRAFT_NAME': 'Sentinel-2' + code, 'instrumentMode': 'IW'} for code in 'ABCD']
        with patch.object(imagery.ee, 'ImageCollection', return_value=Collection(rows)), \
                patch.object(imagery.ee, 'Filter') as filters:
            filters.eq.side_effect = lambda key, val: ('eq', key, val)
            filters.lt.side_effect = lambda key, val: ('lt', key, val)
            filters.listContains.side_effect = lambda key, val: ('contains', key, val)
            for collection in (imagery._s1_collection('2026-01-01', '2026-02-01', 'vvvh', platform='C'),
                               imagery._s2_collection('2026-01-01', '2026-02-01', platform='C')):
                self.assertEqual([row['platform_number'] for row in collection.rows], ['C'])
            self.assertEqual(len(imagery._s1_collection('start', 'end', 'vv').rows), 4)

    def test_absent_s1b_never_retries_all(self):
        rows = [{'platform_number': code, 'instrumentMode': 'IW'} for code in 'AC']
        with patch.object(imagery.ee, 'ImageCollection', return_value=Collection(rows)) as collection, \
                patch.object(imagery.ee, 'Filter') as filters:
            filters.eq.side_effect = lambda key, val: ('eq', key, val)
            filters.listContains.side_effect = lambda key, val: ('contains', key, val)
            self.assertIsNone(imagery.best_s1_scene_for_roi('roi', 'start', 'end', platform='B'))
            collection.assert_called_once()

    def test_s2_empty_selected_platform_is_not_broadened_by_any_composite_path(self):
        rows = [{'SPACECRAFT_NAME': 'Sentinel-2' + code} for code in 'AC']
        with patch.object(imagery.ee, 'ImageCollection', return_value=Collection(rows)) as collection, \
                patch.object(imagery.ee, 'Filter') as filters:
            filters.eq.side_effect = lambda key, val: ('eq', key, val)
            filters.lt.side_effect = lambda key, val: ('lt', key, val)
            for method in ('mosaic', 'median', 'medoid', 'base-score', 'raw'):
                self.assertIsNone(imagery.best_scene_for_roi('sentinel2', 'roi', 'start', 'end',
                                                           composite=True, composite_method=method, platform='B'))
                self.assertIsNone(imagery.composite_plan('sentinel2', 'roi', 'start', 'end', method, platform='B'))
            self.assertIsNone(imagery._point_optical_scene('sentinel2', 'roi', 'start', 'end', 60, True, platform='B'))
            self.assertEqual(collection.call_count, 11)

    def test_sar_fill_cannot_use_other_platform_with_same_orbit(self):
        orbit = dict(orbitProperties_pass='ASCENDING', relativeOrbitNumber_start=128,
                     instrumentMode='IW', **{'system:time_start': 1779235200000})
        rows = [{**orbit, 'platform_number': code, 'system:index': 'fill' + code} for code in 'AC']
        raw = MagicMock()
        raw.toDictionary.return_value.getInfo.return_value = {**orbit, 'platform_number': 'C'}
        scene = imagery.SceneInfo('s1gee', imagery.S1_COLLECTION, 'primary', '2026-05-20', -1, MagicMock())
        with patch.object(s1_mosaic.ee, 'Image', return_value=raw), \
                patch.object(s1_mosaic.ee, 'Reducer'), \
                patch.object(s1_mosaic.ee, 'ImageCollection', return_value=Collection(rows)), \
                patch.object(s1_mosaic, 'coverage_pct', side_effect=[60, 100]), \
                patch.object(s1_mosaic.ee, 'Filter') as filters:
            filters.eq.side_effect = lambda key, val: ('eq', key, val)
            filters.neq.side_effect = lambda key, val: ('neq', key, val)
            filters.listContains.side_effect = lambda key, val: ('contains', key, val)
            result = s1_mosaic.prepare_s1(scene, 'roi', '2026-05-01', '2026-06-01', 'vvvh', True, platform='C')
        self.assertEqual([row['scene_id'] for row in result.scenes], ['primary', 'fillC'])
        self.assertTrue(all(row['platform'] == 'C' for row in result.scenes))

    def test_sar_selected_platform_rejects_wrong_or_missing_primary_before_pixel_work(self):
        scene = imagery.SceneInfo('s1gee', imagery.S1_COLLECTION, 'primary', '2026-05-20', -1, MagicMock())
        for metadata in ({'platform_number': 'A'}, {}):
            raw = MagicMock()
            raw.toDictionary.return_value.getInfo.return_value = metadata
            with patch.object(s1_mosaic.ee, 'Image', return_value=raw), \
                    patch.object(s1_mosaic, 'coverage_pct') as coverage:
                with self.assertRaisesRegex(ValueError, 'platform'):
                    s1_mosaic.prepare_s1(scene, 'roi', 'start', 'end', 'vvvh', platform='C')
                coverage.assert_not_called()


class PlatformAPITests(unittest.TestCase):
    def body(self, **changes):
        return dict(selection_mode='rectangle', center_lon=107, center_lat=10,
                    width_m=200, height_m=100, start_date='2026-01-01', end_date='2026-01-31', **changes)

    def test_invalid_post_and_get_rejected_before_earth_engine(self):
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee') as ensure, \
                patch.object(main, '_resolve_selection', return_value=('roi', Bbox(106, 10, 107, 11), None)), \
                patch.object(main, 'best_s1_scene', return_value=None):
            for platform in ('', 'ALL', 'E'):
                self.assertEqual(client.post('/api/s1gee/scene', json=self.body(platform=platform)).status_code, 422)
                self.assertEqual(client.get('/api/s1gee/download', params=self.body(platform=platform)).status_code, 422)
            for dataset, platform in [('sentinel2', 'D'), ('sentinel2', ''), ('landsat', 'A')]:
                self.assertEqual(client.post('/api/scene', json=self.body(dataset=dataset, platform=platform)).status_code, 422)
                self.assertEqual(client.get('/api/download/' + dataset, params=self.body(platform=platform)).status_code, 422)
            ensure.assert_not_called()

    def test_scene_response_and_download_url_preserve_platform(self):
        scene = imagery.SceneInfo('s1gee', imagery.S1_COLLECTION, 'primary', '2026-01-20', -1, MagicMock())
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), \
                patch.object(main, 'preview_rgb', return_value=None), \
                patch.object(main, 'best_s1_scene', return_value=scene) as s1, \
                patch.object(s1_mosaic, 'prepare_s1', return_value=scene) as prepare, \
                patch.object(main, 'best_scene', return_value=scene) as s2:
            for route, helper in [('/api/s1gee/scene', s1), ('/api/scene', s2)]:
                response = client.post(route, json=self.body(platform='C', merge_scenes=True))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['platform'], 'C')
                self.assertIn('platform=C', response.json()['download_url'])
                self.assertEqual(helper.call_args.kwargs['platform'], 'C')
            self.assertEqual(prepare.call_args.kwargs['platform'], 'C')

    def test_download_forwards_platform_to_search_and_fill(self):
        scene = imagery.SceneInfo('s1gee', imagery.S1_COLLECTION, 'primary', '2026-01-20', -1, MagicMock())
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), \
                patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), \
                patch.object(main, 'download_geotiff', return_value=[]), patch.object(main, 'merge_band_tiffs'), \
                patch.object(s1_mosaic, 'write_s1_export_tags'), \
                patch.object(main, 'best_s1_scene', return_value=scene) as s1, \
                patch.object(s1_mosaic, 'prepare_s1', return_value=scene) as prepare, \
                patch.object(main, 'best_scene', return_value=scene) as s2:
            main.s1gee_download('2026-01-01', '2026-01-31', lon=107, lat=10, platform='C', merge_scenes=True)
            self.assertEqual(s1.call_args.kwargs['platform'], 'C')
            self.assertEqual(prepare.call_args.kwargs['platform'], 'C')
            main.download('sentinel2', '2026-01-01', '2026-01-31', lon=107, lat=10, platform='C', merge_scenes=True)
            self.assertEqual(s2.call_args.kwargs['platform'], 'C')

    def test_empty_selected_platform_response_names_the_requested_satellite(self):
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', return_value=('roi', Bbox(106, 10, 107, 11), None)), \
                patch.object(main, 'best_s1_scene', return_value=None) as s1, \
                patch.object(main, 'best_scene', return_value=None) as s2:
            for route, name, helper in [('/api/s1gee/scene', 'Sentinel-1B', s1),
                                        ('/api/scene', 'Sentinel-2B', s2)]:
                response = client.post(route, json=self.body(platform='B'))
                self.assertEqual(response.status_code, 404)
                self.assertIn(name, response.json()['detail'])
                helper.assert_called_once()

    def test_admin_search_plan_and_download_keep_platform(self):
        region = MagicMock(spec=main.Province)
        region.id, region.name, region.geometry = '79', 'Test', {'type': 'Polygon', 'coordinates': []}
        scene = imagery.SceneInfo('sentinel2', imagery.S2_COLLECTION, 'primary', '2026-01-20', 0, MagicMock())
        body = dict(selection_mode='province', province_id='79', start_date='2026-01-01', end_date='2026-01-31', platform='C')
        with tempfile.TemporaryDirectory() as tmp, TestClient(main.app) as client, \
                patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), patch.object(main, '_ensure_ee'), \
                patch.object(main, '_resolve_selection', return_value=('roi', Bbox(106, 10, 107, 11), region)), \
                patch.object(main, 'preview_rgb', return_value=None), \
                patch.object(main, 'best_scene_for_roi', return_value=scene) as optical, \
                patch.object(main, 'best_s1_scene_for_roi', return_value=scene) as sar, \
                patch.object(main, 'composite_plan', return_value={'scene_count': 1}) as plan, \
                patch.object(s1_mosaic, 'prepare_s1', return_value=scene), \
                patch.object(s1_mosaic, 'write_s1_export_tags'), \
                patch.object(main, 'download_geotiff', return_value=[]), patch.object(main, 'merge_band_tiffs'):
            for route, helper in [('/api/scene', optical), ('/api/s1gee/scene', sar)]:
                response = client.post(route, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('platform=C', response.json()['download_url'])
                self.assertEqual(helper.call_args.kwargs['platform'], 'C')
            self.assertEqual(client.post('/api/composite-plan', json=body).status_code, 200)
            self.assertEqual(plan.call_args.kwargs['platform'], 'C')
            main.download('sentinel2', **body)
            self.assertEqual(optical.call_args.kwargs['platform'], 'C')
            main.s1gee_download(**body, merge_scenes=True)
            self.assertEqual(sar.call_args.kwargs['platform'], 'C')


if __name__ == '__main__':
    unittest.main()
