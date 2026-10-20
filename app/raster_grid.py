"""Shared local-metre rotated raster grids and mask-aware resampling."""
from __future__ import annotations

from contextlib import nullcontext
import math

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT

from .geo import RectangleROI, point_roi


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


def grid_profile(grid, count=1, dtype="float32", nodata=float("nan")) -> dict:
    return {"driver": "GTiff", "crs": grid["crs"], "transform": Affine(*grid["transform"]),
            "width": grid["width"], "height": grid["height"], "count": count,
            "dtype": dtype, "nodata": nodata, "compress": "deflate"}


def write_grid_band(source, destination, grid, source_index=1, destination_index=1,
                    categorical=False):
    """Resample one native source band directly to the final grid, in blocks."""
    options = dict(crs=grid["crs"], transform=Affine(*grid["transform"]),
                   width=grid["width"], height=grid["height"], dtype="float32",
                   nodata=float("nan"))
    with WarpedVRT(source, resampling=Resampling.nearest if categorical else Resampling.bilinear,
                   **options) as values, WarpedVRT(source, resampling=Resampling.nearest,
                                                  **options) as validity:
        for _, window in destination.block_windows(destination_index):
            data = values.read(source_index, window=window, masked=True)
            valid = validity.read_masks(source_index, window=window) > 0
            valid &= ~np.ma.getmaskarray(data) & np.isfinite(data.data)
            fill = destination.nodata if destination.nodata is not None else 0
            destination.write(np.where(valid, data.data, fill).astype(destination.dtypes[destination_index-1]),
                              destination_index, window=window)
    description = source.descriptions[source_index-1]
    if description:
        destination.set_band_description(destination_index, description)


def warp_to_grid(source, destination, grid, categorical=False) -> dict:
    """Warp a raster without treating valid zeros as missing data.

    Sources must cover the ROI envelope plus an interpolation halo. Category flags
    may be a bool or a sequence with one flag per band. CRS/affine stay serialized.
    """
    context = rasterio.open(source) if not hasattr(source, "read") else nullcontext(source)
    with context as src:
        if not src.crs:
            raise ValueError("Raster CRS is missing")
        flags = [categorical] * src.count if isinstance(categorical, bool) else list(categorical)
        if len(flags) != src.count:
            raise ValueError("One categorical flag is required per band")
        categorical_only = all(flags)
        dtype = "uint8" if categorical_only else "float32"
        fill = 255 if categorical_only else float("nan")
        with rasterio.open(destination, "w", **grid_profile(grid, src.count, dtype, fill)) as dst:
            for index, flag in enumerate(flags, 1):
                write_grid_band(src, dst, grid, index, index, flag)
            dst.update_tags(**src.tags())
            dst.scales, dst.offsets, dst.units = src.scales, src.offsets, src.units
    return grid
