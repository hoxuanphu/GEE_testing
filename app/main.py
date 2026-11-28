"""HTTP API for the GEE SDR download app."""
from __future__ import annotations

import datetime as dt
import asyncio
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
import os
import math
import logging
import json
import uuid
import re
import time
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from rasterio.errors import RasterioError
from starlette.background import BackgroundTask

from .request_timing import SearchTimingMiddleware, measure_call
from . import cdse, imagery, scene_thumbnails
from .scene_snapshots import SceneSnapshots
from .download_metrics import DownloadTimingMiddleware, measure as measure_download
from .export_resources import CURRENT_SCENE, QueueTimeout, TransferCancelled, bulk_export, scene_download_admission
from .esri import EsriRequest, EsriService, BusyError as EsriBusyError
from .timeseries import BusyError, TimeSeriesService, TimeSeriesStore
from .timeseries_batches import BatchStore, BatchService
from .timeseries_batch_api import register_batch_routes
from .imagery import list_timeseries_scenes, timeseries_source_metadata
from .timeseries_metadata import search_failure

from .cdse import (
    build_pairs,
    get_token,
    iter_open_product,
    open_product,
    search_s1,
    search_s1_area,
    search_s1_slc,
    search_s2,
    search_s2_area,
    search_s3,
    search_s3_area,
    list_s2_bands,
    download_s2_band_file,
    get_s3_credentials,
    get_product_s3_path,
    s3_list_product_files,
    iter_s3_object,
)
from .aws_stac import search_s2_l2a as aws_search_s2_l2a
from .aws_stac import search_s2_l2a_bbox as aws_search_s2_l2a_bbox
from .aws_stac import get_item as aws_get_item
from .aws_stac import merge_all_bands as aws_merge_all_bands
from .aws_stac import province_mosaic as aws_province_mosaic
from .admin_boundaries import (
    Province,
    get_province,
    list_provinces,
    province_feature,
    province_feature_collection,
)
from .commune_boundaries import Commune, commune_feature, get_commune, list_communes
from .ee_session import init_ee
from .platforms import validate_platform
from .provider_credentials import PROVIDERS, active_credentials
from .provider_credentials_api import router as local_provider_settings_router
from .vector_store import VectorStore
from .vector_api import VectorArea, vector_area, register_vector_routes
from .geo import (
    MAX_RECTANGLE_DIMENSION_M,
    Bbox,
    RectangleROI,
    bbox_to_ee_geom,
    point_roi,
    source_bbox,
    source_footprint,
)
from .raster_grid import build_rotated_grid, warp_to_grid, grid_profile, write_grid_band
from .imagery import (
    LANDSAT_SCALE,
    NICFI_SCALE,
    DYNAMIC_WORLD_SCALE,
    S1_SCALE,
    S2_SCALE,
    s2_export_bands,
    best_scene,
    InsufficientCoverageError,
    best_scene_for_roi,
    best_s1_scene,
    best_s1_scene_for_roi,
    composite_plan,
    download_geotiff,
    merge_band_tiffs,
    preview_rgb,
    MEDOID_MAX_TILE_PIXELS,
)

_DOWNLOADS_DIR = Path(os.environ.get('GEE_DOWNLOADS_DIR') or
                      Path(__file__).resolve().parent.parent / 'downloads').resolve()
_DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
_vector_store = VectorStore(_DOWNLOADS_DIR / 'vectors')
_timeseries_store = TimeSeriesStore(_DOWNLOADS_DIR / "timeseries")
_timeseries_service = TimeSeriesService(_timeseries_store)
_batch_store = BatchStore(_DOWNLOADS_DIR / 'timeseries')
_batch_service = BatchService(_batch_store, _timeseries_service)
_esri_root = _DOWNLOADS_DIR / "esri"
_esri_service = EsriService(_esri_root, recover=False)

from .frame_store import FrameStore
from .frame_jobs import FrameService

_frame_store = FrameStore(_DOWNLOADS_DIR / 'matched_frame')
_frame_service = FrameService(_frame_store)


@asynccontextmanager
async def _application_lifespan(application):
    global _timeseries_service, _esri_service, _frame_service, _batch_service, _download_service
    for provider in PROVIDERS:
        active_credentials(provider)
    if _download_service is not None and _download_service._stop.is_set():
        _download_service = None
    if os.getenv('GEE_DOWNLOAD_JOBS') == '1':
        _get_download_service()
    if _frame_service._closed:
        _frame_service = FrameService(_frame_store)
    _frame_service.recover()
    _frame_service.cleanup()
    if _esri_service._closed:
        _esri_service = EsriService(_esri_root)
    _esri_service.recover()
    if _timeseries_service._closed:
        _timeseries_service = TimeSeriesService(_timeseries_store)
    _timeseries_service.recover_interrupted_jobs()
    _timeseries_service.cleanup_expired_jobs()
    _umbra_store.cleanup_expired_jobs()
    _batch_service = BatchService(_batch_store, _timeseries_service)
    _batch_store.recover()
    _batch_store.cleanup()
    for row in _batch_store.queued_tasks():
        if row['kind'] == 'timeseries':
            _timeseries_service.submit_task(row['id'], _timeseries_service.run_timeseries_job, row['id'])
        else:
            _timeseries_service.submit_task('batch:' + row['id'], _batch_service.run_batch, row['id'])

    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(_timeseries_service.cleanup_expired_jobs)
            await asyncio.to_thread(_umbra_store.cleanup_expired_jobs)
            await asyncio.to_thread(_batch_store.cleanup)
            await asyncio.to_thread(_frame_service.cleanup)
            if _download_service is not None:
                await asyncio.to_thread(_download_service.cleanup)

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await asyncio.to_thread(_umbra_catalog.shutdown)
        await asyncio.to_thread(_batch_service.pause_active)
        await asyncio.to_thread(_esri_service.shutdown)
        if _esri_service._thread is not None:
            await asyncio.to_thread(_esri_service._thread.join)
        await asyncio.to_thread(_timeseries_service.shutdown)
        await asyncio.to_thread(_frame_service.shutdown)
        if _download_service is not None:
            await asyncio.to_thread(_download_service.shutdown)
            if _download_service._thread is not None:
                await asyncio.to_thread(_download_service._thread.join)


@asynccontextmanager
async def _lifespan(application):
    from .task_lock import TaskLock
    task_lock = TaskLock(_DOWNLOADS_DIR)
    if not task_lock.acquire():
        raise RuntimeError('Một server khác đang quản lý thư mục tải này.')
    try:
        async with _application_lifespan(application):
            yield
    finally:
        task_lock.release()


app = FastAPI(title="GEE Surface Reflectance Downloader", version="0.1.0", lifespan=_lifespan)
app.add_middleware(SearchTimingMiddleware)
app.add_middleware(DownloadTimingMiddleware, directory=_DOWNLOADS_DIR / 'download-performance' / 'requests')


@app.exception_handler(QueueTimeout)
async def export_queue_timeout(request,exc):
    from fastapi.responses import JSONResponse
    return JSONResponse({'code':'queue_timeout','detail':'Hàng đợi tải ảnh đã hết thời gian chờ; hãy thử lại sau.'},
                        status_code=503,headers={'Retry-After':'30'})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

_MAX_RADIUS_M = 45_000.0
_DEFAULT_DAYS = 30


def _rectangle_model(
    center_lon: float | None,
    center_lat: float | None,
    width_m: float | None,
    height_m: float | None,
    rotation_deg: float,
) -> RectangleROI:
    values = (center_lon, center_lat, width_m, height_m)
    if any(value is None for value in values):
        raise ValueError(
            "rectangle selection requires center_lon, center_lat, width_m and height_m"
        )
    return RectangleROI(
        center_lon=center_lon,
        center_lat=center_lat,
        width_m=width_m,
        height_m=height_m,
        rotation_deg=rotation_deg,
    )


def _validate_selection_fields(
    selection_mode: str,
    lon: float | None,
    lat: float | None,
    radius_m: float | None,
    center_lon: float | None,
    center_lat: float | None,
    width_m: float | None,
    height_m: float | None,
    rotation_deg: float,
) -> None:
    rectangle_values = (center_lon, center_lat, width_m, height_m)
    if selection_mode == "rectangle":
        if lon is not None or lat is not None:
            raise ValueError("rectangle selection uses center_lon and center_lat")
        _rectangle_model(center_lon, center_lat, width_m, height_m, rotation_deg)
    elif any(value is not None for value in rectangle_values):
        raise ValueError("rectangle fields are only supported for rectangle selection")
    elif selection_mode == "point" and lon is not None and lat is not None:
        point_roi(lon, lat, radius_m, rotation_deg)

# Import rasterio NGAY ở main thread: trên Windows, lần import đầu tiên
# trong worker thread (threadpool FastAPI) bị lỗi DLL "handle is invalid"
# (GDAL init cần main thread). Import sớm để mọi import lazy sau đó chỉ
# lấy từ sys.modules.
try:  # pragma: no cover - phụ thuộc máy
    import rasterio  # noqa: F401
except ImportError:
    rasterio = None

_ee_ready = False
_color_snapshots = SceneSnapshots()
_mosaic_snapshots = SceneSnapshots()
_download_service = None


def _get_download_store():
    from .download_store import SelectionStore
    return SelectionStore(_DOWNLOADS_DIR / 'gee-downloads')


def _get_download_service():
    global _download_service
    from .download_jobs import DownloadService
    from .download_models import DownloadSettings
    root = (_DOWNLOADS_DIR / 'gee-downloads').resolve()
    if _download_service is not None and _download_service.store.root != root:
        _download_service.shutdown()
        _download_service = None
    if _download_service is None:
        _download_service = DownloadService(_get_download_store(),
            settings=DownloadSettings(vrt=os.getenv('GEE_DOWNLOAD_VRT') == '1'),
            # The flag gates new jobs/UI. Existing paused jobs remain resumable
            # after rollback, using their persisted writer profile and graph.
            autostart=True)
    return _download_service


from .download_api import register_download_routes
register_download_routes(app, _get_download_service, lambda: os.getenv('GEE_DOWNLOAD_JOBS') == '1')


def _frozen_download_supported(dataset, mode):
    return (os.getenv('GEE_DOWNLOAD_SNAPSHOTS') == '1' and dataset == 'sentinel2'
            and mode in ('point', 'rectangle'))


def _validate_color_balance(enabled, dataset, selection_mode, merge_scenes, composite_method):
    if enabled and (dataset not in ('sentinel2', 'landsat') or composite_method != 'raw' or
                    (composite_method != 'raw' if selection_mode in ('province', 'commune', 'vector')
                     else not merge_scenes)):
        raise ValueError('Cân bằng màu chỉ hỗ trợ ghép RAW Sentinel-2/Landsat.')


def _mosaic_options(grouping='any', days=3, mask=True, method='raw', dataset='sentinel2'):
    return (dict(scene_grouping=grouping, max_scene_span_days=days, mosaic_mask_clouds=mask)
            if dataset in ('sentinel2', 'landsat') and (grouping != 'any' or method == 'mosaic') else {})


def _validate_mosaic_options(dataset, mode, merge, method, grouping, days, mask):
    if grouping not in ('any', 'near_date', 'same_pass') or days not in (1, 3, 5, 7):
        raise ValueError('Giới hạn thời gian phải là 1/3/5/7 ngày.')
    active = mode in ('province', 'commune', 'vector') or merge
    if dataset in ('sentinel2', 'landsat') and method == 'mosaic' and not active:
        raise ValueError('Bật ghép nhiều cảnh trước khi chọn Mosaic ổn định.')
    if (grouping != 'any' or not mask) and (
            dataset not in ('sentinel2', 'landsat') or not active or method not in ('raw', 'mosaic')):
        raise ValueError('Chọn ghép RAW hoặc Mosaic ổn định Sentinel-2/Landsat trước.')
    if not mask and method != 'mosaic':
        raise ValueError('Tùy chọn mask mây chỉ áp dụng cho Mosaic ổn định.')


def _mosaic_snapshot_key(base, method, grouping, days, mask, balance, merge):
    return base + json.dumps([method, grouping, days, mask, balance, merge])


def _color_snapshot_key(dataset, start, end, mode, bbox, area, rotation, *, s2_band_preset=None, **coordinates):
    active_fields = ('lon', 'lat', 'radius_m') if mode == 'point' else (
        ('center_lon', 'center_lat', 'width_m', 'height_m') if mode == 'rectangle' else ())
    coordinates = {field: coordinates[field] for field in ('platform', *active_fields)}
    return json.dumps(dict(dataset=dataset, start=start, end=end, mode=mode,
                           bbox=_bbox_dict(bbox), area_id=area.id if area else None,
                           geometry=area.geometry if area else None,
                           rotation=rotation % 360, s2_band_preset=s2_band_preset,
                           **coordinates), sort_keys=True)


S2BandPreset = Literal['rgbnir']


def _validate_s2_band_preset(dataset, preset, auxiliary_bands):
    if preset is not None:
        if dataset != 'sentinel2':
            raise ValueError('s2_band_preset requires dataset=sentinel2')
        s2_export_bands(auxiliary_bands, s2_band_preset=preset)


def _ensure_ee() -> None:
    global _ee_ready
    if not _ee_ready:
        try:
            init_ee()
            _ee_ready = True
        except Exception as exc:  # pragma: no cover
            raise HTTPException(
                status_code=503,
                detail=f"Earth Engine initialization failed: {exc}",
            ) from exc


class PointSelectionRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    rotation_deg: float = Field(0.0, ge=0, le=360)
    vector_roi_id: str | None = Field(None, max_length=64)

    @model_validator(mode="after")
    def valid_rotation(self):
        self.rotation_deg %= 360
        if self.selection_mode == 'vector':
            try:
                if str(uuid.UUID(self.vector_roi_id or '')) != self.vector_roi_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('Chọn và áp dụng polygon vector hợp lệ trước.') from None
            if any(getattr(self, field, None) is not None for field in ('lon', 'lat', 'province_id', 'commune_id',
                    'center_lon', 'center_lat', 'width_m', 'height_m')) or 'radius_m' in self.model_fields_set:
                raise ValueError('Vector ROI cannot include point, rectangle or administrative fields')
        elif self.vector_roi_id is not None:
            raise ValueError('vector_roi_id requires vector selection')
        if self.selection_mode not in ("point", "rectangle") and self.rotation_deg:
            raise ValueError("rotation_deg is only supported for point or rectangle selection")
        try:
            _validate_selection_fields(
                self.selection_mode,
                getattr(self, "lon", None),
                getattr(self, "lat", None),
                getattr(self, "radius_m", None),
                getattr(self, "center_lon", None),
                getattr(self, "center_lat", None),
                getattr(self, "width_m", None),
                getattr(self, "height_m", None),
                self.rotation_deg,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self


class SceneRequest(PointSelectionRequest):
    selection_mode: Literal["point", "rectangle", "province", "commune", "vector"] = "point"
    lon: float | None = Field(None, ge=-180, le=180, description="Longitude (degrees)")
    lat: float | None = Field(None, ge=-90, le=90, description="Latitude (degrees)")
    center_lon: float | None = Field(None, ge=-180, le=180)
    center_lat: float | None = Field(None, ge=-90, le=90)
    width_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    height_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    province_id: str | None = Field(None, min_length=1, max_length=20)
    commune_id: str | None = Field(None, min_length=1, max_length=20)
    dataset: str = Field(
        "sentinel2", pattern="^(sentinel2|landsat|nicfi|dynamic_world)$"
    )
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    radius_m: float = Field(2000.0, gt=0, le=_MAX_RADIUS_M)
    merge_scenes: bool = False
    color_balance: bool = False
    scene_grouping: Literal['any', 'near_date', 'same_pass'] = 'any'
    max_scene_span_days: Literal[1, 3, 5, 7] = 3
    mosaic_mask_clouds: bool = True
    platform: str | None = None
    s2_auxiliary_bands: list[str] | None = None
    s2_band_preset: S2BandPreset | None = None

    @field_validator('s2_auxiliary_bands')
    @classmethod
    def valid_auxiliary_bands(cls, value, info):
        if value is not None:
            if info.data.get('dataset') != 'sentinel2':
                raise ValueError('s2_auxiliary_bands requires dataset=sentinel2')
            s2_export_bands(value)
        return value

    composite_method: Literal["mosaic", "median", "medoid", "base-score", "raw"] = Field(
        "raw", description="Province mode: how to composite multiple scenes"
    )

    @model_validator(mode="after")
    def valid_platform(self):
        self.platform = validate_platform(self.dataset, self.platform)
        _validate_s2_band_preset(self.dataset, self.s2_band_preset, self.s2_auxiliary_bands)
        _validate_color_balance(self.color_balance, self.dataset, self.selection_mode,
                                self.merge_scenes, self.composite_method)
        _validate_mosaic_options(self.dataset, self.selection_mode, self.merge_scenes, self.composite_method,
                                self.scene_grouping, self.max_scene_span_days, self.mosaic_mask_clouds)
        return self

    def download_parameters(self):
        parameters = self.model_dump()
        if self.s2_band_preset is None:
            # Keep the request identity of existing download tokens and artifacts.
            parameters.pop('s2_band_preset')
        return parameters

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v


class SceneResponse(BaseModel):
    dataset: str
    platform: str | None = None
    scene_id: str
    date: str
    cloud_pct: float
    bbox: dict[str, float]
    roi_geometry: dict | None = None
    footprint: dict | None = None
    rotation_deg: float = 0
    scale_m: float
    bands: list[str]
    preview_url: str | None = None
    download_url: str | None = None
    selection_mode: Literal["point", "rectangle", "province", "commune", "vector"]
    vector_roi_id: str | None = None
    province_id: str | None = None
    province_name: str | None = None
    commune_id: str | None = None
    commune_name: str | None = None
    composite: bool = False
    composite_method: Literal[
        "mosaic", "median", "medoid", "base-score", "raw", "dynamic-world-mode"
    ] | None = None
    scene_count: int | None = None
    scenes: list[dict] = Field(default_factory=list)
    estimated_seconds: int | None = None
    coverage_pct: float | None = None
    color_balance: dict | None = None
    scene_grouping: dict | None = None
    mosaic_mask_clouds: bool | None = None
    selection_token: str | None = None
    download_job_available: bool = False


@app.exception_handler(InsufficientCoverageError)
async def coverage_error_handler(request, exc):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=404, content={"detail": str(exc)})


def _scale_for(dataset: str) -> float:
    if dataset == "sentinel2":
        return S2_SCALE
    if dataset == "nicfi":
        return NICFI_SCALE
    if dataset == "dynamic_world":
        return DYNAMIC_WORLD_SCALE
    return LANDSAT_SCALE


def _resolve_selection(
    selection_mode: str,
    lon: float | None,
    lat: float | None,
    radius_m: float | None,
    province_id: str | None,
    commune_id: str | None = None,
    rotation_deg: float = 0,
    center_lon: float | None = None,
    center_lat: float | None = None,
    width_m: float | None = None,
    height_m: float | None = None,
    vector_roi_id: str | None = None,
) -> tuple[object, Bbox, Province | Commune | None]:
    """Resolve a trusted point or local administrative polygon into an ROI."""
    if not math.isfinite(rotation_deg) or not 0 <= rotation_deg <= 360:
        raise HTTPException(400, "rotation_deg must be finite and between 0 and 360")
    rotation_deg %= 360
    if selection_mode == 'vector':
        if rotation_deg or any(value is not None for value in (lon, lat, province_id, commune_id,
                center_lon, center_lat, width_m, height_m)):
            raise HTTPException(400, 'Vector ROI cannot include other selection fields or rotation')
        area = vector_area(_vector_store, vector_roi_id)
        return area.ee_geometry(), area.bbox, area
    if vector_roi_id is not None:
        raise HTTPException(400, 'vector_roi_id requires vector selection')
    if selection_mode not in ("point", "rectangle") and rotation_deg:
        raise HTTPException(400, "rotation_deg is only supported for point or rectangle selection")
    if selection_mode == "rectangle":
        try:
            rectangle = _rectangle_model(
                center_lon, center_lat, width_m, height_m, rotation_deg
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        import ee
        return (
            ee.Geometry.Polygon(rectangle.geometry["coordinates"], geodesic=False),
            rectangle.bbox,
            None,
        )
    if selection_mode == "commune":
        if not commune_id:
            raise HTTPException(400, "commune_id is required for commune selection")
        try:
            commune = get_commune(commune_id)
        except RuntimeError as exc:
            raise HTTPException(503, f"Commune boundaries unavailable: {exc}") from exc
        if commune is None:
            raise HTTPException(404, f"Commune not found: {commune_id}")
        return commune.ee_geometry(), commune.bbox, commune

    if selection_mode == "province":
        if not province_id:
            raise HTTPException(400, "province_id is required for province selection")
        try:
            province = get_province(province_id)
        except RuntimeError as exc:
            raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
        if province is None:
            raise HTTPException(404, f"Province not found: {province_id}")
        return province.ee_geometry(), province.bbox, province

    if lon is None or lat is None:
        raise HTTPException(400, "lon and lat are required for point selection")
    if radius_m <= 0 or radius_m > _MAX_RADIUS_M:
        raise HTTPException(400, f"radius_m must be > 0 and <= {_MAX_RADIUS_M:.0f} m")
    try:
        geometry, bbox = point_roi(lon, lat, radius_m, rotation_deg)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if rotation_deg:
        import ee
        return ee.Geometry.Polygon(geometry["coordinates"], geodesic=False), bbox, None
    return bbox_to_ee_geom(bbox), bbox, None


def _bbox_dict(bbox: Bbox) -> dict[str, float]:
    return {
        "west": round(bbox.west, 6),
        "south": round(bbox.south, 6),
        "east": round(bbox.east, 6),
        "north": round(bbox.north, 6),
    }


class TimeSeriesRequest(PointSelectionRequest):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    selection_mode: Literal["point", "rectangle", "vector"] = "point"
    dataset: Literal["sentinel2", "landsat", "nicfi", "dynamic_world", "s1gee"]
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    center_lon: float | None = Field(None, ge=-180, le=180)
    center_lat: float | None = Field(None, ge=-90, le=90)
    width_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    height_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    radius_m: float = Field(2000.0, gt=0, le=_MAX_RADIUS_M)
    start_date: str
    end_date: str
    polarization: Literal["vv", "vh", "vvvh", "hh", "hv", "hhhv"] = "vvvh"
    platform: str | None = None
    s2_auxiliary_bands: list[str] | None = None
    s2_band_preset: S2BandPreset | None = None

    @field_validator('s2_auxiliary_bands')
    @classmethod
    def valid_auxiliary_bands(cls, value, info):
        if value is not None:
            if info.data.get('dataset') != 'sentinel2':
                raise ValueError('s2_auxiliary_bands requires dataset=sentinel2')
            s2_export_bands(value)
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date(cls, value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Date must be YYYY-MM-DD")
        dt.date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def valid_interval(self):
        self.platform = validate_platform(self.dataset, self.platform)
        _validate_s2_band_preset(self.dataset, self.s2_band_preset, self.s2_auxiliary_bands)
        if self.selection_mode == 'point' and (self.lon is None or self.lat is None):
            raise ValueError('lon and lat are required for point selection')
        if self.selection_mode == 'rectangle' and 'radius_m' in self.model_fields_set:
            raise ValueError('rectangle selection uses width_m and height_m, not radius_m')
        if self.start_date > self.end_date:
            raise ValueError("start_date must not exceed end_date")
        if self.end_date == "9999-12-31":
            raise ValueError("end_date must allow an exclusive next day")
        return self


class TimeSeriesSceneResponse(BaseModel):
    asset_id: str
    timestamp: str
    date: str
    cloud_pct: float | None = None
    dataset: str | None = None
    collection: str | None = None
    source_metadata: dict = Field(default_factory=dict)
    footprint: dict | None = None


class TimeSeriesJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_id: str = Field(..., min_length=1, max_length=64)


@app.post("/api/timeseries/search")
def search_timeseries(body: TimeSeriesRequest) -> dict:
    started, request_id = time.monotonic(), uuid.uuid4().hex[:12]
    try:
        _ensure_ee()
    except Exception as exc:
        error = search_failure(exc, stage='initialization', request_id=request_id, started=started)
        raise HTTPException(error.status, error.message,
                            headers={'X-Search-Request-ID': request_id}) from exc
    rectangle = (_rectangle_model(body.center_lon, body.center_lat, body.width_m,
                                  body.height_m, body.rotation_deg)
                 if body.selection_mode == 'rectangle' else None)
    rectangle_args = ({field: getattr(body, field) for field in
                       ('center_lon', 'center_lat', 'width_m', 'height_m')} if rectangle else {})
    roi, bbox, area = _resolve_selection(body.selection_mode, body.lon, body.lat, body.radius_m,
                                     None, rotation_deg=body.rotation_deg, **rectangle_args,
                                     **({'vector_roi_id': body.vector_roi_id} if body.vector_roi_id else {}))
    end_exclusive = (dt.date.fromisoformat(body.end_date) + dt.timedelta(days=1)).isoformat()
    stage = 'metadata'
    try:
        scenes = list_timeseries_scenes(body.dataset,
                                       rectangle.center_lon if rectangle else body.lon or 0,
                                       rectangle.center_lat if rectangle else body.lat or 0,
                                       body.start_date, end_exclusive, body.polarization,
                                       include_footprint=True,
                                       **({"roi": roi} if rectangle or body.rotation_deg or body.selection_mode == 'vector' else {}),
                                       **({'platform': body.platform} if body.platform is not None else {}))
        stage = 'parameters'
        parameters = body.model_dump()
        if rectangle is None:
            for field in ('center_lon', 'center_lat', 'width_m', 'height_m'):
                parameters.pop(field, None)
        parameters.update(timeseries_source_metadata(body.dataset, body.polarization,
                                                     body.s2_auxiliary_bands,
                                                     s2_band_preset=body.s2_band_preset))
        parameters["bbox"] = vars(bbox)
        parameters["crs"] = "EPSG:4326"
        parameters["roi_geometry"] = (area.geometry if body.selection_mode == 'vector' else rectangle.geometry if rectangle else
                                      point_roi(body.lon, body.lat, body.radius_m, body.rotation_deg)[0])
        if rectangle is not None:
            parameters['rectangle'] = rectangle.model_dump()
            parameters['output_grid'] = rectangle.output_grid(parameters['scale_m'])
        elif body.rotation_deg:
            parameters["output_grid"] = build_rotated_grid(body.lon, body.lat, body.radius_m,
                                                          body.rotation_deg, parameters["scale_m"])
        stage = 'validation'
        scenes = [TimeSeriesSceneResponse.model_validate(scene).model_dump() for scene in scenes]
        stage = 'snapshot'
        return _timeseries_thumbnail_links(_timeseries_store.create_search(parameters, scenes))
    except Exception as exc:
        error = search_failure(exc, stage=stage, request_id=request_id, started=started)
        raise HTTPException(error.status, error.message,
                            headers={'X-Search-Request-ID': request_id}) from exc


@app.get("/api/timeseries/search/{search_id}")
def timeseries_search_page(search_id: str, offset: int = 0, limit: int = 50) -> dict:
    if offset < 0 or not 1 <= limit <= 50:
        raise HTTPException(400, "offset must be nonnegative and limit must be 1–50")
    result = _timeseries_store.get_search(search_id, offset, limit)
    if result is None:
        raise HTTPException(404, "Search not found or expired")
    return _timeseries_thumbnail_links(result)


def _timeseries_thumbnail_links(result):
    if result['parameters'].get('roi_geometry'):
        for index, scene in enumerate(result['scenes'], result['offset']):
            scene['thumbnail_url'] = f"/api/timeseries/search/{result['search_id']}/thumbnail/{index}"
    return result


@lru_cache(maxsize=96)
def _timeseries_thumbnail(asset_id, dataset, geometry, polarization):
    return imagery.render_catalog_thumbnail(asset_id, dataset, geometry, polarization)


def _serve_gee_thumbnail(asset_id, dataset, region, polarization='vvvh'):
    try:
        _ensure_ee()
        content = _timeseries_thumbnail(asset_id, dataset, json.dumps(region, separators=(',', ':')), polarization)
    except Exception:
        raise HTTPException(502, 'Không tải được ảnh xem trước của cảnh.') from None
    return scene_thumbnails.response(content)


def _serve_timeseries_thumbnail(search_id, index):
    if index < 0:
        raise HTTPException(404, 'Scene not found')
    page = _timeseries_store.get_search(search_id, index, 1)
    if not page or not page['scenes'] or not page['parameters'].get('roi_geometry'):
        raise HTTPException(404, 'Thumbnail unavailable')
    params = page['parameters']
    return _serve_gee_thumbnail(page['scenes'][0]['asset_id'],
        params['dataset'], params['roi_geometry'], params.get('polarization', 'vvvh'))


@app.get('/api/timeseries/search/{search_id}/thumbnail/{index}')
async def timeseries_thumbnail(search_id: str, index: int):
    return await scene_thumbnails.load(_serve_timeseries_thumbnail, search_id, index)


_component_thumbnail_snapshots = SceneSnapshots(capacity=128)


def _component_thumbnail_links(scenes, dataset, geometry, polarization='vvvh'):
    if not scenes or not geometry:
        return scenes
    token = _component_thumbnail_snapshots.put(dataset, (scenes, geometry, polarization))
    return [{**scene, 'thumbnail_url': f'/api/scene-components/{dataset}/{token}/thumbnail/{index}'}
            for index, scene in enumerate(scenes)]


def _optical_scene_footprints(scenes, dataset):
    try:
        return imagery.source_scene_footprints(scenes, dataset)
    except Exception:
        return list(scenes)


@app.get('/api/scene-components/{dataset}/{token}/thumbnail/{index}')
async def component_thumbnail(dataset: Literal['sentinel2', 'landsat', 's1gee'], token: str, index: int):
    try:
        scenes, geometry, polarization = _component_thumbnail_snapshots.get(token, dataset)
        if not 0 <= index < len(scenes):
            raise IndexError
        scene_id = scenes[index]['scene_id']
        asset_id = (imagery.S1_COLLECTION + '/' + scene_id if dataset == 's1gee'
                    else imagery._native_asset({'system:index': scene_id}, dataset))
    except (LookupError, ValueError):
        raise HTTPException(404, 'Thumbnail unavailable') from None
    return await scene_thumbnails.load(_serve_gee_thumbnail, asset_id, dataset, geometry, polarization)


def _serve_cdse_thumbnail(product_id):
    try:
        media_type, content = cdse.scene_thumbnail(product_id)
    except LookupError:
        raise HTTPException(404, 'CDSE không có ảnh xem trước cho cảnh này.') from None
    except (OSError, ValueError):
        raise HTTPException(502, 'Không tải được ảnh xem trước từ CDSE.') from None
    return scene_thumbnails.response(content, media_type)


@app.get('/api/cdse/thumbnail/{product_id}')
async def cdse_thumbnail(product_id: str):
    return await scene_thumbnails.load(_serve_cdse_thumbnail, product_id)


@app.post("/api/timeseries/jobs")
def create_timeseries_job(body: TimeSeriesJobRequest) -> dict:
    try:
        return _timeseries_service.start_job(body.search_id)
    except BusyError as exc:
        raise HTTPException(409, "One time-series job is already active; wait for it to finish") from exc
    except ValueError as exc:
        raise HTTPException(400, "Search is missing, expired, or contains no scenes") from exc


@app.get("/api/timeseries/jobs/{job_id}")
def timeseries_job_status(job_id: str) -> dict:
    result = _timeseries_store.get_job(job_id)
    if result is None:
        raise HTTPException(404, "Job not found or expired")
    return result


@app.get("/api/timeseries/jobs/{job_id}/download")
def download_timeseries_job(job_id: str):
    job = _timeseries_store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found or expired")
    path = _timeseries_store.download_path(job_id)
    if path is None:
        raise HTTPException(409, "ZIP is not ready")
    from .task_files import TaskFileResponse
    return TaskFileResponse(path, task_directory=path.parent, media_type="application/zip",
                            filename=f"timeseries_{job_id}.zip")


@app.post("/api/esri/jobs")
def create_esri_job(body: EsriRequest) -> dict:
    try:
        return _esri_service.start(body)
    except EsriBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/esri/jobs/{job_id}")
def esri_job_status(job_id: str) -> dict:
    result = _esri_service.get(job_id)
    if result is None:
        raise HTTPException(404, "Esri job not found")
    return result


@app.post("/api/esri/jobs/{job_id}/cancel")
def cancel_esri_job(job_id: str) -> dict:
    result = _esri_service.cancel(job_id)
    if result is None:
        raise HTTPException(404, "Esri job not found")
    return result


@app.post("/api/esri/jobs/{job_id}/resume")
def resume_esri_job(job_id: str) -> dict:
    try:
        result = _esri_service.resume(job_id)
    except EsriBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "Esri job not found")
    return result


@app.get("/api/esri/jobs/{job_id}/download")
def download_esri_job(job_id: str):
    if _esri_service.get(job_id) is None:
        raise HTTPException(404, "Esri job not found")
    path = _esri_service.download_path(job_id)
    if path is None:
        raise HTTPException(409, "No finalized Esri GeoTIFF available")
    return FileResponse(path, media_type="image/tiff", filename=f"esri_{job_id}.tif")


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "ee_initialized": _ee_ready}


@app.get("/api/admin/provinces")
def admin_provinces() -> dict:
    try:
        return {"count": 34, "provinces": list_provinces()}
    except RuntimeError as exc:
        raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc


@app.get("/api/admin/provinces/{province_id}")
def admin_province(province_id: str) -> dict:
    try:
        feature = province_feature(province_id)
    except RuntimeError as exc:
        raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
    if feature is None:
        raise HTTPException(404, f"Province not found: {province_id}")
    return feature


@app.get("/api/admin/provinces-geojson")
def admin_provinces_geojson() -> dict:
    try:
        return province_feature_collection()
    except RuntimeError as exc:
        raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc


@app.get("/api/admin/communes")
def admin_communes() -> dict:
    try:
        communes = list_communes()
        return {"count": len(communes), "communes": communes}
    except RuntimeError as exc:
        raise HTTPException(503, f"Commune boundaries unavailable: {exc}") from exc


@app.get("/api/admin/communes/{commune_id}")
def admin_commune(commune_id: str) -> dict:
    try:
        feature = commune_feature(commune_id)
    except RuntimeError as exc:
        raise HTTPException(503, f"Commune boundaries unavailable: {exc}") from exc
    if feature is None:
        raise HTTPException(404, f"Commune not found: {commune_id}")
    return feature


@app.post("/api/scene", response_model=SceneResponse)
def find_scene(req: SceneRequest) -> SceneResponse:
    measure_call("ee_init", _ensure_ee)
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be <= end_date")

    roi, bbox, province = measure_call("selection", _resolve_selection,
        req.selection_mode,
        req.lon,
        req.lat,
        req.radius_m,
        req.province_id,
        req.commune_id,
        req.rotation_deg,
        req.center_lon,
        req.center_lat,
        req.width_m,
        req.height_m,
        **({'vector_roi_id': req.vector_roi_id} if req.vector_roi_id else {}),
    )

    # Inclusive end date for GEE filterDate (shift by +1 day)
    end_exclusive = (
        dt.date.fromisoformat(req.end_date) + dt.timedelta(days=1)
    ).isoformat()

    try:
        mosaic_options = _mosaic_options(req.scene_grouping, req.max_scene_span_days,
                                        req.mosaic_mask_clouds, req.composite_method, req.dataset)
        if province is not None:
            scene = measure_call("scene_search", best_scene_for_roi,
                req.dataset,
                roi,
                req.start_date,
                end_exclusive,
                composite=True,
                composite_method=req.composite_method,
                platform=req.platform,
                **mosaic_options,
                **({'color_balance': True} if req.color_balance else {}),
            )
        else:
            scene = measure_call("scene_search", best_scene,
                dataset=req.dataset,
                lon=req.center_lon if req.selection_mode == "rectangle" else req.lon,
                lat=req.center_lat if req.selection_mode == "rectangle" else req.lat,
                radius_m=(max(req.width_m, req.height_m) / 2
                          if req.selection_mode == "rectangle" else req.radius_m),
                start_date=req.start_date,
                end_date=end_exclusive,
                merge_scenes=req.merge_scenes,
                roi=roi if req.selection_mode in ("rectangle",) or req.rotation_deg else None,
                bbox=bbox if req.selection_mode == "rectangle" else None,
                platform=req.platform,
                **({'color_balance': True} if req.color_balance else {}),
                **({'freeze_assets': True} if _frozen_download_supported(req.dataset, req.selection_mode) else {}),
                **({'composite_method': req.composite_method, **mosaic_options} if mosaic_options else {}),
            )
    except InsufficientCoverageError:
        raise
    except HTTPException:
        raise
    except Exception as exc:
        msg = str(exc)
        if "planet-nicfi" in msg and (
            "not found" in msg or "does not exist" in msg or "access" in msg.lower()
        ):
            raise HTTPException(
                403,
                "NICFI collection not accessible. Register at "
                "https://docs.planet.com/platform/integrations/google-earth-engine/nicfi-gee/ "
                "and grant the service account access to the Planet NICFI asset.",
            ) from exc
        raise HTTPException(502, f"Earth Engine query failed: {exc}") from exc

    if scene is None:
        raise HTTPException(
            404,
            f"Không có ảnh Sentinel-2{req.platform} trong khoảng thời gian/vùng đã chọn."
            if req.platform is not None else
            "No image found for this selection/date range. "
            "Try widening the date range or increasing cloud tolerance.",
        )

    bbox_dict = _bbox_dict(bbox)

    if req.dataset == "sentinel2":
        bands = s2_export_bands(req.s2_auxiliary_bands, s2_band_preset=req.s2_band_preset)
    elif req.dataset == "nicfi":
        bands = ["B", "G", "R", "N"]
    elif req.dataset == "dynamic_world":
        bands = ["label"]
    else:
        bands = ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]

    selection_token = None
    if _frozen_download_supported(req.dataset, req.selection_mode):
        from .download_snapshots import freeze_selection, provider_scope, restore_selection
        from dataclasses import replace
        snapshot = freeze_selection(scene, req.download_parameters(), provider_scope=provider_scope())
        _get_download_store().create(snapshot)
        selection_token = snapshot.token
        scene = replace(scene, ee_object=restore_selection(snapshot))
    try:
        preview_url = measure_call(
            "preview_url",
            preview_rgb,
            scene.ee_object,
            req.dataset,
            bbox,
            roi if province is not None or req.selection_mode == "rectangle" or req.rotation_deg else None,
        )
    except Exception:
        preview_url = None

    download_path = f"/api/download/{req.dataset}"
    if req.selection_mode == 'vector':
        download_params = (f'?selection_mode=vector&vector_roi_id={req.vector_roi_id}'
                           f'&start_date={req.start_date}&end_date={req.end_date}')
    elif province is not None:
        selection_query = (
            f"selection_mode=commune&commune_id={province.id}"
            if isinstance(province, Commune)
            else f"selection_mode=province&province_id={province.id}"
        )
        download_params = (
            f"?{selection_query}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
        )
    elif req.selection_mode == "rectangle":
        download_params = (
            f"?selection_mode=rectangle&center_lon={req.center_lon}&center_lat={req.center_lat}"
            f"&width_m={req.width_m}&height_m={req.height_m}&rotation_deg={req.rotation_deg}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
        )
    else:
        download_params = (
            f"?selection_mode=point&lon={req.lon}&lat={req.lat}&radius_m={req.radius_m}&rotation_deg={req.rotation_deg}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
        )

    point_merge = province is None and req.merge_scenes and req.dataset in ("sentinel2", "landsat")
    if province is not None:
        download_params += f"&composite_method={req.composite_method}"
    elif point_merge:
        download_params += f"&merge_scenes=true&composite_method={req.composite_method}"
    if req.platform is not None:
        download_params += f"&platform={req.platform}"
    if req.dataset == 'sentinel2' and req.s2_auxiliary_bands is not None:
        download_params += '&s2_auxiliary_bands=' + ','.join(req.s2_auxiliary_bands)
    if req.s2_band_preset is not None:
        download_params += f'&s2_band_preset={req.s2_band_preset}'

    if req.color_balance and not mosaic_options:
        key = _color_snapshot_key(req.dataset, req.start_date, req.end_date, req.selection_mode,
            bbox, province, req.rotation_deg, platform=req.platform, lon=req.lon, lat=req.lat,
            radius_m=req.radius_m, center_lon=req.center_lon, center_lat=req.center_lat,
            width_m=req.width_m, height_m=req.height_m, s2_band_preset=req.s2_band_preset)
        token = _color_snapshots.put(key, scene)
        download_params += f'&color_balance=true&color_snapshot={token}'
    elif req.color_balance:
        download_params += '&color_balance=true'

    if mosaic_options:
        download_params += (f'&scene_grouping={req.scene_grouping}&max_scene_span_days={req.max_scene_span_days}'
                            f'&mosaic_mask_clouds={str(req.mosaic_mask_clouds).lower()}')
        base = _color_snapshot_key(req.dataset, req.start_date, req.end_date, req.selection_mode,
            bbox, province, req.rotation_deg, platform=req.platform, lon=req.lon, lat=req.lat,
            radius_m=req.radius_m, center_lon=req.center_lon, center_lat=req.center_lat,
            width_m=req.width_m, height_m=req.height_m, s2_band_preset=req.s2_band_preset)
        key = _mosaic_snapshot_key(base, req.composite_method, req.scene_grouping,
                                  req.max_scene_span_days, req.mosaic_mask_clouds, req.color_balance,
                                  req.merge_scenes and req.selection_mode in ('point', 'rectangle'))
        download_params += '&mosaic_snapshot=' + _mosaic_snapshots.put(key, scene)

    if selection_token:
        download_params += f'&selection_token={selection_token}'

    sources = _optical_scene_footprints(scene.scenes, req.dataset)
    footprint = source_footprint(sources)
    if footprint is None and not sources:
        try:
            geometry = scene.footprint_geometry
            if geometry is None:
                geometry = imagery.scene_geometry(scene.ee_object)
            footprint = source_footprint([{'footprint': geometry.getInfo()}])
        except Exception:
            pass

    return SceneResponse(
        dataset=req.dataset,
        platform=req.platform,
        scene_id=scene.scene_id,
        date=scene.date,
        cloud_pct=scene.cloud_pct,
        bbox=bbox_dict,
        footprint=footprint,
        roi_geometry=(
            province.geometry
            if province
            else _rectangle_model(
                req.center_lon, req.center_lat, req.width_m, req.height_m, req.rotation_deg
            ).geometry
            if req.selection_mode == "rectangle"
            else point_roi(req.lon, req.lat, req.radius_m, req.rotation_deg)[0]
        ),
        rotation_deg=req.rotation_deg,
        scale_m=_scale_for(req.dataset),
        bands=bands,
        preview_url=preview_url,
        download_url=download_path + download_params,
        selection_mode=req.selection_mode,
        vector_roi_id=req.vector_roi_id,
        province_id=province.id if isinstance(province, (Province, Commune)) else None,
        province_name=(province.province_name if isinstance(province, Commune) else province.name) if isinstance(province, (Province, Commune)) else None,
        commune_id=province.id if isinstance(province, Commune) else None,
        commune_name=province.name if isinstance(province, Commune) else None,
        composite=province is not None or point_merge,
        coverage_pct=scene.coverage_pct,
        composite_method=(
            "dynamic-world-mode"
            if province is not None and req.dataset == "dynamic_world"
            else req.composite_method if province is not None or point_merge else None
        ),
        scene_count=scene.scene_count,
        scenes=sources,
        estimated_seconds=scene.estimated_seconds,
        color_balance=scene.color_balance if req.color_balance else None,
        scene_grouping=getattr(scene, 'scene_grouping', None),
        mosaic_mask_clouds=getattr(scene, 'mosaic_mask_clouds', None),
        selection_token=selection_token,
        download_job_available=bool(selection_token and os.getenv('GEE_DOWNLOAD_JOBS') == '1'),
    )


@app.post("/api/composite-plan")
def plan_composite(req: SceneRequest) -> dict:
    """Preview the cost and scenes before running a province composite."""
    measure_call("ee_init", _ensure_ee)
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be <= end_date")
    if req.selection_mode not in ("province", "commune", "vector"):
        raise HTTPException(400, "Composite planning is only available for administrative selection")
    if req.dataset not in ("sentinel2", "landsat"):
        raise HTTPException(400, "Composite planning is only available for Sentinel-2 or Landsat")

    roi, _bbox, _province = measure_call("selection", _resolve_selection,
        req.selection_mode, req.lon, req.lat, req.radius_m, req.province_id, req.commune_id, req.rotation_deg,
        **({'vector_roi_id': req.vector_roi_id} if req.vector_roi_id else {}),
    )
    end_exclusive = (dt.date.fromisoformat(req.end_date) + dt.timedelta(days=1)).isoformat()
    try:
        plan = measure_call("composite_plan", composite_plan,
            req.dataset, roi, req.start_date, end_exclusive, req.composite_method,
            platform=req.platform,
            **_mosaic_options(req.scene_grouping, req.max_scene_span_days, req.mosaic_mask_clouds, req.composite_method, req.dataset),
        )
    except Exception as exc:
        raise HTTPException(502, f"Earth Engine planning failed: {exc}") from exc
    if plan is None:
        raise HTTPException(404, f"Không có ảnh Sentinel-2{req.platform} trong khoảng thời gian/vùng đã chọn."
                            if req.platform is not None else "No image found for this selection/date range")
    if plan.get('scenes'):
        scenes = _optical_scene_footprints(plan['scenes'], req.dataset)
        plan = {**plan, 'scenes': _component_thumbnail_links(scenes, req.dataset, getattr(_province, 'geometry', None))}
    return plan


@app.get("/api/download/{dataset}")
@bulk_export
def download(
    dataset: str,
    start_date: str,
    end_date: str,
    selection_mode: Literal["point", "rectangle", "province", "commune", "vector"] = "point",
    vector_roi_id: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
    center_lon: float | None = None,
    center_lat: float | None = None,
    width_m: float | None = None,
    height_m: float | None = None,
    radius_m: float = 2000.0,
    province_id: str | None = None,
    commune_id: str | None = None,
    merge_scenes: bool = False,
    composite_method: Literal[
        "mosaic", "median", "medoid", "base-score", "raw"
    ] = "raw",
    rotation_deg: float = 0.0,
    platform: str | None = None,
    s2_auxiliary_bands: str | None = None,
    s2_band_preset: S2BandPreset | None = None,
    color_balance: bool = False,
    color_snapshot: str | None = None,
    selection_token: str | None = None,
    scene_grouping: Literal['any', 'near_date', 'same_pass'] = 'any',
    max_scene_span_days: int = 3,
    mosaic_mask_clouds: bool = True,
    mosaic_snapshot: str | None = None,
):
    auxiliary_bands = None if s2_auxiliary_bands is None else s2_auxiliary_bands.split(',') if s2_auxiliary_bands else []
    try:
        platform = validate_platform(dataset, platform)
        _validate_color_balance(color_balance, dataset, selection_mode, merge_scenes, composite_method)
        _validate_mosaic_options(dataset, selection_mode, merge_scenes, composite_method,
                                scene_grouping, max_scene_span_days, mosaic_mask_clouds)
        mosaic_options = _mosaic_options(scene_grouping, max_scene_span_days, mosaic_mask_clouds, composite_method, dataset)
        if mosaic_snapshot and (not mosaic_options or len(mosaic_snapshot) > 128):
            raise ValueError('Invalid mosaic snapshot')
        if color_snapshot and (not color_balance or len(color_snapshot) > 128):
            raise ValueError('color_snapshot requires color_balance=true and a valid snapshot token')
        if auxiliary_bands is not None:
            if dataset != 'sentinel2':
                raise ValueError('s2_auxiliary_bands requires dataset=sentinel2')
            s2_export_bands(auxiliary_bands)
        _validate_s2_band_preset(dataset, s2_band_preset, auxiliary_bands)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    measure_download('provider_readiness', _ensure_ee)
    if dataset not in ("sentinel2", "landsat", "nicfi", "dynamic_world"):
        raise HTTPException(
            400, "dataset must be sentinel2, landsat, nicfi or dynamic_world"
        )
    roi, bbox, province = _resolve_selection(
        selection_mode,
        lon,
        lat,
        radius_m,
        province_id,
        commune_id,
        rotation_deg,
        center_lon,
        center_lat,
        width_m,
        height_m,
        **({'vector_roi_id': vector_roi_id} if vector_roi_id else {}),
    )

    end_exclusive = (
        dt.date.fromisoformat(end_date) + dt.timedelta(days=1)
    ).isoformat()

    if selection_token:
        from .download_snapshots import provider_scope, restore_selection
        from .download_store import SelectionExpired, SelectionMismatch
        from .imagery import SceneInfo
        request = SceneRequest(dataset=dataset, start_date=start_date, end_date=end_date,
            selection_mode=selection_mode, vector_roi_id=vector_roi_id, lon=lon, lat=lat,
            center_lon=center_lon, center_lat=center_lat, width_m=width_m, height_m=height_m,
            radius_m=radius_m, province_id=province_id, commune_id=commune_id,
            merge_scenes=merge_scenes, composite_method=composite_method,
            rotation_deg=rotation_deg, platform=platform, s2_auxiliary_bands=auxiliary_bands,
            s2_band_preset=s2_band_preset,
            color_balance=color_balance, scene_grouping=scene_grouping,
            max_scene_span_days=max_scene_span_days, mosaic_mask_clouds=mosaic_mask_clouds)
        try:
            snapshot = _get_download_store().get(selection_token, request=request.download_parameters(),
                                                 provider_scope=provider_scope())
        except SelectionExpired as exc:
            raise HTTPException(410, str(exc)) from None
        except SelectionMismatch as exc:
            raise HTTPException(409, str(exc)) from None
        scene = SceneInfo(**snapshot.scene, ee_object=restore_selection(snapshot), selection_recipe=snapshot.recipe)
    elif mosaic_snapshot:
        base = _color_snapshot_key(dataset, start_date, end_date, selection_mode, bbox, province,
            rotation_deg, platform=platform, lon=lon, lat=lat, radius_m=radius_m,
            center_lon=center_lon, center_lat=center_lat, width_m=width_m, height_m=height_m,
            s2_band_preset=s2_band_preset)
        key = _mosaic_snapshot_key(base, composite_method, scene_grouping, max_scene_span_days,
                                  mosaic_mask_clouds, color_balance, merge_scenes and selection_mode in ('point', 'rectangle'))
        try:
            scene = _mosaic_snapshots.get(mosaic_snapshot, key)
        except LookupError as exc:
            raise HTTPException(410, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
    elif color_snapshot:
        key = _color_snapshot_key(dataset, start_date, end_date, selection_mode, bbox, province,
            rotation_deg, platform=platform, lon=lon, lat=lat, radius_m=radius_m,
            center_lon=center_lon, center_lat=center_lat, width_m=width_m, height_m=height_m,
            s2_band_preset=s2_band_preset)
        try:
            scene = _color_snapshots.get(color_snapshot, key)
        except LookupError as exc:
            raise HTTPException(410, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
    elif province is not None:
        if dataset not in ("sentinel2", "landsat"):
            composite_method = "mosaic"
        scene = measure_download('selection', best_scene_for_roi,
            dataset,
            roi,
            start_date,
            end_exclusive,
            composite=True,
            composite_method=composite_method,
            platform=platform,
            **mosaic_options,
            **({'color_balance': True} if color_balance else {}),
        )
    else:
        scene = measure_download('selection', best_scene,
            dataset=dataset,
            lon=center_lon if selection_mode == "rectangle" else lon,
            lat=center_lat if selection_mode == "rectangle" else lat,
            radius_m=(max(width_m, height_m) / 2
                      if selection_mode == "rectangle" else radius_m),
            start_date=start_date,
            end_date=end_exclusive,
            merge_scenes=merge_scenes,
            roi=roi if selection_mode == "rectangle" or rotation_deg % 360 else None,
            bbox=bbox if selection_mode == "rectangle" else None,
            platform=platform,
            **({'color_balance': True} if color_balance else {}),
            **({'composite_method': composite_method, **mosaic_options} if mosaic_options else {}),
        )
    if scene is None:
        raise HTTPException(404, f"Không có ảnh Sentinel-2{platform} trong khoảng thời gian/vùng đã chọn."
                            if platform is not None else "No scene found for this point/date range")

    scale = _scale_for(dataset)
    rectangle = (
        _rectangle_model(center_lon, center_lat, width_m, height_m, rotation_deg)
        if selection_mode == "rectangle"
        else None
    )
    output_grid = (
        rectangle.output_grid(scale)
        if rectangle is not None
        else build_rotated_grid(lon, lat, radius_m, rotation_deg, scale)
        if rotation_deg % 360
        else None
    )
    if output_grid:
        bbox = source_bbox(
            bbox,
            scale,
            rectangle.center_lat if rectangle is not None else lat,
        )
    optimized = bool(selection_token and os.getenv('GEE_DOWNLOAD_TRANSPORT') == '1')
    band_urls = [] if optimized else measure_download('planning_probe', download_geotiff,
        scene.ee_object,
        dataset,
        bbox,
        scale,
        roi if province is not None else None,
        # Keep the interpolation halo for rectangle grids; warp applies the final footprint.
        # Medoid/base-score có graph nhiều scene; tile quá lớn có thể chết
        # "User memory limit exceeded" lúc fetch, nên chia sẵn theo diện tích.
        max_tile_pixels=MEDOID_MAX_TILE_PIXELS
        if province is not None and composite_method in ("medoid", "base-score")
        and dataset in ("sentinel2", "landsat")
        else None,
        **({'s2_auxiliary_bands': auxiliary_bands} if auxiliary_bands is not None else {}),
        **({'s2_band_preset': s2_band_preset} if s2_band_preset is not None else {}),
    )

    if province is not None:
        method_tag = (
            composite_method
            if dataset in ("sentinel2", "landsat")
            else "mode" if dataset == "dynamic_world" else "mosaic"
        )
        fname = f"{dataset}_{province.id}_{start_date}_{end_date}_{method_tag}.tif"
    else:
        fname = f"{dataset}_{scene.scene_id}_{scene.date}.tif"
    if output_grid:
        if rectangle is not None:
            fname = (
                f"{Path(fname).stem}_rect{rectangle.center_lon:.5f}_{rectangle.center_lat:.5f}"
                f"_w{rectangle.width_m:g}_h{rectangle.height_m:g}_a{rectangle.rotation_deg:g}.tif"
            )
        else:
            fname = f"{Path(fname).stem}_pt{lon:.5f}_{lat:.5f}_r{radius_m:g}_a{rotation_deg % 360:g}.tif"
    out = _DOWNLOADS_DIR / f"{uuid.uuid4().hex}.tif"
    if color_balance:
        fname = f'{Path(fname).stem}_balanced.tif'
    if mosaic_options:
        suffix = ('masked' if mosaic_mask_clouds else 'unmasked') if composite_method == 'mosaic' else 'raw'
        fname = f'{Path(fname).stem}_{suffix}_{scene_grouping}.tif'
    if s2_band_preset == 'rgbnir':
        fname = f'{Path(fname).stem}_rgbnir.tif'
    if optimized:
        from .download_pipeline import execute_selection
        from .download_models import DownloadSettings
        from .download_store import AttemptBusy
        from .download_api import ArtifactResponse
        try:
            receipt = execute_selection(snapshot, _get_download_store(),
                settings=DownloadSettings(vrt=os.getenv('GEE_DOWNLOAD_VRT') == '1'))
        except AttemptBusy as exc:
            raise HTTPException(409, str(exc)) from None
        except (RuntimeError, ValueError, TimeoutError, OSError, requests.RequestException) as exc:
            logging.getLogger(__name__).error('Frozen GeoTIFF export failed: error_type=%s',type(exc).__name__)
            raise HTTPException(502, 'Không thể hoàn tất tải ảnh; các ô đã kiểm tra được giữ để tiếp tục.') from exc
        return ArtifactResponse(_get_download_store(), receipt.tile_key, fname)
    raw = out.with_suffix(".source.tif") if output_grid else out
    try:
        measure_download('transfer_assembly', merge_band_tiffs,
            band_urls, raw, categorical=dataset == "dynamic_world",
            **({"preserve_masks": True} if output_grid or selection_mode == 'vector' or color_balance or mosaic_options else {})
        )
        if output_grid:
            measure_download('warp_compress', warp_to_grid, raw, out, output_grid, categorical=dataset == "dynamic_world")
        if selection_mode == 'vector':
            from .vector_mask import mask_raster_to_polygon
            mask_raster_to_polygon(out, province.geometry)
        if color_balance:
            with rasterio.open(out, 'r+') as calibrated:
                calibrated.update_tags(COLOR_BALANCE=json.dumps(scene.color_balance, ensure_ascii=True,
                                                               allow_nan=False),
                                       COMPOSITE_METHOD='raw_color_balanced')
        if mosaic_options:
            from .scene_grouping import export_tags
            with rasterio.open(out, 'r+') as target:
                target.update_tags(**export_tags(scene.__dict__))
    except requests.RequestException as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(502, f"Failed to download GeoTIFF from GEE: {exc}") from exc
    except RuntimeError as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(502, f"Failed to merge band GeoTIFFs: {exc}") from exc
    except (RasterioError, OSError) as exc:
        out.unlink(missing_ok=True)
        logging.getLogger(__name__).exception("Local GeoTIFF write failed: %s", out.name)
        raise HTTPException(500, "Failed to write GeoTIFF locally. Check available disk space and server logs.") from exc
    finally:
        if output_grid:
            raw.unlink(missing_ok=True)

    return FileResponse(
        path=out,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# Static elevation products (no date or cloud filtering).
# ---------------------------------------------------------------------------


@app.get("/api/dem/download")
def dem_download(
    dataset: Literal["alos", "srtm", "copernicus"] = "alos",
    selection_mode: Literal["point", "rectangle", "province", "commune", "vector"] = "point",
    vector_roi_id: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float = 2000.0,
    center_lon: float | None = None,
    center_lat: float | None = None,
    width_m: float | None = None,
    height_m: float | None = None,
    rotation_deg: float = 0.0,
    province_id: str | None = None,
    commune_id: str | None = None,
):
    import ee
    from .dem import (elevation_filename, elevation_grid, elevation_image,
                      finalize_elevation, mask_elevation)

    try:
        _validate_selection_fields(selection_mode, lon, lat, radius_m,
                                   center_lon, center_lat, width_m, height_m, rotation_deg)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _ensure_ee()
    roi, bbox, area = _resolve_selection(
        selection_mode, lon, lat, radius_m, province_id, commune_id,
        rotation_deg, center_lon, center_lat, width_m, height_m,
        **({'vector_roi_id': vector_roi_id} if vector_roi_id else {}),
    )
    rectangle = (_rectangle_model(center_lon, center_lat, width_m, height_m, rotation_deg)
                 if selection_mode == "rectangle" else None)
    grid = elevation_grid(bbox)
    if selection_mode == 'vector':
        roi_geometry = area.geometry
    elif rectangle:
        roi_geometry = rectangle.geometry
    elif selection_mode == "point":
        roi_geometry = point_roi(lon, lat, radius_m, rotation_deg)[0]
    else:
        roi_geometry = roi.getInfo()
    bbox = source_bbox(bbox, 30, (bbox.south + bbox.north) / 2)
    if rectangle:
        region_tag = (f"rect_lon{rectangle.center_lon:.5f}_lat{rectangle.center_lat:.5f}"
                      f"_w{rectangle.width_m:g}_h{rectangle.height_m:g}_a{rectangle.rotation_deg:g}")
    elif selection_mode == "point":
        region_tag = f"pt_lon{lon:.5f}_lat{lat:.5f}_r{radius_m:g}_a{rotation_deg % 360:g}"
    elif selection_mode == 'vector':
        region_tag = f'vector_{vector_roi_id}'
    else:
        region_tag = f"{selection_mode}_{province_id if selection_mode == 'province' else commune_id}"
    filename = elevation_filename(dataset, region_tag)
    out = _DOWNLOADS_DIR / filename
    raw = out.with_suffix(".source.tif") if grid else out
    try:
        image = elevation_image(dataset, roi)
        if dataset == "srtm" and (bbox.south < -56 or bbox.north > 60):
            raise HTTPException(400, "SRTM chỉ phủ khoảng 56°S–60°N. Chọn ALOS hoặc Copernicus.")
        has_data = image.reduceRegion(
            reducer=ee.Reducer.count(), geometry=roi,
            scale=30, maxPixels=100_000_000, bestEffort=True,
        ).get("elevation").getInfo()
        if not has_data:
            raise HTTPException(404, "Không có dữ liệu độ cao tại vùng đã chọn.")
        urls = download_geotiff(image, "dem", bbox, 30, None if grid else roi,
                                max_tile_pixels=4_000_000)
        merge_band_tiffs(urls, raw, preserve_masks=True)
        finalize_elevation(raw)
        if grid:
            warp_to_grid(raw, out, grid)
            finalize_elevation(out)
            mask_elevation(out, roi_geometry)
        if selection_mode == 'vector':
            from .vector_mask import mask_raster_to_polygon
            mask_raster_to_polygon(out, roi_geometry)
    except HTTPException:
        out.unlink(missing_ok=True)
        raise
    except ValueError as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        out.unlink(missing_ok=True)
        raise HTTPException(502, f"Không thể tải DEM từ GEE: {exc}") from exc
    finally:
        if grid:
            raw.unlink(missing_ok=True)
    return FileResponse(out, media_type="image/tiff", filename=filename)


# ---------------------------------------------------------------------------
# Sentinel-1 SAR trên GEE (không cần tài khoản CDSE)
# ---------------------------------------------------------------------------


class S1SceneRequest(PointSelectionRequest):
    selection_mode: Literal["point", "rectangle", "province", "commune", "vector"] = "point"
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    center_lon: float | None = Field(None, ge=-180, le=180)
    center_lat: float | None = Field(None, ge=-90, le=90)
    width_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    height_m: float | None = Field(None, gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    province_id: str | None = Field(None, min_length=1, max_length=20)
    commune_id: str | None = Field(None, min_length=1, max_length=20)
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    radius_m: float = Field(2000.0, gt=0, le=_MAX_RADIUS_M)
    polarization: str = Field("vvvh", pattern="^(vvvh|vv|hhhv|hh)$")
    merge_scenes: bool = False
    group_by_orbit: bool = False
    list_scenes: bool = False
    platform: str | None = None

    @field_validator("platform")
    @classmethod
    def valid_platform(cls, value):
        return validate_platform('s1gee', value)

    @field_validator("start_date", "end_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        try:
            dt.date.fromisoformat(v)
        except ValueError:
            raise ValueError("Date must be in YYYY-MM-DD format")
        return v


@app.post("/api/s1gee/scene")
def find_s1_scene(req: S1SceneRequest):
    """Tìm ảnh Sentinel-1 GRD trên GEE theo độ phủ ROI."""
    _ensure_ee()
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be <= end_date")

    roi, bbox, province = _resolve_selection(
        req.selection_mode,
        req.lon,
        req.lat,
        req.radius_m,
        req.province_id,
        req.commune_id,
        req.rotation_deg,
        req.center_lon,
        req.center_lat,
        req.width_m,
        req.height_m,
        **({'vector_roi_id': req.vector_roi_id} if req.vector_roi_id else {}),
    )

    end_exclusive = (
        dt.date.fromisoformat(req.end_date) + dt.timedelta(days=1)
    ).isoformat()
    from .s1_mosaic import list_s1_results, prepare_s1, province_search_roi, search_s1_by_orbit
    if req.list_scenes:
        province_search = req.selection_mode == 'province' and isinstance(province, Province)
        try:
            search_roi = province_search_roi(province) if province_search else roi
            results = list_s1_results(search_roi, req.start_date, end_exclusive, req.polarization,
                                      req.merge_scenes, req.platform)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"Earth Engine query failed: {exc}") from exc
        items = [_s1_scene_response(req, item, roi, bbox, province, direction)
                 for direction, item in results]
        geometry = items[0]['roi_geometry'] if items else None
        if items:
            sources = {source['scene_id']: source for item in items for source in item['scenes']}
            thumbnail_geometry = geometry
            if province_search:
                from shapely.geometry import box, mapping
                thumbnail_geometry = mapping(box(bbox.west, bbox.south, bbox.east, bbox.north))
            thumbnails = _component_thumbnail_links(list(sources.values()), 's1gee',
                                                    thumbnail_geometry, req.polarization)
            by_id = {source['scene_id']: source for source in thumbnails}
            for item in items:
                item.pop('roi_geometry')
                item['scenes'] = [by_id[source['scene_id']] for source in item['scenes']]
                if not item['composite']:
                    item['preview_url'] = item['scenes'][0].get('thumbnail_url')
        return {'results': items, 'roi_geometry': geometry}
    try:
        if req.group_by_orbit:
            by_orbit = search_s1_by_orbit(roi, req.start_date, end_exclusive, req.polarization,
                                         req.merge_scenes, req.platform)
            if by_orbit:
                return {'results': [_s1_scene_response(req, result, roi, bbox, province, orbit_pass)
                                    for orbit_pass, result in by_orbit.items()]}
            scene = None
        elif province is not None:
            scene = best_s1_scene_for_roi(
                roi, req.start_date, end_exclusive, req.polarization, platform=req.platform
            )
        else:
            scene = best_s1_scene(
                lon=req.center_lon if req.selection_mode == "rectangle" else req.lon,
                lat=req.center_lat if req.selection_mode == "rectangle" else req.lat,
                radius_m=(max(req.width_m, req.height_m) / 2
                          if req.selection_mode == "rectangle" else req.radius_m),
                start_date=req.start_date,
                end_date=end_exclusive,
                polarization=req.polarization,
                roi=roi if req.selection_mode == "rectangle" or req.rotation_deg else None,
                platform=req.platform,
            )
    except Exception as exc:
        raise HTTPException(502, f"Earth Engine query failed: {exc}") from exc

    if scene is None:
        raise HTTPException(
            404,
            f"Không có ảnh Sentinel-1{req.platform} trong khoảng thời gian/vùng đã chọn."
            if req.platform is not None else "No Sentinel-1 scene found for this selection/date range.",
        )

    try:
        scene = prepare_s1(scene, roi, req.start_date, end_exclusive,
                           req.polarization, req.merge_scenes, platform=req.platform)
    except Exception as exc:
        raise HTTPException(502, f"Không thể đánh giá/ghép độ phủ SAR: {exc}") from exc
    return _s1_scene_response(req, scene, roi, bbox, province)


def _s1_scene_response(req, scene, roi, bbox, province, orbit_pass=None):
    preview_url = None
    if req.list_scenes:
        bands = imagery.S1_POLARIZATIONS[req.polarization]
    else:
        try:
            available = set(scene.ee_object.bandNames().getInfo())
        except Exception:
            available = set()
        bands = [b for b in ["VV", "VH", "HH", "HV"] if b in available]
        try:
            preview_url = preview_rgb(
                scene.ee_object, "s1gee", bbox,
                roi if province is not None or req.selection_mode == "rectangle" or req.rotation_deg else None,
            )
        except Exception:
            pass

    if req.selection_mode == 'vector':
        download_params = (f'?selection_mode=vector&vector_roi_id={req.vector_roi_id}'
                           f'&start_date={req.start_date}&end_date={req.end_date}&polarization={req.polarization}')
    elif province is not None:
        selection_query = (
            f"selection_mode=commune&commune_id={province.id}"
            if isinstance(province, Commune)
            else f"selection_mode=province&province_id={province.id}"
        )
        download_params = (
            f"?{selection_query}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
            f"&polarization={req.polarization}"
        )
    elif req.selection_mode == "rectangle":
        download_params = (
            f"?selection_mode=rectangle&center_lon={req.center_lon}&center_lat={req.center_lat}"
            f"&width_m={req.width_m}&height_m={req.height_m}&rotation_deg={req.rotation_deg}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
            f"&polarization={req.polarization}"
        )
    else:
        download_params = (
            f"?selection_mode=point&lon={req.lon}&lat={req.lat}&radius_m={req.radius_m}&rotation_deg={req.rotation_deg}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
            f"&polarization={req.polarization}"
        )
    if req.platform is not None:
        download_params += f"&platform={req.platform}"
    if orbit_pass:
        source_ids = [source['scene_id'] for source in scene.scenes]
        download_params += '&' + urlencode({'orbit_pass': orbit_pass, 'scene_ids': source_ids}, doseq=True)
    full_scene_link = {}
    if (scene.scene_count or 1) == 1 and len(scene.scenes) <= 1:
        full_query = {'extent': 'scene', 'scene_ids': scene.scene_id, 'polarization': req.polarization,
                      'start_date': req.start_date, 'end_date': req.end_date}
        if req.platform is not None:
            full_query['platform'] = req.platform
        if orbit_pass is not None:
            full_query['orbit_pass'] = orbit_pass
        full_scene_link['full_scene_download_url'] = '/api/s1gee/download?' + urlencode(full_query)
    geometry = (province.geometry if province else
                _rectangle_model(req.center_lon, req.center_lat, req.width_m, req.height_m, req.rotation_deg).geometry
                if req.selection_mode == 'rectangle' else
                point_roi(req.lon, req.lat, req.radius_m, req.rotation_deg)[0])
    return {
        "dataset": "s1gee",
        "orbit_pass": orbit_pass,
        "platform": req.platform,
        "scene_id": scene.scene_id,
        "date": scene.date,
        "cloud_pct": scene.cloud_pct,
        "bbox": _bbox_dict(bbox),
        "roi_geometry": geometry,
        "footprint": source_footprint(scene.scenes),
        "rotation_deg": req.rotation_deg,
        "scale_m": S1_SCALE,
        "bands": bands,
        "preview_url": preview_url,
        "download_url": f"/api/s1gee/download{download_params}&merge_scenes={str(req.merge_scenes).lower()}",
        **full_scene_link,
        "coverage_pct": scene.coverage_pct,
        "coverage_scale_m": 100,
        "scene_count": scene.scene_count,
        "scenes": (list(scene.scenes) if req.list_scenes else
                   _component_thumbnail_links(list(scene.scenes), 's1gee', geometry, req.polarization)),
        "composite": (scene.scene_count or 1) > 1,
        "warning": ("Độ phủ SAR dưới 99%; vẫn cho tải phần có dữ liệu. Hãy mở rộng khoảng ngày hoặc thu nhỏ ROI."
                    if scene.coverage_pct is not None and scene.coverage_pct < 99 else None),
        "selection_mode": req.selection_mode,
        "vector_roi_id": req.vector_roi_id,
        "province_id": province.id if isinstance(province, (Province, Commune)) else None,
        "province_name": (province.province_name if isinstance(province, Commune) else province.name) if isinstance(province, (Province, Commune)) else None,
        "commune_id": province.id if isinstance(province, Commune) else None,
        "commune_name": province.name if isinstance(province, Commune) else None,
    }


def _download_s1_full_scene(scene_ids, start_date, end_date, polarization, platform, orbit_pass):
    from .full_scene_export import FullSceneDiskSpaceError, export_s1_full_scene
    from .s1_mosaic import restore_s1_sources

    try:
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
        if start > end:
            raise ValueError
        end_exclusive = (end + dt.timedelta(days=1)).isoformat()
    except (ValueError, OverflowError):
        raise HTTPException(422, 'Khoảng ngày tìm cảnh SAR không hợp lệ.') from None

    scene_id = scene_ids[0]
    asset_id = f'{imagery.S1_COLLECTION}/{scene_id}'
    filename = f'{scene_id}_{polarization}_full_scene.tif'
    out = _DOWNLOADS_DIR / f'{uuid.uuid4().hex}.tif'
    scene_export = CURRENT_SCENE.get()
    if scene_export is not None:
        scene_export.check()
        scene_export.result = out
        scene_export.temporary_result = True
        scene_export.update(filename=filename)

    exported = False
    try:
        _ensure_ee()
        with scene_download_admission():
            try:
                scene = restore_s1_sources(scene_ids, None, start.isoformat(), end_exclusive,
                                           polarization, platform, orbit_pass)
            except ValueError:
                raise HTTPException(422, 'Cảnh SAR không khớp khoảng ngày, vệ tinh, hướng bay hoặc phân cực.') from None
            export_s1_full_scene(asset_id, scene, out, polarization=polarization)
        exported = True
    except (HTTPException, QueueTimeout, TransferCancelled):
        raise
    except FullSceneDiskSpaceError:
        raise HTTPException(507, 'Không đủ dung lượng để xuất GeoTIFF SAR nguyên cảnh.') from None
    except Exception as exc:
        logging.getLogger(__name__).warning('Full Sentinel-1 export failed: %s', type(exc).__name__)
        raise HTTPException(502, 'Không tạo được GeoTIFF Sentinel-1 nguyên cảnh từ GEE.') from None
    finally:
        if not exported and scene_export is None:
            with suppress(OSError):
                out.unlink(missing_ok=True)
    background = BackgroundTask(out.unlink, missing_ok=True) if scene_export is None else None
    return FileResponse(out, media_type='image/tiff', filename=filename, background=background)


@app.get("/api/s1gee/download")
def s1gee_download(
    start_date: str,
    end_date: str,
    selection_mode: Literal["point", "rectangle", "province", "commune", "vector"] = "point",
    vector_roi_id: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
    center_lon: float | None = None,
    center_lat: float | None = None,
    width_m: float | None = None,
    height_m: float | None = None,
    radius_m: float = 2000.0,
    polarization: str = "vvvh",
    province_id: str | None = None,
    commune_id: str | None = None,
    rotation_deg: float = 0.0,
    merge_scenes: bool = False,
    platform: str | None = None,
    orbit_pass: Literal['ASCENDING', 'DESCENDING'] | None = None,
    scene_ids: Annotated[list[str] | None, Query(max_length=25)] = None,
    extent: Literal['roi', 'scene'] = 'roi',
):
    """Tải GeoTIFF S1 GRD (dB) theo vùng đã chọn hoặc nguyên một cảnh."""
    try:
        platform = validate_platform('s1gee', platform)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if polarization not in imagery.S1_POLARIZATIONS:
        raise HTTPException(400, "polarization must be vvvh|vv|hhhv|hh")
    if scene_ids is not None and (
            not scene_ids or len(set(scene_ids)) != len(scene_ids) or
            any(not value.isascii() or not value.replace('_', '').isalnum() or len(value) > 100
                for value in scene_ids)):
        raise HTTPException(422, 'Danh sách cảnh SAR đã chọn không hợp lệ.')
    if extent == 'scene':
        if scene_ids is None or len(scene_ids) != 1:
            raise HTTPException(422, 'Chọn một cảnh SAR để tải nguyên cảnh.')
        return _download_s1_full_scene(scene_ids, start_date, end_date, polarization, platform, orbit_pass)
    if (scene_ids is None) != (orbit_pass is None) or (scene_ids and len(scene_ids) > 1 and not merge_scenes):
        raise HTTPException(422, 'Danh sách cảnh SAR đã chọn không hợp lệ.')
    _ensure_ee()
    roi, bbox, province = _resolve_selection(
        selection_mode,
        lon,
        lat,
        radius_m,
        province_id,
        commune_id,
        rotation_deg,
        center_lon,
        center_lat,
        width_m,
        height_m,
        **({'vector_roi_id': vector_roi_id} if vector_roi_id else {}),
    )

    end_exclusive = (
        dt.date.fromisoformat(end_date) + dt.timedelta(days=1)
    ).isoformat()
    pinned_province = selection_mode == 'province' and scene_ids is not None
    if scene_ids is not None:
        from .s1_mosaic import restore_s1_sources
        try:
            boundary = {'province_geometry': province.geometry} if pinned_province else {}
            validation_roi = bbox_to_ee_geom(bbox) if pinned_province else roi
            scene = restore_s1_sources(scene_ids, validation_roi, start_date, end_exclusive,
                                       polarization, platform, orbit_pass, **boundary)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f'Không thể mở các cảnh SAR đã chọn: {exc}') from exc
    elif province is not None:
        scene = best_s1_scene_for_roi(roi, start_date, end_exclusive, polarization, platform=platform)
    else:
        scene = best_s1_scene(
            center_lon if selection_mode == 'rectangle' else lon,
            center_lat if selection_mode == 'rectangle' else lat,
            max(width_m, height_m) / 2 if selection_mode == 'rectangle' else radius_m,
            start_date, end_exclusive, polarization,
            roi=roi if selection_mode == 'rectangle' or rotation_deg % 360 else None,
            platform=platform,
        )
    if scene is None:
        raise HTTPException(404, f"Không có ảnh Sentinel-1{platform} trong khoảng thời gian/vùng đã chọn."
                            if platform is not None else "No Sentinel-1 scene found for this point/date range")
    if merge_scenes and scene_ids is None:
        from .s1_mosaic import prepare_s1
        try:
            scene = prepare_s1(scene, roi, start_date, end_exclusive, polarization, True, platform=platform)
        except Exception as exc:
            raise HTTPException(502, f"Không thể ghép độ phủ SAR: {exc}") from exc

    rectangle = (
        _rectangle_model(center_lon, center_lat, width_m, height_m, rotation_deg)
        if selection_mode == "rectangle"
        else None
    )
    output_grid = (
        rectangle.output_grid(S1_SCALE)
        if rectangle is not None
        else build_rotated_grid(lon, lat, radius_m, rotation_deg, S1_SCALE)
        if rotation_deg % 360
        else None
    )
    if output_grid:
        bbox = source_bbox(
            bbox, S1_SCALE, rectangle.center_lat if rectangle is not None else lat
        )
    if not pinned_province:
        band_urls = download_geotiff(
            scene.ee_object,
            "s1gee",
            bbox,
            S1_SCALE,
            roi if province is not None else None,
        )

    scene_count = getattr(scene, "scene_count", None) or 1
    if scene_count > 1:
        direction = f'_{orbit_pass.lower()}' if orbit_pass else ''
        fname = f"s1gee_mosaic_{start_date}_{end_date}{direction}_n{scene_count}_{polarization}.tif"
    else:
        fname = f"{scene.scene_id}_{polarization}.tif"
    if province is not None:
        fname = f"{Path(fname).stem}_{selection_mode}_{province.id}.tif"
    elif not output_grid:
        fname = f"{Path(fname).stem}_pt{lon:.5f}_{lat:.5f}_r{radius_m:g}.tif"
    if output_grid:
        if rectangle is not None:
            fname = (
                f"{Path(fname).stem}_rect{rectangle.center_lon:.5f}_{rectangle.center_lat:.5f}"
                f"_w{rectangle.width_m:g}_h{rectangle.height_m:g}_a{rectangle.rotation_deg:g}.tif"
            )
        else:
            fname = f"{Path(fname).stem}_pt{lon:.5f}_{lat:.5f}_r{radius_m:g}_a{rotation_deg % 360:g}.tif"
    out = _DOWNLOADS_DIR / f"{uuid.uuid4().hex}.tif"
    raw = out.with_suffix(".source.tif") if output_grid else out
    try:
        if pinned_province:
            from .full_scene_export import (
                FullSceneDiskSpaceError, SceneDoesNotCoverProvince, export_s1_province,
            )
            footprint = source_footprint(scene.scenes)
            if footprint is None:
                raise HTTPException(502, 'GEE chưa trả footprint hợp lệ cho các cảnh SAR đã chọn.')
            try:
                export_s1_province(scene.ee_object, out, province_geometry=province.geometry,
                                   footprint=footprint, bbox=bbox,
                                   bands=imagery.S1_POLARIZATIONS[polarization])
            except FullSceneDiskSpaceError:
                raise HTTPException(507, 'Không đủ dung lượng để xuất GeoTIFF SAR theo tỉnh.') from None
            except SceneDoesNotCoverProvince:
                raise HTTPException(422, 'Các cảnh SAR đã chọn không phủ ranh giới tỉnh.') from None
        else:
            merge_band_tiffs(band_urls, raw, **({"preserve_masks": True} if output_grid or merge_scenes or scene_ids or selection_mode == 'vector' else {}))
        if output_grid:
            warp_to_grid(raw, out, output_grid)
        if selection_mode == 'vector':
            from .vector_mask import mask_raster_to_polygon
            mask_raster_to_polygon(out, province.geometry)
        from .s1_mosaic import write_s1_export_tags
        try:
            write_s1_export_tags(out, scene, polarization,
                                 extent='province' if pinned_province else 'roi', selection_mode=selection_mode)
        except (RasterioError, OSError):
            with suppress(OSError):
                out.unlink(missing_ok=True)
            raise HTTPException(502, 'Không ghi được metadata vào GeoTIFF Sentinel-1.') from None
    except requests.RequestException as exc:
        raise HTTPException(502, f"Failed to download GeoTIFF from GEE: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(502, f"Failed to merge band GeoTIFFs: {exc}") from exc
    finally:
        if output_grid:
            raw.unlink(missing_ok=True)

    return FileResponse(
        path=out,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# Copernicus Data Space (Sentinel-1 SAR)
# ---------------------------------------------------------------------------


def _validate_date_range(start_date: str, end_date: str) -> None:
    try:
        start = dt.date.fromisoformat(start_date)
        end = dt.date.fromisoformat(end_date)
    except ValueError:
        raise HTTPException(400, "start_date/end_date must be YYYY-MM-DD")
    if start > end:
        raise HTTPException(400, "start_date must be <= end_date")


def _validate_point(lon: float, lat: float) -> None:
    if not -180 <= lon <= 180 or not -90 <= lat <= 90:
        raise HTTPException(400, "lon/lat ngoài phạm vi hợp lệ")


def _validate_limit(limit: int, lo: int, hi: int, name: str = "limit") -> int:
    if not lo <= limit <= hi:
        raise HTTPException(400, f"{name} phải nằm trong khoảng {lo}..{hi}")
    return limit


def _validate_days_range(min_days: float, max_days: float) -> None:
    if not 0 < min_days <= max_days <= 365:
        raise HTTPException(
            400, "min_days/max_days phải dương, min_days <= max_days và <= 365"
        )


# ID sản phẩm OData của CDSE là GUID — chỉ nhận ký tự an toàn để chặn
# path traversal / header injection qua URL tải.
_PRODUCT_ID_RE = re.compile(r"^[0-9a-zA-Z\-]{8,64}$")


def _safe_zip_name(filename: str | None, product_id: str) -> str:
    """Sanitize filename client gửi trước khi đưa vào Content-Disposition."""
    if filename:
        base = os.path.basename(filename.replace("\\", "/"))
        base = re.sub(r"[^A-Za-z0-9._ ()\-]", "_", base).strip()[:150]
        if base and base not in (".", ".."):
            if not base.lower().endswith(".zip"):
                base += ".zip"
            return base
    return f"S1_{product_id}.zip"


@app.get("/api/s1/scenes")
def s1_scenes(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    limit: int = 20,
):
    """Search Sentinel-1 IW GRD products on CDSE catalogue (public, no auth)."""
    _validate_date_range(start_date, end_date)
    _validate_point(lon, lat)
    _validate_limit(limit, 1, 200)
    try:
        products = search_s1(lon, lat, start_date, end_date, max_results=limit)
    except Exception as exc:
        raise HTTPException(502, f"CDSE catalogue query failed: {exc}") from exc
    if not products:
        raise HTTPException(
            404,
            "Không tìm thấy ảnh Sentinel-1 nào trong khoảng ngày này.",
        )
    return {"count": len(products), "products": products}


@app.get("/api/s1/slc/scenes")
def s1_slc_scenes(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    limit: int = 50,
):
    """Search Sentinel-1 IW SLC products (InSAR-ready) on CDSE catalogue.

    Trả về metadata đầy đủ (timestamp, track, pass, polarization) từ
    OData Attributes; trường catalogue không trả về là null.
    """
    _validate_date_range(start_date, end_date)
    _validate_point(lon, lat)
    _validate_limit(limit, 1, 200)
    try:
        products = search_s1_slc(lon, lat, start_date, end_date, max_results=limit)
    except Exception as exc:
        raise HTTPException(502, f"CDSE catalogue query failed: {exc}") from exc
    if not products:
        raise HTTPException(
            404,
            "Không tìm thấy ảnh Sentinel-1 SLC nào trong khoảng ngày này.",
        )
    return {"product_type": "SLC", "count": len(products), "products": products}


@app.get("/api/s1/pairs")
def s1_pairs(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    min_days: float = 6.0,
    max_days: float = 48.0,
    max_pairs: int = 20,
    track: int | None = None,
    orbit_direction: str | None = None,
):
    """Gợi ý cặp Sentinel-1 SLC cho InSAR tại vị trí (lon, lat).

    Ghép các cảnh cùng relative orbit (track) và cùng hướng bay, cách nhau
    min_days..max_days ngày. Perpendicular baseline không có trong catalogue
    CDSE nên luôn null — cặp chỉ là gợi ý dữ liệu, chưa tạo interferogram.
    """
    _validate_date_range(start_date, end_date)
    _validate_point(lon, lat)
    _validate_days_range(min_days, max_days)
    _validate_limit(max_pairs, 1, 100, name="max_pairs")
    if orbit_direction is not None and orbit_direction.upper() not in (
        "ASCENDING",
        "DESCENDING",
    ):
        raise HTTPException(400, "orbit_direction phải là ASCENDING hoặc DESCENDING")

    # Lấy pool SLC đủ lớn rồi ghép cặp phía client/trong build_pairs
    try:
        products = search_s1_slc(lon, lat, start_date, end_date, max_results=200)
    except Exception as exc:
        raise HTTPException(502, f"CDSE catalogue query failed: {exc}") from exc
    if not products:
        raise HTTPException(
            404,
            "Không tìm thấy ảnh Sentinel-1 SLC nào trong khoảng ngày này.",
        )

    pairs = build_pairs(
        products,
        min_days=min_days,
        max_days=max_days,
        max_pairs=max_pairs,
        track=track,
        orbit_direction=orbit_direction,
    )
    if not pairs:
        raise HTTPException(
            404,
            "Không ghép được cặp SLC nào (cần cùng track + cùng hướng bay, "
            "cách nhau trong khoảng ngày đã chọn).",
        )
    return {"count": len(pairs), "pairs": pairs}


@app.get("/api/s1/download")
def s1_download(product_id: str, filename: str | None = None):
    """Stream a Sentinel-1 GRD/SLC product ZIP from CDSE (needs creds)."""
    if not _PRODUCT_ID_RE.match(product_id):
        raise HTTPException(400, "product_id không hợp lệ")
    _ensure_cdse_creds()
    safe_name = _safe_zip_name(filename, product_id)
    # Mở stream + đọc chunk đầu TRƯỚC khi trả header 200 về browser.
    # Nếu proxy chặn / CDSE lỗi, người dùng nhận HTTP error rõ ràng
    # thay vì file ZIP rỗng (StreamingResponse gửi header trước khi
    # generator chạy, lỗi trong generator = file rỗng).
    try:
        resp, first = open_product(product_id)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except requests.HTTPError as exc:
        raise HTTPException(502, f"CDSE download failed: {exc}") from exc
    except requests.RequestException as exc:
        raise HTTPException(502, f"Không kết nối được CDSE: {exc}") from exc
    return StreamingResponse(
        iter_open_product(resp, first),
        media_type="application/zip",
        headers=_cdse_zip_headers(resp, safe_name),
    )


def _cdse_zip_headers(response, filename):
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    length = response.headers.get('Content-Length')
    encoding = response.headers.get('Content-Encoding', 'identity')
    if isinstance(length, str) and length.isascii() and length.isdecimal() and encoding == 'identity':
        headers['Content-Length'] = length
    return headers


def _ensure_cdse_creds() -> None:
    """Check CDSE credentials early so the error is a clear 4xx, not a broken stream."""
    try:
        get_token()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"CDSE authentication failed: {exc}") from exc


@app.get("/api/s1/s3/download")
def s1_s3_download(product_id: str, filename: str | None = None):
    """Stream product S1 (SLC/GRD) dạng ZIP — tải qua S3 eodata theo chunk.

    Vượt giới hạn proxy công ty (300 MB/response): server tải TỪNG object
    của product .SAFE trên S3, mỗi object chia chunk 250 MB (HTTP Range),
    rồi nén stream về browser như 1 file ZIP duy nhất. Nhanh hơn OData ~8x.
    """
    return _s3_zip_response(product_id, filename)


@app.get("/api/s2/s3/download")
def s2_s3_download(product_id: str, filename: str | None = None):
    """Stream product Sentinel-2 L2A (.SAFE đầy đủ) qua S3 eodata theo chunk.

    Cùng cơ chế như S1: ZIP .SAFE ~1 GB bị endpoint /$value chặn qua proxy
    (> 300 MB, không hỗ trợ Range) — đường S3 tải từng object theo chunk
    250 MB rồi ghép stream thành 1 ZIP duy nhất.
    """
    return _s3_zip_response(product_id, filename)


def _s3_zip_response(product_id: str, filename: str | None = None):
    """Dùng chung cho S1/S2/S3: stream cây .SAFE trên S3 eodata thành ZIP.

    ZIP được nén STORED (không nén lại — dữ liệu TIFF/JP2 đã nén sẵn) và
    stream qua sink không-seek nên first byte về browser sau ~3s thay vì
    chờ buffer toàn bộ (~850s với SLC 7.5 GB).
    """
    import zipfile

    if not _PRODUCT_ID_RE.match(product_id):
        raise HTTPException(400, "product_id không hợp lệ")
    try:
        creds = get_s3_credentials()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc

    # Lấy thông tin product trước khi stream (fail sớm, rõ ràng)
    try:
        s3_path = get_product_s3_path(product_id)
    except Exception as exc:
        raise HTTPException(502, f"Không lấy được S3Path: {exc}") from exc

    try:
        files = s3_list_product_files(product_id, creds)
    except Exception as exc:
        raise HTTPException(502, f"Liệt kê file S3 thất bại: {exc}") from exc

    # Tên product .SAFE từ S3Path làm gốc tên ZIP
    safe_root = s3_path.rstrip("/").rsplit("/", 1)[-1]
    if not safe_root.endswith(".SAFE"):
        safe_root = safe_root + ".SAFE"
    zip_name = _safe_zip_name(filename, product_id)

    total_size = sum(f["size"] for f in files)

    class _StreamSink:
        """File-like KHÔNG seek được → zipfile viết data descriptor
        (flag 0x08) thay vì seek-rewrite header — cho phép ghép ZIP
        tuần tự và stream thẳng từng block về browser."""

        def __init__(self) -> None:
            self.pos = 0
            self.chunks: list[bytes] = []

        def write(self, data) -> int:
            self.chunks.append(data)
            self.pos += len(data)
            return len(data)

        def tell(self) -> int:
            return self.pos

        def flush(self) -> None:
            pass

        def seek(self, *args):
            raise OSError("stream sink is not seekable")

    def _zip_stream():
        """Nén toàn bộ cây .SAFE thành ZIP stream (stored, không nén lại).

        Mỗi block 1 MB tải từ S3 được yield NGAY về client — browser thấy
        tiến độ tải liên tục thay vì chờ server buffer đủ 7.5 GB.
        """
        sink = _StreamSink()
        with zipfile.ZipFile(sink, "w", compression=zipfile.ZIP_STORED,
                             allowZip64=True) as zf:
            for f in files:
                arcname = f"{safe_root}/{f['rel']}"
                with zf.open(arcname, "w", force_zip64=True) as dst:
                    for block in iter_s3_object(f["key"], creds, f["size"]):
                        dst.write(block)
                        while sink.chunks:
                            yield sink.chunks.pop(0)
        # central directory + end record sau khi đóng
        while sink.chunks:
            yield sink.chunks.pop(0)

    return StreamingResponse(
        _zip_stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{zip_name}"',
            "X-S3-File-Count": str(len(files)),
            "X-S3-Total-Bytes": str(total_size),
        },
    )


def _s1_grd_mosaic_product(
    product_id: str,
    province,
    pols: list[str],
    meas: dict[str, list[dict]],
    creds,
    dst_tf,
    width: int,
    height: int,
    out,
    tmp_dir: Path,
    dst_crs="EPSG:4326",
) -> None:
    """Warp measurement GRD của 1 product vào mảng `out` dùng chung (WGS84).

    `out` shape (n_pol, height, width) — mảng mosaic của endpoint crop và
    crop-mosaic. Pixel valid của product này chỉ đè lên chỗ còn NaN, giữ
    giá trị product đã warp trước đó ở vùng giao (như mosaic sub-swath).
    """
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.warp import reproject  # noqa: PLC0415

    for bi, pol in enumerate(pols):
        if pol not in meas:
            continue
        for k, f in enumerate(meas[pol]):
            local = tmp_dir / f"{product_id[:8]}_{pol}_{k}.tiff"
            if not (local.exists() and local.stat().st_size == f["size"]):
                # Tải vào .part rồi rename NGUYÊN TỬ: request chạy song
                # song (2 tab cùng bấm "Cắt tỉnh") không thể đọc file
                # tải dở coi như hoàn chỉnh (magic bytes có sẵn sau
                # header nhưng dữ liệu đứt giữa chừng → GDAL lỗi
                # "Chunk and warp failed").
                part = tmp_dir / f"{product_id[:8]}_{pol}_{k}.tiff.part"
                with open(part, "wb") as fh:
                    for block in iter_s3_object(f["key"], creds, f["size"]):
                        fh.write(block)
                part.replace(local)
            with rasterio.open(local) as src:
                gcps, gcp_crs = src.gcps
                if not gcps:
                    raise HTTPException(
                        502,
                        f"{f['rel']} không có GCPs — không warp được.",
                    )
                temp = np.full((height, width), np.nan, dtype="float32")
                reproject(
                    source=rasterio.band(src, 1),
                    destination=temp,
                    gcps=gcps,
                    src_crs=gcp_crs,
                    src_nodata=src.nodata if src.nodata is not None else 0,
                    dst_transform=dst_tf,
                    dst_crs=dst_crs,
                    dst_nodata=np.nan,
                    resampling=Resampling.bilinear,
                    num_threads=2,
                )
            # Mosaic: pixel valid đè lên chỗ còn nan (giữ giá trị trước đó)
            valid = ~np.isnan(temp)
            out[bi][valid] = temp[valid]
            local.unlink(missing_ok=True)  # giải phóng đĩa sớm


def _s1_crop_impl(product_ids: list[str], province, scale_m: float):
    """Lõi dùng chung cho /api/s1/crop (1 product) và crop-mosaic (N product).

    Tải measurement GRD của từng product qua S3 eodata, warp về cùng một
    lưới WGS84 phủ bbox tỉnh, mosaic theo pixel (valid đè NaN), cuối cùng
    mask theo polygon tỉnh. Trả (out_path, fname).
    """
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.features import rasterize  # noqa: PLC0415
    from rasterio.transform import from_bounds  # noqa: PLC0415

    try:
        creds = get_s3_credentials()
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc

    rectangle = province if isinstance(province, RectangleROI) else None
    dst_crs = 'EPSG:4326'
    if rectangle is not None:
        from affine import Affine
        grid = rectangle.output_grid(scale_m)
        width, height = grid['width'], grid['height']
        dst_tf, dst_crs = Affine(*grid['transform']), grid['crs']
    else:
        pb = province.bbox
        res_deg = scale_m / 111_320.0
        width = int(-(-(pb.east - pb.west) // res_deg))  # ceil
        height = int(-(-(pb.north - pb.south) // res_deg))
        dst_tf = from_bounds(pb.west, pb.south, pb.east, pb.north, width, height)
    if width * height > 120_000_000:
        raise HTTPException(
            400,
            f"Vùng quá lớn ở {scale_m}m ({width}x{height} px) — tăng scale_m.",
        )
    import re as _re  # noqa: PLC0415

    # Gom measurement + pols của TẤT CẢ product trước (fail sớm nếu có
    # product không phải GRD), để tránh tải xong N-1 product mới phát hiện
    all_meas: dict[str, dict[str, list[dict]]] = {}  # pid -> pol -> files
    for pid in product_ids:
        try:
            files = s3_list_product_files(pid, creds)
        except Exception as exc:
            raise HTTPException(502, f"Liệt kê file S3 ({pid[:8]}) thất bại: {exc}") from exc
        meas: dict[str, list[dict]] = {}  # pol -> [file]
        for f in files:
            rel = f["rel"]
            if not rel.startswith("measurement/") or not rel.endswith(".tiff"):
                continue
            m = _re.search(r"-grd-(vv|vh|hh|hv)-", rel)
            if m:
                meas.setdefault(m.group(1), []).append(f)
        if not meas:
            raise HTTPException(
                404,
                f"Product {pid[:8]} không có file measurement GRD "
                "(có thể là SLC/OCN — endpoint này chỉ dành cho GRD).",
            )
        all_meas[pid] = meas
    # Thứ tự band cố định: VV trước (pol chính), hợp nhất các product
    pol_set: list[str] = []
    for meas in all_meas.values():
        for p in sorted(meas.keys(), key=lambda x: (x != "vv", x)):
            if p not in pol_set:
                pol_set.append(p)
    pols = pol_set

    # Tên file: s1grd[_mosaic]_<date>_<province>_<pid8>.tif
    first_pid = product_ids[0]
    try:
        s3_path = get_product_s3_path(first_pid)
    except Exception as exc:
        raise HTTPException(502, f"Không lấy được S3Path ({first_pid[:8]}): {exc}") from exc
    safe_root = s3_path.rstrip("/").rsplit("/", 1)[-1]
    m = _re.search(r"_(\d{8})T(\d{6})", safe_root)
    date_tag = (m.group(1) + "T" + m.group(2)) if m else first_pid[:8]
    pol_tag = "".join(p.upper() for p in pols)
    region_tag = _rectangle_tag(rectangle) if rectangle is not None else province.id
    if len(product_ids) == 1:
        fname = f"s1grd_{date_tag}_{region_tag}_{pol_tag}_{first_pid[:8]}.tif"
    else:
        fname = (
            f"s1grd_mosaic{len(product_ids)}_{date_tag}_"
            f"{region_tag}_{pol_tag}.tif"
        )
    request_id = uuid.uuid4().hex
    out_path = _DOWNLOADS_DIR / f'{request_id}_{fname}'
    tmp_dir = _DOWNLOADS_DIR / f'_crop_{request_id}'
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with rasterio.open(
            out_path, "w", driver="GTiff", width=width, height=height,
            count=len(pols), dtype="float32", crs=dst_crs,
            transform=dst_tf, nodata=float("nan"), compress="deflate",
            tiled=True, bigtiff="IF_SAFER",
        ) as dst:
            out = np.full((len(pols), height, width), np.nan, dtype="float32")
            for pid in product_ids:
                _s1_grd_mosaic_product(
                    pid, province, pols, all_meas[pid], creds,
                    dst_tf, width, height, out, tmp_dir,
                    **({'dst_crs': dst_crs} if rectangle else {}),
                )
            # Mask theo polygon tỉnh (MultiPolygon GeoJSON) — 1 lần sau
            # khi mosaic xong mọi product
            if rectangle is not None:
                from rasterio.warp import transform_geom
                mask_geometry = transform_geom('EPSG:4326', dst_crs, rectangle.geometry)
            else:
                mask_geometry = province.geometry
            pmask = rasterize(
                [(mask_geometry, 1)],
                out_shape=(height, width), transform=dst_tf,
                fill=0, dtype="uint8",
            )
            out[:, pmask == 0] = np.nan
            if np.isnan(out).all():
                raise HTTPException(
                    400,
                    ('Không product nào phủ vùng chữ nhật đã chọn.' if rectangle else
                     f'Không product nào phủ tỉnh {province.name} (cảnh GRD nằm hoàn toàn ngoài biên giới).'),
                )
            for bi, pol in enumerate(pols, start=1):
                dst.write(out[bi - 1], bi)
                dst.set_band_description(bi, pol.upper())
            if len(product_ids) > 1:
                from .s1_mosaic import mosaic_tags
                sources = []
                for pid in product_ids:
                    measurement = next(item for band_files in all_meas[pid].values() for item in band_files)
                    product_name = measurement.get('key', '').split('/measurement/', 1)[0].rsplit('/', 1)[-1]
                    sources.append({'product_id': pid, 'product_name': product_name or None})
                dst.update_tags(**mosaic_tags(sources, provider='CDSE S3', policy='last_valid_measurement_wins'))
    except HTTPException:
        out_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        out_path.unlink(missing_ok=True)
        raise HTTPException(502, f"Warp/cắt GRD thất bại: {exc}") from exc
    finally:
        import shutil  # noqa: PLC0415

        shutil.rmtree(tmp_dir, ignore_errors=True)

    return out_path, fname


def _s1_crop_region(selection_mode, province_id, center_lon, center_lat,
                    width_m, height_m, rotation_deg):
    rectangle = _s2_rectangle_selection(selection_mode, center_lon, center_lat, width_m, height_m,
                                         rotation_deg, None, None, 0, province_id, None)
    if rectangle is not None:
        return rectangle
    if rotation_deg or not province_id:
        raise HTTPException(400, 'province_id is required; rotation needs rectangle mode')
    try:
        province = get_province(province_id)
    except RuntimeError as exc:
        raise HTTPException(503, f'Administrative boundaries unavailable: {exc}') from exc
    if province is None:
        raise HTTPException(404, f'Province not found: {province_id}')
    return province


@app.get("/api/s1/crop")
def s1_crop(product_id: str, province_id: str | None = None, scale_m: float = 20.0,
            selection_mode: Literal['province', 'rectangle'] | None = None,
            center_lon: float | None = None, center_lat: float | None = None,
            width_m: float | None = None, height_m: float | None = None,
            rotation_deg: float = 0.0):
    """Cắt product Sentinel-1 GRD theo biên tỉnh → GeoTIFF VV/VH (WGS84).

    Pipeline: tải các file measurement .tiff trong .SAFE qua S3 eodata
    (chunk 250MB — lọt ngưỡng proxy), warp về lưới WGS84 phủ bbox tỉnh
    (measurement GeoTIFF geolocate bằng GCPs EPSG:4326, không có affine
    transform), mosaic các sub-swath iw1/2/3, rồi mask theo polygon tỉnh.
    Giá trị giữ nguyên thô của measurement GRD (chưa hiệu chuẩn — muốn σ0
    chuẩn cần calibration/annotation). scale_m: kích thước pixel (mét),
    mặc định 20m để tỉnh lớn vẫn gọn (< ~250 MB).
    """
    if not _PRODUCT_ID_RE.match(product_id):
        raise HTTPException(400, "product_id không hợp lệ")
    if not math.isfinite(scale_m) or scale_m < 10.0 or scale_m > 500.0:
        raise HTTPException(400, "scale_m phải trong 10..500 mét")

    province = _s1_crop_region(selection_mode, province_id, center_lon, center_lat,
                               width_m, height_m, rotation_deg)
    out_path, fname = _s1_crop_impl([product_id], province, scale_m)

    return FileResponse(
        path=out_path,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/s1/crop-mosaic")
def s1_crop_mosaic(
    product_ids: str,
    province_id: str | None = None,
    scale_m: float = 20.0,
    selection_mode: Literal['province', 'rectangle'] | None = None,
    center_lon: float | None = None, center_lat: float | None = None,
    width_m: float | None = None, height_m: float | None = None,
    rotation_deg: float = 0.0,
):
    """Ghép N product GRD thành 1 GeoTIFF duy nhất theo biên tỉnh.

    Option BỔ SUNG (không thay thế /api/s1/crop): dùng khi 1 cảnh GRD
    không phủ hết tỉnh — chọn nhiều cảnh (thường cùng ngày, khác orbit/
    swath), server warp tất cả về chung 1 lưới WGS84 rồi mosaic theo
    pixel (valid đè NaN), cuối cùng mask theo biên tỉnh → 1 file GeoTIFF
    VV/VH gọn nhẹ. Giới hạn 6 product/lần để không quá lâu qua proxy.

    product_ids: danh sách id ngăn cách dấu phẩy, vd
    /api/s1/crop-mosaic?product_ids=id1,id2&province_id=VN92
    """
    ids = [p.strip() for p in product_ids.split(",") if p.strip()]
    if not (1 <= len(ids) <= 6):
        raise HTTPException(400, "product_ids cần 1..6 id, ngăn cách dấu phẩy")
    for pid in ids:
        if not _PRODUCT_ID_RE.match(pid):
            raise HTTPException(400, f"product_id không hợp lệ: {pid[:16]}…")
    if not math.isfinite(scale_m) or scale_m < 10.0 or scale_m > 500.0:
        raise HTTPException(400, "scale_m phải trong 10..500 mét")

    province = _s1_crop_region(selection_mode, province_id, center_lon, center_lat,
                               width_m, height_m, rotation_deg)
    out_path, fname = _s1_crop_impl(ids, province, scale_m)

    return FileResponse(
        path=out_path,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# Sentinel-2 L2A / Sentinel-3 trên CDSE (mở rộng multi-mission)
# ---------------------------------------------------------------------------


@app.get("/api/province/scenes")
def province_scenes(
    province_id: str,
    dataset: str,
    start_date: str,
    end_date: str,
    cloud_max: float | None = None,
    product_type: str = "OL_2_LFR",
):
    """Liệt kê mọi scene phủ một tỉnh, nhóm theo tile (S2) hoặc theo ngày.

    Dùng cho các dataset tải scene gốc (s1/s1slc/s2/s2aws/s3): khi chọn tỉnh,
    người dùng cần biết tỉnh đó nằm trên bao nhiêu tile/swath và tải từng
    scene cần thiết thay vì bị chặn như trước.
    """
    _validate_date_range(start_date, end_date)
    try:
        province = get_province(province_id)
    except RuntimeError as exc:
        raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
    if province is None:
        raise HTTPException(404, f"Province not found: {province_id}")
    if dataset not in ("s1", "s1slc", "s2", "s2aws", "s3"):
        raise HTTPException(400, "dataset must be one of s1, s1slc, s2, s2aws, s3")

    bbox = (province.bbox.west, province.bbox.south, province.bbox.east, province.bbox.north)
    try:
        if dataset == "s2":
            products = search_s2_area(bbox, start_date, end_date, cloud_max=cloud_max)
        elif dataset == "s2aws":
            products = aws_search_s2_l2a_bbox(
                bbox, start_date, end_date, cloud_max=cloud_max
            )
        elif dataset == "s1":
            products = search_s1_area(bbox, start_date, end_date, product_kind="GRD")
        elif dataset == "s1slc":
            products = search_s1_area(bbox, start_date, end_date, product_kind="SLC")
        else:
            products = search_s3_area(
                bbox, start_date, end_date, product_type=product_type
            )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Catalogue query failed: {exc}") from exc

    # Nhóm "pass khử mây": mỗi pass là một tập tile có ngày quay gần nhau
    # (trong ngưỡng _PASS_DAY_TOLERANCE ngày) — một pass phủ (gần) đủ tỉnh.
    # Ví dụ Lào Cai 4 tile: 4 tile quay cùng ngày/gần nhau -> 1 nhóm;
    # mỗi chu kỳ lấy lại (5 ngày) tạo pass mới.
    if dataset in ("s2", "s2aws"):
        groups = _build_tile_passes(products)
    else:
        groups_by_date: dict[str, list[dict]] = {}
        for p in products:
            groups_by_date.setdefault(p.get("date") or "khác", []).append(p)
        groups = []
        for key, items in groups_by_date.items():
            clouds = [
                p["cloud_pct"] for p in items if p.get("cloud_pct") is not None
            ]
            groups.append(
                {
                    "key": key,
                    "count": len(items),
                    "scenes": items,
                    "cloud_pct_mean": (
                        round(sum(clouds) / len(clouds), 1) if clouds else None
                    ),
                }
            )

    return {
        "province_id": province.id,
        "province_name": province.name,
        "dataset": dataset,
        "date_range": [start_date, end_date],
        "total_scenes": len(products),
        "group_count": len(groups),
        "groups": groups,
    }


# Ngưỡng coi 2 ngày "cùng pass": các tile phủ cùng một tỉnh thường quay
# cùng ngày (cùng orbit) hoặc lệch 1 ngày (orbit kề). Lớn hơn sẽ nối dính
# các chu kỳ 5 ngày của S2 thành 1 cụm (sai ý "gần nhau").
_PASS_DAY_TOLERANCE = 1


def _build_tile_passes(products: list[dict]) -> list[dict]:
    """Gom các tile S2 thành "pass" theo thời gian quay gần nhau.

    Thuật toán:
    1. Mỗi (tile, scene) -> chọn scene đại diện (mới nhất/m ít mây nhất)
       cho tile trong lượt hiện tại; đơn giản hoá: gom theo ngày rồi hợp
       các ngày lệch <= _PASS_DAY_TOLERANCE.
    2. Union-find các ngày gần nhau -> mỗi cụm ngày là một pass; một pass
       gồm các tile quay trong cụm ngày đó.
    Trả về list group {key, count, scenes} mới nhất trước; key là
    "YYYY-MM-DD..YYYY-MM-DD (N tile)".
    """
    # tile -> {date -> [products]}
    by_tile: dict[str, dict[str, list[dict]]] = {}
    for p in products:
        tile = p.get("tile") or "khác"
        date = p.get("date") or "khác"
        by_tile.setdefault(tile, {}).setdefault(date, []).append(p)

    all_dates = sorted({d for td in by_tile.values() for d in td if d != "khác"})
    if not all_dates:
        # Không có ngày hợp lệ: fallback gom tất cả vào 1 nhóm
        return [{
            "key": "khác",
            "count": len(products),
            "scenes": products,
        }]

    # Union-find theo ngày: 2 ngày cùng cụm nếu chênh <= tolerance
    parent = {d: d for d in all_dates}

    def find(d: str) -> str:
        while parent[d] != d:
            parent[d] = parent[parent[d]]
            d = parent[d]
        return d

    import datetime as _dt

    dates_dt = [_dt.date.fromisoformat(d) for d in all_dates]
    for i in range(len(all_dates)):
        for j in range(i + 1, len(all_dates)):
            gap = abs((dates_dt[j] - dates_dt[i]).days)
            if gap <= _PASS_DAY_TOLERANCE:
                ri, rj = find(all_dates[i]), find(all_dates[j])
                if ri != rj:
                    # merge vào ngày nhỏ hơn (gốc cụm)
                    if ri <= rj:
                        parent[rj] = ri
                    else:
                        parent[ri] = rj

    # Cụm ngày -> tập (tile, date)
    cluster_dates: dict[str, list[str]] = {}
    for d in all_dates:
        cluster_dates.setdefault(find(d), []).append(d)

    # Với mỗi cụm: mỗi tile chọn NGÀY gần tâm cụm nhất (1 scene tile/pass)
    passes: list[dict] = []
    for root, ds in cluster_dates.items():
        ds = sorted(ds)
        scenes: list[dict] = []
        tiles_used: set[str] = set()
        for tile, dates_map in by_tile.items():
            dates_in = [d for d in ds if d in dates_map]
            if not dates_in:
                continue
            # Chọn ngày mới nhất trong cụm (scene thường cùng chu kỳ)
            best = max(dates_in)
            # Trong cùng tile+ngày ưu tiên COG, sau đó ít mây nhất
            candidates = dates_map[best]
            candidates = sorted(
                candidates,
                key=lambda p: (not p.get("cog", False), p.get("cloud_pct") or 999),
            )
            scenes.append(candidates[0])
            tiles_used.add(tile)
        if not scenes:
            continue
        label = ds[0] if ds[0] == ds[-1] else f"{ds[0]}..{ds[-1]}"
        # % mây trung bình của nhóm (các scene đại diện tile) — giúp chọn
        # nhanh lượt quay ít mây mà không cần mở từng nhóm.
        clouds = [
            p["cloud_pct"] for p in scenes if p.get("cloud_pct") is not None
        ]
        passes.append(
            {
                "key": f"{label} ({len(tiles_used)} tile)",
                "count": len(scenes),
                "scenes": scenes,
                "date_min": ds[0],
                "date_max": ds[-1],
                "tiles": sorted(tiles_used),
                "cloud_pct_mean": (
                    round(sum(clouds) / len(clouds), 1) if clouds else None
                ),
            }
        )
    passes.sort(key=lambda g: g["date_min"], reverse=True)
    return passes


@app.get("/api/s2/scenes")
def s2_scenes(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    limit: int = 20,
    cloud_max: float | None = None,
    source: str = "cdse",
    radius_m: float = 0.0,
    rotation_deg: float = 0.0,
):
    """Search Sentinel-2 L2A products (surface reflectance).

    source='cdse': CDSE OData catalogue — full archive, cần auth khi tải.
    source='aws': AWS Earth Search STAC — COG per-band, tải không cần auth.
    """
    _validate_date_range(start_date, end_date)
    _validate_point(lon, lat)
    _validate_limit(limit, 1, 200)
    if cloud_max is not None and not 0 <= cloud_max <= 100:
        raise HTTPException(400, "cloud_max phải trong khoảng 0-100")
    if source not in ("cdse", "aws"):
        raise HTTPException(400, "source phải là 'cdse' hoặc 'aws'")

    _s2_output_grid(lon, lat, radius_m, rotation_deg, None)
    geometry, envelope = point_roi(lon, lat, radius_m, rotation_deg) if radius_m > 0 else (None, None)
    try:
        if envelope is not None and rotation_deg % 360:
            bounds = (envelope.west, envelope.south, envelope.east, envelope.north)
            if source == "aws":
                products = aws_search_s2_l2a_bbox(bounds, start_date, end_date,
                                                   max_results=limit, cloud_max=cloud_max)
            else:
                products = search_s2_area(bounds, start_date, end_date,
                                          max_results=limit, cloud_max=cloud_max)
        elif source == "aws":
            products = aws_search_s2_l2a(
                lon, lat, start_date, end_date, max_results=limit, cloud_max=cloud_max
            )
        else:
            products = search_s2(
                lon, lat, start_date, end_date, max_results=limit, cloud_max=cloud_max
            )
    except Exception as exc:
        raise HTTPException(502, f"Catalogue query failed ({source}): {exc}") from exc
    if not products:
        raise HTTPException(
            404,
            "Không tìm thấy ảnh Sentinel-2 L2A nào trong khoảng ngày này.",
        )
    result = {"source": source, "count": len(products), "products": products}
    if geometry is not None:
        result.update(roi_geometry=geometry, bbox=vars(envelope), rotation_deg=rotation_deg % 360)
    return result


@app.get("/api/s3/scenes")
def s3_scenes(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    product_type: str = "OL_2_LFR",
    limit: int = 20,
):
    """Search Sentinel-3 products trên CDSE catalogue (public, no auth).

    product_type mặc định OL_2_LFR (Land reflectance 300 m) — thay thế
    gần nhất cho phân tích đất khi cần thời gian cập nhật nhanh.
    """
    _validate_date_range(start_date, end_date)
    _validate_point(lon, lat)
    _validate_limit(limit, 1, 200)
    try:
        products = search_s3(
            lon, lat, start_date, end_date, product_type=product_type, max_results=limit
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"CDSE catalogue query failed: {exc}") from exc
    if not products:
        raise HTTPException(
            404,
            "Không tìm thấy ảnh Sentinel-3 nào trong khoảng ngày này.",
        )
    return {"product_type": product_type, "count": len(products), "products": products}


@app.get("/api/s2/download")
def s2_download(product_id: str, filename: str | None = None):
    """Stream một sản phẩm Sentinel-2 từ CDSE (cần creds, tương tự S1)."""
    if not _PRODUCT_ID_RE.match(product_id):
        raise HTTPException(400, "product_id không hợp lệ")
    _ensure_cdse_creds()
    safe_name = _safe_zip_name(filename, product_id)
    try:
        resp, first = open_product(product_id)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    except requests.HTTPError as exc:
        raise HTTPException(502, f"CDSE download failed: {exc}") from exc
    except requests.RequestException as exc:
        raise HTTPException(502, f"Không kết nối được CDSE: {exc}") from exc
    return StreamingResponse(
        iter_open_product(resp, first),
        media_type="application/zip",
        headers=_cdse_zip_headers(resp, safe_name),
    )


def _s2_output_grid(lon, lat, radius_m, rotation_deg, province_id):
    if not math.isfinite(radius_m) or radius_m < 0 or radius_m > _MAX_RADIUS_M:
        raise HTTPException(400, "radius_m must be finite and between 0 and 45000")
    for value, limit, name in ((lon, 180, "lon"), (lat, 85.05112878, "lat")):
        if value is not None and (not math.isfinite(value) or not -limit <= value <= limit):
            raise HTTPException(400, f"{name} is outside the supported geographic domain")
    if not math.isfinite(rotation_deg) or not 0 <= rotation_deg <= 360:
        raise HTTPException(400, "rotation_deg must be finite and between 0 and 360")
    if province_id and rotation_deg % 360:
        raise HTTPException(400, "rotation_deg is only supported for point selection")
    if lon is not None and lat is not None and radius_m > 0 and not province_id:
        try:
            point_roi(lon, lat, radius_m, rotation_deg)
            if rotation_deg % 360:
                return build_rotated_grid(lon, lat, radius_m, rotation_deg, S2_SCALE)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    elif rotation_deg % 360:
        raise HTTPException(400, "lon, lat and positive radius_m are required for rotation")
    return None


def _s2_rectangle_selection(selection_mode, center_lon, center_lat, width_m, height_m,
                            rotation_deg, lon, lat, radius_m, province_id, commune_id):
    if selection_mode == 'rectangle':
        if lon is not None or lat is not None or radius_m or province_id or commune_id:
            raise HTTPException(400, 'rectangle selection cannot include point or administrative fields')
        try:
            return _rectangle_model(center_lon, center_lat, width_m, height_m, rotation_deg)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if any(value is not None for value in (center_lon, center_lat, width_m, height_m)):
        raise HTTPException(400, 'rectangle fields require selection_mode=rectangle')
    if selection_mode == 'point' and (lon is None or lat is None or radius_m <= 0 or province_id or commune_id):
        raise HTTPException(400, 'point selection requires lon, lat and a positive radius_m only')
    if selection_mode == 'province' and (not province_id or commune_id):
        raise HTTPException(400, 'province selection requires province_id only')
    if selection_mode == 'commune' and (not commune_id or province_id):
        raise HTTPException(400, 'commune selection requires commune_id only')
    return None


def _rectangle_tag(rectangle):
    return (f'rect{rectangle.center_lon:.5f}_{rectangle.center_lat:.5f}'
            f'_w{rectangle.width_m:g}_h{rectangle.height_m:g}_a{rectangle.rotation_deg:g}')


@app.get("/api/s2/bands")
def s2_bands(
    product_id: str,
    action: Literal["list", "download"] = "list",
    bands: str = "B02,B03,B04,B08",
    province_id: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float = 0.0,
    rotation_deg: float = 0.0,
    commune_id: str | None = None,
    selection_mode: Literal['point', 'rectangle', 'province', 'commune'] | None = None,
    center_lon: float | None = None,
    center_lat: float | None = None,
    width_m: float | None = None,
    height_m: float | None = None,
):
    """Liệt kê hoặc tải band từ product S2 L2A ZIP trên CDSE qua Nodes API.

    action='list': trả về cây band JP2 theo resolution (R10m/R20m/R60m)
    kèm kích thước từng file — mỗi file ~100 MB nên tải được qua proxy
    công ty (khác /$value là cả ZIP ~1.3 GB bị chặn > 300 MB).

    action='download': tải các band (CSV: B02,B03,B04,B08) rồi ghép thành
    GeoTIFF đa lớp, harmonize reflectance như pipeline GEE. Có thể crop
    theo province_id hoặc lon/lat + radius_m.
    """
    rectangle = _s2_rectangle_selection(selection_mode, center_lon, center_lat, width_m, height_m,
                                         rotation_deg, lon, lat, radius_m, province_id, commune_id)
    output_grid = (rectangle.output_grid(S2_SCALE) if rectangle else
                   _s2_output_grid(lon, lat, radius_m, rotation_deg, province_id or commune_id))
    if not _PRODUCT_ID_RE.match(product_id):
        raise HTTPException(400, "product_id không hợp lệ")
    _ensure_cdse_creds()

    if action == "list":
        try:
            info = list_s2_bands(product_id)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"CDSE Nodes query failed: {exc}") from exc
        return {"product_id": product_id, **info}

    # action == download
    wanted = [b.strip().upper() for b in bands.split(",") if b.strip()]
    if not wanted:
        raise HTTPException(400, "bands không được rỗng (vd: B02,B03,B04,B08)")

    try:
        info = list_s2_bands(product_id)
    except Exception as exc:
        raise HTTPException(502, f"CDSE Nodes query failed: {exc}") from exc

    # Tìm band trong resolution ưu tiên 10m → 20m → 60m; tên file khớp
    # hậu tố resolution của folder (R10m → "_B02_10m.jp2", R60m → "_SCL_60m.jp2")
    picks: list[tuple[str, str]] = []  # (resolution, filename)
    for b in wanted:
        found = None
        for res in ("R10m", "R20m", "R60m"):
            suffix = f"_{b}_{res[1:]}.jp2"
            for entry in info["resolutions"].get(res, []):
                if entry["name"].endswith(suffix):
                    found = (res, entry["name"])
                    break
            if found:
                break
        if found is None:
            raise HTTPException(404, f"Band {b} không có trong product")
        picks.append(found)

    # Vùng crop (nếu có)
    crop_bbox = None
    tag = "full"
    if rectangle is not None:
        pb = rectangle.bbox
        crop_bbox = (pb.west, pb.south, pb.east, pb.north)
        tag = _rectangle_tag(rectangle)
    elif province_id or commune_id:
        try:
            province = get_commune(commune_id) if commune_id else get_province(province_id)
        except RuntimeError as exc:
            raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
        if province is None:
            raise HTTPException(404, f"Province not found: {province_id}")
        pb = province.bbox
        crop_bbox = (pb.west, pb.south, pb.east, pb.north)
        tag = province.id
    elif lon is not None and lat is not None and radius_m > 0:
        _, pb = point_roi(lon, lat, radius_m, rotation_deg)
        crop_bbox = (pb.west, pb.south, pb.east, pb.north)
        tag = (f"pt{lon:.5f}_{lat:.5f}_r{radius_m:g}_a{rotation_deg % 360:g}"
               if output_grid else f"pt{lon:.3f}_{lat:.3f}_r{int(radius_m)}")

    # Tên từ product (scene id ngắn) — tile + date lấy từ tên .SAFE
    safe = info["safe"]
    m = re.search(r"_T(\d{2}[A-Z]{3})_(\d{8})T", safe)
    tile = m.group(1) if m else "tile"
    date = m.group(2)[:4] + "-" + m.group(2)[4:6] + "-" + m.group(2)[6:8] if m else "date"
    band_tag = "-".join(wanted)
    fname = f"sentinel2_cdse_{tile}_{date}_{tag}_{band_tag}.tif"
    request_id = uuid.uuid4().hex
    out = _DOWNLOADS_DIR / f"{request_id}_{fname}"
    band_dir = _DOWNLOADS_DIR / f"_nodes_{request_id}"
    band_dir.mkdir(exist_ok=True)

    # Tải từng band JP2 (mỗi file ~100 MB — lọt dưới ngưỡng proxy)
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.warp import transform_bounds  # noqa: PLC0415

    layers: list[tuple[str, object, object, object]] = []  # (band, arr, transform, crs)
    try:
        for b, (res, jp2_name) in zip(wanted, picks):
            jp2_path = band_dir / jp2_name
            if not jp2_path.exists() or jp2_path.stat().st_size < 1000:
                try:
                    download_s2_band_file(product_id, res, jp2_name, jp2_path)
                except ValueError as exc:
                    raise HTTPException(400, str(exc)) from exc
                except Exception as exc:
                    raise HTTPException(502, f"Tải band {b} từ CDSE thất bại: {exc}") from exc

            is_scl = b == "SCL"
            with rasterio.open(jp2_path) as src:
                if crop_bbox is not None:
                    from rasterio.windows import from_bounds  # noqa: PLC0415
                    w, s, e2, n = transform_bounds("EPSG:4326", src.crs, *crop_bbox, densify_pts=21)
                    w = max(w, src.bounds.left)
                    e2 = min(e2, src.bounds.right)
                    s = max(s, src.bounds.bottom)
                    n = min(n, src.bounds.top)
                    if e2 <= w or n <= s:
                        raise HTTPException(
                            400,
                            f"Vùng chọn không giao với band {b} (tile {tile}).",
                        )
                    win = from_bounds(w, s, e2, n, transform=src.transform)
                    if output_grid:
                        from rasterio.windows import Window
                        left, top = math.floor(win.col_off)-1, math.floor(win.row_off)-1
                        right, bottom = math.ceil(win.col_off+win.width)+1, math.ceil(win.row_off+win.height)+1
                        win = Window(left, top, right-left, bottom-top).intersection(
                            Window(0, 0, src.width, src.height))
                    else:
                        win = win.round_lengths().round_offsets()
                    data = src.read(1, window=win, out_dtype="float32", masked=bool(output_grid))
                    transform = src.window_transform(win)
                else:
                    data = src.read(1, out_dtype="float32", masked=bool(output_grid))
                    transform = src.transform
                crs = src.crs

            if not is_scl:
                # Harmonize DN -> reflectance (baseline >= 04.00: -1000, /10000)
                data = (data - 1000.0) / 10000.0
            layers.append((b, data.astype("float32"), transform, crs))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Đọc band JP2 thất bại: {exc}") from exc

    finally:
        import shutil
        shutil.rmtree(band_dir, ignore_errors=True)

    if output_grid:
        with rasterio.open(out, "w", **grid_profile(output_grid, len(layers))) as dst:
            for index, (band, data, transform, crs) in enumerate(layers, 1):
                data = np.ma.asarray(data, dtype="float32")
                with rasterio.MemoryFile() as memory:
                    with memory.open(driver="GTiff", width=data.shape[1], height=data.shape[0],
                                     count=1, dtype="float32", crs=crs, transform=transform,
                                     nodata=float("nan")) as source:
                        source.write(data.filled(float("nan")), 1)
                    with memory.open() as source:
                        write_grid_band(source, dst, output_grid, 1, index, band == "SCL")
                dst.set_band_description(index, band)
        return FileResponse(path=out, media_type="image/tiff", filename=fname)

    if not layers:
        raise HTTPException(502, "Không đọc được band nào")

    # Ghép thành GeoTIFF đa lớp (lớp đầu làm chuẩn shape/transform)
    ref = layers[0]
    height, width = ref[1].shape
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": len(layers),
        "dtype": "float32",
        "crs": ref[3],
        "transform": ref[2],
        "compress": "deflate",
    }
    try:
        with rasterio.open(out, "w", **profile) as dst:
            for idx, (b, arr, _tf, _crs) in enumerate(layers, start=1):
                # Resample nếu band resolution khác lớp đầu (vd SCL 20m trong band 10m)
                if arr.shape != (height, width):
                    import numpy as _np  # noqa: PLC0415
                    resized = _np.empty((height, width), dtype="float32")
                    from rasterio.enums import Resampling as _Res  # noqa: PLC0415
                    with rasterio.MemoryFile() as memfile:
                        with rasterio.open(
                            memfile, "w", driver="GTiff", height=arr.shape[0],
                            width=arr.shape[1], count=1, dtype="float32",
                            crs=_crs, transform=_tf,
                        ) as msrc:
                            msrc.write(arr, 1)
                        with rasterio.open(memfile) as msrc:
                            data2 = msrc.read(
                                1, out_shape=(height, width),
                                resampling=_Res.nearest,
                            )
                    resized[:] = data2
                    arr = resized
                dst.write(arr, idx)
                dst.set_band_description(idx, b)
    except Exception as exc:
        raise HTTPException(502, f"Ghép GeoTIFF thất bại: {exc}") from exc

    return FileResponse(
        path=out,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/s2/aws/download")
def s2_aws_download(
    product_id: str,
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float = 0.0,
    province_id: str | None = None,
    rotation_deg: float = 0.0,
    commune_id: str | None = None,
    selection_mode: Literal['point', 'rectangle', 'province', 'commune'] | None = None,
    center_lon: float | None = None,
    center_lat: float | None = None,
    width_m: float | None = None,
    height_m: float | None = None,
):
    """Tải GeoTIFF đa lớp (tất cả band COG) của một item S2 L2A từ AWS.

    Không cần auth. Ba chế độ chọn vùng:
    - province_id: đọc phần tile GIAO với bbox của tỉnh (chế độ nhóm scene
      theo tỉnh — không phụ thuộc điểm chọn, tile kề vẫn tải đúng phần
      phủ tỉnh).
    - lon/lat + radius_m: đọc windowed quanh điểm.
    - Không có gì: tải cả tile MGRS.
    Tên file chứa sensor, tile, ngày, ID item (tránh trùng khi tải ảnh khác).
    """
    # Lấy item trực tiếp theo ID qua STAC — không phụ thuộc tọa độ điểm
    # (điểm chọn có thể nằm ngoài footprint tile của item, search theo điểm
    # sẽ bỏ sót -> 404 nhầm).
    rectangle = _s2_rectangle_selection(selection_mode, center_lon, center_lat, width_m, height_m,
                                         rotation_deg, lon, lat, radius_m, province_id, commune_id)
    output_grid = (rectangle.output_grid(S2_SCALE) if rectangle else
                   _s2_output_grid(lon, lat, radius_m, rotation_deg, province_id or commune_id))
    try:
        product = aws_get_item(product_id)
    except Exception as exc:
        raise HTTPException(502, f"AWS STAC query failed: {exc}") from exc
    if product is None or not product.get("assets"):
        raise HTTPException(404, f"Không tìm thấy item AWS STAC: {product_id}")

    tile = product.get("tile") or "tile"
    date = product.get("date") or "date"
    # Tên đầy đủ: sentinel2_tile_date_ID.tif — ID đảm bảo không trùng
    fname = f"sentinel2_{tile}_{date}_{product_id[:60]}.tif"
    out = _DOWNLOADS_DIR / fname

    try:
        if rectangle is not None:
            pb = rectangle.bbox
            bbox = (pb.west, pb.south, pb.east, pb.north)
        elif province_id or commune_id:
            # Chế độ tỉnh: window = giao (tile ∩ bbox tỉnh)
            try:
                province = get_commune(commune_id) if commune_id else get_province(province_id)
            except RuntimeError as exc:
                raise HTTPException(
                    503, f"Administrative boundaries unavailable: {exc}"
                ) from exc
            if province is None:
                raise HTTPException(404, f"Province not found: {province_id}")
            pb = province.bbox
            bbox = (pb.west, pb.south, pb.east, pb.north)
            # Ghi rõ nguồn vùng trong tên file
            fname = f"sentinel2_{tile}_{date}_{province.id}_{product_id[:48]}.tif"
            out = _DOWNLOADS_DIR / fname
        elif lon is not None and lat is not None and radius_m > 0:
            _, pb = point_roi(lon, lat, radius_m, rotation_deg)
            bbox = (pb.west, pb.south, pb.east, pb.north)
        else:
            bbox = None
        if output_grid:
            tag = (_rectangle_tag(rectangle) if rectangle else
                   f'pt{lon:.5f}_{lat:.5f}_r{radius_m:g}_a{rotation_deg % 360:g}')
            fname = f"{Path(fname).stem}_{tag}.tif"
            out = _DOWNLOADS_DIR / f"{uuid.uuid4().hex}_{fname}"
        aws_merge_all_bands(product["assets"], out, bbox,
                            **({"output_grid": output_grid} if output_grid else {}))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Không đọc được COG từ AWS: {exc}") from exc

    return FileResponse(
        path=out,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/s2/aws/province-mosaic")
def s2_aws_province_mosaic(
    province_id: str,
    start_date: str,
    end_date: str,
    cloud_max: float | None = None,
):
    """Ghép các tile S2 L2A COG (AWS) thành 1 GeoTIFF cắt đúng biên tỉnh.

    Tương tự option "Sentinel-2 SR (mosaic)" của GEE: mỗi MGRS tile phủ
    tỉnh chọn 1 scene ít mây nhất (theo eo:cloud_cover của STAC), đọc
    windowed vùng tile ∩ bbox tỉnh, mask pixel mây theo SCL, mosaic các
    tile về cùng lưới UTM 10 m rồi cắt theo polygon tỉnh. Không cần auth,
    không qua GEE. Band: B02, B03, B04, B08, SCL (SR harmonize float32).
    """
    _validate_date_range(start_date, end_date)
    try:
        province = get_province(province_id)
    except RuntimeError as exc:
        raise HTTPException(
            503, f"Administrative boundaries unavailable: {exc}"
        ) from exc
    if province is None:
        raise HTTPException(404, f"Province not found: {province_id}")

    pb = province.bbox
    bbox = (pb.west, pb.south, pb.east, pb.north)
    try:
        products = aws_search_s2_l2a_bbox(
            bbox, start_date, end_date, cloud_max=cloud_max
        )
    except Exception as exc:
        raise HTTPException(502, f"AWS STAC query failed: {exc}") from exc
    if not products:
        raise HTTPException(
            404,
            "Không tìm thấy scene S2 L2A nào phủ tỉnh trong khoảng ngày này.",
        )

    # Mỗi tile chọn 1 scene ít mây nhất (eo:cloud_cover), rồi sort theo
    # ngày mới nhất trước — tile sau lấp pixel còn trống sau tile trước.
    best_per_tile: dict[str, dict] = {}
    for p in products:  # products đã sort newest-first
        tile = p.get("tile")
        if not tile or not p.get("assets"):
            continue
        cur = best_per_tile.get(tile)
        if cur is None or (p.get("cloud_pct") or 999) < (
            cur.get("cloud_pct") or 999
        ):
            best_per_tile[tile] = p
    if not best_per_tile:
        raise HTTPException(
            404, "Không scene nào có asset COG khả dụng cho tỉnh này."
        )
    selected = sorted(
        best_per_tile.values(),
        key=lambda p: p.get("date") or "",
        reverse=True,
    )

    fname = (
        f"sentinel2_aws_mosaic_{province.id}_{start_date}_{end_date}.tif"
    )
    out = _DOWNLOADS_DIR / fname
    try:
        used = aws_province_mosaic(
            selected, province.geometry, bbox, out
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Ghép mosaic tỉnh thất bại: {exc}") from exc

    tiles_tag = "-".join(
        sorted({u["tile"] or "?" for u in used})
    )
    return FileResponse(
        path=out,
        media_type="image/tiff",
        filename=f"sentinel2_aws_mosaic_{province.id}_{used[0]['date'] if used else start_date}_{tiles_tag}.tif",
        headers={
            "Content-Disposition": (
                f'attachment; filename="sentinel2_aws_mosaic_{province.id}'
                f'_{start_date}_{end_date}.tif"'
            ),
            "X-Tile-Count": str(len(used)),
        },
    )


# Serve the frontend
from .scene_catalog import register_scene_catalog

_scene_catalog_store = register_scene_catalog(app)

from .frame_api import register_frame_routes

register_frame_routes(app, lambda: _frame_service)

from .s5p import register_s5p

_s5p_store = register_s5p(app)

from .scene_inventory import register_scene_inventory

_scene_inventory_store = register_scene_inventory(app)

register_vector_routes(app, _vector_store)
from .umbra_api import register_umbra

_umbra_catalog, _umbra_store = register_umbra(app, _DOWNLOADS_DIR / 'umbra', _vector_store)
register_batch_routes(app, _batch_store, lambda: _batch_service)
from .task_center import register_task_center
register_task_center(app, _timeseries_store, _batch_store, _frame_store,
                     lambda: _esri_service,
                     lambda: _get_download_store() if (_DOWNLOADS_DIR / 'gee-downloads' / 'state.sqlite3').is_file() else None,
                     lambda: _batch_service, _get_download_service)
app.include_router(local_provider_settings_router)

app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
