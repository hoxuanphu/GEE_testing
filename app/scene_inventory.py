"""Count metadata scenes and export names/dates from the same immutable snapshot."""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import re
from typing import Literal

import requests
from fastapi import HTTPException
from fastapi.responses import Response
from pydantic import ConfigDict, Field, field_validator, model_validator

from . import inventory_sources as sources
from .frame_models import FrameError
from .geo import MAX_GEOGRAPHIC_LAT, MAX_RECTANGLE_DIMENSION_M, point_roi
from .platforms import validate_platform
from .reference_frame import reference_footprint

CSV_COLUMNS = ('name', 'date', 'timestamp', 'asset_id', 'source', 'cloud_pct')
GEE_DATASETS = {'sentinel2', 'landsat', 's1gee', 'nicfi', 'dynamic_world', 's5p'}
logger = logging.getLogger(__name__)


def _csv_cell(value):
    # Names originate at providers. Keep spreadsheets from interpreting a name as a formula.
    if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
        return "'" + value
    return value


def register_scene_inventory(api):
    from . import main

    class InventoryRequest(main.PointSelectionRequest):
        model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
        dataset: Literal['sentinel2', 'landsat', 's1gee', 'nicfi', 'dynamic_world', 's5p',
                         's2', 's2aws', 's1', 's1slc', 's3']
        selection_mode: Literal['point', 'rectangle', 'province', 'commune', 'reference', 'vector'] = 'point'
        source: Literal['cdse', 'aws'] = 'cdse'
        lon: float | None = Field(None, ge=-180, le=180)
        lat: float | None = Field(None, ge=-MAX_GEOGRAPHIC_LAT, le=MAX_GEOGRAPHIC_LAT)
        radius_m: float = Field(2000, gt=0, le=main._MAX_RADIUS_M)
        center_lon: float | None = Field(None, ge=-180, le=180)
        center_lat: float | None = Field(None, ge=-MAX_GEOGRAPHIC_LAT, le=MAX_GEOGRAPHIC_LAT)
        width_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
        height_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
        province_id: str | None = Field(None, min_length=1, max_length=20)
        commune_id: str | None = Field(None, min_length=1, max_length=20)
        reference_id: str | None = Field(None, min_length=1, max_length=64)
        start_date: str
        end_date: str
        platform: str | None = None
        polarization: Literal['vv', 'vh', 'vvvh', 'hh', 'hv', 'hhhv'] = 'vvvh'
        orbit_pass: Literal['ASCENDING', 'DESCENDING'] | None = None
        relative_orbit: int | None = Field(None, ge=1, le=175)
        product: Literal['NO2', 'SO2', 'CO', 'CH4', 'O3', 'HCHO', 'AAI'] = 'NO2'
        product_type: Literal['OL_1_EFR', 'OL_2_LFR', 'OL_2_WRR', 'SL_1_RBT',
                              'SL_2_LST', 'SR_2_LAN', 'SY_2_SYN'] = 'OL_2_LFR'

        @field_validator('start_date', 'end_date')
        @classmethod
        def valid_date(cls, value):
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                raise ValueError('Dates must use YYYY-MM-DD')
            dt.date.fromisoformat(value)
            return value

        @model_validator(mode='after')
        def valid_selection(self):
            if self.start_date > self.end_date or self.end_date == '9999-12-31':
                raise ValueError('Invalid date interval')
            self.platform = validate_platform(self.dataset, self.platform)
            if self.dataset != 's1gee' and (self.orbit_pass is not None or self.relative_orbit is not None):
                raise ValueError('Orbit filters require dataset=s1gee')
            if self.selection_mode == 'point' and (self.lon is None or self.lat is None):
                raise ValueError('Point selection requires lon and lat')
            for mode, field in (('province', 'province_id'), ('commune', 'commune_id'), ('reference', 'reference_id')):
                if self.selection_mode == mode and not getattr(self, field):
                    raise ValueError(f'{mode} selection requires {field}')
                if self.selection_mode != mode and getattr(self, field) is not None:
                    raise ValueError(f'{field} requires {mode} selection')
            if self.selection_mode != 'point' and (self.lon is not None or self.lat is not None):
                raise ValueError('lon/lat require point selection')
            if self.selection_mode != 'point' and 'radius_m' in self.model_fields_set:
                raise ValueError('radius_m requires point selection')
            if self.dataset not in ('s2', 's2aws') and 'source' in self.model_fields_set:
                raise ValueError('Explicit source is only supported for external Sentinel-2')
            return self

    store = main.TimeSeriesStore(main._DOWNLOADS_DIR / 'scene_inventory')

    def geometry_for(body):
        if body.selection_mode == 'vector':
            from .vector_api import vector_area
            return vector_area(main._vector_store, body.vector_roi_id).geometry
        if body.selection_mode == 'point':
            return point_roi(body.lon, body.lat, body.radius_m, body.rotation_deg)[0]
        if body.selection_mode == 'rectangle':
            return main._rectangle_model(body.center_lon, body.center_lat, body.width_m,
                                         body.height_m, body.rotation_deg).geometry
        if body.selection_mode == 'reference':
            try:
                reference = main._frame_store.get_reference(body.reference_id)
            except FrameError as exc:
                raise HTTPException(exc.status, exc.message) from None
            if reference is None:
                raise HTTPException(404, 'Không tìm thấy khung mẫu; hãy upload lại.')
            return reference_footprint(reference['metadata'])['footprint_geojson']
        getter = main.get_province if body.selection_mode == 'province' else main.get_commune
        try:
            area = getter(body.province_id if body.selection_mode == 'province' else body.commune_id)
        except RuntimeError:
            raise HTTPException(503, 'Không đọc được địa giới hành chính.') from None
        if area is None:
            raise HTTPException(404, 'Không tìm thấy địa giới đã chọn.')
        return area.geometry

    def search(body):
        geometry = geometry_for(body)
        source = ('gee' if body.dataset in GEE_DATASETS else
                  'aws' if body.dataset == 's2aws' else body.source if body.dataset == 's2' else 'cdse')
        try:
            scenes = sources.list_scenes(dataset=body.dataset, source=source, geometry=geometry,
                start=body.start_date, end=body.end_date, platform=body.platform,
                polarization=body.polarization, product=body.product, product_type=body.product_type,
                orbit_pass=body.orbit_pass, relative_orbit=body.relative_orbit)
        except HTTPException:
            raise
        except sources.InventoryIncomplete as exc:
            raise HTTPException(502, str(exc)) from None
        except FrameError as exc:
            status = 504 if exc.code == 'timeout' else exc.status
            raise HTTPException(status, {'code': exc.code, 'source': source,
                'message': f'{source.upper()}: {exc.message}'}) from None
        except requests.Timeout:
            raise HTTPException(504, {'code': 'timeout', 'source': source,
                'message': f'{source.upper()} hết thời gian phản hồi; hãy thử lại hoặc thu hẹp khoảng ngày.'}) from None
        except requests.ConnectionError:
            raise HTTPException(502, {'code': 'connection_failed', 'source': source,
                'message': f'Không kết nối được {source.upper()}; kiểm tra kết nối mạng và thử lại.'}) from None
        except Exception as exc:
            # Record useful diagnostics without logging provider bodies, URLs or credentials.
            logger.warning('Scene inventory query failed: source=%s dataset=%s error_type=%s',
                           source, body.dataset, type(exc).__name__)
            raise HTTPException(502, {'code': 'inventory_query_failed', 'source': source,
                'message': f'{source.upper()}: không lấy được danh sách ảnh đầy đủ; hãy thử lại.'}) from None
        parameters = body.model_dump(exclude_none=True)
        parameters.update(source=source, roi_geometry=geometry, cloud_filter=False,
                          spatial_relation='intersects', complete=True,
                          scene_kind='period_composite' if body.dataset == 'nicfi' else 'scene')
        store.cleanup_expired_jobs()
        result = store.create_search(parameters, scenes)
        result['csv_url'] = f'/api/scene-inventory/{result["search_id"]}/csv'
        return result

    def export_csv(search_id: str):
        store.cleanup_expired_jobs()
        if store.get_search(search_id, limit=1) is None:
            raise HTTPException(404, 'Không tìm thấy danh sách ảnh; hãy đếm lại.')
        try:
            parameters, scenes = store._snapshot(search_id)
        except ValueError:
            raise HTTPException(404, 'Không tìm thấy danh sách ảnh; hãy đếm lại.') from None
        text = io.StringIO(newline='')
        writer = csv.DictWriter(text, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for scene in scenes:
            row = {column: scene.get(column, '') for column in CSV_COLUMNS}
            row['source'] = parameters['source']
            writer.writerow({key: _csv_cell(value) for key, value in row.items()})
        filename = f'scenes_{parameters["dataset"]}_{parameters["start_date"]}_{parameters["end_date"]}.csv'
        return Response(text.getvalue().encode('utf-8-sig'), media_type='text/csv; charset=utf-8',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"',
                                 'Cache-Control': 'no-store'})

    search.__annotations__['body'] = InventoryRequest
    api.post('/api/scene-inventory')(search)
    api.get('/api/scene-inventory/{search_id}/csv')(export_csv)
    return store
