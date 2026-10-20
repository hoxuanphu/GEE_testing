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

from . import imagery, eodag_adapter
from .catalog_models import CatalogFailure, CatalogUnavailable


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
        return result

    def search(body):
        params = body.model_dump()
        if body.source != 'eodag':
            params.pop('provider', None)
            params.pop('eodag_collection', None)
        gee = body.dataset in ('sentinel2', 'landsat')
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
            main._ensure_ee()
            rectangle_args = ({field: getattr(body, field) for field in
                ('center_lon', 'center_lat', 'width_m', 'height_m')} if rectangle else {})
            roi, bbox, _ = main._resolve_selection(body.selection_mode, body.lon, body.lat,
                body.radius_m, body.province_id, body.commune_id, rotation_deg=body.rotation_deg,
                **rectangle_args)
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
        try:
            if gee:
                end = (dt.date.fromisoformat(body.end_date) + dt.timedelta(days=1)).isoformat()
                scenes = main.list_timeseries_scenes(body.dataset, body.lon or 0, body.lat or 0,
                    body.start_date, end, roi=roi, cloud_min=body.cloud_min, cloud_max=body.cloud_max)
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
            filtered = []
            for scene in scenes:
                cloud = scene.get('cloud_pct')
                if cloud is None or not math.isfinite(float(cloud)) or not body.cloud_min <= float(cloud) <= body.cloud_max:
                    continue
                row = dict(scene)
                row.pop('assets', None)
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
            created = store.create_search(params, filtered)
            return page(created['search_id'])
        except HTTPException:
            raise
        except CatalogUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except CatalogFailure as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            if body.source == 'eodag':
                raise HTTPException(502, 'EODAG provider search failed') from exc
            raise HTTPException(502, f'Unable to list scenes: {exc}') from exc

    def download(search_id: str, index: int):
        try:
            params, scenes = store._snapshot(search_id)
        except ValueError as exc:
            raise HTTPException(404, 'Search not found') from exc
        if index < 0 or index >= len(scenes):
            raise HTTPException(404, 'Scene not found')
        scene = scenes[index]
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
        if params['selection_mode'] == 'rectangle':
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
