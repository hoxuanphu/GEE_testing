"""Sentinel-2 and Landsat 8/9 surface reflectance collections with cloud masking.

Both collections provide *surface directional reflectance* (SR): reflectance
measured for the actual sun-sensor geometry of the acquisition, without BRDF
normalization.
"""
from __future__ import annotations

import datetime as dt
import math
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import ee
import requests

from .geo import Bbox, bbox_to_ee_geom, point_bbox

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
L8_COLLECTION = "LANDSAT/LC08/C02/T1_L2"
L9_COLLECTION = "LANDSAT/LC09/C02/T1_L2"
S1_COLLECTION = "COPERNICUS/S1_GRD"
# Planet NICFI Analytics Monthly Mosaics (4.77 m, bien dong region covers VN).
# LUU Y: collection nay KHONG public - can dang ky tai khoan NICFI va grant quyen
# cho service account theo huong dan:
# https://docs.planet.com/platform/integrations/google-earth-engine/nicfi-gee/
NICFI_COLLECTION = "projects/planet-nicfi/assets/basemaps/asia"
DYNAMIC_WORLD_COLLECTION = "GOOGLE/DYNAMICWORLD/V1"

S2_SCALE = 10.0
LANDSAT_SCALE = 30.0
NICFI_SCALE = 4.77
DYNAMIC_WORLD_SCALE = 10.0
S1_SCALE = 10.0

# Dynamic World label values and the official catalog palette.  Label 0 is a
# valid class (water), so masked pixels must use a separate nodata value.
DYNAMIC_WORLD_LABEL_BAND = "label"
DYNAMIC_WORLD_NODATA = 255
DYNAMIC_WORLD_PALETTE = [
    "419bdf",  # water
    "397d49",  # trees
    "88b053",  # grass
    "7a87c6",  # flooded vegetation
    "e49635",  # crops
    "dfc35a",  # shrub and scrub
    "c4281b",  # built
    "a59b8f",  # bare
    "b39fe1",  # snow and ice
]

# COPERNICUS/S2_SR_HARMONIZED already removes the processing-baseline
# 04.00 DN shift.  Only the documented scale factor remains.
_S2_OFFSET = 0.0
_S2_SCALE = 10000.0


@dataclass(frozen=True)
class SceneInfo:
    dataset: str          # key: "sentinel2" | "landsat"
    collection: str       # GEE collection id used
    scene_id: str
    date: str             # ISO date (YYYY-MM-DD)
    cloud_pct: float      # scene-level cloud percentage
    ee_object: ee.Image
    scene_count: int | None = None
    scenes: tuple[dict, ...] = ()
    estimated_seconds: int | None = None
    coverage_pct: float | None = None


class InsufficientCoverageError(ValueError):
    pass


# Share the small aggregation budget between simultaneous optical ROI requests.
_OPTICAL_AGGREGATION_LOCK = Lock()


def _coverage_fraction(image: ee.Image, dataset: str, roi: ee.Geometry) -> ee.Number:
    bands = (["B2", "B3", "B4", "B8", "B11", "B12"] if dataset == "sentinel2"
             else ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"])
    valid = image.select(bands).mask().reduce(ee.Reducer.min()).rename("valid")
    # Expand the footprint: missing pixels must count as zero across the entire ROI.
    return ee.Number(valid.unmask(0, sameFootprint=False).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=roi,
        scale=S2_SCALE if dataset == "sentinel2" else LANDSAT_SCALE,
        maxPixels=100_000_000, tileScale=4,
    ).get("valid"))


def _point_optical_scene(dataset, roi, start, end, max_cloud_pct, merge_scenes, clip_output=True):
    coll, cloud_prop, _ = _optical_collection(dataset, start, end)
    coll = coll.filterBounds(roi).filter(ee.Filter.lt(cloud_prop, max_cloud_pct))
    count = coll.size().getInfo()
    if count == 0:
        return None
    clear_fraction = _s2_clear_fraction if dataset == "sentinel2" else _landsat_clear_fraction
    scale = S2_SCALE if dataset == "sentinel2" else LANDSAT_SCALE

    # Evaluate one scene at a time: reductions inside collection.map fan out on EE.
    # Keep scores as Python values so later sorting/export cannot evaluate them again.
    candidates = coll.toList(count)
    scored = []
    for index in range(count):
        image = ee.Image(candidates.get(index))
        coverage = None
        if not merge_scenes:
            with _OPTICAL_AGGREGATION_LOCK:
                coverage = _coverage_fraction(image, dataset, roi).getInfo()
            if coverage is None or coverage < 0.99:
                continue
        with _OPTICAL_AGGREGATION_LOCK:
            info = ee.Dictionary({
                "scene_id": image.get("system:index"),
                "timestamp": image.get("system:time_start"),
                "cloud_pct": image.get(cloud_prop),
                "clear_fraction": clear_fraction(image, roi, scale),
            }).getInfo()
        # A missing QA/SCL reduction has no usable cloud score, not perfect clarity.
        clear = info.get("clear_fraction")
        scored.append((float(clear) if clear is not None else -1.0, image, coverage, info))

    if not merge_scenes:
        if not scored:
            raise InsufficientCoverageError(
                "Không có cảnh phủ ≥99% vùng đã chọn. Hãy bật ghép nhiều cảnh hoặc mở rộng khoảng ngày."
            )
        _, best, coverage, info = max(scored, key=lambda row: row[0])
        return SceneInfo(
            dataset=dataset, collection=_collection_id(dataset),
            scene_id=info["scene_id"], date=_fmt_time_start(info["timestamp"]),
            cloud_pct=float(info["cloud_pct"]) if info["cloud_pct"] is not None else -1.0,
            ee_object=best, coverage_pct=100 * coverage,
        )

    # mosaic gives priority to the LAST scene; clear pixels always beat cloudy fallbacks.
    scored.sort(key=lambda row: row[0])
    ranked = ee.ImageCollection.fromImages([row[1] for row in scored])
    mask_clouds = _mask_s2_clouds if dataset == "sentinel2" else _mask_landsat_clouds
    image = ranked.mosaic().blend(ranked.map(mask_clouds).mosaic())
    if clip_output:
        image = image.clip(roi)
    with _OPTICAL_AGGREGATION_LOCK:
        coverage = 100 * _coverage_fraction(image, dataset, roi).getInfo()
    if coverage < 99:
        raise InsufficientCoverageError(
            f"Các cảnh ghép chỉ phủ {coverage:.2f}% vùng (<99%). Hãy mở rộng khoảng ngày hoặc giảm vùng chọn."
        )
    scenes = tuple(
        {"scene_id": row[3]["scene_id"], "date": _fmt_time_start(row[3]["timestamp"])}
        for row in scored
    )
    return SceneInfo(
        dataset=dataset, collection=_collection_id(dataset),
        scene_id=f"{dataset}_point_mosaic_{start}_{end}",
        date=f"{start} to {end} (end exclusive)", cloud_pct=-1,
        ee_object=image, scene_count=len(scenes), scenes=scenes, coverage_pct=coverage,
    )


def _scale_s2(img: ee.Image) -> ee.Image:
    """Scale S2 DN to reflectance. KHÔNG mask mây — giữ nguyên mọi pixel.

    S2_SR_HARMONIZED stores harmonized integers: reflectance = DN / 10000.
    The collection has already removed the processing-baseline shift. Dividing
    by 10000.0 also forces the band type to float so getDownloadURL exports floats,
    not int32 DN. Cloud ranking is handled separately in best_scene() using
    the SCL band (không xóa pixel).
    """
    optical = (
        img.select("B.*")
        .add(_S2_OFFSET)
        .divide(_S2_SCALE)
        .float()
    )
    return img.addBands(optical, None, True).copyProperties(
        img, ["system:time_start", "system:index"]
    )


def _s2_clear_fraction(img: ee.Image, roi: ee.Geometry, scale: float) -> ee.Number:
    """Tỷ lệ pixel trong suốt trong ROI (theo SCL) — dùng để xếp hạng, không mask."""
    scl = img.select("SCL")
    clear = (
        scl.neq(3)   # cloud shadow
        .And(scl.neq(8))   # cloud medium prob.
        .And(scl.neq(9))   # cloud high prob.
        .And(scl.neq(10))  # cirrus
        .And(scl.neq(11))  # snow
    )
    return clear.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=roi,
        scale=scale,
        maxPixels=10_000_000,
        bestEffort=True,
    ).get("SCL")


def _mask_s2_clouds(img: ee.Image) -> ee.Image:
    """Mask cloudy Sentinel-2 pixels when producing a multi-scene composite."""
    scl = img.select("SCL")
    clear = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
        .And(scl.neq(11))
    )
    return img.updateMask(clear).copyProperties(img, ["system:time_start", "system:index"])


def _landsat_clouds(img: ee.Image) -> ee.Image:
    """Landsat: giữ nguyên pixel (không mask). Ranking mây tính riêng
    từ QA_PIXEL trong best_scene()."""
    return img.copyProperties(
        img, ["system:time_start", "system:index"]
    )


def _landsat_clear_fraction(img: ee.Image, roi: ee.Geometry, scale: float) -> ee.Number:
    """Tỷ lệ pixel trong suốt trong ROI (theo QA_PIXEL) — dùng để xếp hạng."""
    qa = img.select("QA_PIXEL")
    dilated = 1 << 1
    cloud = 1 << 3
    shadow = 1 << 4
    clear = (
        qa.bitwiseAnd(dilated).eq(0)
        .And(qa.bitwiseAnd(cloud).eq(0))
        .And(qa.bitwiseAnd(shadow).eq(0))
    )
    return clear.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=roi,
        scale=scale,
        maxPixels=10_000_000,
        bestEffort=True,
    ).get("QA_PIXEL")


def _mask_landsat_clouds(img: ee.Image) -> ee.Image:
    """Mask cloudy Landsat pixels when producing a multi-scene composite."""
    qa = img.select("QA_PIXEL")
    clear = (
        qa.bitwiseAnd(1 << 1).eq(0)
        .And(qa.bitwiseAnd(1 << 3).eq(0))
        .And(qa.bitwiseAnd(1 << 4).eq(0))
    )
    return img.updateMask(clear).copyProperties(img, ["system:time_start", "system:index"])


def _landsat_scale(img: ee.Image) -> ee.Image:
    """Landsat C2 L2 SR scaling: DN * 0.0000275 - 0.2."""
    optical = img.select("SR_B.*").multiply(0.0000275).add(-0.2)
    return img.addBands(optical, None, True)


def _s2_collection(start: str, end: str, platform: str = "ALL") -> ee.ImageCollection:
    collection = (
        ee.ImageCollection(S2_COLLECTION)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 90))
    )
    if platform != "ALL":
        collection = collection.filter(ee.Filter.eq("SPACECRAFT_NAME", platform))
    return collection.map(_scale_s2)


def _landsat_collection(collection_id: str, start: str, end: str) -> ee.ImageCollection:
    return (
        ee.ImageCollection(collection_id)
        .filterDate(start, end)
        .map(_landsat_scale)
    )


def _nicfi_collection(start: str, end: str, *, preserve_asset_id: bool = False) -> ee.ImageCollection:
    """Planet NICFI Analytics monthly mosaics.

    Each mosaic is a monthly cloud-free composite (no scene-level cloud pct).
    Band DN -> reflectance: DN / 10000 (Surface Reflectance basemaps).
    """
    def scale(img):
        scaled = img.divide(10000.0).float().copyProperties(
            img, ["system:time_start", "system:index"]
        )
        if preserve_asset_id:
            scaled = scaled.copyProperties(img, img.propertyNames()).set({
                "timeseries:asset_id": img.id(),
                "timeseries:collection": NICFI_COLLECTION,
            })
        return scaled

    return ee.ImageCollection(NICFI_COLLECTION).filterDate(start, end).map(scale)


def _dynamic_world_collection(start: str, end: str) -> ee.ImageCollection:
    """Dynamic World label/probability predictions for the date range.

    Dynamic World is already a cloud/cloud-shadow-masked prediction product,
    so there is no source-scene cloud metadata filter to apply here.  Keep all
    bands because the label is used for downloads while the probability bands
    remain available for future confidence-aware composites.
    """
    return ee.ImageCollection(DYNAMIC_WORLD_COLLECTION).filterDate(start, end)


# Sentinel-1 polarization modes supported by the GEE S1_GRD collection.
S1_POLARIZATIONS = {
    "vvvh": ["VV", "VH"],
    "vv": ["VV"],
    "hhhv": ["HH", "HV"],
    "hh": ["HH"],
}


def _s1_collection(start: str, end: str, polarization: str) -> ee.ImageCollection:
    """Sentinel-1 GRD: chọn polarization, clamp dải động dB.

    GEE S1_GRD đã ở đơn vị dB (sigma0 dB, đã hiệu chuẩn; giá trị thực đo
    trên An Giang: -28..+17) — KHÔNG áp log10 thêm (log10 của số âm = NaN,
    đã từng làm toàn bộ pixel ra -inf). Chỉ clamp về dải -30..+10 dB hợp lý
    cho hiển thị/phân tích.
    """
    bands = S1_POLARIZATIONS[polarization]

    def to_db(img: ee.Image) -> ee.Image:
        sel = img.select(bands)
        db = (
            sel.clamp(-30.0, 10.0)  # giới hạn dải động hợp lý
            .float()
        )
        return db.copyProperties(img, ["system:time_start", "system:index"])

    return (
        ee.ImageCollection(S1_COLLECTION)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", bands[0]))
        .filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .map(to_db)
    )


def timeseries_source_metadata(dataset: str, polarization: str = "vvvh") -> dict:
    """Describe exported bands, nominal resolution and source value conversion."""
    sources = {
        "sentinel2": (["B2", "B3", "B4", "B8", "B11", "B12"], S2_SCALE, "reflectance", 0.0001, 0.0),
        "landsat": (["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"], LANDSAT_SCALE, "reflectance", 0.0000275, -0.2),
        "nicfi": (["B", "G", "R", "N"], NICFI_SCALE, "reflectance", 0.0001, 0.0),
        "dynamic_world": ([DYNAMIC_WORLD_LABEL_BAND], DYNAMIC_WORLD_SCALE, "class", 1.0, 0.0),
        "s1gee": (S1_POLARIZATIONS.get(polarization, []), S1_SCALE, "dB", 1.0, 0.0),
    }
    if dataset not in sources:
        raise ValueError(f"Unsupported time series dataset: {dataset}")
    bands, scale, units, factor, offset = sources[dataset]
    if not bands:
        raise ValueError(f"Unsupported Sentinel-1 polarization: {polarization}")
    result = {"bands": list(bands), "scale_m": scale, "units": units,
              "value_scale": factor, "value_offset": offset,
              "nodata": DYNAMIC_WORLD_NODATA if dataset == "dynamic_world" else None}
    if dataset == "s1gee":
        result.update(polarization=polarization, clamp_db=[-30.0, 10.0])
    if dataset == "nicfi":
        result["composite"] = "source-period"
    return result


def _process_timeseries_image(image: ee.Image, dataset: str, polarization: str) -> ee.Image:
    bands = timeseries_source_metadata(dataset, polarization)["bands"]
    if dataset == "sentinel2":
        processed = _scale_s2(image)
    elif dataset == "landsat":
        processed = _landsat_scale(image)
    elif dataset == "nicfi":
        processed = image.divide(10000.0).float()
    elif dataset == "s1gee":
        processed = image.select(bands).clamp(-30.0, 10.0).float()
    else:
        processed = image.select(DYNAMIC_WORLD_LABEL_BAND).toUint8()
    # copyProperties can return an ee.Element; restore the image type for band operations.
    return ee.Image(ee.Image(processed).select(bands).copyProperties(image, image.propertyNames()))


def timeseries_collection(
    dataset: str, start_date: str, end_exclusive: str, polarization: str = "vvvh",
) -> ee.ImageCollection:
    """All source scenes; caller converts inclusive end dates to exclusive dates.

    No cloud ranking, cloud thresholds, orbit preference or temporal composites.
    Full asset IDs are stored independently of EE transformations/merged indexes.
    """
    timeseries_source_metadata(dataset, polarization)

    def prepare(image: ee.Image, collection_id: str) -> ee.Image:
        asset_id = image.id()
        return _process_timeseries_image(image, dataset, polarization).set({
            "timeseries:asset_id": asset_id,
            "timeseries:collection": collection_id,
        })

    if dataset == "nicfi":
        return _nicfi_collection(start_date, end_exclusive, preserve_asset_id=True).select(
            ["B", "G", "R", "N"]
        )
    if dataset == "dynamic_world":
        return _dynamic_world_collection(start_date, end_exclusive).map(
            lambda image: prepare(image, DYNAMIC_WORLD_COLLECTION)
        )
    collection_ids = {
        "sentinel2": [S2_COLLECTION], "landsat": [L8_COLLECTION, L9_COLLECTION],
        "s1gee": [S1_COLLECTION],
    }[dataset]
    collections = []
    for collection_id in collection_ids:
        collection = ee.ImageCollection(collection_id).filterDate(start_date, end_exclusive)
        if dataset == "s1gee":
            collection = collection.filter(ee.Filter.eq("instrumentMode", "IW"))
            for band in S1_POLARIZATIONS[polarization]:
                collection = collection.filter(ee.Filter.listContains("transmitterReceiverPolarisation", band))
        collections.append(collection.map(lambda image, cid=collection_id: prepare(image, cid)))
    result = collections[0]
    for collection in collections[1:]:
        result = result.merge(collection)
    return result


def list_timeseries_scenes(
    dataset: str, lon: float, lat: float, start_date: str, end_exclusive: str,
    polarization: str = "vvvh",
    roi: ee.Geometry | None = None,
    cloud_min: float | None = None,
    cloud_max: float | None = None,
) -> list[dict]:
    """Return scene metadata; optional scene-cloud filters apply before enumeration."""
    point = ee.Geometry.Point([lon, lat])
    collection = timeseries_collection(dataset, start_date, end_exclusive, polarization).filterBounds(
        roi if roi is not None else point)
    if roi is None:
        collection = collection.map(
            lambda image: image.set("timeseries:contains_center", image.geometry().contains(point, ee.ErrorMargin(1)))
        ).filter(ee.Filter.eq("timeseries:contains_center", True))
    source_properties = ["SPACECRAFT_NAME", "SPACECRAFT_ID", "LANDSAT_PRODUCT_ID",
                         "MGRS_TILE", "WRS_PATH", "WRS_ROW", "cadence",
                         "orbitProperties_pass", "relativeOrbitNumber_start",
                         "orbitNumber_start", "platform_number", "instrumentMode",
                         "transmitterReceiverPolarisation"]
    cloud_property = {"sentinel2": "CLOUDY_PIXEL_PERCENTAGE", "landsat": "CLOUD_COVER"}.get(dataset)

    if cloud_property and (cloud_min is not None or cloud_max is not None):
        if cloud_min is not None:
            collection = collection.filter(ee.Filter.gte(cloud_property, cloud_min))
        if cloud_max is not None:
            collection = collection.filter(ee.Filter.lte(cloud_property, cloud_max))

    def metadata(value):
        image = ee.Image(value)
        return ee.Dictionary({
            "asset_id": image.get("timeseries:asset_id"),
            "collection": image.get("timeseries:collection"),
            "timestamp_ms": image.get("system:time_start"),
            "cloud_pct": image.get(cloud_property) if cloud_property else None,
            "source_metadata": image.toDictionary(source_properties),
        })

    rows = collection.toList(collection.size()).map(metadata).getInfo()
    scenes = {}
    for row in rows:
        timestamp = dt.datetime.fromtimestamp(row.pop("timestamp_ms") / 1000, tz=dt.timezone.utc)
        row.update(dataset=dataset, timestamp=timestamp.isoformat().replace("+00:00", "Z"),
                   date=timestamp.date().isoformat())
        cloud = row.get("cloud_pct")
        row["cloud_pct"] = float(cloud) if cloud is not None and 0 <= cloud <= 100 else None
        scenes.setdefault(row["asset_id"], row)
    return sorted(scenes.values(), key=lambda row: (
        dt.datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00")), row["asset_id"]
    ))


def load_timeseries_scene(
    asset_id: str, dataset: str, polarization: str = "vvvh",
) -> ee.Image:
    """Process an exact snapshot asset without reselecting a best/latest scene."""
    timeseries_source_metadata(dataset, polarization)
    allowed = {"sentinel2": [S2_COLLECTION], "landsat": [L8_COLLECTION, L9_COLLECTION],
               "nicfi": [NICFI_COLLECTION], "dynamic_world": [DYNAMIC_WORLD_COLLECTION],
               "s1gee": [S1_COLLECTION]}[dataset]
    if not isinstance(asset_id, str) or not any(
        asset_id.startswith(collection + "/") and
        asset_id[len(collection) + 1:] and "/" not in asset_id[len(collection) + 1:]
        and asset_id[len(collection) + 1:] not in {".", ".."}
        and "\\" not in asset_id for collection in allowed
    ):
        raise ValueError("Asset ID does not belong to the requested dataset")
    return _process_timeseries_image(ee.Image(asset_id), dataset, polarization)


def best_s1_scene(
    lon: float,
    lat: float,
    radius_m: float,
    start_date: str,
    end_date: str,
    polarization: str = "vvvh",
    roi: ee.Geometry | None = None,
) -> SceneInfo | None:
    """Return the newest S1 GRD scene covering the point within the date range.

    Unlike optical, SAR penetrates clouds, so we simply take the most recent
    scene (no cloud scoring needed).
    """
    point = roi if roi is not None else ee.Geometry.Point([lon, lat])
    coll = _s1_collection(start_date, end_date, polarization).filterBounds(point)
    best = _pick_s1_covering(coll, point)
    if best is None:
        return None
    return SceneInfo(
        dataset="s1gee",
        collection=S1_COLLECTION,
        scene_id=best.get("system:index").getInfo(),
        date=ee.Date(best.get("system:time_start")).format("YYYY-MM-dd").getInfo(),
        cloud_pct=-1.0,  # SAR không có khái niệm mây
        ee_object=best,
    )


def _pick_s1_covering(
    coll: ee.ImageCollection, roi: ee.Geometry
) -> ee.Image | None:
    """Chọn cảnh S1 phủ ROI tốt nhất: ưu tiên độ phủ, DESC, rồi mới nhất.

    filterBounds chỉ cần footprint CHẠM ROI — với ROI lớn (cấp tỉnh) các
    cảnh chạm mép có thể phủ <5% diện tích, trong khi cảnh cùng ngày từ
    track khác phủ trọn. Ta tính tỷ lệ pixel có mask trong ROI (thang
    thô 1 km cho nhanh), xếp hạng: cover DESC → orbit DESC → mới nhất.
    Giữ tối đa 12 cảnh có cover cao nhất để giảm số lời gọi reduceRegion.
    """
    if coll.size().getInfo() == 0:
        return None

    def add_cover(img: ee.Image) -> ee.Image:
        frac = (
            img.select(0)
            .mask()
            .reduceRegion(
                ee.Reducer.mean(), roi, 1000, bestEffort=True, maxPixels=1e7
            )
            .get(img.bandNames().get(0))
        )
        return img.set("s1_cover", frac)

    scored = coll.map(add_cover).sort("s1_cover", False).limit(12)
    # orbit ASC làm hạng phụ: cảnh DESC (góc nhìn ổn định cho thay đổi đất)
    # xếp trước cảnh ASC cùng mức phủ.
    desc = scored.filter(ee.Filter.eq("orbitProperties_pass", "DESCENDING"))
    if desc.size().getInfo() > 0:
        scored = desc
    return ee.Image(scored.sort("system:time_start", False).first())


def best_s1_scene_for_roi(
    roi: ee.Geometry,
    start_date: str,
    end_date: str,
    polarization: str = "vvvh",
) -> SceneInfo | None:
    """Return the S1 scene covering the largest fraction of the ROI (newest first)."""
    coll = _s1_collection(start_date, end_date, polarization).filterBounds(roi)
    best = _pick_s1_covering(coll, roi)
    if best is None:
        return None
    return SceneInfo(
        dataset="s1gee",
        collection=S1_COLLECTION,
        scene_id=best.get("system:index").getInfo(),
        date=ee.Date(best.get("system:time_start")).format("YYYY-MM-dd").getInfo(),
        cloud_pct=-1.0,
        ee_object=best,
    )


def _least_cloudy_per_tile(
    coll: ee.ImageCollection,
    dataset: str,
    cloud_prop: str,
    roi: ee.Geometry | None = None,
    keep_clouds: bool = False,
) -> tuple[ee.Image, list[dict]]:
    """Với mỗi tile phủ ROI, lấy 1 scene trong rồi mosaic lại.

    Mỗi tile đóng góp đúng một scene nên kết quả không có hiện tượng
    ``qualityMosaic`` đổi ngày độc lập ở từng pixel như medoid. Khi có ROI,
    xếp hạng theo mean băng xanh (haze) trước; ``CLOUDY_PIXEL_PERCENTAGE``
    vẫn là bộ lọc đầu vào nhưng không đủ để nhận ra khói/sương. Cảnh đã chọn
    được SCL/QA-mask trước khi mosaic để mây residual không thành mảng màu.

    ``keep_clouds=True`` (method "raw"): KHÔNG mask pixel mây — mosaic giữ
    nguyên cảnh như cảm biến ghi, đẹp về thị giác, mây hiện nguyên vẹn.
    """
    ranked = (
        _haze_rank(
            coll,
            "B2" if dataset == "sentinel2" else "SR_B2",
            roi,
        )
        if roi is not None
        else coll
    )
    mask_scene = _mask_s2_clouds if dataset == "sentinel2" else _mask_landsat_clouds

    if dataset == "sentinel2":
        tiles = sorted(set(ranked.aggregate_array("MGRS_TILE").getInfo()))
        tile_props: list[list[tuple[str, object]]] = [
            [("MGRS_TILE", t)] for t in tiles
        ]
    else:  # landsat — nhóm theo cặp WRS path/row
        distinct = ranked.distinct(["WRS_PATH", "WRS_ROW"])
        paths = distinct.aggregate_array("WRS_PATH").getInfo()
        rows = distinct.aggregate_array("WRS_ROW").getInfo()
        tile_props = [
            [("WRS_PATH", p), ("WRS_ROW", r)] for p, r in zip(paths, rows)
        ]

    picked: list[tuple[str, ee.Image]] = []
    infos: list[dict] = []
    for props in tile_props:
        sub = ranked
        for prop, value in props:
            sub = sub.filter(ee.Filter.eq(prop, value))
        sort_prop = "hazeScore" if roi is not None else cloud_prop
        best = ee.Image(sub.sort(sort_prop).first())
        info = best.getInfo()["properties"]
        date = dt.datetime.fromtimestamp(
            info["system:time_start"] / 1000, dt.timezone.utc
        ).strftime("%Y-%m-%d")
        # Mask only after selecting the scene so the selection itself still
        # uses the complete ROI haze score; mosaic then prefers valid pixels.
        # raw: giữ nguyên pixel mây — không mask cảnh được chọn.
        picked.append((date, best if keep_clouds else mask_scene(best)))
        infos.append(
            {
                "scene_id": info["system:index"],
                "date": date,
                "cloud": float(info.get(cloud_prop, -1.0)),
            }
        )

    # Mosaic theo thứ tự ngày để vùng chồng lấp giữa các tile lấy ảnh mới nhất.
    picked.sort(key=lambda p: p[0])
    image = ee.ImageCollection([img for _, img in picked]).mosaic()
    return image, infos


# Base-score chỉ dùng một số cảnh dự phòng tốt nhất cho mỗi tile. Giữ số này
# nhỏ để cảnh nền quyết định màu của phần lớn tile mà không làm graph GEE phình
# lên khi tải cả tỉnh.
BASE_SCORE_MAX_SCENES_PER_TILE = 6


def _base_score_per_tile(
    coll: ee.ImageCollection,
    dataset: str,
    cloud_prop: str,
    roi: ee.Geometry,
) -> tuple[ee.Image, list[dict]]:
    """Ghép theo ``base-score``: cảnh tốt nhất làm nền, cảnh khác chỉ lấp mây.

    Score thấp hơn nghĩa là cảnh trong hơn (mean băng xanh thấp hơn). Trong
    từng MGRS/WRS tile, chỉ giữ vài cảnh có score thấp nhất. Sau khi mask mây,
    các cảnh được đưa vào mosaic theo thứ tự xấu -> tốt để cảnh tốt nhất nằm
    trên cùng và chỉ bị thay thế tại pixel đã mask. Như vậy phần lớn một tile
    đến từ cùng một ngày, khác với qualityMosaic/medoid là có thể đổi ngày ở
    từng pixel.
    """
    blue = "B2" if dataset == "sentinel2" else "SR_B2"
    ranked = _haze_rank(coll, blue, roi)
    mask_scene = _mask_s2_clouds if dataset == "sentinel2" else _mask_landsat_clouds

    if dataset == "sentinel2":
        tiles = sorted(set(ranked.aggregate_array("MGRS_TILE").getInfo()))
        tile_props: list[list[tuple[str, object]]] = [
            [("MGRS_TILE", t)] for t in tiles
        ]
    else:  # Landsat — nhóm theo cặp WRS path/row.
        distinct = ranked.distinct(["WRS_PATH", "WRS_ROW"])
        paths = distinct.aggregate_array("WRS_PATH").getInfo()
        rows = distinct.aggregate_array("WRS_ROW").getInfo()
        tile_props = [
            [("WRS_PATH", p), ("WRS_ROW", r)] for p, r in zip(paths, rows)
        ]

    tile_images: list[ee.Image] = []
    infos: list[dict] = []
    for props in tile_props:
        sub = ranked
        for prop, value in props:
            sub = sub.filter(ee.Filter.eq(prop, value))
        count = min(sub.size().getInfo(), BASE_SCORE_MAX_SCENES_PER_TILE)
        if count == 0:
            continue

        # limit lấy các cảnh tốt nhất; sort ngược lại trước mosaic vì GEE ưu
        # tiên ảnh cuối cùng còn valid, tức cảnh tốt nhất phải đứng cuối.
        selected = sub.sort("hazeScore", True).limit(count)
        masked = selected.map(
            lambda img: mask_scene(img).copyProperties(
                img, ["system:time_start", "system:index", "hazeScore"]
            )
        )
        tile_images.append(masked.sort("hazeScore", False).mosaic())

        ids = selected.aggregate_array("system:index").getInfo()
        times = selected.aggregate_array("system:time_start").getInfo()
        clouds = (
            selected.aggregate_array(cloud_prop).getInfo()
            if cloud_prop
            else [-1.0] * count
        )
        scores = selected.aggregate_array("hazeScore").getInfo()
        for scene_id, timestamp, cloud, score in zip(ids, times, clouds, scores):
            date = dt.datetime.fromtimestamp(
                timestamp / 1000, dt.timezone.utc
            ).strftime("%Y-%m-%d")
            infos.append(
                {
                    "scene_id": scene_id,
                    "date": date,
                    "cloud": float(cloud if cloud is not None else -1.0),
                    "score": round(float(score), 6),
                }
            )

    if not tile_images:
        raise RuntimeError("No tile has a usable base-score scene")
    return ee.ImageCollection(tile_images).mosaic(), infos


# Số băng tải về cho mỗi dataset — dùng để ước lượng thời gian median.
_MEDIAN_BAND_COUNTS = {"sentinel2": 6, "landsat": 6, "nicfi": 4}


def _dynamic_world_scene_info(
    coll: ee.ImageCollection,
    roi: ee.Geometry,
    start_date: str,
    end_date: str,
    size: int,
    composite: bool,
) -> SceneInfo:
    """Build a point image or a categorical date-range DW composite.

    ``Reducer.mode`` is used for province mode: every output pixel is the
    class occurring most often in the selected Dynamic World observations.
    Reducer outputs do not reliably retain the native 10 m projection, so the
    result explicitly inherits the projection of a native label image.
    """
    newest = ee.Image(coll.sort("system:time_start", False).first())
    newest_label = newest.select(DYNAMIC_WORLD_LABEL_BAND)
    if not composite:
        return SceneInfo(
            dataset="dynamic_world",
            collection=DYNAMIC_WORLD_COLLECTION,
            scene_id=newest.get("system:index").getInfo(),
            date=ee.Date(newest.get("system:time_start")).format("YYYY-MM-dd").getInfo(),
            cloud_pct=-1.0,
            ee_object=newest_label,
            scene_count=None,
        )

    labels = coll.select(DYNAMIC_WORLD_LABEL_BAND)
    mode = (
        labels.reduce(ee.Reducer.mode())
        .rename(DYNAMIC_WORLD_LABEL_BAND)
        .setDefaultProjection(newest_label.projection())
        .clip(roi)
    )
    first_ts = coll.aggregate_min("system:time_start").getInfo()
    last_ts = coll.aggregate_max("system:time_start").getInfo()
    date_min = _fmt_time_start(first_ts)
    date_max = _fmt_time_start(last_ts)
    return SceneInfo(
        dataset="dynamic_world",
        collection=DYNAMIC_WORLD_COLLECTION,
        scene_id=f"dynamic_world_mode_{start_date}_{end_date}",
        date=f"{date_min} → {date_max} ({size} cảnh)",
        cloud_pct=-1.0,
        ee_object=mode,
        scene_count=size,
    )


def _collection_id(dataset: str) -> str:
    """Return the collection identifier used in API metadata."""
    return {
        "sentinel2": S2_COLLECTION,
        "landsat": "landsat_merge",
        "nicfi": NICFI_COLLECTION,
        "dynamic_world": DYNAMIC_WORLD_COLLECTION,
    }[dataset]


# Số băng tải về cho mỗi dataset — dùng để ước lượng thời gian median.


def _estimate_median_seconds(
    scene_count: int, area_m2: float, scale: float, band_count: int
) -> int:
    """Ước lượng thô thời gian (giây) tính median + tải GeoTIFF.

    Heuristic: EE tính median ~2e6 pixel·scene/giây (median hội tụ nhanh sau
    ~30 cảnh nên cap lại), còn tải GeoTIFF qua getDownloadURL ~0.5 MP/s mỗi
    band. Chỉ dùng cho hộp thoại xác nhận — con số thực tế có thể chênh lệch.
    """
    pixels = area_m2 / (scale * scale)
    compute = pixels * min(scene_count, 30) / 2e6
    download = pixels * band_count / 5e5
    return int(max(60.0, compute + download + 60.0))


def _optical_collection(
    dataset: str, start: str, end: str, platform: str = "ALL"
) -> tuple[ee.ImageCollection, str | None, float]:
    """Return (collection, cloud metadata property, scale) for a dataset."""
    if dataset == "sentinel2":
        return _s2_collection(start, end), "CLOUDY_PIXEL_PERCENTAGE", S2_SCALE
    if dataset == "landsat":
        l8 = _landsat_collection(L8_COLLECTION, start, end)
        l9 = _landsat_collection(L9_COLLECTION, start, end)
        return l8.merge(l9), "CLOUD_COVER", LANDSAT_SCALE
    if dataset == "nicfi":
        return _nicfi_collection(start, end), None, NICFI_SCALE
    if dataset == "dynamic_world":
        return _dynamic_world_collection(start, end), None, DYNAMIC_WORLD_SCALE
    raise ValueError(f"Unknown dataset: {dataset}")


def composite_plan(
    dataset: str,
    roi: ee.Geometry,
    start_date: str,
    end_date: str,
    method: str = "medoid",
    max_cloud_pct: float = 60.0,
) -> dict | None:
    """Lập kế hoạch composite mà KHÔNG chạy: trả về số cảnh, khoảng ngày và
    (mosaic) danh sách cảnh từng tile kèm % mây, hoặc (median/medoid) thời
    gian ước lượng. Trả về None nếu không có cảnh nào hợp lệ."""
    coll, cloud_prop, scale = _optical_collection(dataset, start_date, end_date)
    coll = coll.filterBounds(roi)
    if cloud_prop is not None:
        coll = coll.filter(ee.Filter.lt(cloud_prop, max_cloud_pct))

    size = coll.size().getInfo()
    if size == 0:
        return None

    times = coll.aggregate_array("system:time_start").getInfo()
    dates = sorted(
        dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).strftime("%Y-%m-%d")
        for t in times
    )

    plan: dict = {
        "dataset": dataset,
        "method": method,
        "scene_count": size,
        "candidate_scene_count": size,
        "selected_scene_count": size,
        "date_min": dates[0],
        "date_max": dates[-1],
        "scale_m": scale,
    }

    if method in ("mosaic", "raw"):
        if cloud_prop is None:  # nicfi — composite tháng, không có mây theo cảnh
            plan["scenes"] = []
            return plan
        _image, infos = _least_cloudy_per_tile(
            coll, dataset, cloud_prop, roi, keep_clouds=(method == "raw")
        )
        plan["scenes"] = infos
        plan["selected_scene_count"] = len(infos)
        plan["scene_count"] = len(infos)
        return plan

    if method == "base-score":
        if cloud_prop is None:
            plan["scenes"] = []
            return plan
        _image, infos = _base_score_per_tile(coll, dataset, cloud_prop, roi)
        plan["scenes"] = infos
        plan["selected_scene_count"] = len(infos)
        plan["scene_count"] = len(infos)
        return plan

    # median/medoid — composite thực tế chỉ giữ tối đa COMPOSITE_MAX_SCENES
    # cảnh trong nhất (xếp hạng haze chạy lúc build, không chạy thêm trong plan).
    selected_size = min(size, COMPOSITE_MAX_SCENES) if cloud_prop else size
    plan["selected_scene_count"] = selected_size
    area_m2 = float(roi.area(maxError=1).getInfo())
    plan["area_km2"] = round(area_m2 / 1e6, 1)
    plan["estimated_seconds"] = _estimate_median_seconds(
        selected_size, area_m2, scale, _MEDIAN_BAND_COUNTS.get(dataset, 6)
    )
    return plan


def _fmt_time_start(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


# Composite chỉ dùng tối đa N cảnh TRONG NHẤT (xem _composite_scene_info):
# vừa tránh "User memory limit exceeded" (bộ nhớ GEE ≈ số cảnh × MP của tile,
# ngưỡng ~120–140), vừa bỏ cảnh nhiều khói — mùa đốt rơm ĐBSCL (tháng 3–5)
# hơn một nửa cảnh bị khói phủ mà CLOUDY_PIXEL_PERCENTAGE không nhận ra
# (khói không phải mây), khiến median/medoid trước đây bị xám, phai.
COMPOSITE_MAX_SCENES = 20
# Medoid còn cần mỗi tile fetch nhỏ (scene × triệu pixel ≈ ngưỡng ~120): 20 cảnh
# × 4 MP = 80, dư biên an toàn cho tile nhiều dữ liệu (đo ở An Giang: 20 ×
# 8.9 MP có tile OK có tile 400).
MEDOID_MAX_TILE_PIXELS = 4_000_000


def _medoid(coll: ee.ImageCollection, bands: list[str]) -> ee.Image:
    """Medoid composite: pixel THẬT của cảnh gần trung vị nhất.

    Median trộn giá trị giữa nhiều cảnh (pixel "không thuộc cảnh nào") nên
    màu bị phai; medoid tính khoảng cách mỗi pixel tới median rồi chọn NGUYÊN
    BỘ giá trị của cảnh có khoảng cách nhỏ nhất — màu sắc giữ đúng một cảnh
    gốc.

    Dùng qualityMosaic với quality = -khoảng_cách: qualityMosaic là reducer
    streaming, mỗi pixel chỉ giữ bộ giá trị của cảnh tốt nhất đang có (không
    giữ stack mọi cảnh), nên chạy được cả tile cấp tỉnh. Các cách khác đã thử
    và đều chết "User memory limit exceeded" (400) ở vùng 45 cảnh:
    - Reducer.min(numInputs): giữ scene_count × bands giá trị/pixel (~22 GB).
    - min(dist) + updateMask + mosaic: vẫn materialize cả stack ảnh masked.
    """
    sel = coll.select(bands)
    median = sel.median()

    def _with_dist(img: ee.Image) -> ee.Image:
        dist = img.subtract(median).pow(2).reduce(ee.Reducer.sum())
        # Tên băng KHÔNG được bắt đầu bằng "_" — GEE từ chối với
        # "Image.rename: Invalid band name" (đã từng dính khi đặt "__dist").
        return img.addBands(dist.multiply(-1).rename("medoidQ"))

    with_q = sel.map(_with_dist)
    # qualityMosaic chọn cảnh có quality LỚN NHẤT = khoảng cách nhỏ nhất.
    medoid_img = with_q.qualityMosaic("medoidQ")
    return medoid_img.select(bands)


def _haze_rank(
    coll: ee.ImageCollection, blue_band: str, roi: ee.Geometry
) -> ee.ImageCollection:
    """Gắn điểm "độ trong" cho từng cảnh: mean băng xanh trên ROI (thang 1 km).

    Khói/aerosol và mây residuals cộng path radiance chủ yếu vào băng xanh
    nên cảnh bị khói có mean xanh cao; cảnh trong có mean xanh thấp (thậm chí
    âm sau offset). CLOUDY_PIXEL_PERCENTAGE là metadata của TOÀN cảnh theo
    thuật toán mây — không thấy khói, trong khi mean xanh đo đúng trên ROI
    của người dùng.
    """
    def _score(img: ee.Image) -> ee.Image:
        # unmask(0.3): pixel bị mask tính là sáng (~0.3 ≈ mây dày) để cảnh
        # nhiều mây tự động xuống hạng; default cũng tránh hazeScore=null.
        mean = (
            img.select(blue_band).unmask(0.3)
            .reduceRegion(ee.Reducer.mean(), roi, 1000, bestEffort=True)
            .get(blue_band, 0.3)
        )
        return img.set("hazeScore", mean)

    return coll.map(_score)


def _composite_scene_info(
    dataset: str,
    coll: ee.ImageCollection,
    cloud_prop: str | None,
    roi: ee.Geometry,
    start_date: str,
    end_date: str,
    size: int,
    method: str,
) -> SceneInfo:
    """Composite của các cảnh trong coll (mask mây từng cảnh trước).

    method="median" — trung vị per-pixel;
    method="medoid" — pixel thật của cảnh gần trung vị nhất (màu trung thực).

    Cả hai method đều chỉ dùng tối đa COMPOSITE_MAX_SCENES cảnh TRONG NHẤT
    (xếp hạng bằng mean băng xanh trên ROI — xem _haze_rank): mùa khói,
    median của mọi cảnh chính là pixel bị khói, mà medoid lại chọn cảnh gần
    median nhất nên cả hai đều xám nếu không bỏ cảnh khói trước. Giới hạn này
    cũng giữ bộ nhớ GEE dưới ngưỡng (scene × MP của tile, xem
    MEDOID_MAX_TILE_PIXELS).
    """
    if cloud_prop and size > COMPOSITE_MAX_SCENES:
        blue = "B2" if dataset == "sentinel2" else "SR_B2"
        coll = _haze_rank(coll, blue, roi).sort("hazeScore").limit(
            COMPOSITE_MAX_SCENES
        )
        size = coll.size().getInfo()
    if dataset == "sentinel2":
        masked = coll.map(_mask_s2_clouds)
        bands = ["B2", "B3", "B4", "B8", "B11", "B12"]
    else:
        masked = coll.map(_mask_landsat_clouds)
        bands = ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]
    if method == "medoid":
        composite = _medoid(masked, bands).clip(roi)
    else:
        composite = masked.median().clip(roi)

    date = (
        f"{_fmt_time_start(coll.aggregate_min('system:time_start').getInfo())} → "
        f"{_fmt_time_start(coll.aggregate_max('system:time_start').getInfo())}"
    )
    cloud = float(coll.aggregate_mean(cloud_prop).getInfo()) if cloud_prop else -1.0
    return SceneInfo(
        dataset=dataset,
        collection={
            "sentinel2": S2_COLLECTION,
            "landsat": "landsat_merge",
            "nicfi": NICFI_COLLECTION,
        }[dataset],
        scene_id=f"{dataset}_{method}_{start_date}_{end_date}",
        date=f"{date} ({size} cảnh)",
        cloud_pct=cloud,
        ee_object=composite,
        scene_count=size,
        estimated_seconds=_estimate_median_seconds(
            size,
            float(roi.area(maxError=1).getInfo()),
            {
                "sentinel2": S2_SCALE,
                "landsat": LANDSAT_SCALE,
            }[dataset],
            _MEDIAN_BAND_COUNTS.get(dataset, 6),
        ),
    )


def best_scene_for_roi(
    dataset: str,
    roi: ee.Geometry,
    start_date: str,
    end_date: str,
    *,
    composite: bool = False,
    composite_method: str = "medoid",
    max_cloud_pct: float = 60.0,
) -> SceneInfo | None:
    """Return an image over ``roi``.

    Point mode gets the least-cloudy single scene. Province mode composites:
    ``composite_method="medoid"`` (default) picks, per pixel, the actual band
    values of the scene closest to the median — màu trung thực, không phai;
    ``composite_method="median"`` takes the per-pixel median of the selected
    cloud-masked scenes — khử mây residual tốt hơn nhưng màu có thể phai.
    Khi có quá nhiều cảnh, cả hai phương pháp ưu tiên tối đa 20 cảnh có mean
    băng xanh thấp nhất trên ROI (ít sương/khói nhất), thay vì chỉ dùng
    metadata cloud percentage;
    ``composite_method="mosaic"`` mosaics the least-cloudy scene of
    each tile covering the ROI (see ``_least_cloudy_per_tile``).
    ``composite_method="raw"`` giống mosaic nhưng KHÔNG mask mây — mỗi tile
    lấy 1 cảnh trong nhất rồi ghép nguyên gốc, mây giữ nguyên (đẹp thị giác).
    ``composite_method="base-score"`` puts the best-scored masked scene on
    top of a small fallback stack per tile, so fallbacks only fill its cloud
    holes and do not replace clear base pixels.
    """
    coll, cloud_prop, scale = _optical_collection(dataset, start_date, end_date)

    coll = coll.filterBounds(roi)
    if cloud_prop is not None:
        coll = coll.filter(ee.Filter.lt(cloud_prop, max_cloud_pct))

    size = coll.size().getInfo()
    if size == 0:
        return None

    if dataset == "dynamic_world":
        return _dynamic_world_scene_info(
            coll, roi, start_date, end_date, size, composite
        )

    if composite:
        if composite_method in ("median", "medoid") and dataset in (
            "sentinel2",
            "landsat",
        ):
            return _composite_scene_info(
                dataset, coll, cloud_prop, roi, start_date, end_date, size,
                composite_method,
            )
        if composite_method == "base-score" and dataset in (
            "sentinel2",
            "landsat",
        ):
            image, picked = _base_score_per_tile(coll, dataset, cloud_prop, roi)
            return SceneInfo(
                dataset=dataset,
                collection={
                    "sentinel2": S2_COLLECTION,
                    "landsat": "landsat_merge",
                    "nicfi": NICFI_COLLECTION,
                }[dataset],
                scene_id=f"{dataset}_base-score_{start_date}_{end_date}",
                date=", ".join(sorted({p["date"] for p in picked})),
                cloud_pct=max((p["cloud"] for p in picked), default=-1.0),
                ee_object=image.clip(roi),
                scene_count=len(picked),
                scenes=tuple(picked),
            )
        if dataset in ("sentinel2", "landsat"):
            is_raw = composite_method == "raw"
            image, picked = _least_cloudy_per_tile(
                coll, dataset, cloud_prop, roi, keep_clouds=is_raw
            )
            return SceneInfo(
                dataset=dataset,
                collection={
                    "sentinel2": S2_COLLECTION,
                    "landsat": "landsat_merge",
                    "nicfi": NICFI_COLLECTION,
                }[dataset],
                scene_id=f"{dataset}_{'raw' if is_raw else 'mosaic'}_{start_date}_{end_date}",
                date=", ".join(sorted({p["date"] for p in picked})),
                cloud_pct=max((p["cloud"] for p in picked), default=-1.0),
                ee_object=image.clip(roi),
                scene_count=len(picked),
                scenes=tuple(
                    {
                        "scene_id": p["scene_id"],
                        "date": p["date"],
                        "cloud": p["cloud"],
                    }
                    for p in picked
                ),
            )
        image = ee.Image(coll.sort("system:time_start", False).first())
        return SceneInfo(
            dataset=dataset,
            collection={
                "sentinel2": S2_COLLECTION,
                "landsat": "landsat_merge",
                "nicfi": NICFI_COLLECTION,
            }[dataset],
            scene_id=f"{dataset}_composite_{start_date}_{end_date}",
            date=f"{start_date} to {end_date}",
            cloud_pct=-1.0,
            ee_object=image.clip(roi),
        )

    # Xếp hạng mây theo tỷ lệ pixel trong suốt trong ROI, tính TRỰC TIẾP từ
    # SCL (S2) / QA_PIXEL (Landsat) — không xóa pixel mây khỏi ảnh tải về.
    # bestEffort=True lets EE aggregate at a coarser scale when the ROI is large.
    if dataset == "sentinel2":
        def scene_score(img: ee.Image) -> ee.Image:
            return img.set(
                "roi_clear_fraction",
                _s2_clear_fraction(img, roi, scale),
            )
    elif dataset == "landsat":
        def scene_score(img: ee.Image) -> ee.Image:
            return img.set(
                "roi_clear_fraction",
                _landsat_clear_fraction(img, roi, scale),
            )
    else:  # nicfi — composite đã khử mây, dùng thời gian mới nhất
        def scene_score(img: ee.Image) -> ee.Image:
            return img.set("roi_clear_fraction", 1.0)

    scored = coll.map(scene_score)
    best = ee.Image(scored.sort("roi_clear_fraction", False).first())

    info = {
        "scene_id": best.get("system:index").getInfo(),
        "date": ee.Date(best.get("system:time_start")).format("YYYY-MM-dd").getInfo(),
        "cloud_pct": best.get(cloud_prop).getInfo() if cloud_prop else None,
    }
    return SceneInfo(
        dataset=dataset,
        collection={
            "sentinel2": S2_COLLECTION,
            "landsat": "landsat_merge",
            "nicfi": NICFI_COLLECTION,
        }[dataset],
        scene_id=info["scene_id"],
        date=info["date"],
        cloud_pct=float(info["cloud_pct"] or -1),
        ee_object=best,
    )


def best_scene(
    dataset: str,
    lon: float,
    lat: float,
    radius_m: float,
    start_date: str,
    end_date: str,
    max_cloud_pct: float = 60.0,
    merge_scenes: bool = False,
    roi: ee.Geometry | None = None,
    bbox: Bbox | None = None,
) -> SceneInfo | None:
    """Select a scene using the supplied true ROI and its search envelope."""
    bbox = bbox or point_bbox(lon, lat, radius_m)
    selection = roi if roi is not None else bbox_to_ee_geom(bbox)
    if dataset in ("sentinel2", "landsat"):
        return _point_optical_scene(
            dataset, selection, start_date, end_date,
            max_cloud_pct, merge_scenes, **({"clip_output": False} if roi is not None else {}),
        )
    return best_scene_for_roi(
        dataset,
        selection,
        start_date,
        end_date,
        max_cloud_pct=max_cloud_pct,
    )


def preview_rgb(
    image: ee.Image,
    dataset: str,
    bbox: Bbox,
    region: ee.Geometry | None = None,
) -> str:
    """Return a PNG (via getThumbURL) for quick visual preview in the browser."""
    if dataset == "dynamic_world":
        rgb = image.select([DYNAMIC_WORLD_LABEL_BAND])
        vis = {
            "min": 0,
            "max": 8,
            "palette": DYNAMIC_WORLD_PALETTE,
        }
    elif dataset == "sentinel2":
        rgb = image.select(["B4", "B3", "B2"])
        vis = {"min": 0.0, "max": 0.3}
    elif dataset == "nicfi":
        rgb = image.select(["R", "G", "B"])
        vis = {"min": 0.0, "max": 0.3}
    elif dataset == "s1gee":
        rgb = image.select(["VV"])
        vis = {"min": -25.0, "max": 0.0, "palette": ["000000", "ffffff"]}
    else:
        rgb = image.select(["SR_B4", "SR_B3", "SR_B2"])
        vis = {"min": 0.0, "max": 0.3}
    vis.update({
        "dimensions": 512,
        "region": region or ee.Geometry.Rectangle(
            [bbox.west, bbox.south, bbox.east, bbox.north]
        ),
        "format": "png",
    })
    return rgb.getThumbURL(vis)


def _tile_bbox(bbox: Bbox, n: int, i: int, j: int) -> Bbox:
    """Chia bbox thành lưới n x n, trả về ô (i, j)."""
    dx = (bbox.east - bbox.west) / n
    dy = (bbox.north - bbox.south) / n
    # chồng lấn nhẹ 1 pixel để ghép không hở đường nối
    pad_x = dx * 0.001
    pad_y = dy * 0.001
    return Bbox(
        west=bbox.west + i * dx - pad_x,
        south=bbox.south + j * dy - pad_y,
        east=bbox.west + (i + 1) * dx + pad_x,
        north=bbox.south + (j + 1) * dy + pad_y,
    )


class _DownloadURL:
    """URL getDownloadURL cho 1 band/region, có thể tạo lại khi cần.

    GEE (hoặc proxy) thỉnh thoảng trả HTTP 200 kèm body rỗng cho getPixels;
    URL đã "dính" response rỗng sẽ rỗng mãi (kết quả được cache theo URL),
    nhưng gọi getDownloadURL lại cho cùng phép tính cho URL mới và tải bình
    thường. Người gọi dùng url() lần đầu, refresh() khi response bị hỏng.
    """

    def __init__(self, image: ee.Image, params: dict):
        self._image = image
        self._params = params
        self._url: str | None = None

    def url(self) -> str:
        if self._url is None:
            self._url = self._image.getDownloadURL(self._params)
        return self._url

    def refresh(self) -> str:
        self._url = self._image.getDownloadURL(self._params)
        return self._url

    def discard(self) -> None:
        """Bỏ URL hiện tại — lần url() kế tiếp tạo URL mới.

        download_geotiff tạo URL eagerly để phát hiện vùng vượt 48 MB cần chia
        tile, rồi discard: URL có thời hạn nên merge_band_tiffs sẽ tạo lại
        ngay trước lúc tải từng tile.
        """
        self._url = None


def download_geotiff(
    image: ee.Image,
    dataset: str,
    bbox: Bbox,
    scale: float,
    clip_region: ee.Geometry | None = None,
    max_tile_pixels: float | None = None,
) -> list[tuple[str, _DownloadURL]]:
    """Fetch per-band GeoTIFFs of the ROI via getDownloadURL.

    Returns a list of (band_name, _DownloadURL). GEE limits a single
    getDownloadURL request to 2^25 bytes (~48 MB); a large ROI at float32
    easily exceeds that, so we download each band separately AND, when a band
    still exceeds the limit, split the ROI into tiles (4 -> 9 -> 16 ...).
    Tiles are merged by the caller (see merge_band_tiffs) via the band name
    suffix "_tIJ".

    max_tile_pixels (nếu đặt) buộc mỗi tile ≤ số pixel ước lượng — dùng cho
    composite medoid/base-score: lỗi "User memory limit exceeded" của graph
    nhiều cảnh chỉ xuất hiện LÚC FETCH (400), không phải lúc tạo URL, nên
    probe 48 MB không phát hiện được; phải chia nhỏ tile sẵn từ đầu theo diện
    tích.

    The scene images coming from best_scene() already have their scale/offset
    applied as floats (see _mask_s2_clouds / _landsat_scale). We only need to
    re-select the bands and force float type, because getDownloadURL otherwise
    casts values back to the collection's native int type.
    """
    if clip_region is not None:
        image = image.clip(clip_region)

    categorical = dataset == "dynamic_world"
    if dataset == "dynamic_world":
        bands = [DYNAMIC_WORLD_LABEL_BAND]
    elif dataset == "sentinel2":
        bands = ["B2", "B3", "B4", "B8", "B11", "B12"]
    elif dataset == "nicfi":
        bands = ["B", "G", "R", "N"]
    elif dataset == "dem":
        bands = ["elevation"]
    elif dataset == "s1gee":
        # chỉ tải các pol có thật trong ảnh (VV/VH hoặc HH/HV)
        available = set(image.bandNames().getInfo())
        bands = [b for b in ["VV", "VH", "HH", "HV"] if b in available]
    else:
        bands = ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]

    def _band_urls_for_region(
        band: str, region: ee.Geometry, tag: str
    ) -> list[tuple[str, _DownloadURL]]:
        """1 URL cho 1 band trong 1 region; nếu vượt 48 MB thì raise EEException."""
        selected = image.select([band])
        if categorical:
            # Expand the masked image footprint before filling it.  With the
            # default sameFootprint=True, pixels outside a clipped province
            # can be rasterized by GEE as 0, which is a valid water class.
            # False makes the rectangular download region outside the ROI use
            # the dedicated Dynamic World NoData value instead.
            one_band = selected.unmask(
                DYNAMIC_WORLD_NODATA, sameFootprint=False
            ).toUint8()
            dtype = "uint8"
            nodata = DYNAMIC_WORLD_NODATA
        else:
            one_band = selected.multiply(1.0).toFloat()
            if dataset == "dem":
                one_band = one_band.unmask(-9999, sameFootprint=False)
            dtype = "float"
            nodata = -9999
        download_params = {
            "name": f"{dataset}_{band}{tag}",
            "scale": scale,
            "crs": "EPSG:4326",
            "region": region,
            "format": "GEO_TIFF",
            "dtype": dtype,
            "filePerBand": False,
            "formatOptions": {"noData": nodata},
        }
        url = _DownloadURL(one_band, download_params)
        # Probe eager để phát hiện vượt 48 MB cần chia tile; cũng có thể
        # dính RefreshError/EEException tạm thời do gateway chặn — retry.
        for k in range(1, 6):
            try:
                url.url()
                break
            except ee.EEException:
                raise  # lỗi thật của graph (vd vượt 48 MB) — nhường cho caller
            except Exception:
                if k == 5:
                    raise
                time.sleep(2.0 * k)
        url.discard()  # ...rồi bỏ: URL có thời hạn, tạo lại ngay trước khi tải
        return [(f"{band}{tag}", url)]

    # Ước lượng tổng số pixel của bbox (1° ≈ 111 km, đủ chính xác để chọn n).
    # Nếu caller giới hạn kích thước tile thì chia sẵn từ đầu thay vì chỉ
    # dựa vào probe 48 MB (không bắt được lỗi bộ nhớ lúc fetch).
    approx_px = (
        (bbox.east - bbox.west) * 111320.0 / scale
        * (bbox.north - bbox.south) * 111320.0 / scale
    )
    min_n = 1
    if max_tile_pixels and approx_px > max_tile_pixels:
        min_n = max(2, math.ceil(math.sqrt(approx_px / max_tile_pixels)))

    band_urls: list[tuple[str, _DownloadURL]] = []
    for band in bands:
        # Thử nguyên vùng; nếu vượt giới hạn 48 MB thì chia lưới tile tăng dần.
        region = ee.Geometry.Rectangle([bbox.west, bbox.south, bbox.east, bbox.north])
        whole_exc: Exception | None = None
        if min_n == 1:
            try:
                band_urls.extend(_band_urls_for_region(band, region, ""))
                continue
            except ee.EEException as exc:
                whole_exc = exc

        tiled = False
        last_exc: Exception | None = None
        for n in (2, 3, 4, 5, 6, 7, 8):
            if n < min_n:
                continue
            try:
                parts: list[tuple[str, _DownloadURL]] = []
                for i in range(n):
                    for j in range(n):
                        tb = _tile_bbox(bbox, n, i, j)
                        tregion = ee.Geometry.Rectangle(
                            [tb.west, tb.south, tb.east, tb.north]
                        )
                        parts.extend(
                            _band_urls_for_region(band, tregion, f"_t{i}{j}")
                        )
                band_urls.extend(parts)
                tiled = True
                break
            except ee.EEException as exc:
                last_exc = exc
                continue
        if not tiled:
            # KHÔNG dùng bare `raise` ở đây: ngoài except context nó thành
            # "No active exception to reraise". Ném lại lỗi thật — ưu tiên
            # lỗi của tile (thường là lỗi thật của graph, vd vượt bộ nhớ),
            # fallback về lỗi của vùng nguyên (vd vượt 48 MB).
            raise (last_exc or whole_exc or RuntimeError(
                f"Cannot create download URL for band {band}"
            ))
    return band_urls


def _fetch_with_retry(url: str, max_retries: int = 5, timeout: int = 600) -> bytes:
    """GET with retries. The corporate gateway occasionally answers 401/403
    ("Certificateincident") for no reason; retrying with backoff usually
    succeeds."""
    import time

    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as exc:
            last_exc = exc
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status is not None and status < 500 and status not in (401, 403, 429):
                raise  # client errors that retrying won't fix
            if attempt < max_retries:
                time.sleep(2.0 * attempt)
    raise last_exc  # type: ignore[misc]


def merge_band_tiffs(
    band_urls: list[tuple[str, "_DownloadURL"]],
    out_path,
    *,
    categorical: bool = False,
    preserve_masks: bool = False,
) -> None:
    """Download per-band GeoTIFFs and merge them into one multi-band file.

    Each entry of band_urls is (band_name, _DownloadURL). Band names may
    carry a tile suffix like "VV_t01" (see download_geotiff): tiles of the
    same base band are mosaicked by georeference into one band first, then
    bands are stacked. Raises requests.RequestException if any download
    fails.
    """
    # Local imports: heavy deps only needed for the merge step.
    import numpy as np
    import rasterio
    from rasterio.merge import merge as rio_merge

    tmp_dir = Path(out_path).parent / (Path(out_path).stem + "_bands_tmp")
    tmp_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Group URLs theo band gốc (bỏ suffix _tIJ nếu có)
        groups: dict[str, list[str]] = {}
        order: list[str] = []
        for band, url in band_urls:
            base = band.split("_t")[0]
            if base not in groups:
                groups[base] = []
                order.append(base)
            groups[base].append(url)

        # Tải tất cả các file về (có retry). Tải SONG SONG: URL do
        # getDownloadURL tạo ra có thời hạn (~1 giờ) — tải tuần tự nhiều tile
        # (median tỉnh lớn có thể cần 50+ tile, mỗi tile mất vài phút) khiến
        # các URL cuối hết hạn và trả 401 Unauthorized trước khi tới lượt tải.
        # Ngoài ra GEE/proxy thỉnh thoảng trả 200 + body rỗng (cache theo
        # URL): kiểm tra magic bytes TIFF, sai thì TẠO LẠI URL rồi mới thử lại.
        from concurrent.futures import ThreadPoolExecutor
        import time

        def _get_url(url_obj: "_DownloadURL", first: bool) -> str:
            """url()/refresh() có thể fail khi gateway chặn request refresh
            OAuth token (google.auth RefreshError trả trang HTML 401) hoặc
            khi getDownloadURL gặp lỗi tạm thời — retry như _fetch_with_retry.
            """
            last_exc: Exception | None = None
            for k in range(1, 6):
                try:
                    return url_obj.url() if first else url_obj.refresh()
                except Exception as exc:
                    last_exc = exc
                    time.sleep(2.0 * k)
            # RuntimeError để endpoint bắt và trả 502 thay vì 500
            raise RuntimeError(
                f"cannot create download URL after {k} attempts "
                f"(proxy/GEE token refresh?): {last_exc}"
            ) from last_exc

        def _download_to(url_obj: "_DownloadURL", path: Path) -> None:
            for attempt in range(1, 7):
                url = _get_url(url_obj, first=attempt == 1)
                try:
                    data = _fetch_with_retry(url)
                except requests.RequestException:
                    # URL hết hạn (401) sau nhiều lần thử lại — tạo URL mới
                    if attempt == 6:
                        raise
                    time.sleep(2.0 * attempt)
                    continue
                if data[:4] in (b"II*\x00", b"MM\x00*"):
                    path.write_bytes(data)
                    return
                # HTTP 200 nhưng body rỗng / không phải TIFF (cache theo URL)
                if attempt == 6:
                    raise RuntimeError(
                        f"tile {path.name}: GEE/proxy response is not a GeoTIFF "
                        f"after {attempt} URL refreshes"
                    )
                time.sleep(2.0 * attempt)

        jobs = [
            (tmp_dir / f"{base}_{k}.tif", url)
            for base, urls in groups.items()
            for k, url in enumerate(urls)
        ]
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            futs = [pool.submit(_download_to, url, p) for p, url in jobs]
            for fut in futs:
                fut.result()
        finally:
            # hủy các job chưa chạy để fail nhanh thay vì đợi cả hàng đợi
            pool.shutdown(wait=True, cancel_futures=True)
        paths: dict[str, list[Path]] = {}
        for base, urls in groups.items():
            paths[base] = [tmp_dir / f"{base}_{k}.tif" for k in range(len(urls))]

        # Mosaic các tile của từng band (nếu có nhiều), rồi stack
        band_files: list[tuple[str, Path]] = []
        for base in order:
            files = paths[base]
            if len(files) == 1:
                band_files.append((base, files[0]))
                continue
            mosaic_path = tmp_dir / f"{base}_mosaic.tif"
            srcs = [rasterio.open(f) for f in files]
            try:
                # rio_merge defaults to nearest-neighbour, which is required
                # for class labels and is also safe for the existing downloads.
                arr, transform = rio_merge(srcs, **({"nodata": float("nan"), "dtype": "float32"} if preserve_masks and not categorical else {}))
                profile = srcs[0].profile.copy()
                if preserve_masks and not categorical:
                    profile.update(dtype="float32", nodata=float("nan"))
                if categorical:
                    arr = arr.astype("uint8", copy=False)
                    profile.update(dtype="uint8", nodata=DYNAMIC_WORLD_NODATA)

                profile.update(
                    height=arr.shape[1], width=arr.shape[2],
                    count=1, transform=transform,
                )
                with rasterio.open(mosaic_path, "w", **profile) as dst:
                    dst.write(arr[0], 1)
            finally:
                for s in srcs:
                    s.close()
            band_files.append((base, mosaic_path))

        with rasterio.open(band_files[0][1]) as ref:
            height, width = ref.height, ref.width
            profile = ref.profile
            nodata = ref.nodata

        dtype = np.uint8 if categorical else np.float32
        stack = np.zeros((len(band_files), height, width), dtype=dtype)
        for i, (band, path) in enumerate(band_files):
            with rasterio.open(path) as src:
                if (src.height, src.width) != (height, width):
                    raise RuntimeError(
                        f"Band {band} has size {src.width}x{src.height}, "
                        f"expected {width}x{height}"
                    )
                if preserve_masks:
                    fill = DYNAMIC_WORLD_NODATA if categorical else float("nan")
                    stack[i] = src.read(1, masked=True).astype(dtype).filled(fill)
                else:
                    stack[i] = src.read(1).astype(dtype)

        if preserve_masks:
            profile.pop("blockxsize", None)
            profile.pop("blockysize", None)
        profile.update(
            count=len(band_files),
            dtype="uint8" if categorical else "float32",
            compress="deflate",
            tiled=True,
        )
        if categorical:
            profile.update(nodata=DYNAMIC_WORLD_NODATA)
        elif preserve_masks:
            nodata = float("nan")
            profile.update(nodata=nodata)
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(stack)
            for i, (band, _p) in enumerate(band_files, start=1):
                dst.set_band_description(i, band)
            # giữ nodata như band gốc (GEE ghi -inf cho pixel bị mask)
            if categorical:
                dst.nodata = DYNAMIC_WORLD_NODATA
            elif nodata is not None:
                dst.nodata = nodata
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)
