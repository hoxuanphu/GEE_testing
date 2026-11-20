"""Bounded, isolated GEE transport for resumable annual time-series batches.

Metadata never constructs processed imagery. Downloads use one connection at a
time, preserve each band's mask and leave final grid alignment/vector clipping
to the batch worker.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import ee
import numpy as np
import rasterio
import requests
from affine import Affine
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from rasterio.crs import CRS
from rasterio.io import MemoryFile

from . import imagery
from .frame_export import FrameExportError, _grid, _tile_grid, iter_grid_tiles
from .frame_search import FrameSearchError, FrameUpstreamError, _RPC as _FrameRPC, _TransientError
from .platforms import apply_platform_filter

MAX_SCENES = 20000
WINDOW_SECONDS = 120
TILE_SECONDS = 180
SCENE_SECONDS = 900
TILE_BYTES = 16 * 1024 * 1024
MAX_TILES = 20000
WIRE_NODATA = -999999.25  # Exactly representable Float32; zero remains valid.
_COLLECTIONS = {
    "sentinel2": [imagery.S2_COLLECTION], "landsat": [imagery.L8_COLLECTION, imagery.L9_COLLECTION],
    "s1gee": [imagery.S1_COLLECTION], "nicfi": [imagery.NICFI_COLLECTION],
    "dynamic_world": [imagery.DYNAMIC_WORLD_COLLECTION],
}
_SOURCE_PROPERTIES = ["SPACECRAFT_NAME", "SPACECRAFT_ID", "LANDSAT_PRODUCT_ID", "MGRS_TILE",
                      "WRS_PATH", "WRS_ROW", "cadence", "orbitProperties_pass",
                      "relativeOrbitNumber_start", "orbitNumber_start", "platform_number",
                      "instrumentMode", "transmitterReceiverPolarisation"]


class BatchTransportError(Exception):
    """Safe fields suitable for persistence and API responses."""

    def __init__(self, code: str, message: str, fatal: bool = False):
        super().__init__(message)
        self.code, self.message, self.fatal = code, message, fatal


class _RPC(_FrameRPC):
    """Restrict both metadata and thumbnail wire responses to the batch cap."""

    def __init__(self, timeout, deadline=None):
        try:
            super().__init__(timeout, deadline=deadline)
        except ee.EEException:
            # EE wraps missing persistent credentials in EEException. Failure
            # here prevents every operation; graph/evaluation exceptions later
            # remain ordinary per-year/per-scene upstream failures.
            raise BatchTransportError("initialization_failed", "Unable to initialize the GEE session.", True) from None

    def request_once(self, method, endpoint, *, body=None, deadline=None, max_bytes=TILE_BYTES):
        return super().request_once(method, endpoint, body=body, deadline=deadline,
                                    max_bytes=min(max_bytes, TILE_BYTES))


class _CheckRaised(BaseException):
    def __init__(self, error):
        self.error = error


def _guard_check(check):
    def guarded():
        try:
            check()
        except Exception as exc:
            raise _CheckRaised(exc) from None
    return guarded


def _remaining(deadline):
    if time.monotonic() >= deadline:
        raise BatchTransportError("timeout", "GEE operation exceeded its time budget.")


def _safe_error(exc):
    if isinstance(exc, BatchTransportError):
        return exc
    status = getattr(exc, "http_status", None)
    code = getattr(exc, "code", "upstream_failed")
    fatal = (status in (401, 403) or code in ("upstream_unauthenticated", "upstream_forbidden")
             or isinstance(exc, (RefreshError, DefaultCredentialsError)))
    if fatal:
        return BatchTransportError(code if code != "upstream_failed" else "upstream_unauthenticated",
                                   "GEE authentication or collection permission failed.", True)
    if (isinstance(exc, (TimeoutError, requests.Timeout)) or code == "timeout"
            or isinstance(exc, _TransientError) and status is None):
        return BatchTransportError("timeout", "GEE operation exceeded its time budget.")
    return BatchTransportError(code if isinstance(exc, FrameSearchError) else "upstream_failed",
                               "Unable to complete the GEE operation.")


def _retry(operation, deadline, check):
    for attempt in range(3):
        check()  # Caller controls interruption; its exception must retain identity.
        _remaining(deadline)
        try:
            result = operation()
            _remaining(deadline)
            return result
        except Exception as exc:
            retryable = isinstance(exc, (_TransientError, TimeoutError, ConnectionError,
                                         requests.Timeout, requests.ConnectionError,
                                         requests.exceptions.ChunkedEncodingError))
            if not retryable or attempt == 2:
                raise _safe_error(exc) from None
            delay = attempt + 1
            if deadline - time.monotonic() <= delay:
                raise BatchTransportError("timeout", "GEE operation exceeded its time budget.") from None
            time.sleep(delay)


def _metadata_graph(parameters, start, end):
    dataset = parameters["dataset"]
    cloud = {"sentinel2": "CLOUDY_PIXEL_PERCENTAGE", "landsat": "CLOUD_COVER"}.get(dataset)
    point_rule = parameters.get("selection_mode", "point") == "point" and not parameters.get("rotation_deg")
    roi = (ee.Geometry.Point([parameters["lon"], parameters["lat"]]) if point_rule else
           ee.Geometry(parameters["roi_geometry"], geodesic=False))
    lists, counts = [], []
    for collection_id in _COLLECTIONS[dataset]:
        collection = ee.ImageCollection(collection_id).filterDate(start, end).filterBounds(roi)
        collection = apply_platform_filter(collection, dataset, parameters.get("platform"))
        if dataset == "s1gee":
            collection = collection.filter(ee.Filter.eq("instrumentMode", "IW"))
            for band in imagery.S1_POLARIZATIONS[parameters.get("polarization", "vvvh")]:
                collection = collection.filter(ee.Filter.listContains("transmitterReceiverPolarisation", band))
        if point_rule:
            collection = collection.map(lambda image: image.set(
                "timeseries:contains_center", image.geometry().contains(roi, ee.ErrorMargin(1))
            )).filter(ee.Filter.eq("timeseries:contains_center", True))

        def metadata(value):
            image = ee.Image(value)
            return ee.Dictionary({
                "asset_id": ee.String(collection_id + "/").cat(ee.String(image.get("system:index"))),
                "collection": collection_id, "timestamp_ms": image.get("system:time_start"),
                "cloud_pct": image.get(cloud) if cloud else None,
                "source_metadata": image.toDictionary(_SOURCE_PROPERTIES),
            })

        lists.append(collection.toList(MAX_SCENES + 1).map(metadata))
        counts.append(collection.size())
    return ee.Dictionary({"rows": ee.List(lists).flatten(), "count": ee.List(counts).reduce(ee.Reducer.sum())})


def _metadata_window(parameters, start, end, *, check=lambda: None):
    deadline = time.monotonic() + WINDOW_SECONDS
    try:
        with _RPC(WINDOW_SECONDS, deadline=deadline) as rpc:
            graph = _metadata_graph(parameters, start, end)
            body = {"expression": ee.serializer.encode(graph, for_cloud_api=True)}

            def operation():
                content = rpc.request_once("POST", rpc.project + "/value:compute", body=body,
                                           deadline=deadline, max_bytes=16 * 1024 * 1024)
                try:
                    result = json.loads(content)["result"]
                    rows, count = result["rows"], result["count"]
                    if not isinstance(rows, list) or isinstance(count, bool) or not isinstance(count, int) or count < 0:
                        raise ValueError()
                    if count > MAX_SCENES:
                        raise BatchTransportError("window_cap", "Metadata window exceeds the scene limit.")
                    if len(rows) != count:
                        raise BatchTransportError("incomplete_year", "GEE returned an incomplete scene list.")
                    return rows
                except (KeyError, TypeError, ValueError):
                    raise _TransientError() from None

            return _retry(operation, deadline, check)
    except Exception as exc:
        raise _safe_error(exc) from None


def list_year_scenes(parameters: dict, start: str, end: str, *, check=lambda: None) -> list[dict]:
    """List a complete inclusive date interval, or raise without returning a prefix."""
    try:
        return _list_year_scenes(parameters, start, end, check=_guard_check(check))
    except _CheckRaised as exc:
        raise exc.error from None
    except Exception as exc:
        raise _safe_error(exc) from None


def _list_year_scenes(parameters, start, end, *, check):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first > last or first.year != last.year:
        raise BatchTransportError("invalid_dates", "An annual interval must stay within one calendar year.")
    if parameters.get("dataset") not in _COLLECTIONS:
        raise BatchTransportError("invalid_dataset", "Unsupported annual time-series dataset.")
    scenes = {}

    def window(begin, finish):
        check()
        try:
            rows = _metadata_window(parameters, begin.isoformat(), finish.isoformat(), check=check)
        except BatchTransportError as exc:
            if exc.code not in ("timeout", "window_cap", "response_too_large") or exc.fatal:
                raise
            days = (finish - begin).days
            if days <= 1:
                raise BatchTransportError("incomplete_year", "A daily metadata window could not be completed.") from None
            middle = begin + timedelta(days=days // 2)
            window(begin, middle)
            window(middle, finish)
            return
        for raw in rows:
            try:
                asset, collection = raw["asset_id"], raw["collection"]
                if collection not in _COLLECTIONS[parameters["dataset"]] or not isinstance(asset, str) or not asset.startswith(collection + "/"):
                    raise ValueError()
                suffix = asset[len(collection) + 1:]
                if not suffix or "/" in suffix or "\\" in suffix or suffix in (".", ".."):
                    raise ValueError()
                stamp = datetime.fromtimestamp(raw["timestamp_ms"] / 1000, tz=timezone.utc)
                if not begin <= stamp.date() < finish or not isinstance(raw.get("source_metadata", {}), dict):
                    raise ValueError()
                cloud = raw.get("cloud_pct")
                cloud = float(cloud) if cloud is not None and 0 <= float(cloud) <= 100 else None
                scenes.setdefault(asset, {**raw, "name": suffix, "dataset": parameters["dataset"],
                                          "timestamp": stamp.isoformat().replace("+00:00", "Z"),
                                          "date": stamp.date().isoformat(), "cloud_pct": cloud})
                scenes[asset].pop("timestamp_ms", None)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                raise BatchTransportError("incomplete_year", "GEE returned invalid scene metadata.") from None
            if len(scenes) > MAX_SCENES:
                raise BatchTransportError("year_cap", "The annual scene limit was exceeded.")

    stop = last + timedelta(days=1)
    cursor = first
    while cursor < stop:
        next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
        finish = min(next_month, stop)
        window(cursor, finish)
        cursor = finish
    check()
    return sorted(scenes.values(), key=lambda row: (row["timestamp"], row["asset_id"]))


def _download_grid(parameters):
    # Export a bounded north-up envelope; worker applies the exact persisted
    # output grid afterwards, including custom/rotated projection fallbacks.
    bbox = parameters["bbox"]
    west, south, east, north = [float(bbox[k]) for k in ("west", "south", "east", "north")]
    scale = float(parameters["scale_m"])
    if not all(math.isfinite(v) for v in (west, south, east, north, scale)) or not (
            -180 <= west < east <= 180 and -90 < south < north < 90 and scale > 0):
        raise BatchTransportError("invalid_grid", "The raster export envelope is invalid.")
    dy = scale / 111320
    dx = dy / max(math.cos(math.radians((south + north) / 2)), 0.01)
    try:
        return _grid({"crs": "EPSG:4326", "transform": [dx, 0, west, 0, -dy, north],
                      "width": math.ceil((east - west) / dx), "height": math.ceil((north - south) / dy)})
    except FrameExportError:
        raise BatchTransportError("scene_limit", "The scene exceeds raster dimensions or pixel limits.") from None


def _read_tile(content, grid, bands):
    if len(content) > TILE_BYTES:
        raise BatchTransportError("tile_limit", "The tile response exceeds the byte limit.")
    try:
        with MemoryFile(content) as memory, memory.open() as src:
            if (src.driver != "GTiff" or src.crs != CRS.from_user_input(grid["crs"])
                    or src.width != grid["width"] or src.height != grid["height"]
                    or src.count != len(bands) or any(dtype != "float32" for dtype in src.dtypes)
                    or any(abs(a-b) > 1e-8 for a, b in zip(list(src.transform)[:6], grid["transform"]))):
                raise BatchTransportError("tile_schema", "GEE returned a tile with an unexpected raster schema.")
            # EE TIFFs may omit descriptions; when present they must be exact.
            if any(src.descriptions) and list(src.descriptions) != bands:
                raise BatchTransportError("tile_schema", "GEE returned an unexpected band order.")
            values = src.read(masked=True)
            invalid = np.ma.getmaskarray(values) | ~np.isfinite(values.data) | (values.data == WIRE_NODATA)
            return np.where(invalid, WIRE_NODATA, values.data).astype("float32")
    except rasterio.errors.RasterioError:
        raise _TransientError() from None


from .export_resources import bulk_export


@bulk_export
def export_batch_scene(parameters: dict, scene: dict, destination: Path, *, check=lambda: None) -> None:
    """Publish one complete raw Float32 TIFF using sequential bounded tile RPCs."""
    try:
        _export_batch_scene(parameters, scene, destination, check=_guard_check(check))
    except _CheckRaised as exc:
        raise exc.error from None
    except Exception as exc:
        raise _safe_error(exc) from None


def _export_batch_scene(parameters, scene, destination, *, check):
    check()
    deadline = time.monotonic() + SCENE_SECONDS
    grid = _download_grid(parameters)
    bands = parameters["bands"]
    if not bands or len(bands) != len(set(bands)):
        raise BatchTransportError("invalid_bands", "The snapshot band schema is invalid.")
    # Reserve TIFF header overhead inside the same wire payload limit.
    tiles = list(iter_grid_tiles(grid, len(bands), max(len(bands) * 4, TILE_BYTES - min(65536, TILE_BYTES // 2))))
    if len(tiles) > MAX_TILES:
        raise BatchTransportError("scene_limit", "The scene exceeds the tile count limit.")
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".part-" + uuid4().hex)
    try:
        with _RPC(TILE_SECONDS, deadline=deadline) as rpc:
            image = imagery.load_timeseries_scene(scene["asset_id"], parameters["dataset"],
                                                 parameters.get("polarization", "vvvh"))
            # unmask is applied per band, without intersecting their validity.
            # unmask's numeric fill can promote Float32 values to Double.
            # Restore the wire dtype after filling to keep four-byte budgets.
            image = image.select(bands).toFloat().unmask(WIRE_NODATA, sameFootprint=False).toFloat()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(temporary, "w", driver="GTiff", width=grid["width"], height=grid["height"],
                               count=len(bands), dtype="float32", crs=grid["crs"],
                               transform=Affine(*grid["transform"]), nodata=WIRE_NODATA,
                               compress="deflate", BIGTIFF="IF_SAFER") as dst:
                dst.descriptions = tuple(bands)
                for window in tiles:
                    check()
                    _remaining(deadline)
                    target = _tile_grid(grid, window)
                    params = {"crs": target["crs"], "crs_transform": target["transform"],
                              "dimensions": [target["width"], target["height"]],
                              "format": "GEO_TIFF", "filePerBand": False}
                    tile_deadline = min(deadline, time.monotonic() + TILE_SECONDS)

                    def operation():
                        return _read_tile(rpc.thumbnail_once(image, params, deadline=tile_deadline), target, bands)

                    values = _retry(operation, tile_deadline, check)
                    dst.write(values, window=window)
                dst.update_tags(SOURCE_SCENE=scene["asset_id"], BAND_ORDER=json.dumps(bands))
            check()
            _remaining(deadline)
            os.replace(temporary, destination)
    except (FrameSearchError, requests.RequestException, ValueError, KeyError) as exc:
        raise _safe_error(exc) from None
    except OSError:
        raise BatchTransportError("disk_error", "Unable to write the raster to local storage.", True) from None
    finally:
        temporary.unlink(missing_ok=True)
