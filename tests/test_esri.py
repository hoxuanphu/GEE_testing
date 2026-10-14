"""Offline Esri engine, lifecycle, and CLI regression tests."""
import json
import math
import shutil
import threading
import time
import unittest
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import rasterio
from PIL import Image
from rasterio.features import rasterize

from app import esri
import commune_esri_download as commune_cli
import esri_tiles_download as bbox_cli


def jpeg(color=(80, 80, 80)):
    buffer = BytesIO()
    Image.new('RGB', (256, 256), color).save(buffer, format='JPEG')
    return buffer.getvalue()


class Response:
    def __init__(self, data=None, blank=False, status=200):
        self.content = jpeg() if data is None else data
        self.status_code = status
        self.headers = {'blank-tile': 'true'} if blank else {}

    def iter_content(self, chunk_size):
        yield self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise esri.requests.HTTPError('HTTP failure')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Session:
    calls = []
    closed = 0
    responder = None

    def __init__(self):
        self.headers = {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        return type(self).responder(url) if type(self).responder else Response()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        type(self).closed += 1


class EsriTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ('esri-test-' + uuid.uuid4().hex)
        self.root.mkdir()
        Session.calls, Session.closed, Session.responder = [], 0, None
        self.session_patch = patch.object(esri.requests, 'Session', Session)
        self.ca_patch = patch.object(esri, '_load_cas')
        self.session_patch.start()
        self.ca = self.ca_patch.start()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.shutdown()
            if service._thread:
                service._thread.join(5)
        self.session_patch.stop()
        self.ca_patch.stop()
        shutil.rmtree(self.root)

    def service(self):
        service = esri.EsriService(self.root / 'jobs')
        self.services.append(service)
        return service

    def finish(self, service, job_id):
        service._thread.join(5)
        self.assertFalse(service._thread.is_alive())
        return service.get(job_id)

    def test_exact_holes_and_grey_tiles(self):
        geometry = {'type': 'Polygon', 'coordinates': [
            [[1,1],[40,1],[40,40],[1,40],[1,1]],
            [[10,10],[10,30],[30,30],[30,10],[10,10]]]}
        plan = esri.make_plan([1,1,40,40], 3, geometry=geometry)
        output = self.root / 'holes.tif'
        stats = esri.download(plan, output, self.root / 'cache')
        self.assertEqual(stats['failed'], 0)
        with rasterio.open(output) as src:
            expected = rasterize([(plan.geometry,255)], out_shape=(plan.height,plan.width),
                                 transform=plan.transform, dtype='uint8')
            np.testing.assert_array_equal(src.dataset_mask(), expected)
            self.assertTrue((expected == 0).any())
            self.assertTrue((expected == 255).any())
            self.assertEqual(src.tags()['acquisition_date'], 'unknown')
            self.assertTrue(np.all(src.read()[:,expected == 255] == 80))
        self.assertEqual(Session.closed, len(Session.calls))
        self.ca.assert_called_once()

    def test_partial_mask_cache_resume_and_overwrite(self):
        plan = esri.make_plan([-1,1,1,5], 3)
        self.assertEqual(len(plan.tiles), 2)
        Session.responder = lambda url: Response(blank=True) if url.endswith('/4') else Response()
        output = self.root / 'partial.tif'
        stats = esri.download(plan, output, self.root / 'cache')
        self.assertEqual(stats['failed'], 1)
        with rasterio.open(output) as src:
            self.assertEqual(src.tags()['missing_tiles'], '1')
            self.assertTrue((src.dataset_mask() == 0).any())
        with self.assertRaises(FileExistsError):
            esri.download(plan, output, self.root / 'cache')
        before = len(Session.calls)
        Session.responder = None
        stats = esri.download(plan, output, self.root / 'cache', overwrite=True)
        self.assertEqual(stats['cached'], 1)
        self.assertEqual(len(Session.calls)-before, 1)
        self.assertFalse(list(self.root.rglob('*.part')))

    def test_invalid_tiles_and_corrupt_cache(self):
        plan = esri.make_plan([1,1,2,2], 3)
        output = self.root / 'invalid.tif'
        for data in (b'not a jpeg', jpeg()[:50]):
            Session.responder = lambda url, data=data: Response(data=data)
            with self.assertRaisesRegex(RuntimeError, 'No usable'):
                esri.download(plan, output, self.root / 'cache')
            self.assertFalse(output.exists())
        Session.responder = None
        cache = self.root / 'cache'
        x,y = plan.tiles[0]
        (cache / f'3_{x}_{y}.jpg').write_bytes(b'corrupt')
        stats = esri.download(plan, output, cache)
        self.assertEqual(stats['cached'], 0)
        self.assertEqual(stats['succeeded'], 1)

    def test_disk_budgets(self):
        plan = esri.make_plan([1,1,2,2], 3)
        with self.assertRaisesRegex(ValueError, 'budget'):
            esri._check_disk(plan, self.root, budget=1)
        with patch.object(esri.shutil, 'disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(ValueError, 'free disk'):
                self.service().start(esri.EsriRequest(bbox=[1,1,2,2],zoom=3))
        self.assertFalse(Session.calls)

    def test_limits_and_request_contract(self):
        for bbox in ([0,0,1,float('nan')], [0,-86,1,2], [0,0,0,1], [0,1,2]):
            with self.assertRaises(ValueError):
                esri.EsriRequest(bbox=bbox)
        for kwargs in ({}, {'bbox':[0,0,1,1], 'commune_id':'1'},
                       {'bbox':[0,0,1,1], 'zoom':20}, {'commune_id':'1','workers':9},
                       {'commune_id':'1','max_tiles':20001}, {'commune_id':'1','bad':1}):
            with self.assertRaises(ValueError):
                esri.EsriRequest(**kwargs)
        with self.assertRaisesRegex(ValueError, 'pixels'):
            esri.make_plan([-180,-80,180,80], 19)
        with self.assertRaisesRegex(ValueError, 'tiles'):
            esri.make_plan([-1,1,1,5], 3, max_tiles=1)
        for kwargs in ({'zoom':0}, {'workers':0}, {'pad':-1}, {'pad':9}):
            with self.assertRaises(ValueError):
                esri.make_plan([1,1,2,2], **kwargs)
        with patch('app.commune_boundaries.get_commune', return_value=None):
            with self.assertRaisesRegex(ValueError, 'Unknown'):
                self.service().start(esri.EsriRequest(commune_id='missing'))
        self.assertFalse(Session.calls)

    def test_service_persistence_partial_resume(self):
        service = self.service()
        Session.responder = lambda url: Response(blank=True) if url.endswith('/4') else Response()
        job = service.start(esri.EsriRequest(bbox=[-1,1,1,5], zoom=3))
        job_id = job['job_id']
        self.assertEqual(self.finish(service, job_id)['status'], 'partial')
        self.assertTrue(service.download_path(job_id).exists())
        service.shutdown()
        recovered = self.service()
        self.assertEqual(recovered.get(job_id)['status'], 'partial')
        Session.responder = None
        before = len(Session.calls)
        recovered.resume(job_id)
        done = self.finish(recovered, job_id)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['progress']['cached'], 1)
        self.assertEqual(len(Session.calls)-before, 1)
        self.assertEqual(recovered.resume(job_id)['status'], 'completed')
        self.assertIsNone(recovered.get('../unsafe'))
        self.assertIsNone(recovered.download_path('../unsafe'))
        self.assertEqual(json.loads((self.root/'jobs'/job_id/'job.json').read_text())['status'], 'completed')

    def test_busy_cancel_stale_output_and_shutdown(self):
        entered, release = threading.Event(), threading.Event()
        def held(url):
            entered.set()
            release.wait(3)
            return Response()
        Session.responder = held
        service = self.service()
        body = esri.EsriRequest(bbox=[1,1,2,2], zoom=3)
        job_id = service.start(body)['job_id']
        self.assertTrue(entered.wait(2))
        with self.assertRaises(esri.BusyError):
            service.start(body)
        with self.assertRaises(esri.BusyError):
            service.resume(job_id)
        service.cancel(job_id)
        release.set()
        self.assertEqual(self.finish(service, job_id)['status'], 'cancelled')
        self.assertIsNone(service.download_path(job_id))
        Session.responder = None
        service.resume(job_id)
        self.assertEqual(self.finish(service, job_id)['status'], 'completed')
        service.shutdown()
        with self.assertRaises(RuntimeError):
            service.start(body)
        self.assertFalse(list(self.root.rglob('*.part')))

    def test_all_network_failures_keep_progress_and_cause(self):
        def failing(url):
            raise esri.requests.ReadTimeout('mock read deadline')
        Session.responder = failing
        service = self.service()
        job_id = service.start(esri.EsriRequest(bbox=[1,1,2,2],zoom=3))['job_id']
        job = self.finish(service,job_id)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['progress'], dict(processed=1,total=1,succeeded=0,failed=1,cached=0))
        self.assertIn('ReadTimeout', job['error'])
        self.assertIn('mock read deadline', job['error'])
        self.assertEqual(len(Session.calls), 3)
        state = json.loads((self.root/'jobs'/job_id/'job.json').read_text())
        self.assertEqual(state['progress'],job['progress'])
        self.assertIsNone(service.download_path(job_id))

    def test_transient_network_failure_retries(self):
        attempts = []
        def transient(url):
            attempts.append(url)
            if len(attempts) < 3:
                raise esri.requests.ConnectionError('transient proxy failure')
            return Response()
        Session.responder = transient
        service = self.service()
        job_id = service.start(esri.EsriRequest(bbox=[1,1,2,2],zoom=3))['job_id']
        job = self.finish(service,job_id)
        self.assertEqual(job['status'],'completed')
        self.assertEqual(job['progress']['succeeded'],1)
        self.assertEqual(len(attempts),3)

    def test_recovery_and_failed_resume(self):
        service = self.service()
        Session.responder = lambda url: Response(blank=True)
        job_id = service.start(esri.EsriRequest(bbox=[1,1,2,2],zoom=3))['job_id']
        self.assertEqual(self.finish(service,job_id)['status'], 'failed')
        Session.responder = None
        service.resume(job_id)
        self.assertEqual(self.finish(service,job_id)['status'], 'completed')
        service.shutdown()
        path = self.root/'jobs'/job_id/'job.json'
        state = json.loads(path.read_text())
        state['status'] = 'running'
        path.write_text(json.dumps(state))
        recovered = self.service()
        self.assertEqual(recovered.get(job_id)['status'], 'interrupted')
        self.assertIsNone(recovered.download_path(job_id))
        recovered.resume(job_id)
        self.assertEqual(self.finish(recovered,job_id)['progress']['cached'], 1)

    def test_commune_geometry_resolution_and_stale_output(self):
        geometry = {'type':'Polygon','coordinates':[[[1,1],[2,1],[2,2],[1,2],[1,1]]]}
        commune = SimpleNamespace(bbox=SimpleNamespace(west=1,south=1,east=2,north=2),
                                  geometry=geometry)
        with patch('app.commune_boundaries.get_commune', return_value=commune) as lookup:
            plan = esri.request_plan(esri.EsriRequest(commune_id='001',zoom=3))
            lookup.assert_called_once_with('001')
        self.assertEqual(plan.geometry['type'], 'Polygon')
        service = self.service()
        job_id = service.start(esri.EsriRequest(bbox=[1,1,2,2],zoom=3))['job_id']
        self.finish(service,job_id)
        service._jobs[job_id]['status'] = 'partial'
        entered, release = threading.Event(), threading.Event()
        original = esri.download
        def held(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch.object(esri,'download',side_effect=held):
            service.resume(job_id)
            self.assertTrue(entered.wait(2))
            self.assertIsNone(service.download_path(job_id))
            release.set()
            self.assertEqual(self.finish(service,job_id)['status'], 'completed')

    def test_cli_names_diacritics_and_arguments(self):
        reader = SimpleNamespace(fields=[('DeletionFlag',),('ten_xa',),('ten_tinh',)],
                                 records=lambda: [('Đông Sơn','Lào Cai'),('Đông Sơn','Hà Nội')])
        self.assertEqual(commune_cli._no_diacritics('Đông Sơn'), 'dong son')
        with self.assertRaisesRegex(ValueError, 'Ambiguous'):
            commune_cli.find_communes(reader,['dong son'],None)
        self.assertEqual(commune_cli.find_communes(reader,['DONG SON'],'lao cai')['dong son'][0],0)
        with patch('sys.argv',['esri_tiles_download.py','--bbox','1','1','2','2','--zoom','3',
                               '--workers','2','--max-tiles','12','--overwrite']), patch.object(bbox_cli,'download_bbox') as call:
            bbox_cli.main()
            self.assertEqual(call.call_args.args[:5], (1.,1.,2.,2.,3))
            self.assertEqual(call.call_args.kwargs, {'workers':2,'overwrite':True})


if __name__ == '__main__':
    unittest.main()
