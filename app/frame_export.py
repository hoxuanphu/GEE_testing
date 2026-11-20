"""Direct, fixed-grid matched-frame exports with atomic verified publication."""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import time
from pathlib import Path
from uuid import uuid4

import ee
import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.windows import Window

from .frame_search import (
    COLLECTIONS, QUALITY_POLICY, S2_NATIVE_RESOLUTION, FrameSearchError,
    FrameUpstreamError, _RPC, _TransientError, _retry, validate_asset,
)

TILE_BYTES_LIMIT = 16 * 1024 * 1024
TILE_DIMENSION_LIMIT = 2048
MIN_FREE_DISK = 512 * 1024 * 1024
# Explicit wire NoData avoids treating native zero as invalid when EE omits a TIFF mask.
_WIRE_NODATA = -999999.25  # Exactly representable Float32, outside native SR/dB range.
_LEGACY_WIRE_NODATA = float(np.float32(-9223372036854775808))
_REQUIRED_TAGS = ("SOURCE_COLLECTION", "SOURCE_SCENE", "DATASET", "ACQUISITION_TIME_UTC",
                  "BAND_ORDER", "UNITS", "RESAMPLING", "GRID_MODE", "REFERENCE_FINGERPRINT",
                  "QUALITY_POLICY", "SOURCE_NATIVE_RESOLUTION_M")
_REFERENCE_KEYS = ("crs", "transform", "width", "height", "corners", "bounds", "footprint_geojson",
                   "descriptions", "dtypes", "count", "tags")


class FrameExportError(FrameSearchError):
    def __init__(self, code: str, message: str, report: dict | None = None):
        super().__init__(code, message)
        self.report = report or {"status": "failed", "error_code": code}


def _grid(grid: dict) -> dict:
    try:
        width, height = grid["width"], grid["height"]
        if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
            raise ValueError()
        if not 0 < width <= 30000 or not 0 < height <= 30000 or width * height > 100000000:
            raise ValueError()
        coefficients = list(grid["transform"])
        if len(coefficients) != 6 or not all(math.isfinite(x) for x in coefficients):
            raise ValueError()
        transform = Affine(*coefficients)
        if transform.determinant == 0:
            raise ValueError()
        CRS.from_user_input(grid["crs"])
        return {"crs": grid["crs"], "transform": coefficients, "width": width, "height": height}
    except (KeyError, TypeError, ValueError, rasterio.errors.CRSError):
        raise FrameExportError("invalid_grid", "Lưới xuất không hợp lệ hoặc vượt giới hạn.") from None


def iter_grid_tiles(grid: dict, band_count: int, byte_limit: int = TILE_BYTES_LIMIT):
    """Yield exact integer windows, each bounded by dimensions and Float32 bytes."""
    grid = _grid(grid)
    if not isinstance(band_count, int) or band_count <= 0 or byte_limit < band_count * 4:
        raise FrameExportError("invalid_tile_limit", "Giới hạn tile không hợp lệ.")
    byte_limit = min(byte_limit, TILE_BYTES_LIMIT)
    pixels = byte_limit // (band_count * 4)
    width = min(grid["width"], TILE_DIMENSION_LIMIT, pixels)
    height = min(grid["height"], TILE_DIMENSION_LIMIT, pixels // width)
    for row in range(0, grid["height"], height):
        for col in range(0, grid["width"], width):
            yield Window(col, row, min(width, grid["width"] - col), min(height, grid["height"] - row))


def _tile_grid(grid: dict, window: Window) -> dict:
    transform = Affine(*grid["transform"]) * Affine.translation(window.col_off, window.row_off)
    return {"crs": grid["crs"], "transform": list(transform)[:6],
            "width": int(window.width), "height": int(window.height)}


def load_frame_image(asset_id: str, dataset: str, bands: list[str]):
    """Load exactly one raw allowlisted asset. Never clamp, log, mosaic or mask zero."""
    validate_asset(asset_id, dataset, bands)
    # The scoped RPC is initialized by callers before constructing EE graphs.
    from .ee_session import init_ee
    init_ee()
    image = ee.Image(asset_id).select(bands).toFloat()
    return image.multiply(0.0001) if dataset == "sentinel2" else image


def _target_image(image, dataset: str, bands: list[str], grid: dict):
    from .frame_validity import _mask_graph
    projection = {"crs": grid["crs"], "crsTransform": grid["transform"]}
    values = []
    for band in bands:
        native = image.select([band])
        # Each native band mask is projected directly to the final grid with EE's
        # default nearest. Reducing before this step loses 10 m masks to B1's 60 m grid.
        continuous = native.resample("bilinear") if dataset == "sentinel2" else native
        values.append(continuous.reproject(**projection))
    common = _mask_graph(image, bands, grid).reduce(ee.Reducer.min())
    # No pre-clip: source neighbours outside each target tile provide the bilinear halo.
    return ee.Image.cat(values).rename(bands).updateMask(common).unmask(
        _WIRE_NODATA, sameFootprint=False).toFloat()


def _assert_raster_grid(dataset, grid: dict, count: int) -> None:
    if (dataset.driver != "GTiff" or dataset.width != grid["width"] or dataset.height != grid["height"]
            or dataset.count != count or dataset.crs != CRS.from_user_input(grid["crs"])
            or any(abs(a - b) > 1e-8 for a, b in zip(list(dataset.transform)[:6], grid["transform"]))):
        raise FrameExportError("grid_mismatch", "Ảnh tải về không khớp lưới hoặc số kênh yêu cầu.")
    if any(dtype != "float32" for dtype in dataset.dtypes):
        raise FrameExportError("dtype_mismatch", "Ảnh tải về không có kiểu Float32.")


def fetch_grid_tile(image, dataset: str, bands: list[str], grid: dict, window: Window,
                    *, rpc=None) -> np.ma.MaskedArray:
    """Download and validate a target window, refreshing corrupt responses at most twice."""
    grid = _grid(grid)
    if (window.col_off < 0 or window.row_off < 0 or window.width <= 0 or window.height <= 0
            or window.col_off + window.width > grid["width"] or window.row_off + window.height > grid["height"]
            or any(float(value) != int(value) for value in (window.col_off, window.row_off, window.width, window.height))
            or window.width > TILE_DIMENSION_LIMIT or window.height > TILE_DIMENSION_LIMIT
            or window.width * window.height * len(bands) * 4 > TILE_BYTES_LIMIT):
        raise FrameExportError("invalid_tile", "Cửa sổ tile vượt giới hạn lưới.")
    if rpc is None:
        with _RPC(180) as scoped:
            return fetch_grid_tile(image, dataset, bands, grid, window, rpc=scoped)
    target = _tile_grid(grid, window)
    graph = _target_image(image, dataset, bands, target)
    params = {"crs": target["crs"], "crs_transform": target["transform"],
              "dimensions": [target["width"], target["height"]],
              "format": "GEO_TIFF", "filePerBand": False}
    deadline = time.monotonic() + 3 * 180 + 3

    def operation():
        content = rpc.thumbnail_once(graph, params, deadline=time.monotonic() + 180)
        try:
            with MemoryFile(content) as memory, memory.open() as source:
                _assert_raster_grid(source, target, len(bands))
                values = source.read(masked=True)
                invalid = (np.ma.getmaskarray(values) | ~np.isfinite(values.data)
                           | (values.data == _WIRE_NODATA) | (values.data == _LEGACY_WIRE_NODATA))
                if np.isinf(values.data).any():
                    raise FrameExportError("invalid_values", "Tile chứa giá trị vô hạn.")
                # The contract uses the common selected-band native validity mask.
                invalid = np.broadcast_to(invalid.any(axis=0), values.shape).copy()
                return np.ma.array(values.data, mask=invalid, dtype=np.float32)
        except FrameExportError:
            raise
        except (rasterio.errors.RasterioError, ValueError):
            raise _TransientError() from None

    return _retry(operation, deadline)


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _safe_reference(reference: dict) -> dict:
    result = {name: reference[name] for name in _REFERENCE_KEYS if name in reference}
    # Only these source hints are permitted; never copy arbitrary upload tags or paths.
    allowed = ("ACQUISITION_DATE", "ACQUISITION_TIME_UTC", "SOURCE_COLLECTION", "SOURCE_SCENE",
               "ORBIT_PASS", "RELATIVE_ORBIT", "PLATFORM")
    result["tags"] = {name: str(value)[:256] for name, value in reference.get("tags", {}).items() if name in allowed}
    return result


def _fingerprint(reference: dict) -> str:
    return hashlib.sha256(_canonical(_safe_reference(reference)).encode("utf-8")).hexdigest()


def _processing(dataset: str, bands: list[str]) -> dict:
    return {"dtype": "float32", "units": "reflectance" if dataset == "sentinel2" else "dB",
            "scale_factor": 0.0001 if dataset == "sentinel2" else 1,
            "resampling": "bilinear" if dataset == "sentinel2" else "nearest",
            "mask_resampling": "nearest", "validity": "intersection of selected native band masks",
            "nodata": "NaN", "clamp": False, "log_transform": False,
            "native_resolution_m": {band: S2_NATIVE_RESOLUTION[band] if dataset == "sentinel2" else 10
                                    for band in bands}}


def _scene_quality(scene: dict) -> dict:
    return {key: scene.get(key) for key in ("platform", "tile", "orbit_pass", "relative_orbit",
            "covers_roi", "band_coverage_pct", "roi_cloud_pct", "roi_shadow_pct", "roi_cloud_shadow_pct",
            "roi_snow_pct", "scl_valid_pct", "tile_cloud_pct", "reference_orbit_match")}


def _metadata(scene: dict, parameters: dict) -> dict[str, str]:
    dataset, bands = parameters["dataset"], parameters["bands"]
    processing = _processing(dataset, bands)
    return {"SOURCE_COLLECTION": COLLECTIONS[dataset], "SOURCE_SCENE": scene["asset_id"],
            "ACQUISITION_DATE": scene.get('date') or 'unknown',
            "PLATFORM": (scene.get('platform') or '').replace('Sentinel-2', ''),
            "ORBIT_PASS": scene.get('orbit_pass') or '',
            "RELATIVE_ORBIT": str(scene.get('relative_orbit') or ''),
            "DATASET": dataset, "ACQUISITION_TIME_UTC": scene.get("timestamp") or scene.get("date") or "unknown",
            "BAND_ORDER": _canonical(bands), "UNITS": processing["units"],
            "RESAMPLING": processing["resampling"], "MASK_RESAMPLING": "nearest",
            "GRID_MODE": parameters.get("grid_mode", "match_extent"),
            "REFERENCE_FINGERPRINT": _fingerprint(parameters.get("reference", {})),
            "QUALITY_POLICY": _canonical(QUALITY_POLICY[dataset]),
            "SOURCE_NATIVE_RESOLUTION_M": _canonical(processing["native_resolution_m"]),
            "ESTIMATED_BAND_COVERAGE_PCT": _canonical(scene.get("band_coverage_pct")),
            "SOURCE_QUALITY": _canonical(_scene_quality(scene)),
            "PROCESSING": _canonical(processing)}


def validate_frame_export(path: Path, grid: dict, bands: list[str]) -> dict:
    """Reopen a TIFF and measure actual, finite native-mask coverage, block by block."""
    grid = _grid(grid)
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(path) as source:
        _assert_raster_grid(source, grid, len(bands))
        if list(source.descriptions) != list(bands):
            raise FrameExportError("band_order_mismatch", "Thứ tự kênh không khớp yêu cầu.")
        tags = source.tags()
        if any(not tags.get(name) for name in _REQUIRED_TAGS):
            raise FrameExportError("metadata_missing", "Ảnh thiếu metadata xử lý bắt buộc.")
        try:
            if json.loads(tags["BAND_ORDER"]) != bands:
                raise ValueError()
            validate_asset(tags["SOURCE_SCENE"], tags["DATASET"], bands)
            processing = _processing(tags["DATASET"], bands)
            if (tags["SOURCE_COLLECTION"] != COLLECTIONS[tags["DATASET"]]
                    or tags["UNITS"] != processing["units"]
                    or tags["RESAMPLING"] != processing["resampling"]
                    or tags.get("MASK_RESAMPLING") != "nearest"
                    or json.loads(tags["SOURCE_NATIVE_RESOLUTION_M"]) != processing["native_resolution_m"]
                    or json.loads(tags.get("PROCESSING", "null")) != processing
                    or json.loads(tags["QUALITY_POLICY"]) != QUALITY_POLICY[tags["DATASET"]]):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise FrameExportError("metadata_mismatch", "Metadata xử lý không khớp yêu cầu.") from None
        if 'EXPORT_PROVENANCE' in tags:
            try:
                provenance = json.loads(tags['EXPORT_PROVENANCE'])
                if provenance.get('version') != 1:
                    raise ValueError()
                strategy = provenance.get('export_strategy')
                expected = processing['resampling']
                if strategy == 'direct':
                    if provenance.get('value_resampling_steps') != [expected]:
                        raise ValueError()
                elif strategy == 'standard_grid_local_warp':
                    from .frame_projection import plan_transport_grid
                    transport = plan_transport_grid(grid, tags['DATASET'], bands)['grid']
                    if (provenance.get('projection_fallback_reason') not in (
                                'unsupported_export_projection', 'custom_reference_projection')
                            or provenance.get('value_resampling_steps') != ['nearest', expected]
                            or provenance.get('transport_crs') != transport['crs']
                            or provenance.get('transport_resolution') != transport['transform'][0]):
                        raise ValueError()
                else:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise FrameExportError('metadata_mismatch', 'Metadata chiến lược xuất không hợp lệ.') from None
        if source.nodata is None or not math.isnan(source.nodata):
            raise FrameExportError("nodata_mismatch", "Ảnh phải giữ NoData bằng NaN.")
        counts = [0] * len(bands)
        common_count = 0
        for _, window in source.block_windows(1):
            values = source.read(window=window, masked=True)
            if np.isinf(values.data).any():
                raise FrameExportError("invalid_values", "Ảnh chứa giá trị vô hạn.")
            valid = (~np.ma.getmaskarray(values) & np.isfinite(values.data)
                     & (values.data != _WIRE_NODATA) & (values.data != _LEGACY_WIRE_NODATA))
            if np.any(np.isfinite(values.data) & ((values.data == _WIRE_NODATA) | (values.data == _LEGACY_WIRE_NODATA))):
                raise FrameExportError('invalid_values', 'Ảnh chứa sentinel transport chưa đổi về NaN.')
            for index in range(len(bands)):
                counts[index] += int(valid[index].sum())
            common_count += int(valid.all(axis=0).sum())
        total = grid["width"] * grid["height"]
        coverage = {band: counts[index] * 100.0 / total for index, band in enumerate(bands)}
        transform = Affine(*grid["transform"])
        corners = [list(transform * point) for point in ((0, 0), (grid["width"], 0),
                   (grid["width"], grid["height"]), (0, grid["height"]))]
        bounds = [min(p[0] for p in corners), min(p[1] for p in corners),
                  max(p[0] for p in corners), max(p[1] for p in corners)]
        report = {"verified": True, "output_grid": grid, "bands": list(bands), "dtype": "float32",
                  "actual_coverage_pct": coverage, "band_coverage_pct": coverage,
                  "valid_pixel_count": dict(zip(bands, counts)), "total_pixels": total,
                  "common_valid_pixel_count": common_count,
                  "common_band_coverage_pct": common_count * 100.0 / total,
                  "all_nodata": common_count == 0, "corners": corners, "bounds": bounds,
                  "metadata": tags}
        if common_count == 0:
            report.update(status="failed", error_code="all_nodata", verified=False)
            raise FrameExportError("all_nodata", "Cảnh không có pixel hợp lệ trong khung.", report)
        return report


from .export_resources import bulk_export


@bulk_export
def export_frame_scene(scene: dict, parameters: dict, destination: Path) -> dict:
    """Write locally to a UUID staging TIFF, verify, then publish atomically."""
    if scene.get("sources"):
        return export_frame_mosaic(scene, parameters, destination)
    dataset, bands = parameters["dataset"], list(parameters["bands"])
    validate_asset(scene["asset_id"], dataset, bands)
    grid = _grid(parameters["output_grid"])
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from .frame_projection import estimate_export_resources, export_via_transport_grid
    reserve = estimate_export_resources(grid, dataset, bands)['disk_reserve_bytes']
    if shutil.disk_usage(destination.parent).free < reserve:
        raise FrameExportError("disk_full", "Không đủ dung lượng dự phòng để xuất cảnh.")
    staging = destination.parent / (uuid4().hex[:16] + ".part.tif")
    tags = _metadata(scene, parameters)
    provenance = None
    try:
        with _RPC(180) as rpc:
            image = load_frame_image(scene["asset_id"], dataset, bands)
            with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(
                    staging, "w", driver="GTiff", width=grid["width"], height=grid["height"],
                    count=len(bands), dtype="float32", crs=grid["crs"], transform=Affine(*grid["transform"]),
                    nodata=float("nan"), compress="deflate", predictor=3, tiled=True,
                    blockxsize=256, blockysize=256, BIGTIFF="IF_SAFER") as output:
                output.descriptions = tuple(bands)
                output.update_tags(**tags)
                provenance = {'version': 1, 'export_strategy': 'direct',
                              'value_resampling_steps': [_processing(dataset, bands)['resampling']]}
                try:
                    if CRS.from_user_input(grid['crs']).to_epsg() is None:
                        raise FrameUpstreamError('custom_reference_projection', 'CRS mẫu cần warp cục bộ.')
                    for window in iter_grid_tiles(grid, len(bands)):
                        values = fetch_grid_tile(image, dataset, bands, grid, window, rpc=rpc)
                        expected = (len(bands), int(window.height), int(window.width))
                        if values.shape != expected:
                            raise FrameExportError("tile_shape_mismatch", "Tile không đúng kích thước yêu cầu.")
                        masked = np.ma.asarray(values, dtype=np.float32)
                        output.write(masked.filled(np.nan), window=window)
                except FrameUpstreamError as exc:
                    if exc.code not in ('unsupported_export_projection', 'custom_reference_projection'):
                        raise
                    provenance = export_via_transport_grid(image, dataset, bands, grid, output, rpc=rpc)
                    provenance['projection_fallback_reason'] = exc.code
                output.update_tags(EXPORT_PROVENANCE=_canonical(provenance))
        report = validate_frame_export(staging, grid, bands)
        report.update(scene_token=scene.get("scene_token"), asset_id=scene["asset_id"],
                      timestamp=scene.get("timestamp"), date=scene.get("date"), platform=scene.get("platform"),
                      estimated_band_coverage_pct=scene.get("band_coverage_pct"),
                      processing=_processing(dataset, bands), export_provenance=provenance,
                      warnings=list(scene.get("warnings", [])))
        _check_coverage(report, parameters)
        report["status"] = "completed"
        report["filename"] = destination.name
        os.replace(staging, destination)
        return report
    except FrameExportError as exc:
        exc.report.update(scene_token=scene.get("scene_token"), asset_id=scene["asset_id"],
                          timestamp=scene.get("timestamp"), date=scene.get("date"), platform=scene.get("platform"),
                          estimated_band_coverage_pct=scene.get("band_coverage_pct"),
                          processing=_processing(dataset, bands), quality=_scene_quality(scene),
                          warnings=list(scene.get("warnings", [])))
        if provenance is not None:
            exc.report['export_provenance'] = provenance
        raise
    except OSError as exc:
        import errno
        if exc.errno in (errno.ENOSPC, errno.EDQUOT) or getattr(exc, "winerror", None) == 112:
            raise FrameExportError("disk_full", "Đĩa đã hết dung lượng khi xuất ảnh.") from None
        raise FrameExportError("export_io_failed", "Không thể ghi ảnh xuất.") from None
    finally:
        for suffix in ('', '.aux.xml', '.msk'):
            Path(str(staging) + suffix).unlink(missing_ok=True)


def _check_coverage(report: dict, parameters: dict) -> None:
    from .frame_models import effective_min_coverage
    minimum = effective_min_coverage(parameters)
    common = report.get('common_band_coverage_pct', min(report['actual_coverage_pct'].values()))
    if (isinstance(common, bool) or not isinstance(common, (int, float))
            or not math.isfinite(common) or not 0 < common <= 100 or common < minimum):
        report.update(status='failed', error_code='insufficient_coverage', verified=False)
        raise FrameExportError('insufficient_coverage',
                               f'Độ phủ thực tế dưới ngưỡng {minimum:g}% đã chọn.', report)


@bulk_export
def export_frame_mosaic(scene: dict, parameters: dict, destination: Path) -> dict:
    """Fill only common-band NoData on the final grid; preserve primary pixels."""
    sources = scene['sources']
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    grid, bands = _grid(parameters['output_grid']), parameters['bands']
    partial = destination.parent / (uuid4().hex + '.mosaic.part.tif')
    source_paths = []
    reports = []
    source_parameters = {**parameters, 'merge_scenes': False, 'require_full_coverage': False,
                         'min_coverage_pct': 0}
    try:
        with rasterio.Env(GDAL_PAM_ENABLED='NO'), rasterio.open(
                partial, 'w', driver='GTiff', width=grid['width'], height=grid['height'],
                count=len(bands), dtype='float32', crs=grid['crs'], transform=Affine(*grid['transform']),
                nodata=np.nan, compress='deflate', predictor=3, tiled=True,
                blockxsize=256, blockysize=256, BIGTIFF='IF_SAFER') as output:
            output.descriptions = tuple(bands)
            output.update_tags(**_metadata(sources[0], parameters))
            for source in sources:
                path = destination.parent / (uuid4().hex + '.source.part.tif')
                source_paths.append(path)
                try:
                    reports.append(export_frame_scene(source, source_parameters, path))
                except FrameExportError as exc:
                    reports.append(exc.report)
                    raise
            for window in iter_grid_tiles(grid, len(bands)):
                data = np.full((len(bands), int(window.height), int(window.width)), np.nan, dtype=np.float32)
                occupied = np.zeros(data.shape[1:], dtype=bool)
                for path in source_paths:
                    with rasterio.open(path) as source:
                        values = source.read(window=window, masked=True)
                    valid = (~np.ma.getmaskarray(values) & np.isfinite(values.data)).all(axis=0)
                    fill = ~occupied & valid
                    data[:, fill] = values.data[:, fill]
                    occupied |= valid
                output.write(data, window=window)
            output.update_tags(MOSAIC_POLICY='primary_first_fill_nodata',
                               SOURCE_SCENES=_canonical([source['asset_id'] for source in sources]),
                               SOURCE_DATES=_canonical([source.get('date') for source in sources]),
                               PRIMARY_SOURCE_SCENE=sources[0]['asset_id'],
                               SOURCE_DETAILS=_canonical(_mosaic_provenance(sources, reports)['sources']),
                               SOURCE_TIME_RANGE_UTC=_canonical(_mosaic_provenance(sources, reports)['source_time_range_utc']))
        report = validate_frame_export(partial, grid, bands)
        report.update(scene_token=scene.get('scene_token'), asset_id=scene['asset_id'],
                      timestamp=scene.get('timestamp'), date=scene.get('date'),
                      processing=_processing(parameters['dataset'], bands),
                      **_mosaic_provenance(sources, reports))
        _check_coverage(report, parameters)
        report.update(status='completed', filename=destination.name)
        os.replace(partial, destination)
        return report
    except FrameExportError as exc:
        exc.report.update(scene_token=scene.get('scene_token'), asset_id=scene['asset_id'],
                          timestamp=scene.get('timestamp'), date=scene.get('date'),
                          processing=_processing(parameters['dataset'], bands),
                          **_mosaic_provenance(sources, reports))
        raise
    finally:
        for path in [partial, *source_paths]:
            for suffix in ('', '.aux.xml', '.msk'):
                Path(str(path) + suffix).unlink(missing_ok=True)


def _mosaic_provenance(sources, reports):
    timestamps = sorted(source['timestamp'] for source in sources if source.get('timestamp'))
    details = []
    for index, source in enumerate(sources):
        report = reports[index] if index < len(reports) else {}
        details.append({**{key: source.get(key) for key in (
            'asset_id', 'scene_token', 'date', 'timestamp', 'platform', 'orbit_pass', 'relative_orbit', 'tile')},
            'priority': index + 1, 'actual_coverage_pct': report.get('actual_coverage_pct'),
            'export_provenance': report.get('export_provenance'),
            'status': report.get('status', 'not_exported')})
    return {'mosaic_policy': 'primary_first_fill_nodata', 'primary_asset_id': sources[0]['asset_id'],
            'source_time_range_utc': [timestamps[0], timestamps[-1]] if timestamps else [],
            'sources': details}


def build_frame_manifest(parameters: dict, reports: list[dict]) -> dict:
    """Build a JSON-finite metadata-only manifest without URLs or local paths."""
    dataset, bands = parameters["dataset"], list(parameters["bands"])
    reference = _safe_reference(parameters.get("reference", {}))
    safe_reports = []
    report_keys = ("scene_token", "asset_id", "timestamp", "date", "platform", "status", "error_code", "verified",
                   "estimated_band_coverage_pct", "actual_coverage_pct", "valid_pixel_count", "total_pixels",
                   "common_valid_pixel_count", "common_band_coverage_pct",
                   "all_nodata", "bands", "dtype", "corners", "bounds", "processing", "quality", "warnings",
                   "export_provenance", "mosaic_policy", "sources", "primary_asset_id", "source_time_range_utc")
    for report in reports:
        item = {key: report[key] for key in report_keys if key in report}
        if report.get("filename"):
            item["filename"] = Path(report["filename"]).name
        safe_reports.append(item)
    manifest = {"version": 1, "dataset": dataset, "source_collection": COLLECTIONS[dataset],
                "selection_context": parameters.get('selection_context', 'reference'),
                "platform": parameters.get('platform'),
                "roi_version": parameters.get('roi_version'),
                "roi_fingerprint": parameters.get('roi_fingerprint'),
                "reference": reference, "reference_fingerprint": _fingerprint(reference),
                "output_grid": _grid(parameters["output_grid"]),
                "grid_mode": parameters.get("grid_mode", "match_extent"), "bands": bands,
                "require_full_coverage": parameters.get("require_full_coverage", True),
                "min_coverage_pct": parameters.get("min_coverage_pct", 0),
                "merge_scenes": parameters.get("merge_scenes", False),
                "processing": _processing(dataset, bands), "quality_policy": QUALITY_POLICY[dataset],
                "quality_parameters": {key: parameters[key] for key in (
                    "min_cloud_shadow_pct", "max_cloud_shadow_pct", "sort", "reference_date", "orbit_pass", "relative_orbit",
                    "platform", "prefer_reference_orbit", "reference_orbit_pass", "reference_relative_orbit")
                    if key in parameters}, "reports": safe_reports,
                "warnings": ["grid_alignment_is_not_coregistration", "coverage_search_is_estimated",
                             "preview_stretch_does_not_modify_export"]}
    # Normalize dates and prevent nonfinite values from escaping persistence.
    def clean(value):
        from datetime import date, datetime
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value]
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return json.loads(_canonical(clean(manifest)))
