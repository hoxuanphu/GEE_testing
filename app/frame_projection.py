"""Bounded, scene-wide standard-grid transport and mask-aware local reprojection."""
from __future__ import annotations

import math
from pathlib import Path
from uuid import uuid4

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform, transform

ESTIMATOR_VERSION = 2
MAX_TRANSPORT_PIXELS = 100_000_000
MAX_TRANSPORT_TILES = 4096


def mask_warp_options(grid):
    # Byte zero means invalid data and must stay a numeric zero through warp.
    # A separate 255 sentinel avoids GDAL treating valid 0/1 masks as alpha.
    return dict(crs=grid['crs'], transform=Affine(*grid['transform']), width=grid['width'],
                height=grid['height'], resampling=Resampling.nearest, src_nodata=255, nodata=255,
                warp_mem_limit=64, NUM_THREADS='1')


def plan_transport_grid(grid, dataset, bands):
    from .frame_export import FrameExportError, _grid, iter_grid_tiles
    grid = _grid(grid)
    crs = CRS.from_user_input(grid['crs'])
    definition = crs.to_dict()
    # A spherical EQC is a map of angular coordinates, not a surveyed datum.
    # Permit its explicit inverse projection only; never invent a datum shift.
    angular_sphere = (definition.get('proj') == 'eqc' and
                      (definition.get('R') or definition.get('a') == definition.get('b')))
    if not angular_sphere and (crs.to_epsg() is None or definition.get('datum') != 'WGS84'):
        raise FrameExportError('unsupported_reference_crs', 'CRS không có phép đổi datum được xác nhận; không dùng ballpark.')
    affine = Affine(*grid['transform'])
    w, h = grid['width'], grid['height']
    points = []
    for a, b in (((0, 0), (w, 0)), ((w, 0), (w, h)), ((w, h), (0, h)), ((0, h), (0, 0))):
        points.extend(affine * (a[0] + (b[0]-a[0])*i/64, a[1] + (b[1]-a[1])*i/64) for i in range(65))
    try:
        lon, lat = transform(crs, 'EPSG:4326', *zip(*points))
        back_x, back_y = transform('EPSG:4326', crs, lon, lat)
        tolerance = max(1e-7, min(math.hypot(affine.a, affine.d), math.hypot(affine.b, affine.e))*1e-5)
        if any(math.hypot(x-p[0], y-p[1]) > tolerance for x, y, p in zip(back_x, back_y, points)):
            raise FrameExportError('unsupported_reference_crs', 'Phép đổi CRS không vượt kiểm tra round-trip.')
        if not all(math.isfinite(v) for v in lon + lat) or min(lat) < -90 or max(lat) > 90:
            raise ValueError()
        # Reject split footprints rather than allocating a near-global bounding box.
        if max(lon) - min(lon) > 180 or min(lon) < -180 or max(lon) > 180:
            raise FrameExportError('unsupported_transport_extent', 'Khung cắt kinh tuyến 180; hãy chia vùng xuất.')
        zones = {min(60, max(1, int((x + 180) // 6) + 1)) for x in lon}
        center_lat = (min(lat) + max(lat))/2
        target = ('EPSG:' + str((32600 if center_lat >= 0 else 32700) + next(iter(zones)))) if (
            len(zones) == 1 and min(lat) >= -80 and max(lat) <= 84 and min(lat)*max(lat) >= 0) else 'EPSG:4326'
        xs, ys = transform(crs, target, *zip(*points))
        px, py = affine * (w/2, h/2)
        cx, cy = transform(crs, target, [px, px+affine.a, px+affine.b], [py, py+affine.d, py+affine.e])
        center_scale = min(math.hypot(cx[i]-cx[0], cy[i]-cy[0]) for i in (1, 2))
        base, _, _ = calculate_default_transform(crs, target, w, h,
            min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points))
        scale = min(abs(base.a), abs(base.e), center_scale)
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError()
        left = (math.floor(min(xs)/scale)-2)*scale
        top = (math.ceil(max(ys)/scale)+2)*scale
        width = math.ceil(max(xs)/scale)+2 - round(left/scale)
        height = round(top/scale) - math.floor(min(ys)/scale)+2
        transport = _grid({'crs': target, 'transform': [scale, 0, left, 0, -scale, top],
                           'width': width, 'height': height})
        tiles = sum(1 for _ in iter_grid_tiles(transport, len(bands)))
        if width*height > MAX_TRANSPORT_PIXELS or tiles > MAX_TRANSPORT_TILES:
            raise ValueError()
        # Output + archive + intermediate float values and per-band byte masks,
        # conservative compression overhead and bounded warp/cache working space.
        reserve = w*h*len(bands)*4*4 + width*height*len(bands)*10 + 256*1024*1024
        return {'grid': transport, 'disk_reserve_bytes': reserve, 'estimator_version': ESTIMATOR_VERSION,
                'transport_tiles': tiles, 'coordinate_policy': 'inverse_spherical_map_angles' if angular_sphere else 'authority_crs'}
    except FrameExportError:
        raise
    except (ValueError, OverflowError, rasterio.errors.RasterioError):
        raise FrameExportError('unsupported_transport_extent', 'Không lập được lưới trung gian hữu hạn trong giới hạn.') from None


def estimate_export_resources(grid, dataset, bands, scenes=1):
    plan = plan_transport_grid(grid, dataset, bands)
    return {'estimator_version': ESTIMATOR_VERSION,
            'disk_reserve_bytes': max(512*1024*1024, plan['disk_reserve_bytes']*scenes)}


def export_via_transport_grid(image, dataset, bands, grid, output, *, rpc):
    from .frame_export import fetch_grid_tile, iter_grid_tiles, FrameExportError
    plan = plan_transport_grid(grid, dataset, bands)
    transport = plan['grid']
    prefix = output.name + '.' + uuid4().hex
    values_path, masks_path = (Path(prefix + suffix) for suffix in ('.values.tif', '.masks.tif'))
    profile = dict(driver='GTiff', width=transport['width'], height=transport['height'], count=len(bands),
                   crs=transport['crs'], transform=Affine(*transport['transform']), tiled=True,
                   blockxsize=256, blockysize=256, compress='deflate', BIGTIFF='IF_SAFER')
    try:
        with rasterio.Env(GDAL_PAM_ENABLED='NO', GDAL_CACHEMAX=64*1024*1024):
            with rasterio.open(values_path, 'w', dtype='float32', nodata=np.nan, **profile) as values, rasterio.open(
                    masks_path, 'w', dtype='uint8', nodata=None, **profile) as masks:
                for window in iter_grid_tiles(transport, len(bands)):
                    # Download masks independently; do not intersect until the destination.
                    for index, band in enumerate(bands, 1):
                        tile = fetch_grid_tile(image, 's1gee', [band], transport, window, rpc=rpc)
                        values.write(tile.filled(np.nan).astype('float32'), indexes=[index], window=window)
                        masks.write((~np.ma.getmaskarray(tile)).astype('uint8'), indexes=[index], window=window)
            options = dict(crs=grid['crs'], transform=Affine(*grid['transform']), width=grid['width'],
                           height=grid['height'], warp_mem_limit=64, NUM_THREADS='1')
            with rasterio.open(values_path) as values, rasterio.open(masks_path) as masks, WarpedVRT(
                    values, resampling=Resampling.bilinear if dataset == 'sentinel2' else Resampling.nearest,
                    src_nodata=np.nan, nodata=np.nan, **options) as warped, WarpedVRT(
                    masks, **mask_warp_options(grid)) as validity:
                for window in iter_grid_tiles(grid, len(bands)):
                    data = warped.read(window=window)
                    common = validity.read(window=window, masked=True).filled(0).all(axis=0)
                    data[:, ~common] = np.nan
                    output.write(data, window=window)
        return {'version': 1, 'export_strategy': 'standard_grid_local_warp',
                'transport_crs': transport['crs'], 'transport_resolution': transport['transform'][0],
                'value_resampling_steps': ['nearest', 'bilinear' if dataset == 'sentinel2' else 'nearest'],
                'projection_fallback_reason': 'unsupported_export_projection',
                'coordinate_policy': plan['coordinate_policy']}
    except rasterio.errors.RasterioError:
        raise FrameExportError('warp_failed', 'Không thể warp ảnh về lưới đích.') from None
    finally:
        for path in (values_path, masks_path):
            for suffix in ('', '.aux.xml', '.msk'):
                Path(str(path)+suffix).unlink(missing_ok=True)
