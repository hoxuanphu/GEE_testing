"""Read only GeoTIFF headers; derive the full affine footprint, not its alpha mask."""
from __future__ import annotations

from datetime import date, datetime
import math
import hashlib
import json
from pathlib import Path, PureWindowsPath
import re

from affine import Affine
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform as transform_coordinates

from .frame_models import FrameError, FrameGrid, ReferenceFrame

TAG_NAMES = ('ACQUISITION_DATE', 'ACQUISITION_TIME_UTC', 'SOURCE_COLLECTION', 'SOURCE_SCENE',
             'ORBIT_PASS', 'RELATIVE_ORBIT', 'PLATFORM')


def safe_name(value: str) -> str:
    value = PureWindowsPath(str(value).replace('/', '\\')).name
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    return value[:160] or 'reference.tif'


def metre_crs(crs: CRS) -> bool:
    try:
        return crs.is_projected and abs(crs.linear_units_factor[1] - 1.0) < 1e-12
    except (ValueError, rasterio.errors.CRSError):
        return False


def safe_tags(tags: dict) -> dict:
    result = {}
    for key in TAG_NAMES:
        value = str(tags.get(key, ''))
        if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
            continue
        try:
            if key == 'ACQUISITION_DATE':
                date.fromisoformat(value)
            elif key == 'ACQUISITION_TIME_UTC':
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            elif key == 'RELATIVE_ORBIT':
                if not 1 <= int(value) <= 175:
                    continue
            elif key == 'ORBIT_PASS' and value not in ('ASCENDING', 'DESCENDING'):
                continue
            elif key == 'PLATFORM' and value not in ('A', 'B', 'C', 'D'):
                continue
            elif key == 'SOURCE_COLLECTION' and value not in ('COPERNICUS/S1_GRD', 'COPERNICUS/S2_SR_HARMONIZED'):
                continue
            elif key == 'SOURCE_SCENE' and not re.fullmatch(r'[A-Za-z0-9_\-]+', value):
                continue
        except ValueError:
            continue
        result[key] = value
    return result


def reference_footprint(frame: dict) -> dict:
    grid = FrameGrid.model_validate({key: frame[key] for key in ('crs', 'transform', 'width', 'height')})
    t = Affine(*grid.transform)
    corners = [t * point for point in ((0, 0), (grid.width, 0), (grid.width, grid.height), (0, grid.height))]
    ring = []
    for start, end in zip(corners, corners[1:] + corners[:1]):
        ring.extend([(start[0] + (end[0] - start[0]) * i / 32,
                      start[1] + (end[1] - start[1]) * i / 32) for i in range(32)])
    ring.append(ring[0])
    xs, ys = transform_coordinates(grid.crs, 'EPSG:4326', [p[0] for p in ring], [p[1] for p in ring])
    if (not all(math.isfinite(v) for v in xs + ys) or
            min(xs) < -180 or max(xs) > 180 or min(ys) < -90 or max(ys) > 90 or
            any(abs(a - b) > 180 for a, b in zip(xs, xs[1:]))):
        raise FrameError('invalid_footprint', 'Khung không hợp lệ hoặc đi qua đường đổi ngày.')
    if max(xs) - min(xs) >= 180:
        raise FrameError('invalid_footprint', 'Khung quá rộng hoặc đi qua đường đổi ngày.')
    return {'corners': [list(p) for p in corners],
            'bounds': [min(p[0] for p in corners), min(p[1] for p in corners),
                       max(p[0] for p in corners), max(p[1] for p in corners)],
            'footprint_geojson': {'type': 'Polygon', 'coordinates': [[list(p) for p in zip(xs, ys)]]}}


def reference_roi_identity(frame: dict) -> dict:
    """Derive a versioned identity from the server reference grid only."""
    geometry = reference_footprint(frame)['footprint_geojson']
    canonical = json.dumps({'version': 2, 'geometry': geometry}, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return {'roi_version': 2, 'roi_geojson': geometry,
            'roi_fingerprint': hashlib.sha256(canonical.encode('utf-8')).hexdigest()}


def validate_roi_identity(parameters: dict) -> None:
    """Reject old or altered snapshots without upgrading persisted history."""
    try:
        expected = reference_roi_identity(parameters['reference'])
        stored = json.dumps({'version': parameters.get('roi_version'),
                             'geometry': parameters.get('roi_geojson')}, sort_keys=True,
                            separators=(',', ':'), allow_nan=False)
        if type(parameters.get('roi_version')) is not int or any(
                parameters.get(key) != value for key, value in expected.items()) or (
                hashlib.sha256(stored.encode('utf-8')).hexdigest() != expected['roi_fingerprint']):
            raise ValueError('ROI identity mismatch')
    except Exception as exc:
        raise FrameError('stale_search', 'Kết quả tìm dùng phiên bản khung cũ; hãy tìm lại.', 409) from exc


def read_reference_metadata(path: Path, display_name: str) -> dict:
    try:
        with rasterio.Env(GDAL_PAM_ENABLED='NO', GDAL_DISABLE_READDIR_ON_OPEN='EMPTY_DIR'):
            with rasterio.open(path, driver='GTiff') as src:
                if src.driver != 'GTiff':
                    raise FrameError('unsupported_file', 'Chỉ nhận GeoTIFF.', 415)
                if src.crs is None or src.transform == Affine.identity() or (src.gcps[0] and src.transform == Affine.identity()):
                    raise FrameError('invalid_georeference', 'Ảnh cần CRS và affine georeference, không chỉ GCP/RPC.')
                crs, t = src.crs, src.transform
                frame = {'crs': crs.to_wkt(), 'transform': list(t)[:6], 'width': src.width, 'height': src.height,
                         'display_name': safe_name(display_name), 'count': src.count,
                         'descriptions': [d[:256] if d else None for d in src.descriptions],
                         'dtypes': list(src.dtypes), 'tags': safe_tags(src.tags()),
                         'pixel_size': [math.hypot(t.a, t.d), math.hypot(t.b, t.e)],
                         'pixel_unit': 'm' if metre_crs(crs) else ('degree' if crs.is_geographic else crs.linear_units),
                         'compatible_grid_modes': ['match_extent', 'match_grid'] if metre_crs(crs) else ['match_grid']}
        frame.update(reference_footprint(frame))
        return ReferenceFrame.model_validate(frame).model_dump(mode='json')
    except FrameError:
        raise
    except rasterio.errors.RasterioIOError as exc:
        raise FrameError('unsupported_file', 'Không đọc được GeoTIFF; hãy chọn tệp .tif/.tiff hợp lệ.', 415) from exc
    except Exception as exc:
        raise FrameError('invalid_georeference', 'CRS hoặc affine của ảnh mẫu không hợp lệ.') from exc
