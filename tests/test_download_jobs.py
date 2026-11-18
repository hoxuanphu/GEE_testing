import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.download_models import FrozenSelection, PIPELINE_VERSION, TileReceipt
from app.download_store import SelectionStore


def selection(store):
    request=dict(dataset='sentinel2',selection_mode='point',lon=107.,lat=10.,radius_m=100.,
                 rotation_deg=0.,s2_auxiliary_bands=[])
    snapshot=FrozenSelection('test-token','selection-hash',request,'{}',{}, {},'scope',
                             PIPELINE_VERSION,time.time(),time.time()+3600)
    store.create(snapshot)
    return snapshot


def wait_for(service,job_id,state):
    end=time.monotonic()+5
    while time.monotonic()<end:
        value=service.status(job_id)
        if value['state']==state:
            return value
        time.sleep(.01)
    raise AssertionError(f'Expected {state}, got {value}')


class JobTests(unittest.TestCase):
    def test_durable_dedupe_pause_resume_and_stale_worker_cannot_publish(self):
        from app.download_jobs import DownloadService
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root)
            snapshot=selection(store)
            release=threading.Event()
            calls=[]
            def runner(snapshot,store,**kwargs):
                calls.append(1)
                kwargs['progress'](stage='downloading',tiles_total=1)
                while not release.wait(.01): kwargs['check']()
                kwargs['check']()
                from app.download_pipeline import export_plan
                key=export_plan(snapshot,kwargs['settings'])['key']
                attempt=store.claim_attempt(key)
                path=store.root/'out.tif'; path.write_bytes(b'completed')
                receipt=TileReceipt(key,str(path),'sha',9,{})
                store.save_artifact(attempt,receipt)
                store.release_attempt(attempt)
                return receipt
            service=DownloadService(store,runner=runner)
            self.addCleanup(service.shutdown)
            job=service.start(snapshot.token)
            self.assertEqual(service.start(snapshot.token)['id'],job['id'])
            wait_for(service,job['id'],'running')
            service.pause(job['id'])
            self.assertEqual(service.status(job['id'])['state'],'paused')
            self.assertNotIn('file_url',service.status(job['id']))
            service.resume(job['id'])
            wait_for(service,job['id'],'running')
            release.set()
            result=wait_for(service,job['id'],'completed')
            self.assertEqual(len(calls),2)
            self.assertEqual(result['stage'],'completed')
            self.assertIn('/file',result['file_url'])
            self.assertNotIn('selection_token',result)
            self.assertNotIn('image_graph',result)
            service.shutdown()
            restored=DownloadService(SelectionStore(root),runner=runner)
            self.addCleanup(restored.shutdown)
            self.assertEqual(restored.status(job['id'])['state'],'completed')
            restored.shutdown()

    def test_restart_interrupts_incomplete_jobs_and_requires_explicit_resume(self):
        from app.download_jobs import DownloadService
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root); snapshot=selection(store)
            service=DownloadService(store,autostart=False)
            job=service.start(snapshot.token)
            service.shutdown()
            restarted=DownloadService(SelectionStore(root),autostart=False)
            self.addCleanup(restarted.shutdown)
            self.assertEqual(restarted.status(job['id'])['state'],'interrupted')
            restarted.resume(job['id'])
            self.assertEqual(restarted.status(job['id'])['state'],'queued')
            restarted.cancel(job['id'])
            self.assertEqual(restarted.status(job['id'])['state'],'cancelled')
            restarted.shutdown()

    def test_new_start_after_cancel_or_artifact_eviction_is_not_stuck(self):
        from app.download_jobs import DownloadService
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);snapshot=selection(store)
            service=DownloadService(store,autostart=False)
            first=service.start(snapshot.token)
            service.cancel(first['id'])
            second=service.start(snapshot.token)
            self.assertNotEqual(first['id'],second['id'])
            store.update_job(second['id'],state='completed',stage='completed')
            self.assertEqual(service.status(second['id'])['state'],'expired')
            third=service.start(snapshot.token)
            self.assertNotEqual(second['id'],third['id'])
            self.assertEqual(third['state'],'queued')
            service.shutdown()

    def test_cancel_new_start_during_progress_does_not_kill_worker(self):
        from app.download_jobs import DownloadService
        from app.download_pipeline import export_plan
        from unittest import mock
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);snapshot=selection(store)
            reached=threading.Event();release=threading.Event();calls=[]
            original=store.update_job
            def delayed(job_id,**kwargs):
                if kwargs.get('stage')=='downloading' and not reached.is_set():
                    reached.set();release.wait(3)
                return original(job_id,**kwargs)
            def runner(snapshot,store,**kwargs):
                calls.append(1);kwargs['progress'](stage='downloading')
                key=export_plan(snapshot,kwargs['settings'])['key']
                attempt=store.claim_attempt(key)
                path=store.root/'done.tif';path.write_bytes(b'done')
                receipt=TileReceipt(key,str(path),'sha',4,{})
                store.save_artifact(attempt,receipt);store.release_attempt(attempt)
                return receipt
            with mock.patch.object(store,'update_job',side_effect=delayed):
                service=DownloadService(store,runner=runner)
                try:
                    first=service.start(snapshot.token)
                    self.assertTrue(reached.wait(3))
                    service.cancel(first['id'])
                    second=service.start(snapshot.token)
                    release.set()
                    wait_for(service,second['id'],'completed')
                    self.assertTrue(service._thread.is_alive())
                finally:
                    release.set();service.shutdown()

    def test_rollback_blocks_new_jobs_but_existing_paused_job_can_resume(self):
        from app import main
        from app.download_pipeline import export_plan
        from app.download_models import DownloadSettings
        from dataclasses import asdict
        from fastapi.testclient import TestClient
        from unittest import mock
        with tempfile.TemporaryDirectory() as root,mock.patch.object(main,'_DOWNLOADS_DIR',Path(root)), \
             mock.patch.object(main,'_download_service',None),mock.patch.dict('os.environ',{'GEE_DOWNLOAD_JOBS':'0'}), \
             TestClient(main.app) as client:
            service=main._get_download_service()
            snapshot=selection(service.store)
            def runner(snapshot,store,**kwargs):
                key=export_plan(snapshot,kwargs['settings'])['key'];attempt=store.claim_attempt(key)
                path=store.root/'rollback.tif';path.write_bytes(b'done')
                receipt=TileReceipt(key,str(path),'sha',4,{})
                store.save_artifact(attempt,receipt);store.release_attempt(attempt);return receipt
            service.runner=runner
            job=service.store.create_job(snapshot,export_plan(snapshot,service.settings)['key'],asdict(service.settings))
            service.store.update_job(job['id'],state='paused',stage='paused')
            self.assertEqual(client.post('/api/download-jobs',json={'selection_token':snapshot.token}).status_code,404)
            response=client.post(f'/api/download-jobs/{job["id"]}/resume')
            self.assertEqual(response.status_code,200,response.text)
            wait_for(service,job['id'],'completed')

    def test_artifact_fencing_and_cleanup_keeps_delivery_lease(self):
        from app.download_store import StaleAttempt
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root)
            attempt=store.claim_attempt('artifact')
            part=Path(root)/'part.tif'; part.write_bytes(b'data')
            receipt=TileReceipt('artifact',str(part),'sha',4,{})
            store.release_attempt(attempt)
            current=store.claim_attempt('artifact')
            target=Path(root)/'final.tif'
            with self.assertRaises(StaleAttempt): store.publish_artifact(attempt,receipt,target)
            self.assertTrue(part.exists())
            receipt=store.publish_artifact(current,receipt,target)
            store.release_attempt(current)
            with store.artifact_lease('artifact'):
                store.cleanup_artifacts(now=time.time()+1000000,quota_bytes=0)
                self.assertTrue(target.exists())
            store.cleanup_artifacts(now=time.time()+1000000,quota_bytes=0)
            self.assertFalse(target.exists())

    def test_pausing_job_fences_artifact_publish_even_after_final_worker_check(self):
        from app.download_jobs import DownloadService
        from app.download_store import StaleAttempt
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);snapshot=selection(store)
            service=DownloadService(store,autostart=False)
            job=service.start(snapshot.token)
            store.update_job(job['id'],state='running')
            attempt=store.claim_attempt('artifact',job_context=(job['id'],0))
            path=store.root/'part.tif';path.write_bytes(b'data')
            receipt=TileReceipt('artifact',str(path),'sha',4,{})
            service.pause(job['id'])
            with self.assertRaises(StaleAttempt):
                store.publish_artifact(attempt,receipt,store.root/'final.tif')
            self.assertTrue(path.exists())
            service.shutdown()

    def test_dead_delivery_process_does_not_pin_expired_file_forever(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root)
            attempt=store.claim_attempt('artifact')
            path=store.root/'out.tif';path.write_bytes(b'data')
            store.save_artifact(attempt,TileReceipt('artifact',str(path),'sha',4,{}))
            store.release_attempt(attempt)
            with store.connect() as db:
                db.execute('INSERT INTO artifact_leases VALUES (?, ?, ?)',('dead','artifact',999999999))
            with mock.patch('app.download_store._process_alive',return_value=False,create=True):
                store.cleanup_artifacts(now=time.time()+1000000,quota_bytes=0)
            self.assertFalse(path.exists())

    def test_orphans_after_crash_expire_but_active_attempt_and_external_files_stay(self):
        import os
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);directory=store.root/'artifacts';directory.mkdir()
            tiles=store.root/'tiles';tiles.mkdir()
            key='e'*64;attempt=store.claim_attempt(key)
            target=directory/(key+'.tif');target.write_bytes(b'data')
            store.prepare_publication(attempt,TileReceipt(key,str(target),'sha',4,{}),target)
            partial=tiles/(attempt.owner+'-'+'f'*64+'.part');partial.write_bytes(b'partial')
            staged=directory/(attempt.owner+'.source.tif');staged.write_bytes(b'source')
            unrelated=directory/'user-file.tif';unrelated.write_bytes(b'user')
            old=time.time()-8*86400
            for path in (target,partial,staged,unrelated):os.utime(path,(old,old))
            store.cleanup_orphans()
            self.assertTrue(target.exists());self.assertTrue(partial.exists())
            store.release_attempt(attempt)
            store.cleanup_orphans()
            self.assertFalse(target.exists());self.assertFalse(partial.exists())
            self.assertFalse(staged.exists())
            self.assertTrue(unrelated.exists())
            self.assertIsNone(store.get_publication_intent(key))
