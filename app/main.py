"""HTTP API for the GEE SDR download app."""
from __future__ import annotations

import datetime as dt
import asyncio
from contextlib import asynccontextmanager, suppress
import os
import re
import time
from pathlib import Path
from typing import Literal

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .esri import EsriRequest, EsriService, BusyError as EsriBusyError
from .timeseries import BusyError, TimeSeriesService, TimeSeriesStore
from .imagery import list_timeseries_scenes, timeseries_source_metadata

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
from .geo import Bbox, bbox_to_ee_geom, point_bbox
from .imagery import (
    LANDSAT_SCALE,
    NICFI_SCALE,
    DYNAMIC_WORLD_SCALE,
    S1_SCALE,
    S2_SCALE,
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

_timeseries_store = TimeSeriesStore(Path(__file__).resolve().parent.parent / "downloads" / "timeseries")
_timeseries_service = TimeSeriesService(_timeseries_store)
_esri_root = Path(__file__).resolve().parent.parent / "downloads" / "esri"
_esri_service = EsriService(_esri_root)


@asynccontextmanager
async def _lifespan(application):
    global _timeseries_service, _esri_service
    if _esri_service._closed:
        _esri_service = EsriService(_esri_root)
    _esri_service.recover()
    if _timeseries_service._closed:
        _timeseries_service = TimeSeriesService(_timeseries_store)
    _timeseries_service.recover_interrupted_jobs()
    _timeseries_service.cleanup_expired_jobs()

    async def cleanup_loop():
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(_timeseries_service.cleanup_expired_jobs)

    cleanup_task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        await asyncio.to_thread(_esri_service.shutdown)
        await asyncio.to_thread(_timeseries_service.shutdown)


app = FastAPI(title="GEE Surface Reflectance Downloader", version="0.1.0", lifespan=_lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"
_DOWNLOADS_DIR.mkdir(exist_ok=True)

_MAX_RADIUS_M = 45_000.0
_DEFAULT_DAYS = 30

# Import rasterio NGAY ở main thread: trên Windows, lần import đầu tiên
# trong worker thread (threadpool FastAPI) bị lỗi DLL "handle is invalid"
# (GDAL init cần main thread). Import sớm để mọi import lazy sau đó chỉ
# lấy từ sys.modules.
try:  # pragma: no cover - phụ thuộc máy
    import rasterio  # noqa: F401
except ImportError:
    rasterio = None

_ee_ready = False


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


class SceneRequest(BaseModel):
    selection_mode: Literal["point", "province", "commune"] = "point"
    lon: float | None = Field(None, ge=-180, le=180, description="Longitude (degrees)")
    lat: float | None = Field(None, ge=-90, le=90, description="Latitude (degrees)")
    province_id: str | None = Field(None, min_length=1, max_length=20)
    commune_id: str | None = Field(None, min_length=1, max_length=20)
    dataset: str = Field(
        "sentinel2", pattern="^(sentinel2|landsat|nicfi|dynamic_world)$"
    )
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    radius_m: float = Field(2000.0, gt=0, le=_MAX_RADIUS_M)
    merge_scenes: bool = False
    composite_method: Literal["mosaic", "median", "medoid", "base-score", "raw"] = Field(
        "medoid", description="Province mode: how to composite multiple scenes"
    )

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
    scene_id: str
    date: str
    cloud_pct: float
    bbox: dict[str, float]
    scale_m: float
    bands: list[str]
    preview_url: str | None = None
    download_url: str | None = None
    selection_mode: Literal["point", "province", "commune"]
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
    radius_m: float,
    province_id: str | None,
    commune_id: str | None = None,
) -> tuple[object, Bbox, Province | Commune | None]:
    """Resolve a trusted point or local administrative polygon into an ROI."""
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
    bbox = point_bbox(lon, lat, radius_m)
    return bbox_to_ee_geom(bbox), bbox, None


def _bbox_dict(bbox: Bbox) -> dict[str, float]:
    return {
        "west": round(bbox.west, 6),
        "south": round(bbox.south, 6),
        "east": round(bbox.east, 6),
        "north": round(bbox.north, 6),
    }


class TimeSeriesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    selection_mode: Literal["point"] = "point"
    dataset: Literal["sentinel2", "landsat", "nicfi", "dynamic_world", "s1gee"]
    lon: float = Field(..., ge=-180, le=180)
    lat: float = Field(..., ge=-90, le=90)
    radius_m: float = Field(2000.0, gt=0, le=_MAX_RADIUS_M)
    start_date: str
    end_date: str
    polarization: Literal["vv", "vh", "vvvh", "hh", "hv", "hhhv"] = "vvvh"

    @field_validator("start_date", "end_date")
    @classmethod
    def valid_date(cls, value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Date must be YYYY-MM-DD")
        dt.date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def valid_interval(self):
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


class TimeSeriesJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_id: str = Field(..., min_length=1, max_length=64)


@app.post("/api/timeseries/search")
def search_timeseries(body: TimeSeriesRequest) -> dict:
    _ensure_ee()
    _, bbox, _ = _resolve_selection("point", body.lon, body.lat, body.radius_m, None)
    end_exclusive = (dt.date.fromisoformat(body.end_date) + dt.timedelta(days=1)).isoformat()
    try:
        scenes = list_timeseries_scenes(body.dataset, body.lon, body.lat,
                                       body.start_date, end_exclusive, body.polarization)
        parameters = body.model_dump()
        parameters.update(timeseries_source_metadata(body.dataset, body.polarization))
        parameters["bbox"] = vars(bbox)
        parameters["crs"] = "EPSG:4326"
        scenes = [TimeSeriesSceneResponse.model_validate(scene).model_dump() for scene in scenes]
        return _timeseries_store.create_search(parameters, scenes)
    except Exception as exc:
        raise HTTPException(502, "Unable to list GEE scenes; check collection access and retry") from exc


@app.get("/api/timeseries/search/{search_id}")
def timeseries_search_page(search_id: str, offset: int = 0, limit: int = 50) -> dict:
    if offset < 0 or not 1 <= limit <= 50:
        raise HTTPException(400, "offset must be nonnegative and limit must be 1–50")
    result = _timeseries_store.get_search(search_id, offset, limit)
    if result is None:
        raise HTTPException(404, "Search not found or expired")
    return result


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
    return FileResponse(path, media_type="application/zip", filename=f"timeseries_{job_id}.zip")


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
    _ensure_ee()
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be <= end_date")

    roi, bbox, province = _resolve_selection(
        req.selection_mode, req.lon, req.lat, req.radius_m, req.province_id, req.commune_id
    )

    # Inclusive end date for GEE filterDate (shift by +1 day)
    end_exclusive = (
        dt.date.fromisoformat(req.end_date) + dt.timedelta(days=1)
    ).isoformat()

    try:
        if province is not None:
            scene = best_scene_for_roi(
                req.dataset,
                roi,
                req.start_date,
                end_exclusive,
                composite=True,
                composite_method=req.composite_method,
            )
        else:
            scene = best_scene(
                dataset=req.dataset,
                lon=req.lon,
                lat=req.lat,
                radius_m=req.radius_m,
                start_date=req.start_date,
                end_date=end_exclusive,
                merge_scenes=req.merge_scenes,
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
            "No image found for this selection/date range. "
            "Try widening the date range or increasing cloud tolerance.",
        )

    bbox_dict = _bbox_dict(bbox)

    if req.dataset == "sentinel2":
        bands = ["B2", "B3", "B4", "B8", "B11", "B12"]
    elif req.dataset == "nicfi":
        bands = ["B", "G", "R", "N"]
    elif req.dataset == "dynamic_world":
        bands = ["label"]
    else:
        bands = ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]

    try:
        preview_url = preview_rgb(
            scene.ee_object, req.dataset, bbox, roi if province is not None else None
        )
    except Exception:
        preview_url = None

    download_path = f"/api/download/{req.dataset}"
    if province is not None:
        selection_query = (
            f"selection_mode=commune&commune_id={province.id}"
            if isinstance(province, Commune)
            else f"selection_mode=province&province_id={province.id}"
        )
        download_params = (
            f"?{selection_query}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
        )
    else:
        download_params = (
            f"?selection_mode=point&lon={req.lon}&lat={req.lat}&radius_m={req.radius_m}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
        )

    point_merge = province is None and req.merge_scenes and req.dataset in ("sentinel2", "landsat")
    if province is not None:
        download_params += f"&composite_method={req.composite_method}"
    elif point_merge:
        download_params += "&merge_scenes=true"

    return SceneResponse(
        dataset=req.dataset,
        scene_id=scene.scene_id,
        date=scene.date,
        cloud_pct=scene.cloud_pct,
        bbox=bbox_dict,
        scale_m=_scale_for(req.dataset),
        bands=bands,
        preview_url=preview_url,
        download_url=download_path + download_params,
        selection_mode=req.selection_mode,
        province_id=province.id if province else None,
        province_name=(province.province_name if isinstance(province, Commune) else province.name) if province else None,
        commune_id=province.id if isinstance(province, Commune) else None,
        commune_name=province.name if isinstance(province, Commune) else None,
        composite=province is not None or point_merge,
        coverage_pct=scene.coverage_pct,
        composite_method=(
            "dynamic-world-mode"
            if province is not None and req.dataset == "dynamic_world"
            else req.composite_method if province is not None else "mosaic" if point_merge else None
        ),
        scene_count=scene.scene_count,
        scenes=list(scene.scenes),
        estimated_seconds=scene.estimated_seconds,
    )


@app.post("/api/composite-plan")
def plan_composite(req: SceneRequest) -> dict:
    """Preview the cost and scenes before running a province composite."""
    _ensure_ee()
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be <= end_date")
    if req.selection_mode not in ("province", "commune"):
        raise HTTPException(400, "Composite planning is only available for administrative selection")
    if req.dataset not in ("sentinel2", "landsat"):
        raise HTTPException(400, "Composite planning is only available for Sentinel-2 or Landsat")

    roi, _bbox, _province = _resolve_selection(
        req.selection_mode, req.lon, req.lat, req.radius_m, req.province_id, req.commune_id
    )
    end_exclusive = (dt.date.fromisoformat(req.end_date) + dt.timedelta(days=1)).isoformat()
    try:
        plan = composite_plan(
            req.dataset, roi, req.start_date, end_exclusive, req.composite_method
        )
    except Exception as exc:
        raise HTTPException(502, f"Earth Engine planning failed: {exc}") from exc
    if plan is None:
        raise HTTPException(404, "No image found for this selection/date range")
    return plan


@app.get("/api/download/{dataset}")
def download(
    dataset: str,
    start_date: str,
    end_date: str,
    selection_mode: Literal["point", "province", "commune"] = "point",
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float = 2000.0,
    province_id: str | None = None,
    commune_id: str | None = None,
    merge_scenes: bool = False,
    composite_method: Literal[
        "mosaic", "median", "medoid", "base-score", "raw"
    ] = "medoid",
):
    _ensure_ee()
    if dataset not in ("sentinel2", "landsat", "nicfi", "dynamic_world"):
        raise HTTPException(
            400, "dataset must be sentinel2, landsat, nicfi or dynamic_world"
        )
    roi, bbox, province = _resolve_selection(
        selection_mode, lon, lat, radius_m, province_id, commune_id
    )

    end_exclusive = (
        dt.date.fromisoformat(end_date) + dt.timedelta(days=1)
    ).isoformat()

    if province is not None:
        if dataset not in ("sentinel2", "landsat"):
            composite_method = "mosaic"
        scene = best_scene_for_roi(
            dataset,
            roi,
            start_date,
            end_exclusive,
            composite=True,
            composite_method=composite_method,
        )
    else:
        scene = best_scene(
            dataset=dataset,
            lon=lon,
            lat=lat,
            radius_m=radius_m,
            start_date=start_date,
            end_date=end_exclusive,
            merge_scenes=merge_scenes,
        )
    if scene is None:
        raise HTTPException(404, "No scene found for this point/date range")

    scale = _scale_for(dataset)
    band_urls = download_geotiff(
        scene.ee_object,
        dataset,
        bbox,
        scale,
        roi if province is not None else None,
        # Medoid/base-score có graph nhiều scene; tile quá lớn có thể chết
        # "User memory limit exceeded" lúc fetch, nên chia sẵn theo diện tích.
        max_tile_pixels=MEDOID_MAX_TILE_PIXELS
        if province is not None and composite_method in ("medoid", "base-score")
        and dataset in ("sentinel2", "landsat")
        else None,
    )

    if province is not None:
        method_tag = (
            composite_method
            if dataset in ("sentinel2", "landsat")
            else "mode" if dataset == "dynamic_world" else "mosaic"
        )
        fname = f"{dataset}_{province.id}_{start_date}_{end_date}_{method_tag}.tif"
    else:
        fname = f"{dataset}_{scene.scene_id[:40]}_{scene.date}.tif"
    out = _DOWNLOADS_DIR / fname
    try:
        merge_band_tiffs(
            band_urls, out, categorical=dataset == "dynamic_world"
        )
    except requests.RequestException as exc:
        raise HTTPException(502, f"Failed to download GeoTIFF from GEE: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(502, f"Failed to merge band GeoTIFFs: {exc}") from exc

    return FileResponse(
        path=out,
        media_type="image/tiff",
        filename=fname,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ---------------------------------------------------------------------------
# Sentinel-1 SAR trên GEE (không cần tài khoản CDSE)
# ---------------------------------------------------------------------------


class S1SceneRequest(BaseModel):
    selection_mode: Literal["point", "province", "commune"] = "point"
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    province_id: str | None = Field(None, min_length=1, max_length=20)
    commune_id: str | None = Field(None, min_length=1, max_length=20)
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD (inclusive)")
    radius_m: float = Field(2000.0, gt=0, le=_MAX_RADIUS_M)
    polarization: str = Field("vvvh", pattern="^(vvvh|vv|hhhv|hh)$")

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
    """Tìm ảnh Sentinel-1 GRD trên GEE (cảnh mới nhất, ưu tiên orbit DESC)."""
    _ensure_ee()
    if req.start_date > req.end_date:
        raise HTTPException(400, "start_date must be <= end_date")

    roi, bbox, province = _resolve_selection(
        req.selection_mode, req.lon, req.lat, req.radius_m, req.province_id, req.commune_id
    )

    end_exclusive = (
        dt.date.fromisoformat(req.end_date) + dt.timedelta(days=1)
    ).isoformat()
    try:
        if province is not None:
            scene = best_s1_scene_for_roi(
                roi, req.start_date, end_exclusive, req.polarization
            )
        else:
            scene = best_s1_scene(
                lon=req.lon,
                lat=req.lat,
                radius_m=req.radius_m,
                start_date=req.start_date,
                end_date=end_exclusive,
                polarization=req.polarization,
            )
    except Exception as exc:
        raise HTTPException(502, f"Earth Engine query failed: {exc}") from exc

    if scene is None:
        raise HTTPException(
            404,
            "No Sentinel-1 scene found for this selection/date range.",
        )

    try:
        available = set(scene.ee_object.bandNames().getInfo())
    except Exception:
        available = set()
    bands = [b for b in ["VV", "VH", "HH", "HV"] if b in available]

    try:
        preview_url = preview_rgb(
            scene.ee_object, "s1gee", bbox, roi if province is not None else None
        )
    except Exception:
        preview_url = None

    if province is not None:
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
    else:
        download_params = (
            f"?selection_mode=point&lon={req.lon}&lat={req.lat}&radius_m={req.radius_m}"
            f"&start_date={req.start_date}&end_date={req.end_date}"
            f"&polarization={req.polarization}"
        )
    return {
        "dataset": "s1gee",
        "scene_id": scene.scene_id,
        "date": scene.date,
        "cloud_pct": scene.cloud_pct,
        "bbox": _bbox_dict(bbox),
        "scale_m": S1_SCALE,
        "bands": bands,
        "preview_url": preview_url,
        "download_url": f"/api/s1gee/download{download_params}",
        "selection_mode": req.selection_mode,
        "province_id": province.id if province else None,
        "province_name": (province.province_name if isinstance(province, Commune) else province.name) if province else None,
        "commune_id": province.id if isinstance(province, Commune) else None,
        "commune_name": province.name if isinstance(province, Commune) else None,
    }


@app.get("/api/s1gee/download")
def s1gee_download(
    start_date: str,
    end_date: str,
    selection_mode: Literal["point", "province", "commune"] = "point",
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float = 2000.0,
    polarization: str = "vvvh",
    province_id: str | None = None,
    commune_id: str | None = None,
):
    """Tải GeoTIFF S1 GRD (dB) theo vùng đã chọn — không cần tài khoản CDSE."""
    _ensure_ee()
    if polarization not in ("vvvh", "vv", "hhhv", "hh"):
        raise HTTPException(400, "polarization must be vvvh|vv|hhhv|hh")
    roi, bbox, province = _resolve_selection(
        selection_mode, lon, lat, radius_m, province_id, commune_id
    )

    end_exclusive = (
        dt.date.fromisoformat(end_date) + dt.timedelta(days=1)
    ).isoformat()
    if province is not None:
        scene = best_s1_scene_for_roi(roi, start_date, end_exclusive, polarization)
    else:
        scene = best_s1_scene(lon, lat, radius_m, start_date, end_exclusive, polarization)
    if scene is None:
        raise HTTPException(404, "No Sentinel-1 scene found for this point/date range")

    band_urls = download_geotiff(
        scene.ee_object, "s1gee", bbox, S1_SCALE, roi if province is not None else None
    )

    if province is not None:
        fname = f"s1gee_{province.id}_{start_date}_{end_date}.tif"
    else:
        fname = f"s1gee_{scene.scene_id[:40]}_{scene.date}.tif"
    out = _DOWNLOADS_DIR / fname
    try:
        merge_band_tiffs(band_urls, out)
    except requests.RequestException as exc:
        raise HTTPException(502, f"Failed to download GeoTIFF from GEE: {exc}") from exc
    except RuntimeError as exc:
        raise HTTPException(502, f"Failed to merge band GeoTIFFs: {exc}") from exc

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
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


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
    _ensure_cdse_creds()

    # Lấy thông tin product trước khi stream (fail sớm, rõ ràng)
    try:
        s3_path = get_product_s3_path(product_id)
    except Exception as exc:
        raise HTTPException(502, f"Không lấy được S3Path: {exc}") from exc

    try:
        creds = get_s3_credentials()
    except Exception as exc:
        raise HTTPException(502, f"Tạo S3 credentials thất bại: {exc}") from exc

    # Chờ key được install trên object storage (doc CDSE khuyến nghị ~5s)
    time.sleep(5)

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
                    dst_crs="EPSG:4326",
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

    _ensure_cdse_creds()

    try:
        creds = get_s3_credentials()
    except Exception as exc:
        raise HTTPException(502, f"Chuẩn bị S3 thất bại: {exc}") from exc
    time.sleep(5)  # chờ key install trên object storage

    # Lưới đích WGS84 phủ bbox tỉnh
    pb = province.bbox
    res_deg = scale_m / 111_320.0
    width = int(-(-(pb.east - pb.west) // res_deg))  # ceil
    height = int(-(-(pb.north - pb.south) // res_deg))
    if width * height > 120_000_000:
        raise HTTPException(
            400,
            f"Vùng quá lớn ở {scale_m}m ({width}x{height} px) — tăng scale_m.",
        )
    dst_tf = from_bounds(pb.west, pb.south, pb.east, pb.north, width, height)

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
    if len(product_ids) == 1:
        fname = f"s1grd_{date_tag}_{province.id}_{pol_tag}_{first_pid[:8]}.tif"
    else:
        fname = (
            f"s1grd_mosaic{len(product_ids)}_{date_tag}_"
            f"{province.id}_{pol_tag}.tif"
        )
    out_path = _DOWNLOADS_DIR / fname
    tmp_dir = _DOWNLOADS_DIR / f"_crop_{first_pid[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with rasterio.open(
            out_path, "w", driver="GTiff", width=width, height=height,
            count=len(pols), dtype="float32", crs="EPSG:4326",
            transform=dst_tf, nodata=float("nan"), compress="deflate",
            tiled=True, bigtiff="IF_SAFER",
        ) as dst:
            out = np.full((len(pols), height, width), np.nan, dtype="float32")
            for pid in product_ids:
                _s1_grd_mosaic_product(
                    pid, province, pols, all_meas[pid], creds,
                    dst_tf, width, height, out, tmp_dir,
                )
            # Mask theo polygon tỉnh (MultiPolygon GeoJSON) — 1 lần sau
            # khi mosaic xong mọi product
            pmask = rasterize(
                [(province.geometry, 1)],
                out_shape=(height, width), transform=dst_tf,
                fill=0, dtype="uint8",
            )
            out[:, pmask == 0] = np.nan
            if np.isnan(out).all():
                raise HTTPException(
                    400,
                    f"Không product nào phủ tỉnh {province.name} "
                    f"(cảnh GRD nằm hoàn toàn ngoài biên giới).",
                )
            for bi, pol in enumerate(pols, start=1):
                dst.write(out[bi - 1], bi)
                dst.set_band_description(bi, pol.upper())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Warp/cắt GRD thất bại: {exc}") from exc
    finally:
        import shutil  # noqa: PLC0415

        shutil.rmtree(tmp_dir, ignore_errors=True)

    return out_path, fname


@app.get("/api/s1/crop")
def s1_crop(product_id: str, province_id: str, scale_m: float = 20.0):
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
    if scale_m < 10.0 or scale_m > 500.0:
        raise HTTPException(400, "scale_m phải trong 10..500 mét")

    try:
        province = get_province(province_id)
    except RuntimeError as exc:
        raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
    if province is None:
        raise HTTPException(404, f"Province not found: {province_id}")

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
    province_id: str,
    scale_m: float = 20.0,
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
    if scale_m < 10.0 or scale_m > 500.0:
        raise HTTPException(400, "scale_m phải trong 10..500 mét")

    try:
        province = get_province(province_id)
    except RuntimeError as exc:
        raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
    if province is None:
        raise HTTPException(404, f"Province not found: {province_id}")

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

    try:
        if source == "aws":
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
    return {"source": source, "count": len(products), "products": products}


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
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"'},
    )


@app.get("/api/s2/bands")
def s2_bands(
    product_id: str,
    action: Literal["list", "download"] = "list",
    bands: str = "B02,B03,B04,B08",
    province_id: str | None = None,
    lon: float | None = None,
    lat: float | None = None,
    radius_m: float = 0.0,
):
    """Liệt kê hoặc tải band từ product S2 L2A ZIP trên CDSE qua Nodes API.

    action='list': trả về cây band JP2 theo resolution (R10m/R20m/R60m)
    kèm kích thước từng file — mỗi file ~100 MB nên tải được qua proxy
    công ty (khác /$value là cả ZIP ~1.3 GB bị chặn > 300 MB).

    action='download': tải các band (CSV: B02,B03,B04,B08) rồi ghép thành
    GeoTIFF đa lớp, harmonize reflectance như pipeline GEE. Có thể crop
    theo province_id hoặc lon/lat + radius_m.
    """
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
    if province_id:
        try:
            province = get_province(province_id)
        except RuntimeError as exc:
            raise HTTPException(503, f"Administrative boundaries unavailable: {exc}") from exc
        if province is None:
            raise HTTPException(404, f"Province not found: {province_id}")
        pb = province.bbox
        crop_bbox = (pb.west, pb.south, pb.east, pb.north)
        tag = province.id
    elif lon is not None and lat is not None and radius_m > 0:
        pb = point_bbox(lon, lat, radius_m)
        crop_bbox = (pb.west, pb.south, pb.east, pb.north)
        tag = f"pt{lon:.3f}_{lat:.3f}_r{int(radius_m)}"

    # Tên từ product (scene id ngắn) — tile + date lấy từ tên .SAFE
    safe = info["safe"]
    m = re.search(r"_T(\d{2}[A-Z]{3})_(\d{8})T", safe)
    tile = m.group(1) if m else "tile"
    date = m.group(2)[:4] + "-" + m.group(2)[4:6] + "-" + m.group(2)[6:8] if m else "date"
    band_tag = "-".join(wanted)
    fname = f"sentinel2_cdse_{tile}_{date}_{tag}_{band_tag}.tif"
    out = _DOWNLOADS_DIR / fname
    band_dir = _DOWNLOADS_DIR / f"_nodes_{product_id[:8]}"
    band_dir.mkdir(exist_ok=True)

    # Tải từng band JP2 (mỗi file ~100 MB — lọt dưới ngưỡng proxy)
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.vrt import WarpedVRT  # noqa: PLC0415
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
                    win = win.round_lengths().round_offsets()
                    data = src.read(1, window=win, out_dtype="float32")
                    transform = src.window_transform(win)
                else:
                    data = src.read(1, out_dtype="float32")
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
    try:
        product = aws_get_item(product_id)
    except Exception as exc:
        raise HTTPException(502, f"AWS STAC query failed: {exc}") from exc
    if product is None or not product.get("assets"):
        raise HTTPException(404, f"Không tìm thấy item AWS STAC: {product_id}")

    tile = product.get("tile") or "tile?"
    date = product.get("date") or "date?"
    # Tên đầy đủ: sentinel2_tile_date_ID.tif — ID đảm bảo không trùng
    fname = f"sentinel2_{tile}_{date}_{product_id[:60]}.tif"
    out = _DOWNLOADS_DIR / fname

    try:
        if province_id:
            # Chế độ tỉnh: window = giao (tile ∩ bbox tỉnh)
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
            # Ghi rõ nguồn vùng trong tên file
            fname = f"sentinel2_{tile}_{date}_{province.id}_{product_id[:48]}.tif"
            out = _DOWNLOADS_DIR / fname
        elif lon is not None and lat is not None and radius_m > 0:
            pb = point_bbox(lon, lat, radius_m)
            bbox = (pb.west, pb.south, pb.east, pb.north)
        else:
            bbox = None
        aws_merge_all_bands(product["assets"], out, bbox)
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
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
