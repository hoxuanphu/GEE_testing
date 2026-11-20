"""Frozen Sentinel-2 export shared by foreground links and durable jobs."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import ExitStack
import copy
import json
import math
from pathlib import Path
import shutil
import time

import ee
import rasterio

from .download_metrics import DownloadMetrics
from .download_models import DownloadSettings, TileReceipt
from .download_snapshots import restore_selection, provider_scope
from .download_transport import (artifact_key, download_tiles, file_sha256, plan_tiles,
                                 raster_schema, verify_receipt)
from .export_resources import RESOURCES
from .geo import RectangleROI, point_roi, source_bbox
from .imagery import s2_export_bands, merge_local_band_tiffs
from .raster_grid import build_rotated_grid, warp_to_grid


def export_plan(snapshot, settings):
    request=snapshot.request
    if request['dataset']!='sentinel2' or request['selection_mode'] not in ('point','rectangle'):
        raise ValueError('unsupported_download_selection')
    scale=10
    if request['selection_mode']=='rectangle':
        roi=RectangleROI(**{key:request[key] for key in
                           ('center_lon','center_lat','width_m','height_m','rotation_deg')})
        bbox,grid,latitude=roi.bbox,roi.output_grid(scale),roi.center_lat
    else:
        _,bbox=point_roi(request['lon'],request['lat'],request['radius_m'],request['rotation_deg'])
        grid=(build_rotated_grid(request['lon'],request['lat'],request['radius_m'],request['rotation_deg'],scale)
              if request['rotation_deg']%360 else None)
        latitude=request['lat']
    if grid:
        bbox=source_bbox(bbox,scale,latitude)
    bands=s2_export_bands(request.get('s2_auxiliary_bands'), s2_band_preset=request.get('s2_band_preset'))
    tiles=plan_tiles(snapshot.selection_hash,bands,bbox,scale)
    key=artifact_key(snapshot.selection_hash,tiles,grid,settings.writer_options())
    # GEE's EPSG:4326 download grid is anchored at zero and covers each
    # requested region by floor/ceil at the nominal metre scale.
    step=scale/111319.49079327358
    west=math.floor(min(tile.region[0] for tile in tiles)/step)*step
    south=math.floor(min(tile.region[1] for tile in tiles)/step)*step
    east=math.ceil(max(tile.region[2] for tile in tiles)/step)*step
    north=math.ceil(max(tile.region[3] for tile in tiles)/step)*step
    expected_grid=grid or dict(crs='EPSG:4326',transform=[step,0,west,0,-step,north],
                               width=round((east-west)/step),height=round((north-south)/step))
    logical=(grid['width']*grid['height']*len(bands)*4 if grid else
             int((bbox.east-bbox.west)*111320/scale*(bbox.north-bbox.south)*111320/scale)*len(bands)*4)
    return dict(bands=bands,tiles=tiles,grid=grid,key=key,logical_bytes=logical,bbox=bbox,expected_grid=expected_grid)


def validate_artifact(path,plan,*,check=lambda:None):
    from affine import Affine
    from rasterio.crs import CRS
    with rasterio.open(path) as source:
        if (source.driver!='GTiff' or source.count!=len(plan['bands']) or
            list(source.descriptions)!=plan['bands'] or any(dtype!='float32' for dtype in source.dtypes)):
            raise ValueError('artifact_schema')
        grid=plan['expected_grid']
        if grid and (source.crs!=CRS.from_user_input(grid['crs']) or source.width!=grid['width'] or
                     source.height!=grid['height'] or not source.transform.almost_equals(Affine(*grid['transform']),precision=1e-9)):
            raise ValueError('artifact_grid')
        for band in range(1,source.count+1):
            for _,window in source.block_windows(band):
                check()
                source.read(band,window=window,masked=True)
        schema=raster_schema(source)
    return TileReceipt(plan['key'],str(Path(path).resolve()),file_sha256(path),Path(path).stat().st_size,schema)


def execute_selection(snapshot,store,*,settings=None,check=lambda:None,progress=lambda **kwargs:None,
                      metrics=None,job_context=None):
    settings=settings or DownloadSettings()
    metrics=metrics or DownloadMetrics('export-'+snapshot.selection_hash[:24])
    with metrics.phase('planning'):
        plan=export_plan(snapshot,settings)
    attempt=store.claim_attempt(plan['key'],snapshot.token,job_context=job_context)
    directory=store.root/'artifacts'
    directory.mkdir(exist_ok=True)
    destination=directory/(plan['key']+'.tif')
    work=directory/(attempt.owner+'.staging.tif')
    intermediate=directory/(attempt.owner+'.source.tif')
    last_heartbeat=0.
    def guarded_check():
        nonlocal last_heartbeat
        check()
        if time.monotonic()-last_heartbeat>30:
            store.heartbeat(attempt)
            last_heartbeat=time.monotonic()
    try:
        progress(stage='preparing',tiles_total=len(plan['tiles']),tiles_verified=0)
        # Completed output, or crash after rename before DB completion. Decode
        # and validate before recording/reusing; never trust only the filename.
        if destination.exists():
            with metrics.phase('artifact_validation'):
                expected=store.get_artifact_record(plan['key']) or store.get_publication_intent(plan['key'])
                valid=expected is not None and Path(expected.filename)==destination and verify_receipt(expected)
                if valid:
                    try:
                        receipt=validate_artifact(destination,plan,check=guarded_check)
                    except (ValueError,rasterio.errors.RasterioError):
                        valid=False
                if valid:
                    store.save_artifact(attempt,receipt)
                    return receipt
                store.invalidate_artifact(plan['key'])
        with ExitStack() as stack:
            with metrics.phase('queue_wait'):
                stack.enter_context(RESOURCES.raster_admission(guarded_check,
                    wait_deadline=float('inf') if job_context else None))
            guarded_check()
            use_vrt=settings.vrt and plan['grid'] is not None
            if shutil.disk_usage(store.root).free<settings.disk_reserve(plan['logical_bytes'],use_vrt):
                raise OSError('insufficient_disk_reserve')
            with metrics.phase('provider_readiness'):
                from .ee_session import init_ee
                init_ee()
                if snapshot.provider_scope!=provider_scope():
                    raise ValueError('provider_scope_mismatch')
                state=ee.data._get_state()
                credentials,project,base=state.credentials,state.cloud_api_user_project,state.cloud_api_base_url
                image=restore_selection(snapshot)
            progress(stage='downloading')
            with metrics.phase('download_tiles'):
                receipts=download_tiles(image,plan['tiles'],store,attempt,
                    credentials_factory=lambda:copy.copy(credentials),project=project,base=base,
                    check=guarded_check,metrics=metrics,workers=settings.workers,backlog=settings.backlog)
            progress(stage='assembling',tiles_verified=len(receipts),
                     verified_bytes=sum(receipt.size for receipt in receipts))
            groups=OrderedDict()
            for tile,receipt in zip(plan['tiles'],receipts):
                groups.setdefault(tile.band,[]).append(Path(receipt.filename))
            block_count=0
            last_block_report=time.monotonic()
            def on_block():
                nonlocal block_count,last_block_report
                block_count+=1
                if time.monotonic()-last_block_report>=1:
                    progress(blocks_written=block_count)
                    last_block_report=time.monotonic()
            with metrics.phase('assembly_compress'), rasterio.Env(GDAL_CACHEMAX=settings.gdal_cache_bytes,
                    GDAL_NUM_THREADS=str(settings.compression_threads)):
                if use_vrt:
                    from .raster_mosaic import assemble_to_grid
                    assemble_to_grid(groups,work,plan['grid'],check=guarded_check,metrics=metrics,on_block=on_block)
                else:
                    raw=intermediate if plan['grid'] else work
                    merge_local_band_tiffs(groups,raw,preserve_masks=True,check=guarded_check)
                    if plan['grid']:
                        warp_to_grid(raw,work,plan['grid'],check=guarded_check)
            if snapshot.scene.get('color_balance'):
                with rasterio.open(work,'r+') as source:
                    source.update_tags(COLOR_BALANCE=json.dumps(snapshot.scene['color_balance'],allow_nan=False),
                                       COMPOSITE_METHOD='raw_color_balanced')
            if snapshot.scene.get('scene_grouping'):
                from .scene_grouping import export_tags
                with rasterio.open(work, 'r+') as source:
                    source.update_tags(**export_tags(snapshot.scene))
            progress(stage='validating')
            if use_vrt:
                progress(blocks_written=block_count)
            with metrics.phase('artifact_validation'):
                receipt=validate_artifact(work,plan,check=guarded_check)
            guarded_check()
            progress(stage='publishing')
            # This attempt exclusively owns this immutable fingerprint. CAS is
            # checked inside publish_artifact before rename and DB completion.
            return store.publish_artifact(attempt,receipt,destination)
    finally:
        store.release_attempt(attempt)
        work.unlink(missing_ok=True)
        intermediate.unlink(missing_ok=True)
        try:
            metrics.write_report(store.root/'reports'/(attempt.owner+'.json'))
        except OSError:
            import logging
            logging.getLogger(__name__).warning('Cannot write download metrics report')
