import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.download_models import TileReceipt
from app.download_store import SelectionStore
from tests.test_download_jobs import selection


class DownloadAPITests(unittest.TestCase):
    def test_real_tcp_head_range_and_disconnect_release_lease_without_provider(self):
        from app.download_api import register_download_routes
        from app.download_jobs import DownloadService
        import socket,threading,uvicorn,requests
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);snapshot=selection(store)
            service=DownloadService(store,autostart=False)
            job=service.start(snapshot.token);key=store.get_job(job['id'])['artifact_key']
            path=store.root/'wire.tif'
            with path.open('wb') as handle:
                for _ in range(160):handle.write(b'0123456789abcdef'*8192)
            attempt=store.claim_attempt(key)
            store.save_artifact(attempt,TileReceipt(key,str(path),'checksum',path.stat().st_size,{}))
            store.release_attempt(attempt);store.update_job(job['id'],state='completed',stage='completed')
            app=FastAPI();register_download_routes(app,lambda:service,lambda:True)
            listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
            port=listener.getsockname()[1]
            server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
            thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
            try:
                deadline=time.monotonic()+5
                while not server.started and time.monotonic()<deadline:time.sleep(.01)
                self.assertTrue(server.started)
                url=f'http://127.0.0.1:{port}/api/download-jobs/{job["id"]}/file'
                with mock.patch('app.ee_session.init_ee',side_effect=AssertionError('file delivery cannot use GEE')),requests.Session() as client:
                    client.trust_env=False
                    response=client.head(url,timeout=5)
                    self.assertEqual(response.status_code,200)
                    response=client.get(url,headers={'Range':'bytes=5-12'},timeout=5)
                    self.assertEqual(response.status_code,206)
                    self.assertEqual(response.content,b'56789abc')
                    response=client.get(url,stream=True,timeout=5)
                    response.raw.read(32);response.close()
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    with store.connect() as db:count=db.execute('SELECT COUNT(*) FROM artifact_leases').fetchone()[0]
                    if count==0:break
                    time.sleep(.02)
                self.assertEqual(count,0)
            finally:
                server.should_exit=True;thread.join(3);listener.close();service.shutdown()
                self.assertFalse(thread.is_alive())

    def test_completed_file_range_head_etag_and_cleanup_lease(self):
        from app.download_api import register_download_routes
        from app.download_jobs import DownloadService
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root); snapshot=selection(store)
            service=DownloadService(store,autostart=False)
            app=FastAPI()
            register_download_routes(app,lambda:service,lambda:True)
            with TestClient(app) as client:
                response=client.post('/api/download-jobs',json={'selection_token':snapshot.token})
                self.assertEqual(response.status_code,202,response.text)
                job_id=response.json()['id']
                response=client.get(f'/api/download-jobs/{job_id}/file')
                self.assertEqual(response.status_code,409)
                job=store.get_job(job_id)
                attempt=store.claim_attempt(job['artifact_key'])
                # Sparse fixture proves offset handling >4GiB; this is not the
                # separate physical BigTIFF/large-raster acceptance test.
                path=store.root/'large.tif'
                offset=2**32+17
                with path.open('wb') as handle:
                    if os.name=='nt':
                        import ctypes,msvcrt
                        from ctypes import wintypes
                        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                        returned=wintypes.DWORD()
                        kernel.DeviceIoControl.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,
                            wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
                        if not kernel.DeviceIoControl(msvcrt.get_osfhandle(handle.fileno()),0x900c4,
                                None,0,None,0,ctypes.byref(returned),None):
                            raise ctypes.WinError(ctypes.get_last_error())
                    handle.seek(offset);handle.write(b'RANGE')
                receipt=TileReceipt(job['artifact_key'],str(path),'checksum',offset+5,{})
                store.save_artifact(attempt,receipt);store.release_attempt(attempt)
                store.update_job(job_id,state='completed',stage='completed')
                target=f'/api/download-jobs/{job_id}/file'
                response=client.head(target)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.content,b'')
                self.assertEqual(int(response.headers['content-length']),offset+5)
                self.assertEqual(response.headers['etag'],'"checksum"')
                original=store.artifact_lease
                from contextlib import contextmanager
                @contextmanager
                def guarded(key):
                    with original(key) as receipt:
                        store.cleanup_artifacts(now=time.time()+1000000,quota_bytes=0)
                        self.assertTrue(path.exists())
                        yield receipt
                with mock.patch.object(store,'artifact_lease',side_effect=guarded):
                    response=client.get(target,headers={'Range':f'bytes={offset}-{offset+4}'})
                self.assertEqual(response.status_code,206)
                self.assertEqual(response.content,b'RANGE')
                self.assertEqual(response.headers['content-range'],f'bytes {offset}-{offset+4}/{offset+5}')
                response=client.get(target,headers={'Range':f'bytes={offset+99}-'})
                self.assertEqual(response.status_code,416)
            service.shutdown()

    def test_invalid_token_disabled_creation_and_rollback_keeps_completed_delivery(self):
        from app.download_api import register_download_routes
        from app.download_jobs import DownloadService
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root)
            service=DownloadService(store,autostart=False)
            app=FastAPI(); enabled=[True]
            register_download_routes(app,lambda:service,lambda:enabled[0])
            with TestClient(app) as client:
                self.assertEqual(client.post('/api/download-jobs',json={'selection_token':'missing'}).status_code,410)
                enabled[0]=False
                self.assertEqual(client.post('/api/download-jobs',json={'selection_token':'missing'}).status_code,404)
            service.shutdown()
