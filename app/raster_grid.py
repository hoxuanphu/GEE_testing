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

from .geo import point_roi


def build_rotated_grid(lon, lat, radius_m, rotation_deg, scale_m) -> dict:
    point_roi(lon, lat, radius_m, rotation_deg)
    if not math.isfinite(scale_m) or scale_m <= 0:
        raise ValueError("scale_m must be finite and positive")
    n = max(1, math.ceil(2 * radius_m / scale_m))
    p = 2 * radius_m / n
    t = math.radians(rotation_deg % 360)
    c, s, r = math.cos(t), math.sin(t), radius_m
    crs = CRS.from_string(
        f"+proj=eqc +lat_ts={lat} +lat_0={lat} +lon_0={lon} "
        f"+R={111320 * 180 / math.pi} +units=m +no_defs")
    return {"crs": crs.to_wkt(), "transform": [p*c, -p*s, -r*c+r*s, -p*s, -p*c, r*s+r*c],
            "width": n, "height": n, "actual_pixel_size_m": p}


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
