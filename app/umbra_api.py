"""Umbra public catalogue, immutable selection, and asset routes."""
from datetime import date, timedelta
import re
from typing import Literal

from fastapi import HTTPException
from fastapi.responses import JSONResponse
from pydantic import ConfigDict, Field, model_validator
import requests
from shapely.errors import ShapelyError
from shapely.geometry import mapping

from . import scene_thumbnails, umbra_transfer
from .admin_boundaries import get_province
from .commune_boundaries import get_commune
from .geo import Bbox, MAX_RECTANGLE_DIMENSION_M, point_roi
from .timeseries import TimeSeriesStore
from .umbra_catalog import (ATTRIBUTION, LICENSE_URL, Catalog, CatalogNotReady,
                            Object, get_sidecar, normalize_geometry, object_url)
from .vector_api import vector_area


def register_umbra(api, directory, vector_store):
    from . import main

    class SearchRequest(main.PointSelectionRequest):
        model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
        selection_mode: Literal['point', 'rectangle', 'province', 'commune', 'vector'] = 'point'
        lon: float | None = Field(None, ge=-180, le=180)
        lat: float | None = Field(None, ge=-90, le=90)
        radius_m: float = Field(2000, gt=0, le=main._MAX_RADIUS_M)
        center_lon: float | None = Field(None, ge=-180, le=180)
        center_lat: float | None = Field(None, ge=-90, le=90)
        width_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
        height_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
        province_id: str | None = Field(None, min_length=1, max_length=20)
        commune_id: str | None = Field(None, min_length=1, max_length=20)
        start_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
        end_date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')

        @model_validator(mode='after')
        def validate_search(self):
            if date.fromisoformat(self.start_date) > date.fromisoformat(self.end_date) or self.end_date == '9999-12-31':
                raise ValueError('Invalid date interval')
            if (self.selection_mode == 'province') != (self.province_id is not None):
                raise ValueError('Use province_id only with province selection')
            if (self.selection_mode == 'commune') != (self.commune_id is not None):
                raise ValueError('Use commune_id only with commune selection')
            if self.selection_mode == 'point':
                if self.lon is None or self.lat is None:
                    raise ValueError('Point selection requires lon and lat')
            elif self.lon is not None or self.lat is not None or 'radius_m' in self.model_fields_set:
                raise ValueError('Point fields require point selection')
            return self

    catalog = Catalog(directory)
    store = TimeSeriesStore(directory / 'snapshots')
    base = '/api/umbra/search'

    def geometry_for(body):
        if body.selection_mode == 'point':
            return point_roi(body.lon, body.lat, body.radius_m, body.rotation_deg)[0]
        if body.selection_mode == 'rectangle':
            return main._rectangle_model(body.center_lon, body.center_lat,
                                         body.width_m, body.height_m, body.rotation_deg).geometry
        if body.selection_mode == 'vector':
            return vector_area(vector_store, body.vector_roi_id).geometry
        area = (get_province(body.province_id) if body.selection_mode == 'province'
                else get_commune(body.commune_id))
        if area is None:
            raise HTTPException(404, 'Không tìm thấy vùng hành chính đã chọn.')
        return area.geometry

    def page(search_id: str, offset: int = 0, limit: int = 50):
        if offset < 0 or not 1 <= limit <= 50:
            raise HTTPException(400, 'offset must be nonnegative and limit must be 1–50')
        result = store.get_search(search_id, offset, limit)
        if result is None:
            raise HTTPException(404, 'Kết quả tìm kiếm Umbra đã hết hạn hoặc không tồn tại.')
        for i, scene in enumerate(result['scenes'], offset):
            route = f'{base}/{search_id}/scenes/{i}'
            for field in ('object_path', 'etag', 'metadata_path', 'metadata_etag',
                          'preview_path', 'preview_etag'):
                scene.pop(field, None)
            scene.update(download_url=route + '/download', metadata_url=route + '/metadata',
                         thumbnail_url=route + '/thumbnail')
        return result

    def search(body: SearchRequest):
        try:
            roi = normalize_geometry(geometry_for(body))
            geometry = mapping(roi)
            stop = (date.fromisoformat(body.end_date) + timedelta(days=1)).isoformat()
            version, scenes = catalog.search(geometry, body.start_date, stop)
        except CatalogNotReady:
            raise HTTPException(503, 'Danh mục Umbra chưa sẵn sàng. Hãy tạo danh mục trước.') from None
        except (ShapelyError, TypeError, ValueError):
            raise HTTPException(422, 'Vùng tìm ảnh Umbra không hợp lệ.') from None
        status = catalog.status()
        bbox = Bbox(*roi.bounds)
        parameters = body.model_dump() | {
            'roi_geometry': geometry, 'bbox': vars(bbox), 'catalog_generation': version['generation'],
            'catalog_updated_at': version['updated_at'], 'catalog_skipped': version['skipped'],
            'catalog_state': status['state'],
        }
        created = store.create_search(parameters, scenes)
        return page(created['search_id'])

    def selected(search_id, index):
        scene = store.get_search_scene(search_id, index)
        if scene is not None:
            return scene
        raise HTTPException(404, 'Không tìm thấy cảnh Umbra trong kết quả tìm kiếm.')

    def download(search_id: str, index: int):
        return umbra_transfer.download(selected(search_id, index))

    def metadata(search_id: str, index: int):
        scene = selected(search_id, index)
        path, etag = scene['metadata_path'], scene['metadata_etag']
        original = catalog.metadata(path, etag)
        if original is None:
            try:
                original = get_sidecar(Object(path, 0, etag))
            except (OSError, requests.RequestException, ValueError):
                pass
        document = {name: scene[name] for name in (
            'id', 'item_id', 'collect_id', 'datetime', 'footprint', 'bbox', 'platform',
            'polarizations', 'filename', 'size_bytes', 'etag', 'resolution_azimuth_m',
            'resolution_range_m',
        )}
        document.update(product='GEC', download_source=object_url(scene['object_path']),
                        metadata_source=object_url(path), original_metadata=original,
                        attribution=ATTRIBUTION, license='CC-BY-4.0', license_url=LICENSE_URL)
        filename = re.sub(r'\.tiff?$', '', scene['filename'], flags=re.I) + '_umbra.json'
        return JSONResponse(document, headers={'Content-Disposition': umbra_transfer.attachment(filename),
                                                'X-Content-Type-Options': 'nosniff'})

    def thumbnail_bytes(search_id, index):
        scene = selected(search_id, index)
        return umbra_transfer.thumbnail(scene['object_path'], scene['etag'], scene['size_bytes'],
                                        scene.get('preview_path'), scene.get('preview_etag'))

    async def thumbnail(search_id: str, index: int):
        try:
            jpeg = await scene_thumbnails.load(thumbnail_bytes, search_id, index)
        except (OSError, ValueError, requests.RequestException):
            raise HTTPException(404, 'Ảnh xem trước Umbra không khả dụng.') from None
        return scene_thumbnails.response(jpeg, 'image/jpeg')

    api.get('/api/umbra/catalog')(catalog.status)
    api.post('/api/umbra/catalog/refresh', status_code=202)(catalog.start)
    api.post(base)(search)
    api.get(base + '/{search_id}')(page)
    api.get(base + '/{search_id}/scenes/{index}/download')(download)
    api.get(base + '/{search_id}/scenes/{index}/metadata')(metadata)
    api.get(base + '/{search_id}/scenes/{index}/thumbnail')(thumbnail)
    return catalog, store
