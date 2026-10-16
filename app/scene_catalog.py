"""Cloud-filtered scene snapshots, separate from automatic best-scene selection."""
from __future__ import annotations

import datetime as dt
import math
import uuid
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import Field, model_validator

from . import imagery


def register_scene_catalog(api):
    # Import at registration time after main's request models and helpers exist.
    from . import main

    class CatalogRequest(main.SceneRequest):
        dataset: str = Field('sentinel2', pattern='^(sentinel2|landsat|s2|s2aws)$')
        source: str = Field('cdse', pattern='^(cdse|aws)$')
        cloud_min: float = Field(0, ge=0, le=100)
        cloud_max: float = Field(100, ge=0, le=100)

        @model_validator(mode='after')
        def valid_catalog(self):
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
            scene['download_url'] = f'/api/scene-catalog/{search_id}/download/{index}'
        return result

    def search(body):
        params = body.model_dump()
        gee = body.dataset in ('sentinel2', 'landsat')
        if gee:
            main._ensure_ee()
            roi, bbox, _ = main._resolve_selection(body.selection_mode, body.lon, body.lat,
                body.radius_m, body.province_id, body.commune_id, rotation_deg=body.rotation_deg)
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
        try:
            if gee:
                end = (dt.date.fromisoformat(body.end_date) + dt.timedelta(days=1)).isoformat()
                scenes = main.list_timeseries_scenes(body.dataset, body.lon or 0, body.lat or 0,
                    body.start_date, end, roi=roi, cloud_min=body.cloud_min, cloud_max=body.cloud_max)
                params['source'] = 'gee'
                truncated = False
            else:
                source = 'aws' if body.dataset == 's2aws' else body.source
                bounds = (bbox.west, bbox.south, bbox.east, bbox.north)
                cap = 4000 if source == 'aws' else 500
                fn = main.aws_search_s2_l2a_bbox if source == 'aws' else main.search_s2_area
                scenes = fn(bounds, body.start_date, body.end_date, max_results=cap,
                    cloud_min=body.cloud_min, cloud_max=body.cloud_max)
                params['source'] = source
                truncated = len(scenes) >= cap
            filtered = []
            for scene in scenes:
                cloud = scene.get('cloud_pct')
                if cloud is None or not math.isfinite(float(cloud)) or not body.cloud_min <= float(cloud) <= body.cloud_max:
                    continue
                row = dict(scene)
                row.pop('assets', None)
                row['cloud_pct'] = float(cloud)
                meta = row.get('source_metadata', {})
                row['platform'] = meta.get('SPACECRAFT_NAME') or meta.get('SPACECRAFT_ID') or (row.get('name') or row.get('id', '')).split('_')[0]
                filtered.append(row)
            filtered.sort(key=lambda row: (row.get('timestamp') or row.get('date') or '', row.get('asset_id') or row.get('id') or ''), reverse=True)
            params['truncated'] = truncated
            created = store.create_search(params, filtered)
            return page(created['search_id'])
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, f'Unable to list scenes: {exc}') from exc

    def download(search_id: str, index: int):
        try:
            params, scenes = store._snapshot(search_id)
        except ValueError as exc:
            raise HTTPException(404, 'Search not found') from exc
        if index < 0 or index >= len(scenes):
            raise HTTPException(404, 'Scene not found')
        scene = scenes[index]
        if params['source'] != 'gee':
            query = {'product_id': scene['id']}
            if params['selection_mode'] == 'point':
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
        roi, bbox, _ = main._resolve_selection(params['selection_mode'], params['lon'], params['lat'],
            params['radius_m'], params['province_id'], params['commune_id'], rotation_deg=params['rotation_deg'])
        image = imagery.load_timeseries_scene(scene['asset_id'], params['dataset'])
        scale = main._scale_for(params['dataset'])
        grid = main.build_rotated_grid(params['lon'], params['lat'], params['radius_m'], params['rotation_deg'], scale) if params['rotation_deg'] else None
        envelope = main.source_bbox(bbox, scale, params['lat']) if grid else bbox
        urls = main.download_geotiff(image, params['dataset'], envelope, scale, clip_region=None if grid else roi)
        out = main._DOWNLOADS_DIR / f'{uuid.uuid4().hex}.tif'
        raw = out.with_suffix('.source.tif') if grid else out
        try:
            main.merge_band_tiffs(urls, raw, preserve_masks=True)
            if grid:
                main.warp_to_grid(raw, out, grid)
        finally:
            if grid:
                raw.unlink(missing_ok=True)
        filename = f'{params["dataset"]}_{Path(scene["asset_id"]).name}.tif'
        return FileResponse(out, media_type='image/tiff', filename=filename)

    # Local Pydantic model is supplied explicitly so FastAPI resolves it without a forward reference.
    search.__annotations__['body'] = CatalogRequest
    api.post('/api/scene-catalog')(search)
    api.get('/api/scene-catalog/{search_id}')(page)
    api.get('/api/scene-catalog/{search_id}/download/{index}')(download)
    return store
