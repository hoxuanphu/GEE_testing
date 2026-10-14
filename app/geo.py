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


def bbox_to_ee_geom(bbox: Bbox) -> ee.Geometry:
    return ee.Geometry.Rectangle([bbox.west, bbox.south, bbox.east, bbox.north])


def point_to_ee_geom(lon: float, lat: float, radius_m: float) -> ee.Geometry:
    return ee.Geometry.Point([lon, lat]).buffer(radius_m)
