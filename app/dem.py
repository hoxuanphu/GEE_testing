"""Static elevation products available in Earth Engine (heights in metres)."""
from __future__ import annotations

import datetime as dt
import math
import re
import uuid

import ee


def elevation_filename(dataset: str, region_tag: str) -> str:
    region_tag = re.sub(r"[^A-Za-z0-9_.-]+", "-", region_tag).strip(".-")[:120]
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{dataset}_elevation_30m_{region_tag}_{timestamp}_{uuid.uuid4().hex[:12]}.tif"

DEM_SOURCES = {
    "alos": {"name": "ALOS AW3D30", "asset": "JAXA/ALOS/AW3D30/V4_1", "band": "DSM", "kind": "DSM", "collection": True},
    "srtm": {"name": "SRTM", "asset": "USGS/SRTMGL1_003", "band": "elevation", "kind": "DEM", "collection": False},
    "copernicus": {"name": "Copernicus GLO-30", "asset": "COPERNICUS/DEM/GLO30_2024_1", "band": "DEM", "kind": "DSM", "collection": True},
}


def elevation_image(dataset: str, roi: ee.Geometry) -> ee.Image:
    source = DEM_SOURCES[dataset]
    if source["collection"]:
        tiles = ee.ImageCollection(source["asset"]).filterBounds(roi).select(source["band"])
        if not tiles.size().getInfo():
            raise ValueError("Không có dữ liệu độ cao tại vùng đã chọn.")
        image = tiles.mosaic()
    else:
        image = ee.Image(source["asset"]).select(source["band"])
    return image.rename("elevation").toFloat()


def elevation_grid(bbox, scale_m: float = 30) -> dict:
    """North-up metric grid in the centre UTM zone (UPS at polar latitudes)."""
    from rasterio.warp import transform_bounds

    lon = (bbox.west + bbox.east) / 2
    lat = (bbox.south + bbox.north) / 2
    zone = min(60, max(1, math.floor((lon + 180) / 6) + 1))
    epsg = (32600 if lat >= 0 else 32700) + zone
    if lat > 84 or lat < -80:
        epsg = 32661 if lat >= 0 else 32761
    crs = f"EPSG:{epsg}"
    west, south, east, north = transform_bounds(
        "EPSG:4326", crs, bbox.west, bbox.south, bbox.east, bbox.north,
        densify_pts=41,
    )
    west = math.floor(west / scale_m) * scale_m
    north = math.ceil(north / scale_m) * scale_m
    return {
        "crs": crs,
        "transform": [scale_m, 0, west, 0, -scale_m, north],
        "width": max(1, math.ceil((east - west) / scale_m)),
        "height": max(1, math.ceil((north - south) / scale_m)),
    }


def mask_elevation(path, geometry: dict) -> None:
    """Apply the true ROI after resampling so rotated ROI corners stay NoData."""
    import rasterio
    from rasterio.features import geometry_mask
    from rasterio.warp import transform_geom

    with rasterio.open(path, "r+") as dst:
        polygon = transform_geom("EPSG:4326", dst.crs, geometry)
        for _, window in dst.block_windows(1):
            values = dst.read(1, window=window)
            inside = geometry_mask([polygon], out_shape=values.shape,
                                   transform=dst.window_transform(window), invert=True)
            values[~inside] = -9999
            dst.write(values, 1, window=window)


def finalize_elevation(path) -> None:
    """Normalize GEE fill values and the merge/warp NaN mask for GIS clients."""
    import numpy as np
    import rasterio

    with rasterio.open(path, "r+") as dst:
        for _, window in dst.block_windows(1):
            values = dst.read(1, window=window, masked=True).filled(-9999)
            values[~np.isfinite(values)] = -9999
            dst.write(values, 1, window=window)
        dst.nodata = -9999
        dst.set_band_description(1, "elevation")
        dst.update_tags(1, units="m")
