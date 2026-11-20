from dataclasses import replace
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.transform import from_origin
from ee.apitestcase import ApiTestCase
import ee


def fixture(path, dtype='float32'):
    with rasterio.open(path, 'w', driver='GTiff', width=2,height=2,count=1,dtype=dtype,
                       crs='EPSG:4326',transform=from_origin(106,11,10/111319.49079327358,10/111319.49079327358), nodata=-9999,
                       BIGTIFF='YES') as dst:
        dst.write(np.array([[0,1],[-9999,2]],dtype=dtype),1)


class PlanTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)

    def test_tile_plan_never_uses_sdk_network_and_preserves_legacy_layout(self):
        from app.download_transport import plan_tiles
        from app.geo import Bbox
        with mock.patch.object(ee.Image,'getDownloadURL',side_effect=AssertionError('SDK network')), \
             mock.patch.object(ee.Image,'getInfo',side_effect=AssertionError('SDK network')):
            tiles = plan_tiles('selection', ['B2','QA60'], Bbox(106,10,106.75,10.7),10)
        self.assertEqual(len(tiles),18)
        self.assertEqual([tile.band for tile in tiles[:9]], ['B2']*9)
        self.assertNotEqual(tiles[0].tile_key,tiles[9].tile_key)

    def test_wire_cast_after_unmask_and_one_retry_controller(self):
        from app.download_transport import plan_tiles,stream_tile_to_file,DownloadHTTPError
        from app.geo import Bbox
        import json
        tile=plan_tiles('selection',['B2'],Bbox(106,10.9998,106.0002,11),10)[0]
        image=ee.Image.constant(1).rename('B2')
        graphs=[]
        class RPC:
            calls=0
            def prepare_tile(self,graph,params,**kwargs):
                graphs.append(json.loads(graph.serialize()))
                return 'resource'
            def stream_pixels(self,resource,destination,**kwargs):
                self.calls+=1
                if self.calls==1: raise DownloadHTTPError(503)
                fixture(destination)
        rpc=RPC()
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.object(ee.Image,'getDownloadURL',side_effect=AssertionError('SDK network')), \
             mock.patch.object(ee.Image,'getInfo',side_effect=AssertionError('SDK network')), \
             mock.patch.object(ee.data,'setDeadline',side_effect=AssertionError('global SDK mutation')):
            receipt=stream_tile_to_file(image,tile,Path(root)/'out.part',rpc=rpc,
                deadline=time.monotonic()+5,check=lambda:None)
        self.assertEqual(rpc.calls,2)
        root=graphs[0]['values'][graphs[0]['result']]
        self.assertEqual(root['functionInvocationValue']['functionName'],'Image.toFloat')


class TransportTests(unittest.TestCase):
    def test_auth_errors_are_safe_and_transient_auth_is_marked_retryable(self):
        from app.download_transport import DownloadRPC,DownloadAuthError
        from google.auth.exceptions import RefreshError,TransportError
        for failure,retryable in [(RefreshError('secret https://provider.test/token'),False),
                                  (TransportError('secret https://provider.test/token'),True)]:
            session=mock.Mock();credentials=mock.Mock();credentials.before_request.side_effect=failure
            with mock.patch('ee.serializer.encode',return_value={}),DownloadRPC(
                    lambda:credentials,'test',session_factory=lambda:session) as rpc:
                with self.assertRaises(DownloadAuthError) as raised:
                    rpc.prepare_tile(mock.Mock(),{},deadline=time.monotonic()+1,check=lambda:None)
            self.assertEqual(raised.exception.retryable,retryable)
            self.assertNotIn('secret',str(raised.exception))
            self.assertNotIn('https:',str(raised.exception))
            session.request.assert_not_called()

    def test_late_response_is_closed_before_raising_deadline(self):
        from app.download_transport import DownloadRPC
        response=mock.Mock(status_code=200,headers={})
        session=mock.Mock()
        def late(*args,**kwargs):
            time.sleep(.03)
            return response
        session.request.side_effect=late
        with DownloadRPC(lambda:mock.Mock(),'test',session_factory=lambda:session) as rpc:
            with self.assertRaises(TimeoutError):
                rpc.stream_pixels('projects/test/thumbnails/abc',Path('unused.part'),
                                  deadline=time.monotonic()+.01,check=lambda:None)
        response.close.assert_called_once()

    def test_gee_negative_infinity_nodata_is_normalized_to_wire_sentinel(self):
        from app.download_transport import normalize_wire_tile,validate_tile,plan_tiles
        from app.download_models import canonical
        from app.geo import Bbox
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'gee.tif';fixture(path)
            with rasterio.open(path,'r+') as source:
                source.nodata=float('-inf')
            normalize_wire_tile(path)
            with rasterio.open(path) as source:
                self.assertEqual(source.nodata,-9999)
                self.assertTrue(source.read(1,masked=True).mask[1,0])
                self.assertEqual(source.read(1,masked=True)[0,0],0)
            tile=plan_tiles('selection',['B2'],Bbox(106,10.9998,106.0002,11),10)[0]
            receipt=validate_tile(tile,path)
            self.assertIn('-9999',canonical(receipt.schema))

    def test_stream_and_validation_never_read_response_content_and_accept_bigtiff(self):
        from app.download_transport import DownloadRPC, validate_tile, plan_tiles
        from app.geo import Bbox
        class Response:
            status_code=200
            headers={}
            def __init__(self,data): self.data=data; self.closed=False
            @property
            def content(self): raise AssertionError('full RAM response')
            def iter_content(self,size):
                for offset in range(0,len(self.data),11): yield self.data[offset:offset+11]
            def close(self): self.closed=True
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'source.tif'
            fixture(path)
            response=Response(path.read_bytes())
            session=mock.Mock()
            session.request.return_value=response
            credentials=mock.Mock()
            with DownloadRPC(lambda: credentials,'test',session_factory=lambda:session) as rpc:
                target=Path(root)/'out.part'
                rpc.stream_pixels('projects/test/thumbnails/abc',target,deadline=time.monotonic()+5,check=lambda:None)
            self.assertTrue(response.closed)
            tile=plan_tiles('selection',['B2'],Bbox(106,10.9998,106.0002,11),10)[0]
            receipt=validate_tile(tile,target)
            self.assertEqual(receipt.size,target.stat().st_size)
            self.assertEqual(receipt.sha256,hashlib.sha256(target.read_bytes()).hexdigest())
            with rasterio.open(target) as src:
                self.assertEqual(src.read(1,masked=True)[0,0],0)
                self.assertTrue(src.read(1,masked=True).mask[1,0])
            fixture(path,dtype='float64')
            with self.assertRaisesRegex(ValueError,'schema'):
                validate_tile(tile,path)

    def test_auth_failures_are_not_retried_and_429_is_retryable(self):
        from app.download_transport import DownloadRPC, DownloadHTTPError
        for status, retryable in [(403,False),(401,False),(429,True),(503,True)]:
            response=mock.Mock(status_code=status,headers={})
            session=mock.Mock()
            session.request.return_value=response
            with mock.patch('ee.serializer.encode',return_value={}), \
                 DownloadRPC(lambda:mock.Mock(),'test',session_factory=lambda:session) as rpc:
                with self.assertRaises(DownloadHTTPError) as raised:
                    rpc.prepare_tile(mock.Mock(),{},deadline=time.monotonic()+1,check=lambda:None)
            self.assertEqual(raised.exception.retryable,retryable)
            self.assertEqual(session.request.call_count,1)
            response.close.assert_called_once()

    def test_attempt_fencing_cache_corruption_and_compression_independence(self):
        from app.download_store import SelectionStore, AttemptBusy, StaleAttempt
        from app.download_transport import plan_tiles,validate_tile,artifact_key
        from app.geo import Bbox
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root)
            tile=plan_tiles('selection',['B2'],Bbox(106,10.9998,106.0002,11),10)[0]
            self.assertNotEqual(artifact_key('selection',[tile],None,{'compress':'a'}),
                                artifact_key('selection',[tile],None,{'compress':'b'}))
            attempt=store.claim_attempt('artifact')
            with self.assertRaises(AttemptBusy): store.claim_attempt('artifact')
            path=Path(root)/'tile.tif'; fixture(path)
            receipt=validate_tile(tile,path)
            store.commit_tile(attempt,receipt)
            self.assertIsNotNone(store.get_verified_tile(tile.tile_key))
            path.write_bytes(b'broken')
            self.assertIsNone(store.get_verified_tile(tile.tile_key))
            store.release_attempt(attempt)
            new=store.claim_attempt('artifact')
            with self.assertRaises(StaleAttempt): store.commit_tile(attempt,receipt)
            store.release_attempt(new)

    def test_redownload_repairs_corrupt_content_addressed_tile(self):
        from app.download_store import SelectionStore
        from app.download_transport import plan_tiles,validate_tile,download_tiles
        from dataclasses import replace
        from app.geo import Bbox
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);directory=store.root/'tiles';directory.mkdir()
            tile=plan_tiles('selection',['B2'],Bbox(106,10.9998,106.0002,11),10)[0]
            path=directory/'original.tif';fixture(path);original=path.read_bytes()
            receipt=validate_tile(tile,path)
            target=directory/(receipt.sha256+'.tif');path.rename(target)
            receipt=replace(receipt,filename=str(target))
            attempt=store.claim_attempt('artifact');store.commit_tile(attempt,receipt)
            target.write_bytes(b'corrupted')
            def transfer(image,tile,destination,**kwargs):
                destination.write_bytes(original)
                return validate_tile(tile,destination)
            with mock.patch('app.download_transport.DownloadRPC'),mock.patch(
                    'app.download_transport.stream_tile_to_file',side_effect=transfer):
                result=download_tiles(object(),[tile],store,attempt,credentials_factory=lambda:None,
                                      project='test',base='https://earthengine.googleapis.com')
            self.assertEqual(Path(result[0].filename).read_bytes(),original)
            self.assertIsNotNone(store.get_verified_tile(tile.tile_key))
            store.release_attempt(attempt)

    def test_one_worker_reuses_its_session_and_closes_it_after_all_tiles(self):
        from app.download_store import SelectionStore
        from app.download_transport import plan_tiles,validate_tile,download_tiles
        from app.geo import Bbox
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);attempt=store.claim_attempt('artifact')
            tiles=plan_tiles('selection',['B2','B3'],Bbox(106,10.9998,106.0002,11),10)
            def transfer(image,tile,destination,**kwargs):
                fixture(destination);return validate_tile(tile,destination)
            with mock.patch('app.download_transport.DownloadRPC') as constructor,mock.patch(
                    'app.download_transport.stream_tile_to_file',side_effect=transfer):
                result=download_tiles(object(),tiles,store,attempt,credentials_factory=lambda:None,
                                      project='test',base='https://earthengine.googleapis.com',workers=1,backlog=2)
                self.assertEqual(len(result),2)
                self.assertEqual(constructor.call_count,1)
                constructor.return_value.close.assert_called_once()
            store.release_attempt(attempt)

    def test_stream_deadline_interrupts_a_trickling_response(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        import threading
        from app.download_transport import DownloadRPC
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(200);self.send_header('Content-Length','1000');self.end_headers()
                try:
                    for _ in range(100):
                        self.wfile.write(b'x'*10);self.wfile.flush();time.sleep(.02)
                except (OSError,ConnectionError): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with tempfile.TemporaryDirectory() as root, DownloadRPC(lambda:mock.Mock(),'test',
                    base=f'http://127.0.0.1:{server.server_port}') as rpc:
                started=time.monotonic()
                import requests
                with self.assertRaises((TimeoutError,requests.exceptions.ChunkedEncodingError,requests.ConnectionError)):
                    rpc.stream_pixels('projects/test/thumbnails/abc',Path(root)/'tile.part',
                                      deadline=time.monotonic()+.2,check=lambda:None)
                self.assertLess(time.monotonic()-started,.7,'continuous chunks cannot reset the whole tile deadline')
        finally:
            server.shutdown();server.server_close();thread.join(2)
