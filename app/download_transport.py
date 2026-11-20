"""Isolated REST download with one retry budget, disk streaming and tile receipts."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from dataclasses import asdict, replace
import copy
import hashlib
import json
import math
from pathlib import Path
import random
import re
import time

import ee
import requests
from google.auth.transport.requests import Request
from google.auth.exceptions import GoogleAuthError,TransportError
import rasterio

from .download_models import TileSpec, TileReceipt, digest, LAYOUT_VERSION

CHUNK_BYTES = 1024 * 1024
MAX_WIRE_BYTES = 32 * 1024 * 1024


class DownloadHTTPError(RuntimeError):
    def __init__(self, status, retry_after=0):
        self.retryable = status in (408,429) or status >= 500
        self.retry_after = retry_after
        self.status = status
        super().__init__(f'GEE HTTP {status}')


class InvalidTile(ValueError):
    pass


class DownloadAuthError(RuntimeError):
    def __init__(self,*,retryable=False):
        self.retryable=retryable
        super().__init__('gee_auth_failed')


def remaining(deadline):
    value = deadline-time.monotonic()
    if value <= 0:
        raise TimeoutError('tile_deadline')
    return value


class BoundedAuthRequest(Request):
    def __init__(self, session, deadline, check):
        super().__init__(session=session)
        self.deadline, self.check, self.calls = deadline, check, 0

    def __call__(self, *args, **kwargs):
        self.check()
        if self.calls:
            raise DownloadAuthError(retryable=True)
        self.calls += 1
        kwargs['timeout'] = min(15., remaining(self.deadline))
        return super().__call__(*args, **kwargs)


class DownloadRPC:
    """Constructor is network-free. Each worker owns credentials and sessions."""
    def __init__(self, credentials_factory, project, *, base='https://earthengine.googleapis.com',
                 session_factory=requests.Session):
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+', project):
            raise ValueError('invalid_project')
        self.base, self.project = base.rstrip('/'), project
        self.credentials = credentials_factory()
        self.session, self.auth_session = session_factory(), requests.Session()
        # requests default is zero retries; explicitly retain it on both sessions.
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.session.mount('https://', adapter)
        self.auth_session.mount('https://', requests.adapters.HTTPAdapter(max_retries=0))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.session.close()
        self.auth_session.close()

    @contextmanager
    def response(self, method, resource, *, deadline, check, body=None):
        check()
        url = self.base + '/v1/' + resource
        headers = {}
        try:
            self.credentials.before_request(BoundedAuthRequest(self.auth_session, deadline, check),
                                            method, url, headers)
        except GoogleAuthError as exc:
            raise DownloadAuthError(retryable=isinstance(exc,TransportError) or
                                    bool(getattr(exc,'retryable',False))) from None
        check()
        response = self.session.request(method, url, json=body, headers=headers, stream=True,
            timeout=(min(15.,remaining(deadline)),min(180.,remaining(deadline))), allow_redirects=False)
        # A socket read timeout resets when bytes trickle in. Close the actual
        # socket at the absolute tile deadline even if iter_content has not yet
        # produced its first 1MiB chunk. No SDK/global session is changed.
        import threading,socket
        def abort_stream():
            try:
                connection=getattr(response.raw,'_connection',None)
                sock=getattr(connection,'sock',None)
                if sock is None:
                    sock=response.raw._fp.fp.raw._sock
                sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError,OSError):
                pass
        timer=None
        try:
            timer=threading.Timer(remaining(deadline),abort_stream)
            timer.daemon=True
            timer.start()
            check()
            remaining(deadline)
            if response.status_code >= 300:
                try:
                    delay = max(0., float(response.headers.get('Retry-After',0)))
                except ValueError:
                    delay = 0
                raise DownloadHTTPError(response.status_code, delay)
            yield response
        finally:
            if timer is not None:
                timer.cancel()
            response.close()

    def prepare_tile(self, image, params, *, deadline, check):
        graph = image._build_download_id_image(params)
        body = dict(expression=ee.serializer.encode(graph,for_cloud_api=True),fileFormat='GEO_TIFF')
        with self.response('POST','projects/'+self.project+'/thumbnails',deadline=deadline,check=check,body=body) as response:
            raw = bytearray()
            for chunk in response.iter_content(8192):
                check(); remaining(deadline)
                raw.extend(chunk)
                if len(raw)>65536:
                    raise InvalidTile('invalid_resource_response')
        try:
            name=json.loads(raw)['name']
            if not re.fullmatch(r'projects/'+re.escape(self.project)+r'/thumbnails/[A-Za-z0-9_-]+',name):
                raise ValueError()
        except (ValueError,KeyError,TypeError):
            raise InvalidTile('invalid_resource_response') from None
        return name

    def stream_pixels(self, resource_name, destination, *, deadline, check, metrics=None, tile_id='tile', attempt=1):
        if not re.fullmatch(r'projects/'+re.escape(self.project)+r'/thumbnails/[A-Za-z0-9_-]+',resource_name):
            raise ValueError('invalid_resource')
        size=0
        started=time.perf_counter()
        with self.response('GET',resource_name+':getPixels',deadline=deadline,check=check) as response:
            with Path(destination).open('wb') as handle:
                for chunk in response.iter_content(CHUNK_BYTES):
                    check(); remaining(deadline)
                    if not chunk:
                        continue
                    if size==0 and metrics:
                        metrics.record_tile(tile_id,event='ttfb',elapsed_s=time.perf_counter()-started,attempt=attempt)
                    size+=len(chunk)
                    if size>MAX_WIRE_BYTES:
                        raise InvalidTile('tile_too_large')
                    handle.write(chunk)
                    if metrics:
                        metrics.record_tile(tile_id,event='received',bytes_count=len(chunk),attempt=attempt)
        remaining(deadline)
        return size


def plan_tiles(selection_hash, bands, bbox, scale):
    """Same initial WGS84 geometry and ordering as legacy; no provider RPC."""
    from .imagery import _tile_bbox
    approximate=(bbox.east-bbox.west)*111320/scale*(bbox.north-bbox.south)*111320/scale
    n=max(1,math.ceil(math.sqrt(approximate/8_000_000)))
    result=[]
    for band in bands:
        for i in range(n):
            for j in range(n):
                box=_tile_bbox(bbox,n,i,j) if n>1 else bbox
                region=(box.west,box.south,box.east,box.north)
                key=digest(dict(selection_hash=selection_hash,band=band,region=region,scale=scale,
                                crs='EPSG:4326',layout=LAYOUT_VERSION,wire='float32-nodata-9999'))
                result.append(TileSpec(key,selection_hash,band,region,scale))
    return result


def artifact_key(selection_hash, tiles, grid, writer_options):
    return digest(dict(selection_hash=selection_hash,tiles=[tile.tile_key for tile in tiles],
                       grid=grid,writer=writer_options))


def file_sha256(path):
    result=hashlib.sha256()
    with Path(path).open('rb') as handle:
        while chunk:=handle.read(CHUNK_BYTES):
            result.update(chunk)
    return result.hexdigest()


def raster_schema(source):
    return dict(crs=source.crs.to_wkt(),transform=list(source.transform)[:6],width=source.width,
                height=source.height,count=source.count,dtypes=list(source.dtypes),
                nodata=[str(value) if value is not None and not math.isfinite(value) else value for value in source.nodatavals],
                descriptions=list(source.descriptions))


def normalize_wire_tile(path, *, check=lambda:None):
    """GEE tags Float32 TIFF NoData as -inf even after unmask(-9999).

    The wire sentinel and nonfinite values are missing data; zeros remain valid.
    Normalize only the private partial, before hashing/publication/checkpointing.
    """
    import numpy as np
    with rasterio.open(path,'r+') as source:
        if source.count!=1 or source.dtypes!=('float32',):
            raise InvalidTile('tile_schema')
        if source.nodata is not None and math.isfinite(source.nodata) and source.nodata!=-9999:
            raise InvalidTile('tile_nodata_schema')
        source.nodata=-9999
        for _,window in source.block_windows(1):
            check()
            values=source.read(1,window=window)
            invalid=~np.isfinite(values)
            if invalid.any():
                values[invalid]=-9999
                source.write(values,1,window=window)


def validate_tile(tile, path, *, check=lambda:None):
    path=Path(path)
    with path.open('rb') as handle:
        if handle.read(4) not in (b'II*\x00',b'MM\x00*',b'II+\x00',b'MM\x00+'):
            raise InvalidTile('invalid_tiff_header')
    with rasterio.open(path) as source:
        from rasterio.crs import CRS
        if (source.driver!='GTiff' or source.crs!=CRS.from_user_input(tile.crs) or source.count!=1
                or source.dtypes!=('float32',) or source.width*source.height>tile.max_pixels
                or source.transform.b or source.transform.d or source.transform.a<=0 or source.transform.e>=0
                or (source.descriptions[0] is not None and source.descriptions[0]!=tile.band)):
            raise InvalidTile('tile_schema')
        # GEE rounds scale/region to its native WGS84 grid. Accept one pixel,
        # but never accept another region, projection, resolution or layout.
        west,south,east,north=tile.region
        tolerance=max(source.res)*2
        if (max(abs(a-b) for a,b in zip(source.bounds,(west,south,east,north)))>tolerance
                or abs(source.res[0]-tile.scale/111319.49079327358)>tile.scale/111319.49079327358*.02
                or abs(source.res[1]-source.res[0])>source.res[0]*.02):
            raise InvalidTile('tile_grid_schema')
        # Decode every block, including masks; no full-band allocations.
        for _,window in source.block_windows(1):
            check()
            source.read(1,window=window,masked=True)
        schema=raster_schema(source)
    return TileReceipt(tile.tile_key,str(path.resolve()),file_sha256(path),path.stat().st_size,schema)


def verify_receipt(receipt):
    try:
        path=Path(receipt.filename)
        if path.stat().st_size!=receipt.size or file_sha256(path)!=receipt.sha256:
            return False
        with rasterio.open(path) as source:
            return raster_schema(source)==receipt.schema
    except (OSError,rasterio.errors.RasterioError,ValueError):
        return False


def stream_tile_to_file(image,tile,destination,*,rpc,deadline,check,metrics=None):
    destination=Path(destination)
    params=dict(name='s2_'+tile.band,scale=tile.scale,crs=tile.crs,
        region=ee.Geometry.Rectangle(list(tile.region)),format='GEO_TIFF',dtype='float',
        filePerBand=False,formatOptions={'noData':-9999})
    graph=image.select([tile.band]).multiply(1.).toFloat().unmask(-9999,sameFootprint=False).toFloat()
    def tile_check():
        check()
        remaining(deadline)
    try:
        for attempt in range(1,4):
            check(); remaining(deadline)
            try:
                start=time.perf_counter()
                resource=rpc.prepare_tile(graph,params,deadline=deadline,check=tile_check)
                if metrics:
                    metrics.record_tile(tile.tile_key,event='create_resource',elapsed_s=time.perf_counter()-start,attempt=attempt)
                rpc.stream_pixels(resource,destination,deadline=deadline,check=tile_check,metrics=metrics,
                                  tile_id=tile.tile_key,attempt=attempt)
                normalize_wire_tile(destination,check=tile_check)
                receipt=validate_tile(tile,destination,check=tile_check)
                tile_check()
                if metrics:
                    metrics.record_tile(tile.tile_key,event='verified',bytes_count=receipt.size,attempt=attempt)
                return receipt
            except (DownloadHTTPError,DownloadAuthError,InvalidTile,rasterio.errors.RasterioError,requests.Timeout,requests.ConnectionError,
                    requests.exceptions.ChunkedEncodingError) as exc:
                destination.unlink(missing_ok=True)
                if (isinstance(exc,(DownloadHTTPError,DownloadAuthError)) and not exc.retryable) or attempt==3:
                    raise
                delay=max(getattr(exc,'retry_after',0),attempt+random.random())
                if metrics:
                    metrics.record_tile(tile.tile_key,event='retry',attempt=attempt)
                until=time.monotonic()+min(delay,remaining(deadline))
                while time.monotonic()<until:
                    check(); remaining(deadline)
                    time.sleep(min(.1,max(0,until-time.monotonic())))
    finally:
        # This is only an unverified, attempt-local partial. Verified cache survives.
        if destination.exists() and 'receipt' not in locals():
            destination.unlink(missing_ok=True)


def download_tiles(image,tiles,store,attempt,*,credentials_factory,project,base,
                   check=lambda:None,metrics=None,workers=6,backlog=12):
    """Bound both threads and pending futures; receipts keep original tile order."""
    if not 1<=workers<=6 or not workers<=backlog<=12:
        raise ValueError('invalid_download_limits')
    directory=store.root/'tiles'
    directory.mkdir(exist_ok=True)
    output={}
    import threading
    stopped=threading.Event()
    last_heartbeat=[0.]
    heartbeat_lock=threading.Lock()
    worker_local=threading.local()
    sessions=[]
    sessions_lock=threading.Lock()
    def guarded_check():
        if stopped.is_set():
            raise InterruptedError('tile_pool_stopped')
        check()
        with heartbeat_lock:
            if time.monotonic()-last_heartbeat[0]>30:
                store.heartbeat(attempt)
                last_heartbeat[0]=time.monotonic()
    def work(tile):
        guarded_check()
        receipt=store.get_verified_tile(tile.tile_key)
        if receipt:
            store.commit_tile(attempt,receipt)
            if metrics:
                metrics.record_tile(tile.tile_key,event='cache_hit',bytes_count=receipt.size)
            return receipt
        partial=directory/(attempt.owner+'-'+tile.tile_key+'.part')
        try:
            if not hasattr(worker_local,'rpc'):
                worker_local.rpc=DownloadRPC(credentials_factory,project,base=base)
                with sessions_lock:
                    sessions.append(worker_local.rpc)
            receipt=stream_tile_to_file(image,tile,partial,rpc=worker_local.rpc,deadline=time.monotonic()+900,
                                       check=guarded_check,metrics=metrics)
            guarded_check()
            target=directory/(receipt.sha256+'.tif')
            # Identical bytes may already exist; no mutable shared filename.
            if not target.exists() or not verify_receipt(replace(receipt,filename=str(target))):
                partial.replace(target)
            else:
                partial.unlink(missing_ok=True)
            receipt=replace(receipt,filename=str(target))
            store.commit_tile(attempt,receipt)
            return receipt
        finally:
            partial.unlink(missing_ok=True)
    iterator=iter(tiles)
    pending={}
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                while True:
                    guarded_check()
                    while len(pending)<backlog:
                        tile=next(iterator,None)
                        if tile is None:
                            break
                        pending[pool.submit(work,tile)]=tile
                    if not pending:
                        break
                    done,_=wait(pending,timeout=.1,return_when=FIRST_COMPLETED)
                    for future in done:
                        tile=pending.pop(future)
                        output[tile.tile_key]=future.result()
            finally:
                stopped.set()
                for future in pending:
                    future.cancel()
    finally:
        # The pool has joined; no worker is using these isolated sessions now.
        for rpc in sessions:
            rpc.close()
    return [output[tile.tile_key] for tile in tiles]
