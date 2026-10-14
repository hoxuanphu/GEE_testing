"""Administrative-boundary lookup for the 63-province selection mode."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import ee

from .geo import Bbox


_BOUNDARIES_FILE = (
    Path(__file__).resolve().parent.parent / "shapefile34" / "vn34.json"
)

# Địa giới hành chính mới (sáp nhập 2025, 34 tỉnh/thành). Tên hiển thị lấy
# thẳng từ dữ liệu (ten_tinh) — không cần ghi đè như bộ 63 tỉnh cũ.


@dataclass(frozen=True)
class Province:
    """One administrative province, identified by the source dataset's ID."""

    id: str
    name: str
    geometry: dict[str, Any]
    bbox: Bbox

    def ee_geometry(self) -> ee.Geometry:
        return ee.Geometry(self.geometry)


def _coordinate_pairs(value: Any):
    if isinstance(value, (list, tuple)):
        if len(value) >= 2 and all(isinstance(v, (int, float)) for v in value[:2]):
            yield float(value[0]), float(value[1])
        else:
            for child in value:
                yield from _coordinate_pairs(child)


def _bbox_for_geometry(geometry: dict[str, Any]) -> Bbox:
    pairs = list(_coordinate_pairs(geometry.get("coordinates")))
    if not pairs:
        raise ValueError("geometry has no coordinates")
    lons, lats = zip(*pairs)
    return Bbox(west=min(lons), south=min(lats), east=max(lons), north=max(lats))


@lru_cache(maxsize=1)
def _provinces_by_id() -> dict[str, Province]:
    try:
        raw = json.loads(_BOUNDARIES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Boundary file is missing: {_BOUNDARIES_FILE}") from exc

    features = raw.get("features")
    if not isinstance(features, list):
        raise RuntimeError("Boundary file is not a GeoJSON FeatureCollection")

    provinces: dict[str, Province] = {}
    for feature in features:
        props = feature.get("properties") or {}
        province_id = props.get("id")
        name = props.get("name")
        geometry = feature.get("geometry")
        if (
            not isinstance(province_id, str)
            or not isinstance(name, str)
            or not isinstance(geometry, dict)
            or geometry.get("type") not in {"Polygon", "MultiPolygon"}
        ):
            raise RuntimeError("Boundary file contains an invalid province feature")
        if province_id in provinces:
            raise RuntimeError(f"Boundary file contains duplicate ID: {province_id}")
        provinces[province_id] = Province(
            id=province_id,
            name=name,
            geometry=geometry,
            bbox=_bbox_for_geometry(geometry),
        )

    if len(provinces) != 34:
        raise RuntimeError(f"Expected 34 provinces, found {len(provinces)}")
    return provinces


def list_provinces() -> list[dict[str, str]]:
    return [
        {"id": province.id, "name": province.name}
        for province in sorted(_provinces_by_id().values(), key=lambda item: item.name)
    ]


def get_province(province_id: str) -> Province | None:
    return _provinces_by_id().get(province_id)


def province_feature(province_id: str) -> dict[str, Any] | None:
    province = get_province(province_id)
    if province is None:
        return None
    return {
        "type": "Feature",
        "properties": {"id": province.id, "name": province.name},
        "geometry": province.geometry,
    }


def province_feature_collection() -> dict[str, Any]:
    provinces = _provinces_by_id().values()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"id": province.id, "name": province.name},
                "geometry": province.geometry,
            }
            for province in provinces
        ],
    }
