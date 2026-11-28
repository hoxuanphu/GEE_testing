"""Raw matched-frame scene search; no legacy processing or global EE timeouts."""
from __future__ import annotations

import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import ee
import requests
from google.auth.transport.requests import AuthorizedSession, Request

from .ee_session import init_ee
from .frame_models import FrameError, effective_min_coverage, known_positive_coverage

if TYPE_CHECKING:
    from .frame_models import FrameSearchRequest

COLLECTIONS = {"sentinel2": "COPERNICUS/S2_SR_HARMONIZED", "s1gee": "COPERNICUS/S1_GRD"}
_SEARCH_LOCK = threading.Lock()
S2_NATIVE_RESOLUTION = {"B1": 60, "B2": 10, "B3": 10, "B4": 10, "B5": 20,
                        "B6": 20, "B7": 20, "B8": 10, "B8A": 20, "B9": 60,
                        "B11": 20, "B12": 20}
QUALITY_POLICY = {
    "sentinel2": {"scale_m": 20, "estimated": True, "maxPixels": 10000000,
                  "bestEffort": False, "cloud_classes": [8, 9, 10],
                  "shadow_classes": [3], "snow_classes": [11],
                  "quality_denominator": "valid SCL area excluding classes 0/1 and native mask",
                  "coverage_denominator": "entire reference footprint area"},
    "s1gee": {"scale_m": 100, "estimated": True, "maxPixels": 10000000,
              "bestEffort": False, "coverage_denominator": "entire reference footprint area"},
}


class FrameSearchError(FrameError):
    """Safe public error, with a stable machine-readable code."""

    def __init__(self, code: str, message: str):
        super().__init__(code, message, status=409 if code == "busy" else 422)


class FrameUpstreamError(FrameSearchError):
    def __init__(self, code: str, message: str, *, http_status=None, reason=None):
        super().__init__(code, message)
        self.status = 502
        self.http_status = http_status
        self.reason = reason or code


class _TransientError(FrameUpstreamError):
    def __init__(self, http_status=None):
        code = ('upstream_quota' if http_status == 429 else 'timeout' if http_status == 408
                else 'upstream_unavailable' if http_status and http_status >= 500 else 'upstream_failed')
        messages = {'upstream_quota': 'GEE vượt quota; hãy thử lại sau.',
                    'timeout': 'GEE hết thời gian phản hồi; hãy thu hẹp yêu cầu.',
                    'upstream_unavailable': 'Dịch vụ GEE tạm thời không khả dụng.'}
        super().__init__(code, messages.get(code, 'Không nhận được phản hồi GEE hợp lệ.'),
                         http_status=http_status)


class _DeadlineError(FrameUpstreamError):
    def __init__(self):
        super().__init__("timeout", "Đã hết thời gian xử lý GEE; hãy thu hẹp yêu cầu.")


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _DeadlineError()
    return remaining


def _retryable(exc: Exception) -> bool:
    return isinstance(exc, (_TransientError, requests.Timeout, requests.ConnectionError,
                            requests.exceptions.ChunkedEncodingError, TimeoutError, ConnectionError))


def _retry(operation, deadline: float):
    for attempt in range(3):
        _remaining(deadline)
        try:
            result = operation()
            _remaining(deadline)  # Discard late results, without pretending to cancel a worker.
            return result
        except _DeadlineError:
            raise
        except Exception as exc:
            if not _retryable(exc) or attempt == 2:
                if isinstance(exc, FrameSearchError):
                    raise
                raise FrameUpstreamError("upstream_failed", "GEE không thể xử lý yêu cầu này.") from None
            delay = attempt + 1
            if _remaining(deadline) <= delay:
                raise _DeadlineError() from None
            time.sleep(delay)


class _BoundedAuthRequest(Request):
    def __init__(self, session, timeout):
        super().__init__(session=session)
        self.timeout = timeout

    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = min(kwargs.get("timeout") or self.timeout, self.timeout)
        return super().__call__(*args, **kwargs)


class _RPC:
    """Dedicated authenticated REST transport using the installed EE serializer.

    EE 1.7 keeps its cloud resource in process-wide state. Reading credentials
    and serializing graphs is safe; replacing that resource/deadline is not.
    This session therefore never calls EE getInfo/getDownloadURL or setDeadline.
    """

    def __init__(self, timeout: float, deadline: float | None = None):
        init_ee()
        state = ee.data._get_state()
        self.timeout = timeout
        self.deadline = deadline
        self.base = state.cloud_api_base_url.rstrip("/")
        self.project = "projects/" + state.cloud_api_user_project
        self.auth_session = requests.Session()
        self.session = AuthorizedSession(
            state.credentials, max_refresh_attempts=0, refresh_timeout=timeout,
            auth_request=_BoundedAuthRequest(self.auth_session, timeout))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.session.close()
        self.auth_session.close()

    def request_once(self, method: str, endpoint: str, *, body=None,
                     deadline: float | None = None, max_bytes: int = 32 * 1024 * 1024) -> bytes:
        end = min(deadline or float("inf"), self.deadline or float("inf"),
                  time.monotonic() + self.timeout)
        url = self.base + "/v1/" + endpoint
        try:
            with self.session.request(method, url, json=body, stream=True,
                                      timeout=_remaining(end), max_allowed_time=_remaining(end)) as response:
                if response.status_code in (408, 429) or response.status_code >= 500:
                    raise _TransientError(response.status_code)
                if response.status_code >= 400:
                    import json
                    raw = bytearray()
                    for chunk in response.iter_content(1024):
                        _remaining(end)
                        raw.extend(chunk[:4097 - len(raw)])
                        if len(raw) > 4096:
                            break
                    projection = False
                    if len(raw) <= 4096:
                        try:
                            error = json.loads(raw).get("error", {})
                            projection = (error.get("status") == "INVALID_ARGUMENT" and
                                          "Unable to write GeoTIFFs in projection" in error.get("message", ""))
                        except (ValueError, TypeError, AttributeError):
                            pass
                    code = {400: "unsupported_export_projection" if projection else "invalid_upstream_request",
                            401: "upstream_unauthenticated", 403: "upstream_forbidden"}.get(
                                response.status_code, "upstream_rejected")
                    messages = {"unsupported_export_projection": "GEE không hỗ trợ ghi CRS đích; cần warp cục bộ.",
                                "invalid_upstream_request": "GEE không chấp nhận tham số yêu cầu.",
                                "upstream_unauthenticated": "Phiên xác thực GEE không hợp lệ.",
                                "upstream_forbidden": "GEE từ chối quyền truy cập tài nguyên."}
                    raise FrameUpstreamError(code, messages.get(code, "GEE từ chối yêu cầu."),
                                             http_status=response.status_code, reason=code)
                result = bytearray()
                for chunk in response.iter_content(64 * 1024):
                    _remaining(end)
                    result.extend(chunk)
                    if len(result) > max_bytes:
                        raise FrameUpstreamError("response_too_large", "Phản hồi GEE vượt giới hạn.")
                _remaining(end)
                if not result:
                    raise _TransientError()
                return bytes(result)
        except requests.exceptions.RequestException as exc:
            if _retryable(exc):
                raise _TransientError() from None
            raise FrameUpstreamError("upstream_failed", "Không thể kết nối GEE.") from None

    def compute(self, expression):
        import json
        deadline = self.deadline or time.monotonic() + self.timeout * 3 + 3
        body = {"expression": ee.serializer.encode(expression, for_cloud_api=True)}

        def operation():
            content = self.request_once("POST", self.project + "/value:compute", body=body)
            try:
                return json.loads(content)["result"]
            except (ValueError, KeyError, TypeError):
                raise _TransientError() from None

        return _retry(operation, deadline)

    def thumbnail_once(self, image, params: dict, *, deadline: float, preview=False) -> bytes:
        import json
        if preview:
            graph, rest = image._apply_spatial_transformations(params)
            graph, rest = graph._apply_visualization(rest)
        else:
            graph = image._build_download_id_image(params)
        body = {"expression": ee.serializer.encode(graph, for_cloud_api=True),
                "fileFormat": "PNG" if preview else "GEO_TIFF"}
        raw = self.request_once("POST", self.project + "/thumbnails", body=body, deadline=deadline)
        try:
            name = json.loads(raw)["name"]
            if not re.fullmatch(r"projects/[^/]+/thumbnails/[A-Za-z0-9_-]+", name):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise _TransientError() from None
        # A refreshed thumbnail is created on each attempt. No signed URL escapes.
        return self.request_once("GET", name + ":getPixels", deadline=deadline,
                                 max_bytes=8 * 1024 * 1024 if preview else 32 * 1024 * 1024)


def _value(request, name, default=None):
    result = request.get(name, default) if isinstance(request, dict) else getattr(request, name, default)
    return getattr(result, "value", result)


def _bands(request) -> list[str]:
    from .frame_models import bands_for
    return list(bands_for(request))


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(value)


def _frame_roi(frame: dict, parameters: dict | None = None):
    from .reference_frame import reference_roi_identity, validate_roi_identity
    # GDAL and EE interpret latitude_of_origin differently for custom EQC WKT.
    # Use the same densified WGS84 polygon as the map, never reinterpret its WKT in EE.
    if parameters is not None:
        validate_roi_identity(parameters)
        footprint = parameters["roi_geojson"]
    else:
        footprint = reference_roi_identity(frame)["roi_geojson"]
    return ee.Geometry.Polygon(footprint["coordinates"], proj="EPSG:4326", geodesic=False)


def validate_asset(asset_id: str, dataset: str, bands: list[str]) -> None:
    prefix = COLLECTIONS.get(dataset)
    if not prefix or not isinstance(asset_id, str) or not re.fullmatch(
            re.escape(prefix) + r"/[A-Za-z0-9_-]+", asset_id):
        raise FrameSearchError("invalid_asset", "Cảnh không thuộc bộ sưu tập được phép.")
    allowed = set(S2_NATIVE_RESOLUTION) if dataset == "sentinel2" else {"VV", "VH", "HH", "HV"}
    if not bands or len(set(bands)) != len(bands) or not set(bands) <= allowed:
        raise FrameSearchError("invalid_bands", "Danh sách kênh không hợp lệ.")
    if dataset == "s1gee" and bands not in (["VV", "VH"], ["VV"], ["HH", "HV"], ["HH"]):
        raise FrameSearchError("invalid_bands", "Cặp phân cực không hợp lệ.")


def _area_sums(layers: list, names: list[str], roi, projection, scale: int):
    image = ee.Image.cat([layer.rename(name) for layer, name in zip(layers, names)])
    return image.multiply(ee.Image.pixelArea()).reduceRegion(
        reducer=ee.Reducer.sum(), geometry=roi, crs=projection, scale=scale,
        maxPixels=10000000, bestEffort=False)


def _ratio(numerator, denominator):
    denominator = ee.Number(ee.Algorithms.If(denominator, denominator, 0))
    numerator = ee.Number(ee.Algorithms.If(numerator, numerator, 0))
    return ee.Algorithms.If(denominator.gt(0), numerator.divide(denominator).multiply(100), None)


def s2_roi_metrics(image, roi, bands: list[str]):
    """Area-weighted SCL quality; native mask and classes 0/1 are excluded."""
    scl = image.select("SCL")
    valid = scl.mask().gt(0).And(scl.neq(0)).And(scl.neq(1)).unmask(0, sameFootprint=False)
    cloud = scl.eq(8).Or(scl.eq(9)).Or(scl.eq(10)).And(valid).unmask(0, sameFootprint=False)
    shadow = scl.eq(3).And(valid).unmask(0, sameFootprint=False)
    snow = scl.eq(11).And(valid).unmask(0, sameFootprint=False)
    projection = scl.projection().atScale(20)
    band_masks = [image.select([band]).mask().gt(0).unmask(0, sameFootprint=False).reproject(projection)
                  for band in bands]
    coverage = ee.Image.cat(band_masks).reduce(ee.Reducer.min())
    names = ["total", "valid", "cloud", "shadow", "cloud_shadow", "snow", "coverage"]
    sums = _area_sums([ee.Image.constant(1), valid, cloud, shadow, cloud.Or(shadow), snow, coverage],
                      names, roi, scl.projection(), 20)
    total, valid_area = sums.get("total"), sums.get("valid")
    return ee.Dictionary({
        "roi_cloud_pct": _ratio(sums.get("cloud"), valid_area),
        "roi_shadow_pct": _ratio(sums.get("shadow"), valid_area),
        "roi_cloud_shadow_pct": _ratio(sums.get("cloud_shadow"), valid_area),
        "roi_snow_pct": _ratio(sums.get("snow"), valid_area),
        "scl_valid_pct": _ratio(valid_area, total),
        "band_coverage_pct": _ratio(sums.get("coverage"), total),
    })


def s1_roi_metrics(image, roi, bands: list[str]):
    coverage = image.select(bands).mask().reduce(ee.Reducer.min()).gt(0).unmask(0, sameFootprint=False)
    sums = _area_sums([ee.Image.constant(1), coverage], ["total", "coverage"], roi,
                      image.select(bands[0]).projection(), 100)
    return ee.Dictionary({"band_coverage_pct": _ratio(sums.get("coverage"), sums.get("total")),
                          **{name: None for name in ("roi_cloud_pct", "roi_shadow_pct", "roi_cloud_shadow_pct",
                                                    "roi_snow_pct", "scl_valid_pct")}})


def _orbit_match(scene, request) -> bool:
    reference_pass = _value(request, "reference_orbit_pass")
    reference_orbit = _value(request, "reference_relative_orbit")
    return bool(_value(request, "prefer_reference_orbit", False) and reference_pass and reference_orbit
                and scene.get("orbit_pass") == reference_pass
                and scene.get("relative_orbit") == reference_orbit)


def rank_frame_scenes(scenes: list[dict], request) -> list[dict]:
    reference = _value(request, "reference_date")
    reference = _as_date(reference) if reference else None
    sort = _value(request, "sort", "least_cloud")
    if sort == "nearest_date" and reference is None:
        raise FrameSearchError("reference_date_required", "Cần ngày tham chiếu để xếp theo ngày gần nhất.")

    def key(scene):
        distance = abs((_as_date(scene["date"]) - reference).days) if reference else 0
        timestamp = scene["timestamp"]
        tail = (timestamp, scene["asset_id"])
        if _value(request, "dataset") == "s1gee":
            recency = distance if reference else -_as_date(scene['date']).toordinal()
            return (not _orbit_match(scene, request), recency, *tail)
        cloud = scene.get("roi_cloud_shadow_pct")
        coverage = scene.get("band_coverage_pct")
        quality = (cloud if cloud is not None else float("inf"),
                   -coverage if coverage is not None else float("inf"))
        return (distance, *quality, *tail) if sort == "nearest_date" else (*quality, distance, *tail)

    return sorted(scenes, key=key)


def _scene_row(raw: dict, request) -> dict:
    stamp = datetime.fromtimestamp(raw["time"] / 1000, timezone.utc)
    optical = _value(request, "dataset") == "sentinel2"
    metrics = {name: raw.get(name) for name in ("band_coverage_pct", "roi_cloud_pct", "roi_shadow_pct",
               "roi_cloud_shadow_pct", "roi_snow_pct", "scl_valid_pct")}
    for name, value in metrics.items():
        metrics[name] = (float(value) if not isinstance(value, bool) and isinstance(value, (int, float))
                         and math.isfinite(value) and 0 <= value <= 100 else None)
    coverage = metrics["band_coverage_pct"]
    merging = _value(request, "merge_scenes", False)
    minimum = effective_min_coverage(request)
    eligible = known_positive_coverage(coverage) and (merging or coverage >= minimum)
    if not merging and _value(request, "require_full_coverage", True):
        eligible = eligible and raw.get("covers_roi") is True
    min_cloud = _value(request, "min_cloud_shadow_pct")
    max_cloud = _value(request, "max_cloud_shadow_pct")
    if optical and (min_cloud is not None or max_cloud is not None):
        cloud = metrics["roi_cloud_shadow_pct"]
        eligible = eligible and cloud is not None and (
            min_cloud is None or cloud >= min_cloud) and (max_cloud is None or cloud <= max_cloud)
    warnings = ["coverage_estimated_20m" if optical else "coverage_estimated_100m",
                "grid_alignment_is_not_coregistration"]
    if coverage is None or coverage < 99.9 or not raw.get("covers_roi"):
        warnings.append("incomplete_or_unknown_coverage")
    if optical:
        warnings += ["scl_does_not_guarantee_haze_free", "native_resolution_10_20_60m"]
    row = {"scene_token": str(uuid4()), "asset_id": raw["asset_id"],
           "timestamp": stamp.isoformat().replace("+00:00", "Z"), "date": stamp.date().isoformat(),
           "platform": raw.get("platform"), "tile": raw.get("tile"), "orbit_pass": raw.get("orbit_pass"),
           "relative_orbit": raw.get("relative_orbit"), "covers_roi": raw.get("covers_roi") is True,
           "tile_cloud_pct": raw.get("tile_cloud_pct") if optical else None,
           "footprint": raw.get("footprint"),
           **metrics, "eligible": bool(eligible), "warnings": warnings,
           "quality_context": QUALITY_POLICY["sentinel2" if optical else "s1gee"],
           "reference_orbit_match": _orbit_match(raw, request) if not optical else False}
    if not optical and _orbit_match(row, request):
        warnings.append("reference_orbit_match")
    return row


def search_frame_scenes(request: FrameSearchRequest, frame: dict) -> list[dict]:
    """Search one immutable collection and rank exact assets, within a 300 s budget."""
    if not _SEARCH_LOCK.acquire(blocking=False):
        raise FrameSearchError("busy", "Một yêu cầu tìm cảnh khác đang chạy.")
    try:
        return _search(request, frame)
    finally:
        _SEARCH_LOCK.release()


def _search(request, frame: dict) -> list[dict]:
    deadline = time.monotonic() + 300
    dataset = _value(request, "dataset")
    if dataset not in COLLECTIONS:
        raise FrameSearchError("invalid_dataset", "Nguồn dữ liệu không hợp lệ.")
    start, end = _as_date(_value(request, "start_date")), _as_date(_value(request, "end_date"))
    if end < start or (end - start).days >= 366:
        raise FrameSearchError("invalid_dates", "Khoảng tìm kiếm phải từ 1 đến 366 ngày.")
    rank_frame_scenes([], request)
    bands = _bands(request)
    with _RPC(120, deadline) as rpc:
        roi = _frame_roi(frame)
        collection = ee.ImageCollection(COLLECTIONS[dataset]).filterBounds(roi).filterDate(
            start.isoformat(), (end + timedelta(days=1)).isoformat())
        from .platforms import apply_platform_filter
        collection = apply_platform_filter(collection, dataset, _value(request, 'platform'))
        if dataset == "s1gee":
            collection = collection.filter(ee.Filter.eq("instrumentMode", "IW"))
            for band in bands:
                collection = collection.filter(ee.Filter.listContains("transmitterReceiverPolarisation", band))
        for field, prop in (("orbit_pass", "SENSING_ORBIT_DIRECTION" if dataset == "sentinel2" else "orbitProperties_pass"),
                            ("relative_orbit", "SENSING_ORBIT_NUMBER" if dataset == "sentinel2" else "relativeOrbitNumber_start")):
            value = _value(request, field)
            if value is not None:
                collection = collection.filter(ee.Filter.eq(prop, value))
        ids = rpc.compute(collection.limit(501).aggregate_array("system:id"))
        if len(ids) > 500:
            raise FrameSearchError("too_many_scenes", "Có hơn 500 cảnh; hãy thu hẹp khoảng ngày hoặc bộ lọc.")
        if not ids:
            return []

        def describe(asset_id):
            from .imagery import scene_geometry
            image = ee.Image(asset_id)
            optical = dataset == "sentinel2"
            props = image.toDictionary(["system:time_start", "SPACECRAFT_NAME", "MGRS_TILE",
                "SENSING_ORBIT_DIRECTION", "SENSING_ORBIT_NUMBER", "CLOUDY_PIXEL_PERCENTAGE"] if optical else
                ["system:time_start", "platform_number", "orbitProperties_pass", "relativeOrbitNumber_start"])
            metrics = s2_roi_metrics(image, roi, bands) if optical else s1_roi_metrics(image, roi, bands)
            return ee.Dictionary({"asset_id": asset_id, "time": props.get("system:time_start"),
                "platform": props.get("SPACECRAFT_NAME" if optical else "platform_number"),
                "tile": props.get("MGRS_TILE") if optical else None,
                "orbit_pass": props.get("SENSING_ORBIT_DIRECTION" if optical else "orbitProperties_pass"),
                "relative_orbit": props.get("SENSING_ORBIT_NUMBER" if optical else "relativeOrbitNumber_start"),
                "tile_cloud_pct": props.get("CLOUDY_PIXEL_PERCENTAGE") if optical else None,
                "footprint": scene_geometry(image),
                "covers_roi": image.geometry().contains(roi, maxError=1)}).combine(metrics)

        raw = rpc.compute(ee.List([describe(asset_id) for asset_id in ids]))
        _remaining(deadline)
        return rank_frame_scenes([_scene_row(row, request) for row in raw], request)


def make_frame_preview(scene: dict, parameters: dict, *, dimensions=512) -> bytes:
    """Render a separate, display-only graph; return PNG bytes, never signed URLs."""
    from .reference_frame import validate_roi_identity
    validate_roi_identity(parameters)
    dataset = parameters["dataset"]
    bands = ["B4", "B3", "B2"] if dataset == "sentinel2" else [
        "VV" if "VV" in parameters["bands"] else "HH"]
    validate_asset(scene["asset_id"], dataset, bands)
    budget, attempt = (18, 10) if dimensions == 192 else (180, 120)
    deadline = time.monotonic() + budget
    with _RPC(attempt, deadline) as rpc:
        image = ee.Image(scene["asset_id"]).select(bands).toFloat()
        if dataset == "sentinel2":
            image = image.multiply(0.0001)
        params = {"region": _frame_roi(parameters["reference"], parameters), "dimensions": dimensions,
                  "format": "png", "bands": bands,
                  "min": 0 if dataset == "sentinel2" else -25,
                  "max": 0.3 if dataset == "sentinel2" else 0}

        def operation():
            data = rpc.thumbnail_once(image, params, deadline=min(deadline, time.monotonic() + attempt), preview=True)
            if not data.startswith(b"\x89PNG\r\n\x1a\n"):
                raise _TransientError()
            return data

        return _retry(operation, deadline)


def _frame_selection_grid(parameters: dict) -> dict:
    from rasterio.crs import CRS
    from .frame_export import _grid
    from .frame_projection import plan_transport_grid

    grid = _grid(parameters['output_grid'])
    # Use the very same standard transport grid as mask preflight for custom CRS.
    if CRS.from_user_input(grid['crs']).to_epsg() is None:
        return plan_transport_grid(grid, parameters['dataset'], parameters['bands'])['grid']
    return grid


def _frame_selection_image(scenes: list[dict], parameters: dict):
    """Build the display graph after scoped RPC initialization, without fetching it.

    The 512-pixel display simulates source selection; its resampled edges are not
    a pixel-level validation of the final TIFF's locally warped mask.
    """
    from .frame_validity import _mask_graph

    grid = _frame_selection_grid(parameters)
    dataset, bands = parameters['dataset'], parameters['bands']
    display = ['B4', 'B3', 'B2'] if dataset == 'sentinel2' else [
        'VV' if 'VV' in bands else 'HH']
    combined = None
    for scene in scenes:
        image = ee.Image(scene['asset_id'])
        # Reproject each mask with nearest sampling before intersecting ALL export
        # bands, including the bands that this display does not visualize.
        common = _mask_graph(image, bands, grid).reduce(ee.Reducer.min()).gt(0)
        values = image.select(display).toFloat().reproject(
            crs=grid['crs'], crsTransform=grid['transform'])
        if dataset == 'sentinel2':
            values = values.multiply(0.0001)
        values = values.updateMask(common)
        # A shared per-source mask keeps all display bands from the same source.
        # EE mosaic would give the opposite priority unless its list was reversed.
        combined = values if combined is None else combined.unmask(values, sameFootprint=False)
    return combined


def make_frame_selection_preview(scenes: list[dict], parameters: dict) -> bytes:
    """Render the immutable ordered selection using one bounded PNG request scope."""
    from .reference_frame import validate_roi_identity

    deadline = time.monotonic() + 120
    validate_roi_identity(parameters)
    if not 1 <= len(scenes) <= 10:
        raise FrameSearchError('invalid_selection', 'Choose between 1 and 10 sources for preview.')
    dataset, bands = parameters['dataset'], parameters['bands']
    for scene in scenes:
        validate_asset(scene['asset_id'], dataset, bands)
    grid = _frame_selection_grid(parameters)
    _remaining(deadline)
    with _RPC(120, deadline) as rpc:
        image = _frame_selection_image(scenes, parameters)
        params = {'region': _frame_roi(parameters['reference'], parameters),
                  'crs': grid['crs'], 'dimensions': 512, 'format': 'png',
                  'bands': ['B4', 'B3', 'B2'] if dataset == 'sentinel2' else [
                      'VV' if 'VV' in bands else 'HH'],
                  'min': 0 if dataset == 'sentinel2' else -25,
                  'max': 0.3 if dataset == 'sentinel2' else 0}

        def operation():
            data = rpc.thumbnail_once(image, params, deadline=deadline, preview=True)
            if not data.startswith(b'\x89PNG\r\n\x1a\n'):
                raise _TransientError()
            return data

        return _retry(operation, deadline)
