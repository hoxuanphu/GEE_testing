"""Readiness, immutable quality, and group preview contract regressions."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.frame_api import register_frame_routes
from app.frame_jobs import FrameService
from app.frame_store import FrameStore
from app.reference_frame import reference_roi_identity


class GroupPreflightTests(unittest.TestCase):
    def test_legacy_partial_export_without_explicit_threshold_keeps_zero(self):
        from app.frame_models import effective_min_coverage
        self.assertEqual(effective_min_coverage({'require_full_coverage':False}),0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FrameStore(Path(self.temp.name))
        self.service = FrameService(self.store)
        self.addCleanup(self.service.shutdown)
        frame = {'crs':'EPSG:32648', 'transform':[10,0,400000,0,-10,2400000], 'width':10,'height':2}
        self.params = {'dataset':'sentinel2', 'bands':['B4'], 'reference':frame,
                       'output_grid':frame, 'grid_mode':'match_extent', 'merge_scenes':True,
                       'require_full_coverage':False, 'min_coverage_pct':95,
                       **reference_roi_identity(frame)}
        self.rows = [{'scene_token':str(uuid.uuid4()), 'asset_id':'COPERNICUS/S2_SR_HARMONIZED/'+n,
                      'platform':'Sentinel-2A', 'date':'2026-10-01', 'timestamp':'2026-10-01T00:00:00Z',
                      'eligible':True,'band_coverage_pct':60} for n in ['first','second']]
        self.search = self.store.save_search(self.params,self.rows)
        self.quality = {'measurement_version':1, 'method':'output_grid_mask', 'estimated':True,
                        'band_coverage_pct':60., 'valid_pixel_count':12, 'total_pixels':20,
                        'ordered_asset_ids':[s['asset_id'] for s in self.rows],
                        'roi_fingerprint':self.params['roi_fingerprint'],
                        'merge_policy':'primary_first_fill_nodata'}
        app=FastAPI();register_frame_routes(app,self.service)
        self.client=TestClient(app);self.addCleanup(self.client.close)

    def preflight(self, coverage=60):
        quality={**self.quality, 'band_coverage_pct':coverage,
                 'valid_pixel_count':round(20*coverage/100)}
        with mock.patch('app.frame_validity.measure_frame_selection', return_value=quality):
            return self.client.post('/api/matched-frame/preflight',json={
                'search_id':self.search['search_id'], 'scene_tokens':[s['scene_token'] for s in self.rows]})

    def test_insufficient_group_explained_before_job_creation(self):
        response=self.preflight()
        self.assertEqual(response.status_code,200,response.text)
        data=response.json()
        self.assertIs(data.get('can_start'),False)
        self.assertEqual(data.get('preflight_version'),2)
        self.assertEqual(data.get('blocking_reason'),'insufficient_selection_coverage')
        self.assertEqual(data['selection_quality']['band_coverage_pct'],60)
        with mock.patch.object(self.store,'create_job') as create:
            response=self.client.post('/api/matched-frame/jobs',json={
                'preflight_id':data['preflight_id'],'digest':data['digest'],'acknowledged':True})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['detail']['code'],'insufficient_selection_coverage')
        create.assert_not_called()

    def test_complementary_sources_ready_and_quality_tampering_rejected(self):
        data=self.preflight(100).json()
        self.assertIs(data.get('can_start'),True)
        tampered=copy.deepcopy(data)
        tampered['selection_quality']['band_coverage_pct']=50
        tampered['selection_quality']['valid_pixel_count']=10
        saved=self.store.save_preflight(tampered)
        with mock.patch.object(self.store,'create_job') as create:
            response=self.client.post('/api/matched-frame/jobs',json={
                'preflight_id':saved['preflight_id'],'digest':saved['digest'],'acknowledged':True})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['detail']['code'],'stale_preflight')
        create.assert_not_called()

    def test_group_preview_allowed_even_when_job_is_blocked_and_cached(self):
        data=self.preflight().json()
        self.assertIn('preview_url',data)
        with mock.patch('app.frame_search.make_frame_selection_preview',return_value=b'\x89PNG\r\n\x1a\ntest') as preview:
            first=self.client.get(data['preview_url'])
            second=self.client.get(data['preview_url'])
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.content,second.content)
        preview.assert_called_once()
        self.assertEqual([s['asset_id'] for s in preview.call_args.args[0]],self.quality['ordered_asset_ids'])

    def test_worker_cannot_bypass_blocked_preflight(self):
        data=self.preflight().json()
        job=self.store.create_job(data)
        with mock.patch('app.frame_export.export_frame_scene') as export:
            self.service.run_job(job['job_id'])
        export.assert_not_called()
        job=self.store.get_job(job['job_id'])
        self.assertEqual(job['status'],'failed')
        self.assertEqual(job.get('error_code'),'insufficient_selection_coverage')

    def test_selected_platform_cannot_include_other_platform_sources(self):
        snapshot=self.store.save_search({**self.params,'platform':'C'},self.rows)
        with mock.patch('app.frame_validity.measure_frame_selection') as measure:
            response=self.client.post('/api/matched-frame/preflight',json={
                'search_id':snapshot['search_id'],'scene_tokens':[s['scene_token'] for s in self.rows]})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(response.json()['detail']['code'],'incompatible_platforms')
        measure.assert_not_called()

    def test_s2_platform_d_rejected_before_search(self):
        reference=self.store.save_reference(self.params['reference'])
        with mock.patch('app.frame_search.search_frame_scenes') as search:
            response=self.client.post('/api/matched-frame/searches',json={
                'reference_id':reference['reference_id'],'dataset':'sentinel2','platform':'D',
                'start_date':'2026-10-01','end_date':'2026-10-25'})
        self.assertEqual(response.status_code,422,response.text)
        search.assert_not_called()

    def test_worker_rejects_job_source_reordering_before_staging(self):
        data = self.preflight(100).json()
        job = self.store.create_job(data)
        job['scenes'][0]['sources'].reverse()
        self.store.update_job(job['job_id'], scenes=job['scenes'])
        with mock.patch('app.frame_export.export_frame_scene') as export:
            self.service.run_job(job['job_id'])
        export.assert_not_called()
        result = self.store.get_job(job['job_id'])
        self.assertEqual(result.get('error_code'), 'stale_preflight')
        self.assertFalse(self.store.job_dir(job['job_id']).exists())

    def test_worker_rejects_replaced_preflight_after_job_confirmation(self):
        from app.frame_jobs import _digest, preflight_digest_payload
        data = self.preflight(100).json()
        job = self.store.create_job(data)
        changed = self.store.get_preflight(data['preflight_id'])
        changed['parameters']['selection_context'] = 'reference_roi'
        changed['digest'] = _digest(preflight_digest_payload(changed))
        with mock.patch.object(self.store, 'get_preflight', return_value=changed), \
                mock.patch('app.frame_export.export_frame_scene') as export:
            self.service.run_job(job['job_id'])
        export.assert_not_called()
        self.assertEqual(self.store.get_job(job['job_id']).get('error_code'), 'stale_preflight')

    def test_quality_contract_requires_explicit_versions_and_strict_flags(self):
        from app.frame_models import FrameSelectionQuality
        for field in ('measurement_version', 'method', 'estimated', 'merge_policy'):
            quality = {key: value for key, value in self.quality.items() if key != field}
            with self.subTest(missing=field), self.assertRaises(ValueError):
                FrameSelectionQuality.model_validate(quality)
        for change in ({'measurement_version': True}, {'estimated': 1},
                       {'band_coverage_pct': True, 'valid_pixel_count': 1, 'total_pixels': 100}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                FrameSelectionQuality.model_validate({**self.quality, **change})


if __name__=='__main__':unittest.main()
