"""Commune/ward boundary lookup backed by the project shapefile."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import ee
import shapefile

from .geo import Bbox


_SHAPEFILE = (
    Path(__file__).resolve().parent.parent
    / "Shapefile_PX_VN_34"
    / "Việt Nam (phường xã) - 34.shp"
)


@dataclass(frozen=True)
class Commune:
    id: str
    name: str
    province_id: str
    province_name: str
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
def _communes_by_id() -> dict[str, Commune]:
    try:
        reader = shapefile.Reader(str(_SHAPEFILE), encoding="utf-8")
    except (OSError, shapefile.ShapefileException) as exc:
        raise RuntimeError(f"Commune boundary shapefile is unavailable: {_SHAPEFILE}") from exc

    communes: dict[str, Commune] = {}
    for shape, record in zip(reader.iterShapes(), reader.iterRecords()):
        props = record.as_dict()
        commune_id = str(props.get("ma_xa") or "").strip()
        name = str(props.get("ten_xa") or "").strip()
        province_id = str(props.get("ma_tinh") or "").strip()
        province_name = str(props.get("ten_tinh") or "").strip()
        geometry = shape.__geo_interface__
        if not commune_id or not name or not province_id:
            raise RuntimeError("Commune boundary shapefile contains an invalid feature")
        if commune_id in communes:
            raise RuntimeError(f"Commune boundary shapefile contains duplicate ID: {commune_id}")
        communes[commune_id] = Commune(
            id=commune_id,
            name=name,
            province_id=province_id,
            province_name=province_name,
            geometry=geometry,
            bbox=_bbox_for_geometry(geometry),
        )
    if not communes:
        raise RuntimeError("Commune boundary shapefile contains no features")
    return communes


def list_communes() -> list[dict[str, str]]:
    return [
        {
            "id": commune.id,
            "name": commune.name,
            "province_id": commune.province_id,
            "province_name": commune.province_name,
        }
        for commune in sorted(_communes_by_id().values(), key=lambda item: (item.province_name, item.name))
    ]


def get_commune(commune_id: str) -> Commune | None:
    return _communes_by_id().get(commune_id)


def commune_feature(commune_id: str) -> dict[str, Any] | None:
    commune = get_commune(commune_id)
    if commune is None:
        return None
    return {
        "type": "Feature",
        "properties": {
            "id": commune.id,
            "name": commune.name,
            "province_id": commune.province_id,
            "province_name": commune.province_name,
        },
        "geometry": commune.geometry,
    }