"""Cloud-filtered scene snapshots, separate from automatic best-scene selection."""
from __future__ import annotations

import datetime as dt
from copy import deepcopy
from functools import lru_cache
import json
import logging
import math
import re
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import Field, model_validator
from starlette.background import BackgroundTask

from . import aws_stac, cdse, imagery, eodag_adapter, scene_thumbnails
from .catalog_models import CatalogFailure, CatalogUnavailable
from .export_resources import QueueTimeout
from .full_scene_export import FullSceneDiskSpaceError, export_full_scene
from .timeseries_metadata import search_failure
from .vector_import import VectorImportError, validate_geometry
from .vector_mask import mask_raster_to_polygon

logger = logging.getLogger(__name__)


def _scene_footprint(footprint):
    if not isinstance(footprint, dict) or footprint.get('type') not in ('Polygon', 'MultiPolygon'):
        return None
    geometry = {'type': footprint['type'], 'coordinates': footprint.get('coordinates')}
    try:
        validate_geometry(geometry)
    except VectorImportError:
        return None
    return geometry


def _has_thumbnail(parameters, scene):
    return (parameters['source'] == 'gee' and parameters['dataset'] in ('sentinel2', 'landsat')
            and bool(scene.get('asset_id')))


def _aws_scene_id(scene):
    identifier = scene.get('id')
    return isinstance(identifier, str) and re.fullmatch(r'S2[A-D]_\d{2}[A-Z]{3}_\d{8}_\d+_L2A', identifier)


def register_scene_catalog(api):
    # Import at registration time after main's request models and helpers exist.
    from . import main

    class CatalogRequest(main.SceneRequest):
        dataset: str = Field('sentinel2', pattern='^(sentinel2|landsat|s2|s2aws)$')
        source: str = Field('cdse', pattern='^(cdse|aws|eodag)$')
        provider: str | None = None
        eodag_collection: str | None = None
        cloud_min: float = Field(0, ge=0, le=100)
        cloud_max: float = Field(100, ge=0, le=100)

        @model_validator(mode='after')
        def valid_catalog(self):
            if self.source == 'eodag':
                if self.dataset != 's2':
                    raise ValueError('EODAG requires dataset=s2')
                eodag_adapter.validate_selection(self.provider, self.eodag_collection)
            elif self.provider is not None or self.eodag_collection is not None:
                raise ValueError('provider and eodag_collection require source=eodag')
            if self.selection_mode == 'rectangle':
                if self.province_id is not None or self.commune_id is not None:
                    raise ValueError('rectangle selection cannot include administrative IDs')
                if 'radius_m' in self.model_fields_set:
                    raise ValueError('rectangle selection uses width_m and height_m, not radius_m')
            if self.cloud_min > self.cloud_max:
                raise ValueError('cloud_min must not exceed cloud_max')
            if self.start_date > self.end_date or self.end_date == '9999-12-31':
                raise ValueError('Invalid date interval')
            return self

    store = main.TimeSeriesStore(main._DOWNLOADS_DIR / 'scene_catalog')

    def page(search_id: str, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 50:
            raise HTTPException(400, 'offset must be nonnegative and limit must be 1–50')
        result = store.get_search(search_id, offset, limit)
        if result is None:
            raise HTTPException(404, 'Search not found')
        for index, scene in enumerate(result['scenes'], start=offset):
            params = result['parameters']
            if params['source'] == 'eodag':
                scene.pop('download_url', None)
                scene['asset_keys'] = list(scene.pop('asset_names', []))
            else:
                if params['selection_mode'] == 'rectangle' and params['source'] in ('aws', 'cdse'):
                    scene['download_capabilities'] = {'status': 'supported'}
                scene['download_url'] = f'/api/scene-catalog/{search_id}/download/{index}'
                if _has_thumbnail(params, scene):
                    scene['thumbnail_url'] = f'/api/scene-catalog/{search_id}/thumbnail/{index}'
                    scene['full_scene_download_url'] = scene['download_url'] + '?extent=scene'
                elif params['source'] == 'cdse' and (url := cdse.thumbnail_path(scene.get('id'))):
                    scene['thumbnail_url'] = url
                elif params['source'] == 'aws':
                    public_url = aws_stac.thumbnail_url(scene.pop('browse_path', None))
                    if public_url:
                        scene['thumbnail_url'] = public_url
                    elif _aws_scene_id(scene):
                        scene['thumbnail_url'] = f'/api/scene-catalog/{search_id}/thumbnail/{index}'
        return result

    def search(body):
        started, request_id = time.monotonic(), uuid.uuid4().hex[:12]
        params = body.model_dump()
        if body.source != 'eodag':
            params.pop('provider', None)
            params.pop('eodag_collection', None)
        gee = body.dataset in ('sentinel2', 'landsat')
        if body.selection_mode == 'vector' and not gee:
            raise HTTPException(409, 'Vector ROI search/download is unsupported for external providers; use scene counts instead')
        rectangle = None
        if body.selection_mode == 'rectangle':
            rectangle = main._rectangle_model(body.center_lon, body.center_lat,
                body.width_m, body.height_m, body.rotation_deg)
            geometry, bbox = rectangle.model_geometry()
            params['roi_geometry'] = geometry
            params['rectangle'] = rectangle.model_dump()
            if gee:
                params['output_grid'] = rectangle.output_grid(main._scale_for(body.dataset))
        else:
            for field in ('center_lon', 'center_lat', 'width_m', 'height_m'):
                params.pop(field, None)
        if gee:
            try:
                main._ensure_ee()
            except Exception as exc:
                error = search_failure(exc, stage='initialization', request_id=request_id, started=started)
                raise HTTPException(error.status, error.message,
                                    headers={'X-Search-Request-ID': request_id}) from exc
            rectangle_args = ({field: getattr(body, field) for field in
                ('center_lon', 'center_lat', 'width_m', 'height_m')} if rectangle else {})
            vector_args = ({'vector_roi_id': body.vector_roi_id}
                           if body.selection_mode == 'vector' else {})
            roi, bbox, area = main._resolve_selection(body.selection_mode, body.lon, body.lat,
                body.radius_m, body.province_id, body.commune_id, rotation_deg=body.rotation_deg,
                **rectangle_args, **vector_args)
            if body.selection_mode == 'vector':
                params['roi_geometry'] = deepcopy(area.geometry)
            elif body.selection_mode in ('province', 'commune') and getattr(area, 'geometry', None):
                params['roi_geometry'] = area.geometry
        elif rectangle is not None:
            pass  # External providers search the envelope; the true polygon stays in the snapshot.
        elif body.selection_mode == 'point':
            if body.lon is None or body.lat is None:
                raise HTTPException(400, 'lon and lat are required')
            _, bbox = main.point_roi(body.lon, body.lat, body.radius_m, body.rotation_deg)
        else:
            getter = main.get_commune if body.selection_mode == 'commune' else main.get_province
            identifier = body.commune_id if body.selection_mode == 'commune' else body.province_id
            if not identifier:
                raise HTTPException(400, 'Administrative selection is required')
            area = getter(identifier)
            if area is None:
                raise HTTPException(404, 'Administrative selection not found')
            bbox = area.bbox
        params['bbox'] = vars(bbox)
        stage = 'metadata'
        try:
            if gee:
                end = (dt.date.fromisoformat(body.end_date) + dt.timedelta(days=1)).isoformat()
                scenes = main.list_timeseries_scenes(body.dataset, body.lon or 0, body.lat or 0,
                    body.start_date, end, roi=roi, cloud_min=body.cloud_min, cloud_max=body.cloud_max,
                    platform=body.platform, include_footprint=True)
                params['source'] = 'gee'
                truncated = False
            elif body.source == 'eodag':
                result = eodag_adapter.search((bbox.west, bbox.south, bbox.east, bbox.north),
                    body.start_date, body.end_date, provider=body.provider,
                    eodag_collection=body.eodag_collection, cloud_min=body.cloud_min,
                    cloud_max=body.cloud_max, directory=main._DOWNLOADS_DIR / 'eodag')
                scenes = result.scenes
                params.update(result.parameters)
                truncated = result.parameters['truncated']
            else:
                source = 'aws' if body.dataset == 's2aws' else body.source
                bounds = (bbox.west, bbox.south, bbox.east, bbox.north)
                cap = 4000 if source == 'aws' else 500
                fn = main.aws_search_s2_l2a_bbox if source == 'aws' else main.search_s2_area
                scenes = fn(bounds, body.start_date, body.end_date, max_results=cap,
                    cloud_min=body.cloud_min, cloud_max=body.cloud_max)
                params['source'] = source
                truncated = len(scenes) >= cap
            stage = 'validation'
            filtered = []
            for scene in scenes:
                cloud = scene.get('cloud_pct')
                if cloud is None or not math.isfinite(float(cloud)) or not body.cloud_min <= float(cloud) <= body.cloud_max:
                    continue
                row = dict(scene)
                row.pop('assets', None)
                if params['source'] == 'aws' and isinstance(row.get('thumbnail_url'), str):
                    href = row.pop('thumbnail_url')
                    path = urlsplit(href).path
                    if aws_stac.thumbnail_url(path) == href:
                        row['browse_path'] = path
                row['footprint'] = _scene_footprint(row.get('footprint'))
                row['cloud_pct'] = float(cloud)
                meta = row.get('source_metadata', {})
                if body.source == 'eodag':
                    # The shared sanitizer removes keys containing "key"; persist safe names instead.
                    row['asset_names'] = row.pop('asset_keys', [])
                    row['quicklook_available'] = False
                    row['download_capabilities'] = dict(eodag_adapter.CAPABILITY)
                else:
                    row['platform'] = meta.get('SPACECRAFT_NAME') or meta.get('SPACECRAFT_ID') or (row.get('name') or row.get('id', '')).split('_')[0]
                    if rectangle is not None and not gee:
                        row.pop('download_url', None)
                        row['download_capabilities'] = {'status': 'supported'}
                filtered.append(row)
            filtered.sort(key=lambda row: (row.get('timestamp') or row.get('date') or '', row.get('asset_id') or row.get('id') or ''), reverse=True)
            params['truncated'] = truncated
            stage = 'snapshot'
            created = store.create_search(params, filtered)
            return page(created['search_id'])
        except HTTPException:
            raise
        except CatalogUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except CatalogFailure as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            if gee:
                error = search_failure(exc, stage=stage, request_id=request_id, started=started)
                raise HTTPException(error.status, error.message,
                                    headers={'X-Search-Request-ID': request_id}) from exc
            if body.source == 'eodag':
                raise HTTPException(502, 'EODAG provider search failed') from exc
            raise HTTPException(502, f'Unable to list scenes: {exc}') from exc

    def snapshot_scene(search_id, index):
        try:
            params, scenes = store._snapshot(search_id)
        except ValueError as exc:
            raise HTTPException(404, 'Search not found') from exc
        if index < 0 or index >= len(scenes):
            raise HTTPException(404, 'Scene not found')
        scene = scenes[index]
        if params.get('platform') is not None:
            expected_platform = 'Sentinel-2' + params['platform']
            if params['dataset'] != 'sentinel2' or scene.get('platform') != expected_platform:
                raise HTTPException(409, 'Scene does not match the selected platform; search again')
        return params, scene

    @lru_cache(maxsize=96)
    def thumbnail_bytes(asset_id, dataset, region_json):
        return imagery.render_catalog_thumbnail(asset_id, dataset, region_json)

    def serve_thumbnail(search_id, index):
        params, scene = snapshot_scene(search_id, index)
        if params['source'] == 'aws' and _aws_scene_id(scene):
            row = aws_stac.get_item(scene['id'], timeout=8)
            if row and row.get('id') == scene['id'] and row.get('thumbnail_url'):
                return RedirectResponse(row['thumbnail_url'])
            raise HTTPException(404, 'Thumbnail unavailable for this scene')
        if not _has_thumbnail(params, scene):
            raise HTTPException(404, 'Thumbnail unavailable for this scene')
        try:
            region = params.get('roi_geometry')
            if region is None:
                if params['selection_mode'] == 'point':
                    region, _ = main.point_roi(params['lon'], params['lat'], params['radius_m'], params.get('rotation_deg', 0))
                else:  # Administrative snapshots saved before ROI geometry was included.
                    bbox = main.Bbox(**params['bbox'])
                    corners = [[bbox.west, bbox.south], [bbox.east, bbox.south],
                               [bbox.east, bbox.north], [bbox.west, bbox.north]]
                    region = {'type': 'Polygon', 'coordinates': [[*corners, corners[0]]]}
            main._ensure_ee()
            content = thumbnail_bytes(scene['asset_id'], params['dataset'], json.dumps(region, separators=(',', ':')))
        except Exception:
            raise HTTPException(502, 'Không tải được ảnh xem trước của cảnh.') from None
        return scene_thumbnails.response(content)

    async def thumbnail(search_id: str, index: int):
        return await scene_thumbnails.load(serve_thumbnail, search_id, index)

    def download(search_id: str, index: int, extent: Literal['roi', 'scene'] = 'roi'):
        params, scene = snapshot_scene(search_id, index)
        if extent == 'scene':
            if not _has_thumbnail(params, scene):
                raise HTTPException(409, 'Chế độ nguyên cảnh chỉ hỗ trợ Sentinel-2 và Landsat GEE.')
            main._ensure_ee()
            out = main._DOWNLOADS_DIR / f'{uuid.uuid4().hex}.tif'
            band_options = {key: params[key] for key in ('s2_auxiliary_bands', 's2_band_preset')
                            if params.get(key) is not None}
            try:
                export_full_scene(scene['asset_id'], params['dataset'], out, **band_options)
            except QueueTimeout:
                raise
            except FullSceneDiskSpaceError:
                raise HTTPException(507, 'Máy chủ không đủ dung lượng để tạo GeoTIFF nguyên cảnh.') from None
            except Exception as exc:
                out.unlink(missing_ok=True)
                logger.warning('Full scene export failed: dataset=%s error_type=%s',
                               params['dataset'], type(exc).__name__)
                raise HTTPException(502, 'Không tạo được GeoTIFF nguyên cảnh từ GEE. Hãy thử tải lại.') from None
            suffix = '_rgbnir' if params.get('s2_band_preset') == 'rgbnir' else ''
            filename = f'{params["dataset"]}_{Path(scene["asset_id"]).name}{suffix}_full_scene.tif'
            return FileResponse(out, media_type='image/tiff', filename=filename,
                                background=BackgroundTask(out.unlink, missing_ok=True))
        if params['selection_mode'] == 'vector' and params['source'] != 'gee':
            raise HTTPException(409, 'Vector ROI downloads are unsupported for external providers')
        if params['source'] == 'eodag':
            raise HTTPException(409, 'EODAG MVP is catalogue-only; downloads are unsupported')
        if params['source'] != 'gee':
            query = {'product_id': scene['id']}
            if params['selection_mode'] == 'rectangle':
                query.update(selection_mode='rectangle', **params['rectangle'])
            elif params['selection_mode'] == 'point':
                query.update({k: params[k] for k in ('lon', 'lat', 'radius_m', 'rotation_deg')})
            elif params['selection_mode'] == 'province':
                query['province_id'] = params['province_id']
            else:
                query['commune_id'] = params['commune_id']
            if params['source'] == 'aws':
                target = '/api/s2/aws/download'
            else:
                target = '/api/s2/bands'
                query.update(action='download', bands='B02,B03,B04,B08,B11,B12')
            return RedirectResponse(target + '?' + urlencode(query), status_code=303)
        main._ensure_ee()
        if params['selection_mode'] == 'vector':
            import ee
            roi = ee.Geometry(params['roi_geometry'], geodesic=False)
            bbox = main.Bbox(**params['bbox'])
            grid = None
            center_lat = (bbox.south + bbox.north) / 2
        elif params['selection_mode'] == 'rectangle':
            import ee
            roi = ee.Geometry.Polygon(params['roi_geometry']['coordinates'], geodesic=False)
            bbox = main.Bbox(**params['bbox'])
            grid = params['output_grid']
            center_lat = params['rectangle']['center_lat']
        else:
            roi, bbox, _ = main._resolve_selection(params['selection_mode'], params['lon'], params['lat'],
                params['radius_m'], params['province_id'], params['commune_id'], rotation_deg=params['rotation_deg'])
            scale = main._scale_for(params['dataset'])
            grid = main.build_rotated_grid(params['lon'], params['lat'], params['radius_m'], params['rotation_deg'], scale) if params['rotation_deg'] else None
            center_lat = params['lat']
        image = imagery.load_timeseries_scene(scene['asset_id'], params['dataset'])
        scale = main._scale_for(params['dataset'])
        envelope = main.source_bbox(bbox, scale, center_lat) if grid else bbox
        band_options = {key: params[key] for key in ('s2_auxiliary_bands', 's2_band_preset')
                        if params.get(key) is not None}
        urls = main.download_geotiff(image, params['dataset'], envelope, scale,
                                    clip_region=None if grid else roi, **band_options)
        out = main._DOWNLOADS_DIR / f'{uuid.uuid4().hex}.tif'
        raw = out.with_suffix('.source.tif') if grid else out
        try:
            main.merge_band_tiffs(urls, raw, preserve_masks=True)
            if grid:
                main.warp_to_grid(raw, out, grid)
            if params['selection_mode'] == 'vector':
                mask_raster_to_polygon(out, params['roi_geometry'])
        finally:
            if grid:
                raw.unlink(missing_ok=True)
        suffix = '_rgbnir' if params.get('s2_band_preset') == 'rgbnir' else ''
        filename = f'{params["dataset"]}_{Path(scene["asset_id"]).name}{suffix}.tif'
        return FileResponse(out, media_type='image/tiff', filename=filename)

    # Local Pydantic model is supplied explicitly so FastAPI resolves it without a forward reference.
    search.__annotations__['body'] = CatalogRequest
    api.post('/api/scene-catalog')(search)
    api.get('/api/scene-catalog/{search_id}')(page)
    api.get('/api/scene-catalog/{search_id}/thumbnail/{index}')(thumbnail)
    api.get('/api/scene-catalog/{search_id}/download/{index}')(download)
    return store
