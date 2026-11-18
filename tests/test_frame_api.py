"""Reference uploads and snapshot API without network calls."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app.frame_api import register_frame_routes, FrameUploadLimitMiddleware
from app.frame_store import FrameStore
from app.frame_jobs import FrameService


def tiff_bytes():
    with MemoryFile() as mem:
        with mem.open(driver='GTiff', width=5, height=6, count=1, dtype='uint8',
                      crs='EPSG:32648', transform=from_origin(398712, 2406162, 3, 3)) as dst:
            dst.write(np.zeros((1, 6, 5), dtype='uint8'))
        return mem.read()


class FrameAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = FrameStore(Path(self.temp.name))
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
        self.app = FastAPI()
        register_frame_routes(self.app, self.service)
        self.client = TestClient(self.app)

    def upload(self):
        response = self.client.post('/api/matched-frame/references', files={'file': ('mẫu.tif', tiff_bytes(), 'image/tiff')})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_upload_returns_safe_metadata_and_cleans_pixel_file(self):
        data = self.upload()
        self.assertEqual(data['metadata']['width'], 5)
        self.assertEqual(data['metadata']['display_name'], 'mẫu.tif')
        self.assertEqual(list((self.store.root / 'upload').glob('*')), [])
        response = self.client.get('/api/matched-frame/references/' + data['reference_id'])
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(str(self.store.root), response.text)

    def test_reject_bad_uploads_and_origin(self):
        for filename, content, expected in [('fake.tif', b'<VRTDataset/>', 415), ('sample.txt', tiff_bytes(), 415)]:
            r = self.client.post('/api/matched-frame/references', files={'file': (filename, content)})
            self.assertEqual(r.status_code, expected, r.text)
        r = self.client.post('/api/matched-frame/references', files={'file': ('a.tif', tiff_bytes())},
                             headers={'Origin': 'http://evil.invalid'})
        self.assertEqual(r.status_code, 403)
        r = self.client.post('/api/matched-frame/references', files={'file': ('a.tif', tiff_bytes())},
                             headers={'Origin': 'http://testserver'})
        self.assertEqual(r.status_code, 200)
        r = self.client.post('/api/matched-frame/references', files=[('file', ('a.tif', tiff_bytes())), ('file', ('b.tif', tiff_bytes()))])
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.get('/api/matched-frame/references/not-a-uuid').status_code, 404)

    def test_stream_body_limit_without_length(self):
        app = FastAPI()
        register_frame_routes(app, self.service)
        # Override the matched-frame body limit for this synthetic request.
        app.user_middleware[0].kwargs['body_limit'] = 128
        client = TestClient(app)
        boundary = 'tiny'
        body = b'--tiny\r\nContent-Disposition: form-data; name="file"; filename="x.tif"\r\nContent-Type: image/tiff\r\n\r\n' + b'a' * 200 + b'\r\n--tiny--\r\n'
        r = client.post('/api/matched-frame/references', content=iter([body[:120], body[120:]]),
                        headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        self.assertEqual(r.status_code, 413, r.text)

    def test_verified_files_and_archive_have_no_leaked_lease(self):
        preflight = self.store.save_preflight({'parameters': {}, 'scenes': [dict(
            scene_token=str(uuid.uuid4()), date='2026-03-28', asset_id='scene')],
            'search_id': str(uuid.uuid4()), 'digest': '1' * 64})
        job = self.store.create_job(preflight)
        key, token = job['job_id'], str(uuid.uuid4())
        directory = self.store.job_dir(key)
        directory.mkdir(parents=True)
        (directory / f'{token}.tif').write_bytes(tiff_bytes())
        import zipfile
        with zipfile.ZipFile(directory / 'result.zip', 'w') as z:
            z.writestr('manifest.json', '{}')
        job['scenes'][0].update(status='succeeded', file_token=token, filename='result.tif')
        self.store.update_job(key, status='completed', scenes=job['scenes'])
        for url, mime in [(f'/api/matched-frame/jobs/{key}/files/{token}', 'image/tiff'),
                          (f'/api/matched-frame/jobs/{key}/download', 'application/zip')]:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, r.text[:100] if r.status_code != 200 else '')
            self.assertIn(mime, r.headers['content-type'])
            self.assertTrue(r.content)
            self.assertEqual(self.store.leases, {})
        r = self.client.get(f'/api/matched-frame/jobs/{key}/files/{uuid.uuid4()}')
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.store.leases, {})

    def test_reference_search_mosaic_job_download_end_to_end(self):
        import time
        from app import frame_export as export
        reference = self.upload()
        rows = [dict(scene_token=str(uuid.uuid4()), asset_id=f'COPERNICUS/S2_SR_HARMONIZED/SCENE{i}',
                     timestamp='2026-03-28T03:42:00Z', date='2026-03-28', eligible=True,
                     band_coverage_pct=50, roi_cloud_shadow_pct=5) for i in range(2)]
        body = dict(reference_id=reference['reference_id'], start_date='2026-03-01', end_date='2026-03-31',
                    band_preset='rgb', merge_scenes=True, require_full_coverage=False, min_coverage_pct=100,
                    min_cloud_shadow_pct=0, max_cloud_shadow_pct=10)
        with mock.patch('app.frame_search.search_frame_scenes', return_value=rows):
            found = self.client.post('/api/matched-frame/searches', json=body)
        self.assertEqual(found.status_code, 200, found.text)
        snapshot = found.json()
        check = self.client.post('/api/matched-frame/preflight', json=dict(
            search_id=snapshot['search_id'], scene_tokens=[s['scene_token'] for s in rows]))
        self.assertEqual(check.status_code, 200, check.text)
        confirmation = check.json()
        self.assertEqual(confirmation['estimate']['output_count'], 1)
        def tile(image, dataset, bands, grid, window, **kwargs):
            values = np.ma.array(np.full((len(bands), int(window.height), int(window.width)),
                                        0 if image.endswith('0') else .2, np.float32), mask=False)
            split = grid['height'] // 2
            ys = np.arange(int(window.row_off), int(window.row_off + window.height))
            values.mask[:, ys >= split if image.endswith('0') else ys < split, :] = True
            return values
        with mock.patch.object(export, '_RPC'), \
                mock.patch.object(export, 'load_frame_image', side_effect=lambda asset, *args: asset), \
                mock.patch.object(export, 'fetch_grid_tile', side_effect=tile):
            created = self.client.post('/api/matched-frame/jobs', json=dict(
                preflight_id=confirmation['preflight_id'], digest=confirmation['digest'], acknowledged=True))
            self.assertEqual(created.status_code, 200, created.text)
            key = created.json()['job_id']
            for _ in range(100):
                result = self.client.get(f'/api/matched-frame/jobs/{key}').json()
                if result['status'] not in ('queued', 'running'):
                    break
                time.sleep(.02)
        self.assertEqual(result['status'], 'completed', result)
        record = result['scenes'][0]
        self.assertEqual(len(record['sources']), 2)
        response = self.client.get(record['download_url'])
        self.assertEqual(response.status_code, 200)
        self.assertIn('image/tiff', response.headers['content-type'])
        with MemoryFile(response.content) as memory, memory.open() as source:
            self.assertEqual(source.descriptions, ('B4', 'B3', 'B2'))
            self.assertEqual(source.width, confirmation['output_grid']['width'])
            self.assertTrue(np.isfinite(source.read()).all())
            self.assertEqual(source.read(1)[0, 0], 0)
            self.assertAlmostEqual(float(source.read(1)[-1, 0]), .2)
        archive = self.client.get(result['download_url'])
        self.assertEqual(archive.status_code, 200)
        import zipfile, json
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            manifest = json.loads(zipped.read('manifest.json'))
            self.assertEqual(len(zipped.namelist()), 2)
            self.assertEqual(len(manifest['reports'][0]['sources']), 2)
            self.assertTrue(manifest['merge_scenes'])
        self.assertEqual(list(self.store.job_dir(key).glob('*.part.tif')), [])

    def test_stale_snapshot_returns_409_before_preflight_and_preview(self):
        reference = self.upload()
        token = str(uuid.uuid4())
        snapshot = self.store.save_search({'reference': reference['metadata']}, [dict(scene_token=token, eligible=True)])
        sid = snapshot['search_id']
        for response in (
            self.client.post('/api/matched-frame/preflight', json={'search_id': sid, 'scene_tokens': [token]}),
            self.client.get(f'/api/matched-frame/searches/{sid}/scenes/{token}/preview')):
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()['detail']['code'], 'stale_search')

    def test_search_pagination_preflight_and_validation(self):
        reference = self.upload()
        token = str(uuid.uuid4())
        row = dict(scene_token=token, asset_id='COPERNICUS/S2_SR_HARMONIZED/SCENE', timestamp='2026-03-28T03:42:00Z',
                   date='2026-03-28', eligible=True, band_coverage_pct=100, roi_cloud_shadow_pct=0)
        request = dict(reference_id=reference['reference_id'], start_date='2026-03-01', end_date='2026-03-31')
        with mock.patch('app.frame_jobs.FrameService.search') as search:
            grid = {'crs': 'EPSG:32648', 'transform': [10, 0, 398712, 0, -10, 2406162], 'width': 2, 'height': 2}
            params = {**request, 'dataset': 'sentinel2', 'bands': ['B2'], 'output_grid': grid,
                      'reference': reference['metadata'], 'grid_mode': 'match_extent'}
            from app.reference_frame import reference_roi_identity
            params.update(reference_roi_identity(params['reference']))
            saved = self.store.save_search(params, [row])
            search.return_value = saved
            r = self.client.post('/api/matched-frame/searches', json=request)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn('/preview', r.json()['scenes'][0]['preview_url'])
        sid = saved['search_id']
        self.assertEqual(self.client.get(f'/api/matched-frame/searches/{sid}?limit=51').status_code, 422)
        with mock.patch('app.frame_jobs.shutil.disk_usage', return_value=type('Disk', (), {'free': 10 ** 12})()):
            r = self.client.post('/api/matched-frame/preflight', json={'search_id': sid, 'scene_tokens': [token]})
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()
        self.assertEqual(len(p['digest']), 64)
        r = self.client.post('/api/matched-frame/jobs', json={'preflight_id': p['preflight_id'], 'digest': '0' * 64, 'acknowledged': True})
        self.assertEqual(r.status_code, 409)
        r = self.client.post('/api/matched-frame/preflight', json={'search_id': sid, 'scene_tokens': [token, token]})
        self.assertEqual(r.status_code, 422)
        r = self.client.get('/api/matched-frame/jobs/' + str(uuid.uuid4()) + '/download')
        self.assertEqual(r.status_code, 404)
