"""Shared local-metre rotated raster grids and mask-aware resampling."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import math
import os
from pathlib import Path
import uuid

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from .geo import RectangleROI, point_roi


# Band-at-a-time writers must not repeatedly rewrite pixel-interleaved strips.
# Force BigTIFF even when compression makes the final size hard to predict.
GEOTIFF_OPTIONS = dict(BIGTIFF="YES", tiled=True, blockxsize=256, blockysize=256,
                       interleave="band", compress="deflate")


@contextmanager
def atomic_raster_path(destination):
    """Publish a closed raster on the same filesystem, preserving prior results."""
    destination = Path(destination)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.partial.tif")
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _build_rotated_grid(center_lon, center_lat, width_m, height_m, rotation_deg, scale_m) -> dict:
    if not math.isfinite(scale_m) or scale_m <= 0:
        raise ValueError("scale_m must be finite and positive")
    columns = max(1, math.ceil(width_m / scale_m))
    rows = max(1, math.ceil(height_m / scale_m))
    pixel_width = width_m / columns
    pixel_height = height_m / rows
    t = math.radians(rotation_deg % 360)
    c, s = math.cos(t), math.sin(t)
    half_width, half_height = width_m / 2, height_m / 2
    crs = CRS.from_string(
        f"+proj=eqc +lat_ts={center_lat} +lat_0={center_lat} +lon_0={center_lon} "
        f"+R={111320 * 180 / math.pi} +units=m +no_defs")
    return {
        "crs": crs.to_wkt(),
        "transform": [
            pixel_width * c,
            -pixel_height * s,
            -half_width * c + half_height * s,
            -pixel_width * s,
            -pixel_height * c,
            half_width * s + half_height * c,
        ],
        "width": columns,
        "height": rows,
        "actual_pixel_size_m": pixel_width if columns == rows else (pixel_width, pixel_height),
        "actual_pixel_width_m": pixel_width,
        "actual_pixel_height_m": pixel_height,
    }


def build_rotated_grid(lon, lat, radius_m, rotation_deg, scale_m) -> dict:
    point_roi(lon, lat, radius_m, rotation_deg)
    return _build_rotated_grid(lon, lat, 2 * radius_m, 2 * radius_m, rotation_deg, scale_m)


def build_rectangle_grid(roi: RectangleROI, scale_m: float) -> dict:
    """Build a pixel-aligned rotated grid whose footprint is exactly ``roi``."""
    return _build_rotated_grid(
        roi.center_lon,
        roi.center_lat,
        roi.width_m,
        roi.height_m,
        roi.rotation_deg,
        scale_m,
    )


def build_reference_grid(frame: dict, mode: str = 'match_extent', resolution_m: int = 10) -> dict:
    from .frame_models import FrameError, FrameGrid, MAX_DIMENSION, MAX_OUTPUT_PIXELS
    from .reference_frame import metre_crs

    validated = FrameGrid.model_validate({key: frame[key] for key in ('crs', 'transform', 'width', 'height')})
    crs, transform = CRS.from_user_input(validated.crs), Affine(*validated.transform)
    width, height = validated.width, validated.height
    if mode == 'match_extent':
        if not metre_crs(crs):
            raise FrameError('unsupported_units', 'Đồng biên theo mét cần CRS mét; hãy chọn đồng lưới mẫu.')
        if resolution_m not in (10, 20, 30, 60):
            raise FrameError('invalid_resolution', 'Độ phân giải phải là 10, 20, 30 hoặc 60 m.')
        columns = max(1, math.ceil(width * math.hypot(transform.a, transform.d) / resolution_m))
        rows = max(1, math.ceil(height * math.hypot(transform.b, transform.e) / resolution_m))
        transform = transform * Affine.scale(width / columns, height / rows)
        width, height = columns, rows
    elif mode != 'match_grid':
        raise FrameError('invalid_grid_mode', 'Chế độ đồng khung không hợp lệ.')
    if max(width, height) > MAX_DIMENSION or width * height > MAX_OUTPUT_PIXELS:
        raise FrameError('grid_too_large', 'Lưới vượt 30.000 pixel/cạnh hoặc 100 triệu pixel; chọn pixel lớn hơn.')
    result = {'crs': crs.to_wkt(), 'transform': list(transform)[:6], 'width': width, 'height': height,
              'actual_pixel_width': math.hypot(transform.a, transform.d),
              'actual_pixel_height': math.hypot(transform.b, transform.e),
              'pixel_unit': 'm' if metre_crs(crs) else ('degree' if crs.is_geographic else crs.linear_units)}
    if metre_crs(crs):
        result.update(actual_pixel_width_m=result['actual_pixel_width'],
                      actual_pixel_height_m=result['actual_pixel_height'])
    return result


def grid_profile(grid, count=1, dtype="float32", nodata=float("nan")) -> dict:
    return {"driver": "GTiff", "crs": grid["crs"], "transform": Affine(*grid["transform"]),
            "width": grid["width"], "height": grid["height"], "count": count,
            "dtype": dtype, "nodata": nodata, **GEOTIFF_OPTIONS}


def write_grid_band(source, destination, grid, source_index=1, destination_index=1,
                    categorical=False, check=lambda:None):
    """Resample one native source band directly to the final grid, in blocks."""
    options = dict(crs=grid["crs"], transform=Affine(*grid["transform"]),
                   width=grid["width"], height=grid["height"], dtype="float32",
                   nodata=float("nan"), warp_mem_limit=64)
    with WarpedVRT(source, resampling=Resampling.nearest if categorical else Resampling.bilinear,
                   **options) as values, WarpedVRT(source, resampling=Resampling.nearest,
                                                  **options) as validity:
        for _, window in destination.block_windows(destination_index):
            check()
            data = values.read(source_index, window=window, masked=True)
            valid = validity.read_masks(source_index, window=window) > 0
            valid &= ~np.ma.getmaskarray(data) & np.isfinite(data.data)
            fill = destination.nodata if destination.nodata is not None else 0
            destination.write(np.where(valid, data.data, fill).astype(destination.dtypes[destination_index-1]),
                              destination_index, window=window)
    description = source.descriptions[source_index-1]
    if description:
        destination.set_band_description(destination_index, description)


def warp_to_grid(source, destination, grid, categorical=False, *, check=lambda:None) -> dict:
    """Warp a raster without treating valid zeros as missing data.

    Sources must cover the ROI envelope plus an interpolation halo. Category flags
    may be a bool or a sequence with one flag per band. CRS/affine stay serialized.
    """
    context = rasterio.open(source) if not hasattr(source, "read") else nullcontext(source)
    with context as src:
        if not src.crs:
            raise ValueError("Raster CRS is missing")
        flags = [categorical] * src.count if isinstance(categorical, bool) else list(categorical)
        if categorical is False:
            from .imagery import S2_CATEGORICAL_BANDS
            flags = [name in S2_CATEGORICAL_BANDS for name in src.descriptions]
        if len(flags) != src.count:
            raise ValueError("One categorical flag is required per band")
        categorical_only = all(flags)
        dtype = "uint8" if categorical_only else "float32"
        fill = 255 if categorical_only else float("nan")
        with atomic_raster_path(destination) as temporary:
            with rasterio.open(temporary, "w", **grid_profile(grid, src.count, dtype, fill)) as dst:
                for index, flag in enumerate(flags, 1):
                    write_grid_band(src, dst, grid, index, index, flag, check=check)
                    dst.update_tags(index, **src.tags(index))
                dst.update_tags(**src.tags())
                dst.scales, dst.offsets, dst.units = src.scales, src.offsets, src.units
    return grid


def write_grid_blocks(source,destination,grid,categorical_flags,*,check=lambda:None,on_block=lambda:None):
    """Reuse the legacy full-band GDAL warp semantics, once per output block.

    A one-band warp changes GDAL's unified NoData/kernel behavior. Keep the
    full source schema in both views and bound arrays to one256x256 block.
    """
    options=dict(crs=grid['crs'],transform=Affine(*grid['transform']),width=grid['width'],
                 height=grid['height'],dtype='float32',nodata=float('nan'),warp_mem_limit=64)
    flags=np.asarray(categorical_flags,dtype=bool)[:,None,None]
    fill=destination.nodata if destination.nodata is not None else 0
    with WarpedVRT(source,resampling=Resampling.bilinear,**options) as values, \
         WarpedVRT(source,resampling=Resampling.nearest,**options) as validity:
        for _,window in destination.block_windows(1):
            check()
            # GDAL's all-band RasterIO fast path changes warp chunk sampling.
            # Read bands individually, exactly like write_grid_band, while the
            # shared views reuse their computed full-band blocks in the cache.
            spectral=np.ma.stack([values.read(index,window=window,masked=True)
                                  for index in range(1,source.count+1)])
            nearest=np.ma.stack([validity.read(index,window=window,masked=True)
                                 for index in range(1,source.count+1)])
            data=np.where(flags,nearest.data,spectral.data)
            invalid=np.where(flags,np.ma.getmaskarray(nearest),np.ma.getmaskarray(spectral))
            masks=np.stack([validity.read_masks(index,window=window)
                            for index in range(1,source.count+1)])
            valid=(masks>0)&~invalid&np.isfinite(data)
            destination.write(np.where(valid,data,fill).astype(destination.dtypes[0]),window=window)
            on_block()
