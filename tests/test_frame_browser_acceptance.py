"""The browser harness cannot publish completion from partial client evidence."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest import mock
from uuid import uuid4
import zipfile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.frame_store import FrameStore


class BrowserAcceptanceTests(unittest.TestCase):
    def setUp(self):
        from validate_matched_frame_browser import attach_browser_acceptance
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.run = Path(self.temporary.name)
        self.fixture = self.run / 'reference.tif'
        self.fixture.write_bytes(b'fixture')
        self.store = FrameStore(self.run / 'store')
        self.app = FastAPI()
        attach_browser_acceptance(self.app, self.run, self.fixture, {'code_sha256': {}}, self.store)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_pending_browser_is_not_complete_and_assets_are_served_without_gee(self):
        status = self.client.get('/__browser/status')
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()['complete'])
        self.assertEqual(self.client.get('/__browser/reference.tif').content, b'fixture')
        self.assertEqual(self.client.get('/__browser').status_code, 200)
        self.assertEqual(self.client.get('/__browser/harness.js').status_code, 200)

    def test_cross_origin_cannot_submit_browser_evidence(self):
        result = self.client.post('/__browser/result', json={'complete': False},
                                  headers={'Origin': 'https://example.invalid'})
        self.assertEqual(result.status_code, 403)
        self.assertFalse(self.client.get('/__browser/status').json()['complete'])

    def test_missing_origin_cannot_submit_browser_evidence(self):
        self.assertEqual(self.client.post('/__browser/result', json={'complete': False}).status_code, 403)

    def test_failed_browser_run_is_saved_without_marking_it_complete(self):
        result = self.client.post('/__browser/result', json={'complete': False, 'error': 'UI test failed'},
                                  headers={'Origin': 'http://testserver'})
        self.assertEqual(result.status_code, 200)
        status = self.client.get('/__browser/status').json()
        self.assertFalse(status['complete'])
        self.assertEqual(status['browser']['error'], 'UI test failed')

    def test_client_success_without_all_required_checks_is_rejected(self):
        result = self.client.post('/__browser/result', json={'complete': True, 'checks': {}},
                                  headers={'Origin': 'http://testserver'})
        self.assertEqual(result.status_code, 422)
        self.assertFalse(self.client.get('/__browser/status').json()['complete'])

    def test_oversize_evidence_is_rejected_before_persistence(self):
        result = self.client.post('/__browser/result', content=b'x' * (1024 * 1024 + 1),
                                  headers={'Origin': 'http://testserver', 'Content-Type': 'application/json'})
        self.assertEqual(result.status_code, 413)
        self.assertFalse(self.client.get('/__browser/status').json()['complete'])

    def completed_job_evidence(self):
        import numpy as np
        from app.frame_jobs import FrameService
        from app.frame_export import export_frame_scene, build_frame_manifest
        from app.reference_frame import reference_roi_identity
        from validate_matched_frame_browser import REQUIRED_CHECKS
        frame = dict(crs='EPSG:32648', transform=[10, 0, 400000, 0, -10, 2400000], width=2, height=2)
        reference = self.store.save_reference(frame)
        params = dict(reference_id=reference['reference_id'], reference=frame, output_grid=frame,
                      dataset='s1gee', platform='D', bands=['VV'], grid_mode='match_grid',
                      selection_context='reference_roi', require_full_coverage=False, min_coverage_pct=99.9)
        params.update(reference_roi_identity(frame))
        source = dict(scene_token=str(uuid4()), asset_id='COPERNICUS/S1_GRD/test', platform='D',
                      date='2026-10-23', timestamp='2026-10-23T11:13:48Z', eligible=True, band_coverage_pct=100)
        search = self.store.save_search(params, [source])
        service = FrameService(self.store)
        self.addCleanup(service.shutdown)
        preflight = service.preflight(search['search_id'], [source['scene_token']])
        job = self.store.create_job(preflight)
        folder = self.store.job_dir(job['job_id'])
        folder.mkdir(parents=True)
        token = str(uuid4())
        path = folder / (token + '.tif')
        # The TIFF/ZIP are real; the deterministic tile fixture has no provider.
        with mock.patch('app.frame_export._RPC'), \
             mock.patch('app.frame_export.load_frame_image',return_value=object()), \
             mock.patch('app.frame_export.fetch_grid_tile', return_value=np.arange(4, dtype='float32').reshape(1, 2, 2)):
            report = export_frame_scene(source, params, path)
        record = {**job['scenes'][0], 'status': 'succeeded', 'file_token': token, 'filename': 'verified.tif', 'report': report}
        self.store.update_job(job['job_id'], status='completed', scenes=[record])
        archive_path = folder / 'result.zip'
        with zipfile.ZipFile(archive_path, 'w') as archive:
            archive.write(path, 'verified.tif')
            archive.writestr('manifest.json', json.dumps(build_frame_manifest(params, [report])))
        return dict(complete=True, checks={name: True for name in REQUIRED_CHECKS}, upload_count=1,
                    reference_id=reference['reference_id'], search_id=search['search_id'],
                    preflight_id=preflight['preflight_id'], job_id=job['job_id'],
                    download_sha256={'tiff': hashlib.sha256(path.read_bytes()).hexdigest(),
                                     'zip': hashlib.sha256(archive_path.read_bytes()).hexdigest()})

    def test_browser_success_is_verified_against_real_tiff_and_zip(self):
        payload = self.completed_job_evidence()
        result = self.client.post('/__browser/result', json=payload, headers={'Origin': 'http://testserver'})
        self.assertEqual(result.status_code, 200, result.text)
        saved = self.client.get('/__browser/status').json()
        self.assertTrue(saved['complete'])
        self.assertEqual(saved['server_verification']['grid_checks']['common_band_coverage_pct'], 100)

    def test_offline_fixture_never_initializes_a_live_provider(self):
        with mock.patch('app.frame_search.init_ee',side_effect=AssertionError('Offline fixture reached GEE')):
            self.completed_job_evidence()

    def test_wrong_browser_download_checksum_cannot_mark_complete(self):
        payload = self.completed_job_evidence()
        payload['download_sha256']['tiff'] = '0' * 64
        result = self.client.post('/__browser/result', json=payload, headers={'Origin': 'http://testserver'})
        self.assertEqual(result.status_code, 422, result.text)
        self.assertEqual(result.json()['error'], 'browser_download_mismatch')
        self.assertFalse(self.client.get('/__browser/status').json()['complete'])
