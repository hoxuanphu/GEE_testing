import json
from dataclasses import replace
from unittest import mock
from urllib.parse import urlsplit, parse_qsl, urlencode

import rasterio

from tests.test_color_balance_api import BalanceAPITests, BODY
from app import main
from app.admin_boundaries import Province
from app.commune_boundaries import Commune
from app.vector_api import VectorArea


class MosaicOptionsAPITests(BalanceAPITests):
    """Reuse real TIFF fixture and selection mocks; inherited balance tests also run."""
    def setUp(self):
        super().setUp()
        self.stack.enter_context(mock.patch.dict('os.environ', {'GEE_DOWNLOAD_SNAPSHOTS': '0'}))
        self.body = {**BODY, 'color_balance': False, 'composite_method': 'mosaic',
                     'mosaic_mask_clouds': False, 'scene_grouping': 'near_date', 'max_scene_span_days': 3}

    def test_options_freeze_preview_download_and_tiff(self):
        report = dict(mode='near_date', max_scene_span_days=3, span_days=.5, same_pass=False,
                      data_coverage_pct=100., output_coverage_pct=100.)
        self.scene = replace(self.scene, color_balance=None, scene_grouping=report, mosaic_mask_clouds=False)
        self.search.return_value = self.admin.return_value = self.scene
        result = self.client.post('/api/scene', json=self.body)
        self.assertEqual(result.status_code, 200, result.text)
        data = result.json()
        self.assertFalse(data['mosaic_mask_clouds'])
        self.assertEqual(data['scene_grouping']['mode'], 'near_date')
        self.assertEqual(data['composite_method'], 'mosaic')
        self.assertEqual(self.search.call_args.kwargs['composite_method'], 'mosaic')
        self.assertIn('mosaic_mask_clouds=false', data['download_url'])
        self.assertIn('mosaic_snapshot=', data['download_url'])
        self.search.side_effect = AssertionError('Frozen graph only')
        response = self.client.get(data['download_url'])
        self.assertEqual(response.status_code, 200, response.text if response.status_code != 200 else '')
        with rasterio.MemoryFile(response.content) as memory, memory.open() as src:
            self.assertEqual(src.tags()['MOSAIC_MASK_CLOUDS'], 'false')
            self.assertEqual(json.loads(src.tags()['SCENE_GROUPING'])['mode'], 'near_date')
        changed = self.client.get(data['download_url'].replace('max_scene_span_days=3', 'max_scene_span_days=7'))
        self.assertEqual(changed.status_code, 409, changed.text)
        changed = self.client.get(data['download_url'].replace('mosaic_mask_clouds=false', 'mosaic_mask_clouds=true'))
        self.assertEqual(changed.status_code, 409, changed.text)

    def test_policy_defaults_and_reject_incompatible_before_provider(self):
        request = main.SceneRequest(**{k: v for k, v in BODY.items() if k != 'color_balance'})
        self.assertEqual(request.scene_grouping, 'any')
        self.assertEqual(request.max_scene_span_days, 3)
        self.assertTrue(request.mosaic_mask_clouds)
        for values in [dict(merge_scenes=False), dict(dataset='nicfi'), dict(max_scene_span_days=2),
                       dict(composite_method='median'), dict(composite_method='raw')]:
            with self.subTest(values=values), mock.patch.object(main, '_ensure_ee') as ensure:
                response = self.client.post('/api/scene', json={**self.body, **values})
                self.assertEqual(response.status_code, 422, response.text)
                ensure.assert_not_called()

    def test_default_stable_snapshot_cannot_be_reused_as_unmerged_point(self):
        result = self.client.post('/api/scene', json={**self.body, 'scene_grouping': 'any', 'mosaic_mask_clouds': True})
        self.assertEqual(result.status_code, 200, result.text)
        changed = self.client.get(result.json()['download_url'].replace('merge_scenes=true', 'merge_scenes=false'))
        self.assertIn(changed.status_code, (409, 422), changed.text)

    def test_grouped_color_balance_exposes_only_policy_bound_snapshot(self):
        body = {**BODY, 'scene_grouping':'near_date', 'max_scene_span_days':3}
        result = self.client.post('/api/scene', json=body)
        self.assertEqual(result.status_code, 200, result.text)
        url = result.json()['download_url']
        self.assertIn('color_balance=true', url)
        self.assertIn('mosaic_snapshot=', url)
        self.assertNotIn('color_snapshot=', url)
        changed = self.client.get(url.replace('max_scene_span_days=3', 'max_scene_span_days=7'))
        self.assertEqual(changed.status_code, 409)

    def test_nonoptical_composite_keeps_legacy_links_without_new_mosaic_options(self):
        self.resolve.return_value = ('roi', self.bbox, Province('VN01','Province',self.geometry,self.bbox))
        body = {**BODY, 'dataset':'nicfi', 'selection_mode':'province', 'province_id':'VN01',
                'merge_scenes':False, 'color_balance':False, 'composite_method':'mosaic'}
        response = self.client.post('/api/scene', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('mosaic_snapshot=', response.json()['download_url'])
        self.assertNotIn('mosaic_mask_clouds=', response.json()['download_url'])

    def test_new_options_follow_actual_links_and_direct_download_in_all_roi_modes(self):
        self.scene = replace(self.scene, scene_grouping={'mode':'near_date'}, mosaic_mask_clouds=False)
        self.search.return_value = self.admin.return_value = self.scene
        selections = [
            ({'selection_mode':'point'}, None),
            ({'selection_mode':'rectangle','center_lon':107,'center_lat':10,'width_m':2000,'height_m':1000}, None),
            ({'selection_mode':'province','province_id':'VN01'}, Province('VN01','Province',self.geometry,self.bbox)),
            ({'selection_mode':'commune','commune_id':'001'}, Commune('001','Commune','VN01','Province',self.geometry,self.bbox)),
            ({'selection_mode':'vector','vector_roi_id':'00000000-0000-0000-0000-000000000001'},
             VectorArea('00000000-0000-0000-0000-000000000001','Vector',self.geometry,self.bbox))]
        for fields, area in selections:
            with self.subTest(mode=fields['selection_mode']):
                body = {**self.body, **fields}
                if fields['selection_mode'] in ('rectangle','vector'):
                    body.pop('lon'); body.pop('lat')
                if fields['selection_mode'] == 'vector': body.pop('radius_m')
                self.resolve.return_value = ('roi', self.bbox, area)
                response = self.client.post('/api/scene', json=body)
                self.assertEqual(response.status_code, 200, response.text)
                url = response.json()['download_url']
                downloaded = self.client.get(url)
                self.assertEqual(downloaded.status_code, 200, str(downloaded.status_code))
                direct = urlsplit(url).path + '?' + urlencode([(key,value) for key,value in parse_qsl(urlsplit(url).query)
                                                              if key != 'mosaic_snapshot'])
                downloaded = self.client.get(direct)
                self.assertEqual(downloaded.status_code, 200, str(downloaded.status_code))
                search = self.admin if area else self.search
                self.assertEqual(search.call_args.kwargs['scene_grouping'], 'near_date')
                self.assertFalse(search.call_args.kwargs['mosaic_mask_clouds'])
