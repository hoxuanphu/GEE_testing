"""Common geospatial helpers for querying imagery over a point."""
from __future__ import annotations

import math
from dataclasses import dataclass

import ee


@dataclass
class Bbox:
    """Axis-aligned bounding box in geographic coordinates (degrees)."""

    west: float
    south: float
    east: float
    north: float


def point_bbox(lon: float, lat: float, radius_m: float) -> Bbox:
    """Build a square bbox around (lon, lat) with the given radius in meters."""
    d_lat = radius_m / 111_320.0
    d_lon = radius_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return Bbox(
        west=lon - d_lon,
        south=lat - d_lat,
        east=lon + d_lon,
        north=lat + d_lat,
    )


def point_roi(lon: float, lat: float, radius_m: float, rotation_deg: float = 0) -> tuple[dict, Bbox]:
    """Return the clockwise-rotated local-metre square and its WGS84 envelope."""
    if not all(math.isfinite(v) for v in (lon, lat, radius_m, rotation_deg)):
        raise ValueError("Point coordinates, radius and rotation must be finite")
    if not -180 <= lon <= 180 or not -85.05112878 <= lat <= 85.05112878:
        raise ValueError("Point coordinates are outside the supported geographic domain")
    if not 0 < radius_m <= 45000:
        raise ValueError("radius_m must be > 0 and <= 45000 m")
    if not 0 <= rotation_deg <= 360:
        raise ValueError("rotation_deg must be between 0 and 360")
    angle = math.radians(rotation_deg % 360)
    if angle == 0:
        bbox = point_bbox(lon, lat, radius_m)
        ring = [[bbox.west, bbox.south], [bbox.east, bbox.south],
                [bbox.east, bbox.north], [bbox.west, bbox.north]]
    else:
        c, s = math.cos(angle), math.sin(angle)
        r = radius_m
        ring = [[lon + (x*c + y*s) / (111320 * max(math.cos(math.radians(lat)), 1e-6)),
                 lat + (-x*s + y*c) / 111320]
                for x, y in ((-r, -r), (r, -r), (r, r), (-r, r))]
        bbox = Bbox(min(p[0] for p in ring), min(p[1] for p in ring),
                    max(p[0] for p in ring), max(p[1] for p in ring))
    if bbox.west < -180 or bbox.east > 180 or bbox.south < -85.05112878 or bbox.north > 85.05112878:
        raise ValueError("Point ROI crosses the supported geographic domain")
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}, bbox


def source_bbox(bbox: Bbox, scale_m: float, lat: float) -> Bbox:
    """Expand the source envelope for interpolation without changing the output ROI."""
    halo = point_bbox(0, lat, max(60.0, scale_m * 2))
    return Bbox(max(-180, bbox.west - halo.east), max(-85.05112878, bbox.south - (halo.north-lat)),
                min(180, bbox.east + halo.east), min(85.05112878, bbox.north + (halo.north-lat)))


def bbox_to_ee_geom(bbox: Bbox) -> ee.Geometry:
    return ee.Geometry.Rectangle([bbox.west, bbox.south, bbox.east, bbox.north])


def point_to_ee_geom(lon: float, lat: float, radius_m: float) -> ee.Geometry:
    return ee.Geometry.Point([lon, lat]).buffer(radius_m)
