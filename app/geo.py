"""Common geospatial helpers for point and rectangle regions of interest."""
from __future__ import annotations

import math
from dataclasses import dataclass

import ee
from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_GEOGRAPHIC_LAT = 85.05112878
MAX_RECTANGLE_DIMENSION_M = 90_000.0


@dataclass
class Bbox:
    """Axis-aligned bounding box in geographic coordinates (degrees)."""

    west: float
    south: float
    east: float
    north: float


class RectangleROI(BaseModel):
    """A validated local-metre rectangle ROI with clockwise rotation."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    center_lon: float = Field(..., ge=-180, le=180)
    center_lat: float = Field(..., ge=-MAX_GEOGRAPHIC_LAT, le=MAX_GEOGRAPHIC_LAT)
    width_m: float = Field(..., gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    height_m: float = Field(..., gt=0, le=MAX_RECTANGLE_DIMENSION_M)
    rotation_deg: float = Field(0.0, ge=0, le=360)

    @model_validator(mode="after")
    def validate_geometry(self) -> "RectangleROI":
        self.rotation_deg %= 360
        rectangle_roi(
            self.center_lon,
            self.center_lat,
            self.width_m,
            self.height_m,
            self.rotation_deg,
        )
        return self

    @property
    def geometry(self) -> dict:
        return rectangle_roi(
            self.center_lon, self.center_lat, self.width_m, self.height_m, self.rotation_deg
        )[0]

    @property
    def bbox(self) -> Bbox:
        return rectangle_roi(
            self.center_lon, self.center_lat, self.width_m, self.height_m, self.rotation_deg
        )[1]

    @property
    def polygon(self) -> dict:
        """GeoJSON polygon for the rotated rectangle."""
        return self.geometry

    def model_geometry(self) -> tuple[dict, Bbox]:
        """Return the polygon and envelope as one reusable geometry result."""
        return rectangle_roi(
            self.center_lon, self.center_lat, self.width_m, self.height_m, self.rotation_deg
        )

    def grid_inputs(self, scale_m: float) -> dict:
        """Return serialized output-grid inputs for this ROI."""
        from .raster_grid import build_rectangle_grid
        return build_rectangle_grid(self, scale_m)

    def output_grid_inputs(self, scale_m: float) -> dict:
        """Alias for :meth:`grid_inputs` for request-building callers."""
        return self.grid_inputs(scale_m)

    def output_grid(self, scale_m: float) -> dict:
        """Build the final raster grid for this ROI."""
        return self.grid_inputs(scale_m)


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


def rectangle_roi(
    center_lon: float,
    center_lat: float,
    width_m: float,
    height_m: float,
    rotation_deg: float = 0,
) -> tuple[dict, Bbox]:
    """Return a clockwise-rotated local-metre rectangle and its WGS84 envelope."""
    values = (center_lon, center_lat, width_m, height_m, rotation_deg)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Rectangle coordinates, dimensions and rotation must be finite")
    if not -180 <= center_lon <= 180 or not -MAX_GEOGRAPHIC_LAT <= center_lat <= MAX_GEOGRAPHIC_LAT:
        raise ValueError("Rectangle center is outside the supported geographic domain")
    if not 0 < width_m <= MAX_RECTANGLE_DIMENSION_M:
        raise ValueError(f"width_m must be > 0 and <= {MAX_RECTANGLE_DIMENSION_M:.0f} m")
    if not 0 < height_m <= MAX_RECTANGLE_DIMENSION_M:
        raise ValueError(f"height_m must be > 0 and <= {MAX_RECTANGLE_DIMENSION_M:.0f} m")
    if not 0 <= rotation_deg <= 360:
        raise ValueError("rotation_deg must be between 0 and 360")

    angle = math.radians(rotation_deg % 360)
    c, s = math.cos(angle), math.sin(angle)
    half_width, half_height = width_m / 2, height_m / 2
    meters_per_degree_lon = 111320.0 * max(math.cos(math.radians(center_lat)), 1e-6)
    ring = [
        [
            center_lon + (x * c + y * s) / meters_per_degree_lon,
            center_lat + (-x * s + y * c) / 111320.0,
        ]
        for x, y in (
            (-half_width, -half_height),
            (half_width, -half_height),
            (half_width, half_height),
            (-half_width, half_height),
        )
    ]
    bbox = Bbox(
        min(point[0] for point in ring),
        min(point[1] for point in ring),
        max(point[0] for point in ring),
        max(point[1] for point in ring),
    )
    if (
        bbox.west < -180
        or bbox.east > 180
        or bbox.south < -MAX_GEOGRAPHIC_LAT
        or bbox.north > MAX_GEOGRAPHIC_LAT
    ):
        raise ValueError("Rectangle ROI crosses the supported geographic domain")
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
