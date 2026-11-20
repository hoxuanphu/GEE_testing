"""Individual Sentinel-5P OFFL L3 scenes; no temporal aggregation or unit conversion."""
from __future__ import annotations

import datetime as dt
from copy import deepcopy
import re
import uuid

import ee
from fastapi import HTTPException
from fastapi.responses import FileResponse
from pydantic import ConfigDict, Field, model_validator
from starlette.background import BackgroundTask
from typing import Literal
from .vector_mask import mask_raster_to_polygon

SCALE_M = 1113.2
PRODUCTS = {
    'NO2': ('NO2', 'tropospheric_NO2_column_number_density', 'mol/m²', 0, 0.0002),
    'SO2': ('SO2', 'SO2_column_number_density', 'mol/m²', 0, 0.0005),
    'CO': ('CO', 'CO_column_number_density', 'mol/m²', 0, 0.05),
    'CH4': ('CH4', 'CH4_column_volume_mixing_ratio_dry_air', 'ppb', 1750, 1900),
    'O3': ('O3', 'O3_column_number_density', 'mol/m²', 0.12, 0.15),
    'HCHO': ('HCHO', 'tropospheric_HCHO_column_number_density', 'mol/m²', 0, 0.0003),
    'AAI': ('AER_AI', 'absorbing_aerosol_index', 'dimensionless', -1, 2),
}
WARNING = ('L3 grid ~1.1 km is not native sensor resolution. Column quantities / CH4 dry-air '
           'mixing ratio / absorbing aerosol index are not surface concentrations or PM2.5. '
           'Catalog quality masks are retained; a scene may have no valid pixels in the ROI.')


def product_metadata(product):
    suffix, band, unit, low, high = PRODUCTS[product]
    return dict(product=product, collection=f'COPERNICUS/S5P/OFFL/L3_{suffix}',
                band=band, unit=unit, scale_m=SCALE_M, warning=WARNING,
                visualization={'min': low, 'max': high, 'palette': ['000080', '00ffff', 'ffff00', 'ff0000']})


def list_scenes(product, roi, start, end, limit=2000):
    """Read each asset's ID/time, capped explicitly (end exclusive)."""
    meta = product_metadata(product)
    collection = (ee.ImageCollection(meta['collection']).filterBounds(roi)
                  .filterDate(start, end).sort('system:time_start'))
    images = collection.limit(limit + 1).toList(limit + 1)
    rows = images.map(lambda obj: ee.Image(obj).toDictionary(
        ['system:index', 'system:time_start'])).getInfo()
    scenes = []
    for row in rows[:limit]:
        index = row['system:index']
        if not re.fullmatch(r'[A-Za-z0-9_-]+', index):
            raise ValueError('Invalid Sentinel-5P scene index')
        timestamp = dt.datetime.fromtimestamp(row['system:time_start'] / 1000, dt.timezone.utc)
        scenes.append(dict(scene_id=index, asset_id=f"{meta['collection']}/{index}",
                           timestamp=timestamp.isoformat().replace('+00:00', 'Z'),
                           date=timestamp.date().isoformat()))
    return scenes, len(rows) > limit


def register_s5p(api):
    from . import main

    class SearchRequest(main.PointSelectionRequest):
        model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
        product: Literal['NO2', 'SO2', 'CO', 'CH4', 'O3', 'HCHO', 'AAI'] = 'NO2'
        selection_mode: Literal['point', 'rectangle', 'province', 'commune', 'vector'] = 'point'
        lon: float | None = Field(None, ge=-180, le=180)
        lat: float | None = Field(None, ge=-90, le=90)
        radius_m: float = Field(2000, gt=0, le=main._MAX_RADIUS_M)
        center_lon: float | None = Field(None, ge=-180, le=180)
        center_lat: float | None = Field(None, ge=-90, le=90)
        width_m: float | None = Field(None, gt=0, le=main.MAX_RECTANGLE_DIMENSION_M)
        height_m: float | None = Field(None, gt=0, le=main.MAX_RECTANGLE_DIMENSION_M)
        province_id: str | None = Field(None, min_length=1, max_length=20)
        commune_id: str | None = Field(None, min_length=1, max_length=20)
        start_date: str
        end_date: str

        @model_validator(mode='after')
        def valid_search(self):
            for value in (self.start_date, self.end_date):
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    raise ValueError('Dates must use YYYY-MM-DD')
                dt.date.fromisoformat(value)
            if self.start_date > self.end_date or self.end_date == '9999-12-31':
                raise ValueError('Invalid date interval')
            if self.selection_mode == 'point' and (self.lon is None or self.lat is None):
                raise ValueError('Point selection requires lon and lat')
            if self.selection_mode == 'province' and not self.province_id:
                raise ValueError('Province selection requires province_id')
            if self.selection_mode == 'commune' and not self.commune_id:
                raise ValueError('Commune selection requires commune_id')
            if self.selection_mode != 'province' and self.province_id is not None:
                raise ValueError('province_id requires province selection')
            if self.selection_mode != 'commune' and self.commune_id is not None:
                raise ValueError('commune_id requires commune selection')
            if self.selection_mode != 'point' and (self.lon is not None or self.lat is not None):
                raise ValueError('lon/lat require point selection')
            if self.selection_mode == 'rectangle' and 'radius_m' in self.model_fields_set:
                raise ValueError('Rectangle uses width_m and height_m')
            return self

    store = main.TimeSeriesStore(main._DOWNLOADS_DIR / 's5p')

    def resolve(params):
        keys = ('selection_mode', 'lon', 'lat', 'radius_m', 'province_id', 'commune_id',
                'rotation_deg', 'center_lon', 'center_lat', 'width_m', 'height_m')
        kwargs = {key: params.get(key) for key in keys}
        if params.get('vector_roi_id') is not None:
            kwargs['vector_roi_id'] = params['vector_roi_id']
        return main._resolve_selection(**kwargs)

    def page(search_id: str, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 50:
            raise HTTPException(400, 'offset must be nonnegative; limit must be 1–50')
        result = store.get_search(search_id, offset, limit)
        if result is None:
            raise HTTPException(404, 'Search not found')
        for index, scene in enumerate(result['scenes'], offset):
            base = f'/api/s5p/search/{search_id}/scenes/{index}'
            scene.update(download_url=base + '/download', preview_url=base + '/preview')
        return result

    def search(body: SearchRequest):
        main._ensure_ee()
        params = body.model_dump()
        roi, bbox, area = resolve(params)
        if body.selection_mode == 'vector':
            params['roi_geometry'] = deepcopy(area.geometry)
        try:
            end = (dt.date.fromisoformat(body.end_date) + dt.timedelta(days=1)).isoformat()
            scenes, truncated = list_scenes(body.product, roi, body.start_date, end)
        except Exception as exc:
            raise HTTPException(502, 'Sentinel-5P search failed; retry or narrow the date range') from exc
        params.update(product_metadata(body.product), bbox=vars(bbox), truncated=truncated)
        # Persist the exact trusted geometry, so a later download cannot change the selection.
        try:
            if body.selection_mode != 'vector':
                params['roi_geometry'] = roi.getInfo()
        except Exception as exc:
            raise HTTPException(502, 'Sentinel-5P ROI evaluation failed') from exc
        if body.selection_mode == 'rectangle':
            params['output_grid'] = main._rectangle_model(body.center_lon, body.center_lat,
                body.width_m, body.height_m, body.rotation_deg).output_grid(SCALE_M)
        elif body.selection_mode == 'point' and body.rotation_deg:
            params['output_grid'] = main.build_rotated_grid(body.lon, body.lat, body.radius_m,
                body.rotation_deg, SCALE_M)
        created = store.create_search(params, scenes)
        return page(created['search_id'])

    def selected(search_id, index):
        if index < 0:
            raise HTTPException(404, 'Scene not found')
        result = page(search_id, index, 1)
        if not result['scenes']:
            raise HTTPException(404, 'Scene not found')
        main._ensure_ee()
        params, scene = result['parameters'], result['scenes'][0]
        meta = product_metadata(params['product'])
        if scene['asset_id'] != f"{meta['collection']}/{scene['scene_id']}":
            raise HTTPException(400, 'Scene does not belong to the selected product')
        try:
            roi = ee.Geometry(params['roi_geometry'], **({'geodesic': False}
                              if params['selection_mode'] == 'vector' else {}))
            image = ee.Image(scene['asset_id']).select(meta['band']).toFloat().clip(roi)
        except Exception as exc:
            raise HTTPException(502, 'Sentinel-5P scene unavailable') from exc
        return params, scene, image, roi

    def preview(search_id: str, index: int):
        params, scene, image, _ = selected(search_id, index)
        try:
            url = image.getThumbURL({**params['visualization'], 'region': params['roi_geometry'],
                                     'dimensions': 768, 'format': 'png'})
        except Exception as exc:
            raise HTTPException(502, 'Sentinel-5P preview unavailable') from exc
        return dict(url=url, timestamp=scene['timestamp'], unit=params['unit'], warning=WARNING)

    def download(search_id: str, index: int):
        params, scene, image, roi = selected(search_id, index)
        grid = params.get('output_grid')
        bbox = main.Bbox(**params['bbox'])
        if grid:
            center_lat = params.get('center_lat') if params['selection_mode'] == 'rectangle' else params['lat']
            bbox = main.source_bbox(bbox, SCALE_M, center_lat)
        out = main._DOWNLOADS_DIR / f'{uuid.uuid4().hex}.tif'
        raw = out.with_suffix('.source.tif') if grid else out
        try:
            urls = main.download_geotiff(image, 's5p_' + params['product'], bbox, SCALE_M, roi)
            main.merge_band_tiffs(urls, raw, preserve_masks=True)
            if grid:
                main.warp_to_grid(raw, out, grid)
            if params['selection_mode'] == 'vector':
                mask_raster_to_polygon(out, params['roi_geometry'])
        except Exception as exc:
            out.unlink(missing_ok=True)
            raise HTTPException(502, 'Sentinel-5P GeoTIFF download failed; retry or select a smaller ROI') from exc
        finally:
            if grid:
                raw.unlink(missing_ok=True)
        filename = f"S5P_{params['product']}_{scene['scene_id']}_{search_id[:8]}.tif"
        return FileResponse(out, media_type='image/tiff', filename=filename,
                            background=BackgroundTask(out.unlink, missing_ok=True))

    search.__annotations__['body'] = SearchRequest
    api.add_api_route('/api/s5p/products', lambda: list(map(product_metadata, PRODUCTS)), methods=['GET'])
    api.add_api_route('/api/s5p/search', search, methods=['POST'])
    api.add_api_route('/api/s5p/search/{search_id}', page, methods=['GET'])
    api.add_api_route('/api/s5p/search/{search_id}/scenes/{index}/preview', preview, methods=['GET'])
    api.add_api_route('/api/s5p/search/{search_id}/scenes/{index}/download', download, methods=['GET'])
    return store
