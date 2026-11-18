"""Immutable preflight, persistence and worker lifecycle regressions."""
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest import mock
import uuid
import zipfile

import numpy as np
import rasterio

from app.frame_jobs import FrameService
from app.frame_models import FrameError
from app.frame_store import FrameStore, TTL_SECONDS
from app.raster_grid import build_reference_grid
from app.reference_frame import reference_roi_identity


class FrameJobTests(unittest.TestCase):
    def setUp(self):
        directory = Path('.frame-job-test-' + uuid.uuid4().hex)
        directory.mkdir()
        self.addCleanup(shutil.rmtree, directory)
        self.store = FrameStore(directory)
        self.service = FrameService(self.store)
        self.addCleanup(self.service.shutdown)
        def quality(scenes, parameters, **kwargs):
            total = parameters['output_grid']['width'] * parameters['output_grid']['height']
            return {'measurement_version':1, 'method':'output_grid_mask', 'estimated':True,
                    'band_coverage_pct':100., 'valid_pixel_count':total, 'total_pixels':total,
                    'ordered_asset_ids':[s['asset_id'] for s in scenes],
                    'roi_fingerprint':parameters['roi_fingerprint'], 'merge_policy':'primary_first_fill_nodata'}
        masks = mock.patch('app.frame_validity.measure_frame_selection', side_effect=quality)
        masks.start(); self.addCleanup(masks.stop)
        self.disk = mock.patch('app.frame_jobs.shutil.disk_usage', return_value=type('Disk', (), {'free': 10**12})())
        self.disk.start()
        self.addCleanup(self.disk.stop)
        frame = {'crs': 'EPSG:32648', 'transform': [10, 0, 398712, 0, -10, 2406162], 'width': 5, 'height': 5}
        params = dict(dataset='sentinel2', bands=['B2'], output_grid=build_reference_grid(frame),
                      reference=frame, grid_mode='match_extent', require_full_coverage=True)
        params.update(reference_roi_identity(frame))
        self.rows = [dict(scene_token=str(uuid.uuid4()), asset_id=f'COPERNICUS/S2_SR_HARMONIZED/SCENE{i}',
                         date='2026-03-28', timestamp='2026-03-28T03:42:00Z', eligible=True,
                         band_coverage_pct=100, roi_cloud_shadow_pct=0) for i in range(2)]
        self.search = self.store.save_search(params, self.rows)

    def preflight(self):
        return self.service.preflight(self.search['search_id'], [s['scene_token'] for s in self.rows])

    def test_stale_search_rejected_before_preflight_and_cached_preview(self):
        legacy = self.store.save_search({k: v for k, v in self.search['parameters'].items()
                                         if not k.startswith('roi_')}, self.rows)
        token = self.rows[0]['scene_token']
        directory = self.store.root / 'previews' / legacy['search_id']
        directory.mkdir(parents=True)
        (directory / f'{token}.png').write_bytes(b'\x89PNG\r\n\x1a\nlegacy')
        for operation in (lambda: self.service.preflight(legacy['search_id'], [token]),
                          lambda: self.service.preview(legacy['search_id'], token)):
            with self.subTest(operation=operation), mock.patch('app.frame_search.make_frame_preview') as preview:
                with self.assertRaises(FrameError) as caught:
                    operation()
                self.assertEqual((caught.exception.code, caught.exception.status), ('stale_search', 409))
                preview.assert_not_called()

    def test_stale_preflight_rejected_on_start_and_queued_worker(self):
        preflight = self.preflight()
        preflight['parameters'] = {k: v for k, v in preflight['parameters'].items() if not k.startswith('roi_')}
        from app.frame_jobs import _digest
        preflight['digest'] = _digest({k: preflight[k] for k in ('search_id', 'parameters', 'scenes')})
        stale = self.store.save_preflight(preflight)
        with mock.patch.object(self.store, 'create_job') as create:
            with self.assertRaises(FrameError) as caught:
                self.service.start_job(stale['preflight_id'], stale['digest'], True)
            self.assertEqual(caught.exception.code, 'stale_search')
            create.assert_not_called()

    def test_queued_worker_rejects_stale_roi_before_staging(self):
        preflight = self.preflight()
        preflight['parameters'] = {k: v for k, v in preflight['parameters'].items() if not k.startswith('roi_')}
        stale = self.store.save_preflight(preflight)
        job = self.store.create_job(stale)
        with mock.patch('app.frame_export.export_frame_scene') as export:
            self.service.run_job(job['job_id'])
        export.assert_not_called()
        result = self.store.get_job(job['job_id'])
        self.assertEqual((result['status'], result['error_code']), ('failed', 'stale_search'))
        self.assertFalse(self.store.job_dir(job['job_id']).exists())
        self.assertEqual(self.store.get_preflight(stale['preflight_id'])['parameters'], stale['parameters'])

    def test_preflight_rejects_unknown_zero_or_invalid_coverage_even_if_marked_eligible(self):
        params = {**self.search['parameters'], 'require_full_coverage': False, 'min_coverage_pct': 0}
        for coverage in (None, 0, -1, 101):
            with self.subTest(coverage=coverage):
                snapshot = self.store.save_search(params, [{**self.rows[0], 'band_coverage_pct': coverage}])
                with self.assertRaises(FrameError) as caught:
                    self.service.preflight(snapshot['search_id'], [self.rows[0]['scene_token']])
                self.assertEqual(caught.exception.code, 'invalid_selection')
        snapshot = self.store.save_search(params, [{**self.rows[0], 'band_coverage_pct': .01}])
        self.assertTrue(self.service.preflight(snapshot['search_id'], [self.rows[0]['scene_token']]))

    def test_new_search_records_server_roi_identity(self):
        from app.reference_frame import reference_footprint
        reference = self.store.save_reference(self.search['parameters']['reference'])
        from app.frame_models import FrameSearchRequest
        request = FrameSearchRequest(reference_id=reference['reference_id'], start_date='2026-03-01', end_date='2026-03-31')
        with mock.patch('app.frame_search.search_frame_scenes', return_value=self.rows):
            found = self.service.search(request)
        params = found['parameters']
        self.assertEqual(params.get('roi_version'), 2)
        self.assertEqual(params['roi_geojson'], reference_footprint(params['reference'])['footprint_geojson'])
        self.assertEqual(len(params['roi_fingerprint']), 64)
        self.assertEqual(self.service.preflight(found['search_id'], [self.rows[0]['scene_token']])['parameters'], params)

    def test_preflight_rejects_null_threshold_before_selection(self):
        params = {**self.search['parameters'], 'require_full_coverage': False, 'min_coverage_pct': None}
        snapshot = self.store.save_search(params, self.rows)
        with self.assertRaises(FrameError) as caught:
            self.service.preflight(snapshot['search_id'], [self.rows[0]['scene_token']])
        self.assertEqual(caught.exception.code, 'invalid_coverage')

    def test_selection_digest_and_quota(self):
        preflight = self.preflight()
        self.assertEqual(preflight['estimate']['uncompressed_bytes'], 5*5*4*2)
        with self.assertRaises(FrameError):
            self.service.start_job(preflight['preflight_id'], '0'*64, True)
        with self.assertRaises(FrameError):
            self.service.preflight(self.search['search_id'], [str(uuid.uuid4())])
        with self.assertRaises(FrameError):
            self.service.preflight(self.search['search_id'], [self.rows[0]['scene_token']]*2)
        with mock.patch('app.frame_jobs.shutil.disk_usage', return_value=type('Disk', (), {'free': 0})()):
            with self.assertRaises(FrameError):
                self.preflight()

    def test_estimator_version_and_old_snapshot_reserve_recomputed(self):
        preflight = self.preflight()
        self.assertEqual(preflight['estimate']['estimator_version'], 2)
        preflight['estimate'] = {'disk_reserve_bytes': 1}
        old = self.store.save_preflight(preflight)
        job = self.store.create_job(old)
        before = self.store.get_preflight(old['preflight_id'])
        with mock.patch.object(self.service, '_space', side_effect=FrameError('disk_space','safe')), mock.patch('app.frame_export.export_frame_scene') as export:
            self.service.run_job(job['job_id'])
        export.assert_not_called()
        self.assertEqual(self.store.get_preflight(old['preflight_id']), before)
        self.assertEqual(self.store.get_job(job['job_id'])['status'], 'failed')

    def test_mosaic_preflight_and_job_group_sources_into_one_output(self):
        params = {**self.search['parameters'], 'merge_scenes': True}
        snapshot = self.store.save_search(params, self.rows)
        selected = list(reversed(self.rows))
        preflight = self.service.preflight(snapshot['search_id'], [s['scene_token'] for s in selected])
        self.assertEqual(preflight['estimate']['uncompressed_bytes'], 5*5*4)
        self.assertEqual(preflight['estimate']['output_count'], 1)
        self.assertEqual(preflight['estimate']['source_count'], 2)
        job = self.store.create_job(preflight)
        self.assertEqual(job['total'], 1)
        self.assertEqual(job['scenes'][0]['sources'], selected)
        self.assertEqual(job['scenes'][0]['asset_id'], selected[0]['asset_id'])
        self.assertEqual(len(self.store.get_search(snapshot['search_id'])['scenes']), 2)

    def test_sar_mosaic_rejects_incompatible_tracks(self):
        params = {**self.search['parameters'], 'dataset': 's1gee', 'bands': ['VV'], 'merge_scenes': True}
        rows = [{**s, 'orbit_pass': 'DESCENDING', 'relative_orbit': i+1} for i, s in enumerate(self.rows)]
        snapshot = self.store.save_search(params, rows)
        with self.assertRaises(FrameError) as caught:
            self.service.preflight(snapshot['search_id'], [s['scene_token'] for s in rows])
        self.assertEqual(caught.exception.code, 'incompatible_orbits')

    def test_job_idempotency_busy_and_recover(self):
        preflight = self.preflight()
        first = self.store.create_job(preflight)
        self.assertEqual(self.store.create_job(preflight)['job_id'], first['job_id'])
        other = self.preflight()
        with self.assertRaises(FrameError) as ctx:
            self.store.create_job(other)
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.store.recover(), 1)
        self.assertEqual(self.store.get_job(first['job_id'])['status'], 'interrupted')
        self.assertEqual(self.store.get_job(first['job_id'])['scenes'][0]['status'], 'interrupted')

    def test_partial_worker_archive_and_frozen_grid(self):
        preflight = self.preflight()
        job = self.store.create_job(preflight)
        original_grid = preflight['output_grid'].copy()

        def export(scene, parameters, path):
            if scene['asset_id'].endswith('1'):
                raise RuntimeError('https://secret.invalid/?token=private')
            grid = parameters['output_grid']
            self.assertEqual(grid, original_grid)
            with rasterio.open(path, 'w', driver='GTiff', count=1, dtype='float32',
                               width=5, height=5, crs=grid['crs'], transform=rasterio.Affine(*grid['transform'])) as dst:
                dst.write(np.zeros((1, 5, 5), dtype='float32'))
                dst.set_band_description(1, 'B2')
            return {'actual_coverage_pct': {'B2': 100}, 'path': 'C:\\private', 'download_url': 'https://private'}

        with mock.patch('app.frame_export.export_frame_scene', side_effect=export):
            self.service.run_job(job['job_id'])
        result = self.store.get_job(job['job_id'])
        self.assertEqual((result['status'], result['succeeded'], result['failed']), ('partial', 1, 1))
        path, name = self.store.result_path(job['job_id'])
        with zipfile.ZipFile(path) as archive:
            manifest = archive.read('manifest.json').decode()
            self.assertNotIn('https://', manifest)
            self.assertNotIn('C:\\\\private', manifest)
            self.assertEqual(len(archive.namelist()), 2)
            decoded = json.loads(manifest)
            self.assertEqual(decoded['reports'][0]['actual_coverage_pct'], {'B2': 100})
            self.assertEqual(decoded['reports'][0]['estimated_band_coverage_pct'], 100)
            self.assertEqual(decoded['reports'][1]['error_code'], 'export_failed')
        scene = result['scenes'][0]
        tif, _ = self.store.result_path(job['job_id'], scene['file_token'])
        self.assertTrue(tif.exists())
        with self.assertRaises(FrameError):
            self.store.result_path(job['job_id'], '../bad')
        self.assertEqual(list(self.store.job_dir(job['job_id']).glob('*.partial*')), [])

    def test_mosaic_provenance_survives_sqlite_manifest_and_archive(self):
        params = {**self.search['parameters'], 'merge_scenes': True, 'platform': 'C'}
        rows = [{**row, 'platform': 'Sentinel-2C', 'date': f'2026-03-{28+i}',
                 'timestamp': f'2026-03-{28+i}T03:42:00Z'} for i, row in enumerate(self.rows)]
        snapshot = self.store.save_search(params, rows)
        preflight = self.service.preflight(snapshot['search_id'], [row['scene_token'] for row in rows])
        job = self.store.create_job(preflight)
        sources = [{**row, 'priority': i+1, 'export_provenance': {'version': 1,
                    'export_strategy': 'direct', 'value_resampling_steps': ['bilinear']}}
                   for i, row in enumerate(rows)]
        report = {'actual_coverage_pct': {'B2': 100}, 'common_band_coverage_pct': 100,
                  'common_valid_pixel_count': 25, 'primary_asset_id': rows[0]['asset_id'],
                  'source_time_range_utc': [row['timestamp'] for row in rows],
                  'mosaic_policy': 'primary_first_fill_nodata', 'sources': sources}
        def export(record, parameters, path):
            path.write_bytes(b'synthetic verified TIFF')
            return report
        with mock.patch('app.frame_export.export_frame_scene', side_effect=export):
            self.service.run_job(job['job_id'])
        reopened = FrameStore(self.store.root)
        result = reopened.get_job(job['job_id'])
        self.assertEqual(result['scenes'][0]['report'], report)
        self.assertEqual(result['preflight_digest'], preflight['digest'])
        path, _ = reopened.result_path(job['job_id'])
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            saved = manifest['reports'][0]
            self.assertEqual(saved['sources'], sources)
            self.assertEqual(saved['primary_asset_id'], rows[0]['asset_id'])
            self.assertEqual(saved['source_time_range_utc'], report['source_time_range_utc'])
            self.assertEqual(saved['common_valid_pixel_count'], 25)
            self.assertIn('2026-03-28_2026-03-29', saved['filename'])

    def test_all_selection_preserves_each_source_platform_in_results_and_manifests(self):
        params = {**self.search['parameters'], 'platform': None}
        rows = [{**row, 'platform': platform} for row, platform in
                zip(self.rows, ('Sentinel-2A', 'Sentinel-2C'))]
        rows.append({**self.rows[0], 'scene_token': str(uuid.uuid4()),
                     'asset_id': 'COPERNICUS/S2_SR_HARMONIZED/SCENE2', 'platform': 'Sentinel-2B'})
        snapshot = self.store.save_search(params, rows)
        preflight = self.service.preflight(snapshot['search_id'], [row['scene_token'] for row in rows])
        job = self.store.create_job(preflight)

        def tile(image, dataset, bands, grid, window, **kwargs):
            if image.endswith('SCENE2'):
                raise RuntimeError('upstream unavailable')
            return np.ma.array(np.zeros((len(bands), int(window.height), int(window.width)), np.float32))

        with mock.patch('app.frame_export._RPC'), \
                mock.patch('app.frame_export.load_frame_image', side_effect=lambda asset, *args: asset), \
                mock.patch('app.frame_export.fetch_grid_tile', side_effect=tile):
            self.service.run_job(job['job_id'])

        reopened = FrameStore(self.store.root)
        result = reopened.get_job(job['job_id'])
        self.assertEqual((result['status'], result['succeeded'], result['failed']), ('partial', 2, 1))
        expected = ['Sentinel-2A', 'Sentinel-2C', 'Sentinel-2B']
        self.assertEqual([row.get('platform') for row in result['scenes']], expected)
        with self.subTest(storage='sqlite_reports'):
            self.assertEqual([row['report'].get('platform') for row in result['scenes'][:2]], expected[:2])
        manifests = [('persisted', json.loads((reopened.job_dir(job['job_id']) / 'manifest.json').read_text(encoding='utf-8')))]
        archive_path, _ = reopened.result_path(job['job_id'])
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(len(archive.namelist()), 3)
            manifests.append(('archive', json.loads(archive.read('manifest.json'))))
        for storage, manifest in manifests:
            with self.subTest(storage=storage):
                self.assertIsNone(manifest['platform'])
                self.assertEqual([report['status'] for report in manifest['reports']],
                                 ['succeeded', 'succeeded', 'failed'])
                self.assertEqual(manifest['reports'][2]['error_code'], 'export_failed')
                self.assertEqual([report.get('platform') for report in manifest['reports']], expected)

    def test_cleanup_leases_and_active_jobs(self):
        preflight = self.preflight()
        job = self.store.create_job(preflight)
        future = time.time() + TTL_SECONDS * 2
        self.store.cleanup(future)
        self.assertEqual(self.store.get_job(job['job_id'])['status'], 'queued')
        self.store.update_job(job['job_id'], status='failed')
        with self.store.lease(job['job_id']):
            with self.store.lease(job['job_id']):
                self.store.cleanup(future)
            self.store.cleanup(future)
            self.assertEqual(self.store.get_job(job['job_id'])['status'], 'failed')
        self.store.cleanup(future)
        with self.assertRaises(FrameError):
            self.store.get_job(job['job_id'])

    def test_preview_is_cached_and_expired_preview_removed(self):
        key, token = self.search['search_id'], self.rows[0]['scene_token']
        content = b'\x89PNG\r\n\x1a\n' + b'synthetic'
        with mock.patch('app.frame_search.make_frame_preview', return_value=content) as preview:
            self.assertEqual(self.service.preview(key, token), content)
            self.assertEqual(self.service.preview(key, token), content)
            self.assertEqual(preview.call_count, 1)
        self.assertTrue((self.store.root / 'previews' / key).is_dir())
        self.store.cleanup(time.time() + TTL_SECONDS * 2)
        self.assertFalse((self.store.root / 'previews' / key).exists())

    def test_snapshot_reads_do_not_mutate_stored_scene(self):
        result = self.store.get_search(self.search['search_id'])
        result['scenes'][0]['asset_id'] = 'mutated'
        result['parameters']['output_grid']['width'] = 999
        again = self.store.get_search(self.search['search_id'])
        self.assertEqual(again['parameters']['output_grid']['width'], 5)
        self.assertNotEqual(again['scenes'][0]['asset_id'], 'mutated')
