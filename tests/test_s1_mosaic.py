"""SAR fill mosaics keep the primary pixels and orbit geometry."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from app import main, s1_mosaic
from app.geo import Bbox
from app.imagery import SceneInfo


def scene():
    return SceneInfo('s1gee', 'COPERNICUS/S1_GRD', 'primary', '2026-05-20', -1, MagicMock())


class MosaicTests(unittest.TestCase):
    def setUp(self):
        reducer = patch.object(s1_mosaic.ee, 'Reducer')
        reducer.start()
        self.addCleanup(reducer.stop)

    def test_disabled_or_complete_does_not_search_candidates(self):
        for enabled, coverage in [(False, 45), (True, 100)]:
            with self.subTest(enabled=enabled), patch.object(s1_mosaic, 'coverage_pct', return_value=coverage), patch.object(s1_mosaic.ee, 'ImageCollection') as collection:
                result = s1_mosaic.prepare_s1(scene(), object(), '2026-05-01', '2026-06-01', 'vvvh', enabled)
                self.assertEqual(result.scene_count, 1)
                self.assertEqual(result.coverage_pct, coverage)
                collection.assert_not_called()

    def test_fill_keeps_primary_and_filters_orbit_and_all_bands(self):
        base = scene()
        raw = MagicMock()
        raw.toDictionary.return_value.getInfo.return_value = {
            'orbitProperties_pass': 'DESCENDING', 'relativeOrbitNumber_start': 18,
            'system:time_start': 1779235200000,
        }
        candidate = raw.select.return_value.clamp.return_value.float.return_value.updateMask.return_value
        collection = MagicMock()
        collection.filterBounds.return_value = collection
        collection.filterDate.return_value = collection
        collection.filter.return_value = collection
        collection.sort.return_value = collection
        collection.map.return_value = collection
        collection.limit.return_value = collection
        metadata = {'system:index': ['fill1', 'fill2'],
                    'system:time_start': [1779235200000, 1779148800000],
                    'platform_number': ['A', 'A']}
        collection.aggregate_array.side_effect = lambda key: MagicMock(getInfo=MagicMock(return_value=metadata[key]))
        with patch.object(s1_mosaic, 'coverage_pct', side_effect=[60, 60, 95]), patch.object(s1_mosaic.ee, 'Image', return_value=raw), patch.object(s1_mosaic.ee, 'ImageCollection', return_value=collection), patch.object(s1_mosaic.ee, 'Filter') as filters:
            result = s1_mosaic.prepare_s1(base, 'roi', '2026-05-01', '2026-06-01', 'vvvh', True)
        self.assertEqual(result.scene_count, 2)
        self.assertEqual(result.coverage_pct, 95)
        self.assertEqual([row['scene_id'] for row in result.scenes], ['primary', 'fill2'])
        self.assertTrue(result.scene_id.startswith('s1gee_mosaic_'))
        filters.eq.assert_any_call('orbitProperties_pass', 'DESCENDING')
        filters.eq.assert_any_call('relativeOrbitNumber_start', 18)
        filters.listContains.assert_any_call('transmitterReceiverPolarisation', 'VV')
        filters.listContains.assert_any_call('transmitterReceiverPolarisation', 'VH')
        primary_image = base.ee_object.select.return_value.updateMask.return_value
        primary_image.unmask.assert_called_with(candidate, sameFootprint=False)
        collection.limit.assert_called_once_with(24)

    def test_missing_orbit_metadata_refuses_unsafe_merge(self):
        raw = MagicMock()
        raw.toDictionary.return_value.getInfo.return_value = {}
        with patch.object(s1_mosaic, 'coverage_pct', return_value=50), patch.object(s1_mosaic.ee, 'Image', return_value=raw):
            with self.assertRaisesRegex(ValueError, 'quỹ đạo'):
                s1_mosaic.prepare_s1(scene(), 'roi', '2026-05-01', '2026-06-01', 'vvvh', True)

    def test_coverage_counts_uncovered_area_as_zero(self):
        image = MagicMock()
        valid = image.mask.return_value.reduce.return_value.rename.return_value
        valid.unmask.return_value.gt.return_value.reduceRegion.return_value.get.return_value.getInfo.return_value = .45
        with patch.object(s1_mosaic.ee, 'Reducer'):
            self.assertEqual(s1_mosaic.coverage_pct(image, 'roi'), 45)
        valid.unmask.assert_called_once_with(0, sameFootprint=False)


class MosaicAPITests(unittest.TestCase):
    def test_search_exposes_each_orbit_and_pins_its_download_sources(self):
        from dataclasses import replace
        up = replace(scene(), scene_id='up', coverage_pct=99.9, scenes=({'scene_id': 'up'}, {'scene_id': 'fill'}))
        down = replace(scene(), scene_id='down', coverage_pct=76, scenes=({'scene_id': 'down'},))
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), \
             patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), \
             patch.object(main, 'best_s1_scene', return_value=up), \
             patch.object(s1_mosaic, 'search_s1_by_orbit', return_value={'ASCENDING': up, 'DESCENDING': down}), \
             patch.object(main, 'preview_rgb', return_value=None), patch.object(s1_mosaic, 'prepare_s1', return_value=up):
            response = client.post('/api/s1gee/scene', json=dict(lon=107, lat=11, start_date='2026-05-01',
                                   end_date='2026-05-31', group_by_orbit=True, merge_scenes=True))
        self.assertEqual(response.status_code, 200, response.text)
        results = response.json()['results']
        self.assertEqual([item['orbit_pass'] for item in results], ['ASCENDING', 'DESCENDING'])
        for item, expected in zip(results, (['up', 'fill'], ['down'])):
            query = parse_qs(urlsplit(item['download_url']).query)
            self.assertEqual(query['scene_ids'], expected)
            self.assertEqual(query['orbit_pass'], [item['orbit_pass']])

    def test_option_metadata_and_download_link_all_modes(self):
        from dataclasses import replace
        result = replace(scene(), scene_id='s1gee_mosaic_2026-05-01_2026-06-01',
                         scene_count=2, coverage_pct=92,
                         scenes=({'scene_id': 'primary', 'date': '2026-05-20'},
                                 {'scene_id': 'fill', 'date': '2026-05-19'}))
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), patch.object(main, 'best_s1_scene', return_value=scene()), patch.object(main, 'preview_rgb', return_value=None), patch.object(s1_mosaic, 'prepare_s1', return_value=result) as prepare:
            for mode in ('point', 'rectangle'):
                body = dict(start_date='2026-05-01', end_date='2026-05-31', merge_scenes=True, selection_mode=mode)
                body.update(dict(lon=107, lat=11) if mode == 'point' else dict(center_lon=107, center_lat=11, width_m=2000, height_m=1000))
                response = client.post('/api/s1gee/scene', json=body)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertTrue(data['composite'])
                self.assertEqual(data['coverage_pct'], 92)
                self.assertIsNotNone(data['warning'])
                self.assertIn('merge_scenes=true', data['download_url'])
                self.assertEqual(len(data['scenes']), 2)
                self.assertTrue(prepare.call_args.args[-1])

    def test_administrative_modes_pass_merge_and_preserve_region_links(self):
        from dataclasses import replace
        result = replace(scene(), coverage_pct=88, scene_count=1)
        with TestClient(main.app) as client, patch.object(main, '_ensure_ee'), patch.object(main, 'best_s1_scene_for_roi', return_value=scene()), patch.object(main, 'preview_rgb', return_value=None), patch.object(s1_mosaic, 'prepare_s1', return_value=result) as prepare:
            for mode in ('province', 'commune'):
                region = MagicMock(spec=main.Commune if mode == 'commune' else main.Province)
                region.id = '79'; region.name = 'Test'; region.province_name = 'Test'
                region.geometry = {'type': 'Polygon', 'coordinates': []}
                with patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), region)):
                    response = client.post('/api/s1gee/scene', json=dict(selection_mode=mode, **{mode + '_id': '79'}, start_date='2026-05-01', end_date='2026-05-31', merge_scenes=True))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn('merge_scenes=true', response.json()['download_url'])
                self.assertIn(f'{mode}_id=79', response.json()['download_url'])
                self.assertFalse(response.json()['composite'])
                self.assertIsNotNone(response.json()['warning'])
                self.assertTrue(prepare.call_args.args[-1])

    def test_download_uses_mosaic_and_region_filename(self):
        from dataclasses import replace
        result = replace(scene(), scene_count=2, coverage_pct=92)
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, '_DOWNLOADS_DIR', Path(tmp)), patch.object(main, '_ensure_ee'), patch.object(main, '_resolve_selection', return_value=(MagicMock(), Bbox(106, 10, 107, 11), None)), patch.object(main, 'best_s1_scene', return_value=scene()), patch.object(s1_mosaic, 'prepare_s1', return_value=result), patch.object(main, 'download_geotiff', return_value=[]) as download, patch.object(main, 'merge_band_tiffs'):
            response = main.s1gee_download('2026-05-01', '2026-05-31', lon=107, lat=11, merge_scenes=True)
            self.assertIn('mosaic_2026-05-01_2026-05-31_n2_vvvh_pt', response.filename)
            self.assertIs(download.call_args.args[0], result.ee_object)
