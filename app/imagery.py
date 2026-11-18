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
from . import color_balance as color_calibration
from . import scene_grouping as grouping
from .download_metrics import measure as measure_download
from .export_resources import bulk_export
from .platforms import apply_platform_filter, validate_platform
from .timeseries_metadata import (MAX_SCENES as TIMESERIES_MAX_SCENES,
                                  MetadataSearchError,
                                  compute_metadata as _compute_timeseries_metadata)

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
S2_RGB_NIR_BANDS = ["B2", "B3", "B4", "B8"]
# Preserve the original first six channels for existing RGB/index consumers.
S2_SPECTRAL_BANDS = S2_RGB_NIR_BANDS + ["B11", "B12", "B1", "B5", "B6", "B7", "B8A", "B9"]
S2_AUXILIARY_BANDS = [
    "AOT", "WVP", "SCL", "TCI_R", "TCI_G", "TCI_B", "MSK_CLDPRB", "MSK_SNWPRB",
    "QA10", "QA20", "QA60", "MSK_CLASSI_OPAQUE", "MSK_CLASSI_CIRRUS", "MSK_CLASSI_SNOW_ICE",
]
S2_EXPORT_BANDS = S2_SPECTRAL_BANDS + S2_AUXILIARY_BANDS


def s2_export_bands(auxiliary_bands: list[str] | None = None, *,
                    s2_band_preset: str | None = None) -> list[str]:
    """Resolve the ordered export bands. By default, include all auxiliary bands."""
    if s2_band_preset is not None:
        if s2_band_preset != 'rgbnir':
            raise ValueError('Unsupported Sentinel-2 band preset')
        if auxiliary_bands:
            raise ValueError('s2_band_preset=rgbnir requires no auxiliary bands')
        return list(S2_RGB_NIR_BANDS)
    if auxiliary_bands is None:
        return list(S2_EXPORT_BANDS)
    if len(auxiliary_bands) != len(set(auxiliary_bands)) or any(
        band not in S2_AUXILIARY_BANDS for band in auxiliary_bands
    ):
        raise ValueError('s2_auxiliary_bands must contain unique Sentinel-2 auxiliary band names')
    return S2_SPECTRAL_BANDS + [band for band in S2_AUXILIARY_BANDS if band in auxiliary_bands]


S2_CATEGORICAL_BANDS = ["SCL", "QA10", "QA20", "QA60", "MSK_CLASSI_OPAQUE",
                        "MSK_CLASSI_CIRRUS", "MSK_CLASSI_SNOW_ICE"]
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
    platform: str | None = None
    color_balance: dict | None = None
    selection_recipe: dict | None = None
    scene_grouping: dict | None = None
    mosaic_mask_clouds: bool | None = None


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


def _point_optical_scene(dataset, roi, start, end, max_cloud_pct, merge_scenes, clip_output=True, platform=None,
                         freeze_assets=False):
    coll, cloud_prop, _ = _optical_collection(dataset, start, end, platform=platform)
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
        if freeze_assets:
            best = load_timeseries_scene(_collection_id(dataset) + '/' + info['scene_id'], dataset)
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


def _raw_optical_scene(dataset, roi, start, end, max_cloud_pct, clip_output=True, platform=None,
                       color_balance=False, freeze_assets=False):
    """Build an unmasked RAW mosaic, filling original data gaps across dates.

    Rank with the coarse blue-band haze score on GEE, not a Python loop of
    full-resolution SCL/QA aggregations. Preserve the rectangle warp halo.
    """
    coll, cloud_prop, _ = _optical_collection(dataset, start, end, platform=platform)
    coll = coll.filterBounds(roi).filter(ee.Filter.lt(cloud_prop, max_cloud_pct))
    if coll.size().getInfo() == 0:
        return None
    from .download_models import SelectionTrace
    trace = SelectionTrace() if freeze_assets else None
    image, picked = _least_cloudy_per_tile(coll, dataset, cloud_prop, roi, keep_clouds=True,
        **({'color_balance': True} if color_balance else {}),
        **({'selection_trace': trace} if trace is not None else {}))
    if clip_output:
        image = image.clip(roi)
    with _OPTICAL_AGGREGATION_LOCK:
        coverage = 100 * _coverage_fraction(image, dataset, roi).getInfo()
    if coverage < 99:
        raise InsufficientCoverageError(
            f"RAW chỉ phủ {coverage:.2f}% vùng (<99%). Hãy mở rộng khoảng ngày hoặc giảm vùng chọn."
        )
    return SceneInfo(
        dataset=dataset, collection=_collection_id(dataset),
        scene_id=f"{dataset}_raw_{start}_{end}",
        date=", ".join(sorted({row["date"] for row in picked})),
        cloud_pct=max((row["cloud"] for row in picked), default=-1),
        ee_object=image, scene_count=len(picked), scenes=tuple(picked), coverage_pct=coverage,
        color_balance=_color_balance_report(picked) if color_balance else None,
        selection_recipe=({**trace.__dict__, 'mode': 'raw', 'clipped': clip_output}
                          if trace is not None else None),
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
    # Optional quality bands can be absent in older products. Keep a stable
    # export schema using masked (NoData), never zero-filled, placeholders.
    placeholders = ee.Image.constant([0] * len(S2_EXPORT_BANDS)).rename(S2_EXPORT_BANDS).toFloat()
    placeholders = placeholders.updateMask(ee.Image.constant(0))
    return img.addBands(placeholders.select(ee.List(S2_EXPORT_BANDS).removeAll(img.bandNames()))).addBands(optical, None, True).select(
        S2_EXPORT_BANDS
    ).copyProperties(img, ["system:time_start", "system:index"])


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


def _s2_collection(start: str, end: str, platform: str | None = None) -> ee.ImageCollection:
    platform = validate_platform('sentinel2', platform)
    collection = (
        ee.ImageCollection(S2_COLLECTION)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 90))
    )
    collection = apply_platform_filter(collection, 'sentinel2', platform)
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
                "timeseries:asset_id": ee.String(NICFI_COLLECTION + '/').cat(ee.String(img.get('system:index'))),
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


def _s1_collection(start: str, end: str, polarization: str, platform: str | None = None) -> ee.ImageCollection:
    """Sentinel-1 GRD: chọn polarization, clamp dải động dB.

    GEE S1_GRD đã ở đơn vị dB (sigma0 dB, đã hiệu chuẩn; giá trị thực đo
    trên An Giang: -28..+17) — KHÔNG áp log10 thêm (log10 của số âm = NaN,
    đã từng làm toàn bộ pixel ra -inf). Chỉ clamp về dải -30..+10 dB hợp lý
    cho hiển thị/phân tích.
    """
    platform = validate_platform('s1gee', platform)
    bands = S1_POLARIZATIONS[polarization]

    def to_db(img: ee.Image) -> ee.Image:
        sel = img.select(bands)
        db = (
            sel.clamp(-30.0, 10.0)  # giới hạn dải động hợp lý
            .float()
        )
        return db.copyProperties(img, ["system:time_start", "system:index", "platform_number",
                                       "orbitProperties_pass", "relativeOrbitNumber_start"])

    collection = (
        ee.ImageCollection(S1_COLLECTION)
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", bands[0]))
        .filterDate(start, end)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
    )
    return apply_platform_filter(collection, 's1gee', platform).map(to_db)


def timeseries_source_metadata(dataset: str, polarization: str = "vvvh",
                               s2_auxiliary_bands: list[str] | None = None, *,
                               s2_band_preset: str | None = None) -> dict:
    """Describe exported bands, nominal resolution and source value conversion."""
    sources = {
        "sentinel2": (s2_export_bands(s2_auxiliary_bands, s2_band_preset=s2_band_preset), S2_SCALE,
                      "reflectance" if s2_band_preset == 'rgbnir' else "mixed", 0.0001, 0.0),
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
    if dataset == "sentinel2":
        result["band_metadata"] = {
            band: {"units": "reflectance" if band in S2_SPECTRAL_BANDS else
                   "cm" if band == "WVP" else "class" if band in S2_CATEGORICAL_BANDS else "source",
                   "value_scale": 0.0001 if band in S2_SPECTRAL_BANDS else
                   0.001 if band in ("AOT", "WVP") else 1.0,
                   "value_offset": 0.0,
                   "export_scale": 0.0001 if band in S2_SPECTRAL_BANDS else 1.0}
            for band in bands
        }
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
    platform: str | None = None,
    *, roi: ee.Geometry | None = None,
) -> ee.ImageCollection:
    """Native metadata only; caller converts inclusive end dates to exclusive dates.

    No cloud ranking, cloud thresholds, orbit preference or temporal composites.
    Full asset IDs are stored independently of EE transformations/merged indexes.
    """
    timeseries_source_metadata(dataset, polarization)
    platform = validate_platform(dataset, platform)

    def prepare(image: ee.Image, collection_id: str) -> ee.Image:
        # Image.id() inside a collection map can be only the relative index.
        # Tag the complete native ID before any merge rewrites system:index.
        asset_id = ee.String(collection_id + '/').cat(ee.String(image.get('system:index')))
        return image.set({
            "timeseries:asset_id": asset_id,
            "timeseries:collection": collection_id,
        })

    collection_ids = {
        "sentinel2": [S2_COLLECTION], "landsat": [L8_COLLECTION, L9_COLLECTION],
        "s1gee": [S1_COLLECTION], "nicfi": [NICFI_COLLECTION],
        "dynamic_world": [DYNAMIC_WORLD_COLLECTION],
    }[dataset]
    collections = []
    for collection_id in collection_ids:
        collection = ee.ImageCollection(collection_id).filterDate(start_date, end_exclusive)
        if roi is not None:
            collection = collection.filterBounds(roi)
        collection = apply_platform_filter(collection, dataset, platform)
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
    platform: str | None = None,
) -> list[dict]:
    """Return scene metadata; optional scene-cloud filters apply before enumeration."""
    platform = validate_platform(dataset, platform)
    point = ee.Geometry.Point([lon, lat])
    collection = timeseries_collection(dataset, start_date, end_exclusive, polarization,
                                       platform=platform, roi=roi if roi is not None else point)
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

    # One extra row detects an incomplete result; never publish a truncated list.
    rows = _compute_timeseries_metadata(collection.toList(TIMESERIES_MAX_SCENES + 1).map(metadata))
    scenes = {}
    try:
        for raw in rows:
            row = dict(raw)
            stamp = row.pop('timestamp_ms')
            asset, source = row['asset_id'], row['collection']
            if (isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or
                    not isinstance(asset, str) or not isinstance(source, str) or
                    not asset.startswith(source + '/') or not isinstance(row.get('source_metadata'), dict)):
                raise ValueError('Invalid scene metadata')
            timestamp = dt.datetime.fromtimestamp(stamp / 1000, tz=dt.timezone.utc)
            row.update(dataset=dataset, timestamp=timestamp.isoformat().replace("+00:00", "Z"),
                       date=timestamp.date().isoformat())
            cloud = row.get("cloud_pct")
            row["cloud_pct"] = float(cloud) if cloud is not None and 0 <= cloud <= 100 else None
            scenes.setdefault(asset, row)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise MetadataSearchError('invalid_metadata', 'GEE trả về metadata cảnh không hợp lệ; kết quả chưa được lưu.') from exc
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
    platform: str | None = None,
) -> SceneInfo | None:
    """Return the newest S1 GRD scene covering the point within the date range.

    Unlike optical, SAR penetrates clouds, so we simply take the most recent
    scene (no cloud scoring needed).
    """
    point = roi if roi is not None else ee.Geometry.Point([lon, lat])
    coll = _s1_collection(start_date, end_date, polarization, platform=platform).filterBounds(point)
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
        platform=best.get("platform_number").getInfo(),
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
    platform: str | None = None,
) -> SceneInfo | None:
    """Return the S1 scene covering the largest fraction of the ROI (newest first)."""
    coll = _s1_collection(start_date, end_date, polarization, platform=platform).filterBounds(roi)
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
        platform=best.get("platform_number").getInfo(),
    )


def _raw_gap_fill(
    coll: ee.ImageCollection,
    ranked: ee.ImageCollection,
    dataset: str,
    cloud_prop: str,
    roi: ee.Geometry | None,
    color_balance: bool = False,
    *, selection_trace=None,
) -> tuple[ee.Image, list[dict]]:
    """Prefer whole valid scene footprints; use other dates only in data gaps.

    Freeze ranking in one metadata RPC so exports never rerun haze reductions.
    A source-index mosaic also prevents masked auxiliary bands from silently
    taking values from another date. Clouds and valid zero values stay intact.
    """
    sort_prop = "hazeScore" if roi is not None else cloud_prop
    ordered = ranked.sort(sort_prop, False)  # best LAST: GEE mosaic priority
    tile_properties = ["MGRS_TILE"] if dataset == "sentinel2" else ["WRS_PATH", "WRS_ROW"]
    properties = ["system:index", "system:time_start", cloud_prop] + tile_properties
    rows = ordered.toList(ordered.size()).map(
        lambda value: ee.Image(value).toDictionary(properties)
    ).getInfo()
    bands = (["B2", "B3", "B4", "B8", "B11", "B12"] if dataset == "sentinel2"
             else ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"])
    source_band = "raw_source_index"
    sources = []
    for index, row in enumerate(rows):
        original = (load_timeseries_scene(_collection_id(dataset) + '/' + row['system:index'], dataset)
                    if selection_trace is not None else
                    ee.Image(coll.filter(ee.Filter.eq("system:index", row["system:index"])).first()))
        valid = original.select(bands).mask().reduce(ee.Reducer.min())
        # Edge masks become fractional at thumbnail/pyramid scales. Mosaic
        # alpha-blends those masks, which would invent intermediate source IDs.
        # Expand before thresholding so the label's footprint cannot add alpha.
        # Only the categorical label mask is binary; original band masks stay.
        source_valid = valid.unmask(0, sameFootprint=False).gt(0)
        source = ee.Image.constant(index).toInt32().rename(source_band).updateMask(source_valid)
        # Absent S2 auxiliaries use Float placeholders; native QA bands are
        # integer. Normalize before mosaic (exports already use Float32).
        sources.append(original.toFloat().addBands(source).set("__raw_index", index))

    # Start with one preferred scene per tile. Add ranked dates in doubling
    # batches only while data coverage is below 99%. Lower-priority batches
    # never replace a valid pixel already supplied by an earlier batch.
    per_tile = {}
    for index in reversed(range(len(rows))):
        key = tuple(rows[index].get(prop) for prop in tile_properties)
        per_tile.setdefault(key, []).append(index)
    selected = []
    batch_size = 1
    while True:
        wanted = {index for indices in per_tile.values() for index in indices[:batch_size]}
        layer = sorted(wanted.difference(selected))
        selected = layer + selected
        if selection_trace is not None:
            selection_trace.layers.append(layer)
        stack = ee.ImageCollection.fromImages([sources[index] for index in selected])
        owner = stack.select(source_band).mosaic()
        with _OPTICAL_AGGREGATION_LOCK:
            histogram = ee.Dictionary(owner.unmask(-1, sameFootprint=False).reduceRegion(
                reducer=ee.Reducer.frequencyHistogram(),
                geometry=roi if roi is not None else coll.geometry(),
                scale=S2_SCALE if dataset == "sentinel2" else LANDSAT_SCALE,
                maxPixels=100_000_000, tileScale=4,
            ).get(source_band)).getInfo() or {}
        total = sum(histogram.values())
        missing = sum(count for key, count in histogram.items() if float(key) < 0)
        if (total > 0 and missing / total <= .01) or len(selected) == len(rows):
            break
        batch_size *= 2

    used = {int(float(key)) for key, count in histogram.items() if float(key) >= 0 and count > 0}
    if selection_trace is not None:
        selection_trace.assets = [_collection_id(dataset) + '/' + rows[index]['system:index']
                                  for index in selected]
        selection_trace.priority = list(selected)
        selection_trace.contributors = sorted(used)
    calibration = {}
    if color_balance and used:
        priority = [index for index in reversed(selected) if index in used]

        def evaluate(sample):
            with _OPTICAL_AGGREGATION_LOCK:
                return sample.getInfo()

        calibrated, report = color_calibration.balance_sources(
            [sources[index] for index in priority], dataset,
            roi if roi is not None else coll.geometry(), evaluate,
            [rows[index]['system:index'] for index in priority])
        for index, image, detail in zip(priority, calibrated, report['scenes']):
            sources[index] = image
            calibration[index] = detail
        stack = ee.ImageCollection.fromImages([sources[index] for index in selected])

    # Apply ownership to ALL bands, retaining each band's original NoData mask.
    image = stack.map(lambda value: value.updateMask(
        owner.eq(ee.Image.constant(value.get("__raw_index"))))).mosaic()
    image = image.select(ee.Image(sources[selected[0]]).bandNames().remove(source_band))
    infos = [
        {"scene_id": row["system:index"], "date": _fmt_time_start(row["system:time_start"]),
         "cloud": float(row.get(cloud_prop) if row.get(cloud_prop) is not None else -1),
         **({'color_balance': calibration[index]} if index in calibration else {})}
        for index, row in enumerate(rows) if index in used
    ]
    return image, infos


def _color_balance_report(picked):
    details = [row['color_balance'] for row in picked if 'color_balance' in row]
    return dict(method=color_calibration.METHOD,
                spectral_values_modified=any(row['status'] in ('applied', 'partial') for row in details),
                sample_scale_m=color_calibration.SAMPLE_SCALE_M, max_samples=color_calibration.MAX_SAMPLES,
                min_samples=color_calibration.MIN_SAMPLES, scenes=details)


_GROUP_PROPERTIES = ['system:index', 'system:time_start', 'SPACECRAFT_NAME', 'SPACECRAFT_ID',
    'DATATAKE_IDENTIFIER', 'SENSING_ORBIT_NUMBER', 'MGRS_TILE', 'WRS_PATH', 'WRS_ROW',
    'MEAN_SOLAR_ZENITH_ANGLE', 'MEAN_SOLAR_AZIMUTH_ANGLE', 'MEAN_INCIDENCE_ZENITH_ANGLE_B4',
    'SUN_ELEVATION', 'SUN_AZIMUTH', 'ROLL_ANGLE', 'PROCESSING_BASELINE', 'PROCESSING_LEVEL']


def _native_asset(row, dataset):
    index = row['system:index']
    if dataset == 'sentinel2':
        return S2_COLLECTION + '/' + index
    # Merged Landsat collections prefix system:index with 1_/2_.
    for marker, collection in (('LC08_', L8_COLLECTION), ('LC09_', L9_COLLECTION)):
        if marker in index:
            return collection + '/' + index[index.index(marker):]
    raise ValueError('Không xác định được asset Landsat gốc.')


def _load_mosaic_asset(asset, dataset):
    image = load_timeseries_scene(asset, dataset)
    # The time-series export loader intentionally selects six Landsat SR
    # bands. Mosaic cloud masking/calibration still needs native QA_PIXEL.
    if dataset == 'landsat':
        image = image.addBands(ee.Image(asset).select('QA_PIXEL'))
    return image


def _group_metadata(coll, roi, cloud_prop):
    def metadata(value):
        image = ee.Image(value)
        return image.toDictionary(_GROUP_PROPERTIES).set('cloud', image.get(cloud_prop)).set(
            'footprint', image.geometry().intersection(roi, ee.ErrorMargin(60)))
    # A FeatureCollection nested in Dictionary.getInfo yields only its schema.
    # Explicit dictionary rows materialize both properties and geometry.
    candidates = coll.limit(grouping.MAX_CANDIDATES + 1).toList(
        grouping.MAX_CANDIDATES + 1).map(metadata)
    with _OPTICAL_AGGREGATION_LOCK:
        payload = ee.Dictionary({'roi': roi, 'rows': candidates}).getInfo()
    rows = payload['rows']
    if len(rows) > grouping.MAX_CANDIDATES:
        raise ValueError('Có quá nhiều cảnh (>400); hãy thu hẹp khoảng ngày hoặc ROI.')
    return rows, payload['roi']


def _coherent_optical_scene(coll, dataset, cloud_prop, roi, start, end, method,
                            scene_grouping, max_scene_span_days, *, mosaic_mask_clouds=True,
                            color_balance=False, clip_output=True):
    rows, roi_geometry = _group_metadata(coll, roi, cloud_prop)
    if not rows:
        return None
    groups = (grouping.rank_groups(rows, dataset, scene_grouping, max_scene_span_days, roi_geometry)
              if scene_grouping != 'any' else [{'indices': list(range(len(rows)))}])
    best_coverage, checks = 0., 0
    from .download_models import SelectionTrace
    for group in groups[:grouping.MAX_PIXEL_CHECKS]:
        chosen = [rows[index] for index in group['indices']]
        # Bind native sources before ranking/mosaicking. Neither preview nor
        # snapshots subsequently query a live collection to reselect a scene.
        source_images = [_load_mosaic_asset(_native_asset(row, dataset), dataset).set(
            {key: value for key, value in row.items() if key != 'footprint'}) for row in chosen]
        subset = ee.ImageCollection.fromImages(source_images)
        trace = SelectionTrace() if dataset == 'sentinel2' else None
        image, picked = _least_cloudy_per_tile(subset, dataset, cloud_prop, roi,
            keep_clouds=(method == 'raw'), mosaic_mask_clouds=False,
            **({'selection_trace': trace} if trace is not None else {}))
        with _OPTICAL_AGGREGATION_LOCK:
            coverage = 100 * (_coverage_fraction(image, dataset, roi).getInfo() or 0.)
        checks += 1
        best_coverage = max(best_coverage, coverage)
        if scene_grouping != 'any' and coverage < 99:
            continue
        if method == 'mosaic' and mosaic_mask_clouds:
            metadata = {row['system:index']: row for row in chosen}
            mask = _mask_s2_clouds if dataset == 'sentinel2' else _mask_landsat_clouds
            image = ee.ImageCollection.fromImages([ee.Image(mask(_load_mosaic_asset(
                _native_asset(metadata[item['scene_id']], dataset), dataset))).toFloat()
                for item in sorted(picked, key=lambda item: item['date'])]).mosaic()
        if method == 'raw' and color_balance:
            trace = SelectionTrace() if trace is not None else None
            image, picked = _least_cloudy_per_tile(subset, dataset, cloud_prop, roi,
                keep_clouds=(method == 'raw'), mosaic_mask_clouds=mosaic_mask_clouds,
                **({'color_balance': True} if color_balance else {}),
                **({'selection_trace': trace} if trace is not None else {}))
        metadata = {row['system:index']: row for row in chosen}
        details = [{**item, **{key: value for key, value in metadata[item['scene_id']].items()
                              if key not in ('footprint', 'cloud', 'system:index', 'system:time_start')},
                    'timestamp': metadata[item['scene_id']]['system:time_start']} for item in picked]
        times = [item['timestamp'] for item in details]
        keys = [grouping.acquisition_key(metadata[item['scene_id']], dataset) for item in details]
        with _OPTICAL_AGGREGATION_LOCK:
            output_coverage = 100 * (_coverage_fraction(image, dataset, roi).getInfo() or 0.)
        report = dict(mode=scene_grouping, max_scene_span_days=max_scene_span_days,
            span_days=(max(times) - min(times)) / grouping.DAY_MS,
            start_utc=dt.datetime.fromtimestamp(min(times) / 1000, dt.timezone.utc).isoformat(),
            end_utc=dt.datetime.fromtimestamp(max(times) / 1000, dt.timezone.utc).isoformat(),
            same_pass=keys[0] is not None and all(key == keys[0] for key in keys),
            data_coverage_pct=coverage, output_coverage_pct=output_coverage,
            groups_checked=checks, shortlisted_groups=len(groups), candidate_count=len(rows),
            missing_metadata_scenes=[item['scene_id'] for item in details
                if grouping.missing_fields(metadata[item['scene_id']], dataset)],
            missing_metadata_fields={item['scene_id']: grouping.missing_fields(metadata[item['scene_id']], dataset)
                for item in details if grouping.missing_fields(metadata[item['scene_id']], dataset)})
        recipe = ({**trace.__dict__, 'mode': 'raw', 'clipped': clip_output} if trace is not None and method == 'raw' else
                  dict(mode=method, assets=[_native_asset(metadata[item['scene_id']], dataset) for item in details]))
        return SceneInfo(dataset, _collection_id(dataset), f'{dataset}_{method}_{start}_{end}',
            ', '.join(sorted({item['date'] for item in details})),
            max((item['cloud'] for item in details), default=-1), image.clip(roi) if clip_output else image,
            scene_count=len(details), scenes=tuple(details), coverage_pct=output_coverage,
            color_balance=_color_balance_report(picked) if color_balance else None,
            selection_recipe=recipe, scene_grouping=report,
            mosaic_mask_clouds=mosaic_mask_clouds if method == 'mosaic' else None)
    raise InsufficientCoverageError(
        f'Nhóm cảnh tương đồng chỉ phủ tối đa {best_coverage:.2f}% dữ liệu (<99%). '
        f'Không tự mở rộng cửa sổ {max_scene_span_days} ngày; hãy tăng giới hạn ngày hoặc giảm ROI.')


def _least_cloudy_per_tile(
    coll: ee.ImageCollection,
    dataset: str,
    cloud_prop: str,
    roi: ee.Geometry | None = None,
    keep_clouds: bool = False,
    color_balance: bool = False,
    *, selection_trace=None, mosaic_mask_clouds=True,
) -> tuple[ee.Image, list[dict]]:
    """Build a masked per-tile mosaic, or an original-data-gap-filling RAW mosaic.

    Với mosaic có mask mây, mỗi tile đóng góp đúng một scene nên không có hiện tượng
    ``qualityMosaic`` đổi ngày độc lập ở từng pixel như medoid. Khi có ROI,
    xếp hạng theo mean băng xanh (haze) trước; ``CLOUDY_PIXEL_PERCENTAGE``
    vẫn là bộ lọc đầu vào nhưng không đủ để nhận ra khói/sương. Cảnh đã chọn
    được SCL/QA-mask trước khi mosaic để mây residual không thành mảng màu.

    ``keep_clouds=True`` (method "raw"): giữ mây và pixel gốc của cảnh ưu tiên;
    dùng thêm ngày khác (kể cả cùng tile) chỉ để bù footprint/NoData bị thiếu.
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
    if keep_clouds:
        return _raw_gap_fill(coll, ranked, dataset, cloud_prop, roi,
                             **({'color_balance': True} if color_balance else {}),
                             **({'selection_trace': selection_trace} if selection_trace is not None else {}))
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
        if selection_trace is not None:
            best = load_timeseries_scene(_native_asset(info, dataset), dataset)
        date = dt.datetime.fromtimestamp(
            info["system:time_start"] / 1000, dt.timezone.utc
        ).strftime("%Y-%m-%d")
        # Mask only after selecting the scene so the selection itself still
        # uses the complete ROI haze score; mosaic then prefers valid pixels.
        # raw: giữ nguyên pixel mây — không mask cảnh được chọn.
        picked.append((date, ee.Image(mask_scene(best) if mosaic_mask_clouds else best).toFloat()))
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
_MEDIAN_BAND_COUNTS = {"sentinel2": len(S2_EXPORT_BANDS), "landsat": 6, "nicfi": 4}


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
    dataset: str, start: str, end: str, platform: str | None = None
) -> tuple[ee.ImageCollection, str | None, float]:
    """Return (collection, cloud metadata property, scale) for a dataset."""
    platform = validate_platform(dataset, platform)
    if dataset == "sentinel2":
        return _s2_collection(start, end, platform=platform), "CLOUDY_PIXEL_PERCENTAGE", S2_SCALE
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
    method: str = "raw",
    max_cloud_pct: float = 60.0,
    platform: str | None = None,
    scene_grouping: str = 'any', max_scene_span_days: int = 3, mosaic_mask_clouds: bool = True,
) -> dict | None:
    """Lập kế hoạch composite mà KHÔNG chạy: trả về số cảnh, khoảng ngày và
    (mosaic) danh sách cảnh từng tile kèm % mây, hoặc (median/medoid) thời
    gian ước lượng. Trả về None nếu không có cảnh nào hợp lệ."""
    if dataset in ('sentinel2', 'landsat') and method in ('raw', 'mosaic') and (
            scene_grouping != 'any' or method == 'mosaic'):
        scene = best_scene_for_roi(dataset, roi, start_date, end_date, composite=True,
            composite_method=method, max_cloud_pct=max_cloud_pct, platform=platform,
            scene_grouping=scene_grouping, max_scene_span_days=max_scene_span_days,
            mosaic_mask_clouds=mosaic_mask_clouds)
        return (dict(method=method, scenes=list(scene.scenes), scene_count=scene.scene_count,
                     candidate_scene_count=scene.scene_grouping['candidate_count'], selected_scene_count=scene.scene_count,
                     date_min=scene.scene_grouping['start_utc'][:10], date_max=scene.scene_grouping['end_utc'][:10],
                     estimated_seconds=None, scene_grouping=scene.scene_grouping,
                     mosaic_mask_clouds=scene.mosaic_mask_clouds) if scene else None)
    coll, cloud_prop, scale = _optical_collection(dataset, start_date, end_date, platform=platform)
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
    # Quality bits and optional masked bands must not affect spectral distance.
    distance_bands = ["B2", "B3", "B4", "B8", "B11", "B12"] if "B2" in bands else bands
    median = sel.select(distance_bands).median()

    def _with_dist(img: ee.Image) -> ee.Image:
        dist = img.select(distance_bands).subtract(median).pow(2).reduce(ee.Reducer.sum())
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
        bands = S2_EXPORT_BANDS
    else:
        masked = coll.map(_mask_landsat_clouds)
        bands = ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"]
    if method == "medoid":
        composite = _medoid(masked, bands).clip(roi)
    else:
        composite = masked.median().clip(roi)
        if dataset == "sentinel2":
            # Median can invent fractional class IDs and invalid QA bit patterns.
            categorical = masked.select(S2_CATEGORICAL_BANDS).mode().clip(roi)
            composite = composite.addBands(categorical, None, True).select(bands)

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
    composite_method: str = "raw",
    max_cloud_pct: float = 60.0,
    platform: str | None = None,
    color_balance: bool = False,
    scene_grouping: str = 'any', max_scene_span_days: int = 3, mosaic_mask_clouds: bool = True,
) -> SceneInfo | None:
    """Return an image over ``roi``.

    Point mode gets the least-cloudy single scene. Province mode composites:
    ``composite_method="medoid"`` picks, per pixel, the actual band
    values of the scene closest to the median — màu trung thực, không phai;
    ``composite_method="median"`` takes the per-pixel median of the selected
    cloud-masked scenes — khử mây residual tốt hơn nhưng màu có thể phai.
    Khi có quá nhiều cảnh, cả hai phương pháp ưu tiên tối đa 20 cảnh có mean
    băng xanh thấp nhất trên ROI (ít sương/khói nhất), thay vì chỉ dùng
    metadata cloud percentage;
    ``composite_method="mosaic"`` mosaics the least-cloudy scene of
    each tile covering the ROI (see ``_least_cloudy_per_tile``).
    ``composite_method="raw"`` (default) giữ nguyên mây và pixel của cảnh ưu tiên;
    các ngày khác chỉ bù phần footprint/NoData thiếu, kể cả trong cùng tile.
    ``composite_method="base-score"`` puts the best-scored masked scene on
    top of a small fallback stack per tile, so fallbacks only fill its cloud
    holes and do not replace clear base pixels.
    """
    coll, cloud_prop, scale = _optical_collection(dataset, start_date, end_date, platform=platform)

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
        if dataset in ('sentinel2', 'landsat') and composite_method in ('raw', 'mosaic') and (
                scene_grouping != 'any' or composite_method == 'mosaic'):
            return _coherent_optical_scene(coll, dataset, cloud_prop, roi, start_date, end_date,
                composite_method, scene_grouping, max_scene_span_days,
                mosaic_mask_clouds=mosaic_mask_clouds, color_balance=color_balance)
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
                coll, dataset, cloud_prop, roi, keep_clouds=is_raw,
                **({'color_balance': True} if color_balance and is_raw else {})
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
                        **({'color_balance': p['color_balance']} if 'color_balance' in p else {}),
                    }
                    for p in picked
                ),
                color_balance=_color_balance_report(picked) if color_balance and is_raw else None,
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
    platform: str | None = None,
    color_balance: bool = False,
    freeze_assets: bool = False,
    composite_method: str = 'raw', scene_grouping: str = 'any', max_scene_span_days: int = 3,
    mosaic_mask_clouds: bool = True,
) -> SceneInfo | None:
    """Select a scene using the supplied true ROI and its search envelope."""
    platform = validate_platform(dataset, platform)
    bbox = bbox or point_bbox(lon, lat, radius_m)
    selection = roi if roi is not None else bbox_to_ee_geom(bbox)
    if dataset in ("sentinel2", "landsat"):
        if merge_scenes:
            if scene_grouping != 'any' or composite_method == 'mosaic':
                coll, cloud_prop, _ = _optical_collection(dataset, start_date, end_date, platform=platform)
                coll = coll.filterBounds(selection).filter(ee.Filter.lt(cloud_prop, max_cloud_pct))
                return _coherent_optical_scene(coll, dataset, cloud_prop, selection, start_date, end_date,
                    composite_method, scene_grouping, max_scene_span_days,
                    mosaic_mask_clouds=mosaic_mask_clouds, color_balance=color_balance, clip_output=roi is None)
            return _raw_optical_scene(
                dataset, selection, start_date, end_date, max_cloud_pct,
                clip_output=roi is None, platform=platform,
                **({'color_balance': True} if color_balance else {}),
                **({'freeze_assets': True} if freeze_assets else {}),
            )
        return _point_optical_scene(
            dataset, selection, start_date, end_date,
            max_cloud_pct, merge_scenes, platform=platform,
            **({"clip_output": False} if roi is not None else {}),
            **({'freeze_assets': True} if freeze_assets else {}),
        )
    return best_scene_for_roi(
        dataset,
        selection,
        start_date,
        end_date,
        max_cloud_pct=max_cloud_pct,
        platform=platform,
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
            self._url = measure_download('create_url', self._image.getDownloadURL, self._params)
        return self._url

    def refresh(self) -> str:
        self._url = measure_download('create_url', self._image.getDownloadURL, self._params)
        return self._url

    def discard(self) -> None:
        """Bỏ URL hiện tại — lần url() kế tiếp tạo URL mới.

        download_geotiff tạo URL eagerly để phát hiện vùng vượt 32 MiB cần chia
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
    s2_auxiliary_bands: list[str] | None = None,
    *, s2_band_preset: str | None = None,
) -> list[tuple[str, _DownloadURL]]:
    """Fetch per-band GeoTIFFs of the ROI via getDownloadURL.

    Returns a list of (band_name, _DownloadURL). GEE limits a single
    getDownloadURL request to 2^25 bytes (32 MiB); a large ROI at float32
    easily exceeds that, so we download each band separately AND, when a band
    still exceeds the limit, split the ROI into tiles (4 -> 9 -> 16 ...).
    Tiles are merged by the caller (see merge_band_tiffs) via the band name
    suffix "_tIJ".

    max_tile_pixels (nếu đặt) buộc mỗi tile ≤ số pixel ước lượng — dùng cho
    composite medoid/base-score: lỗi "User memory limit exceeded" của graph
    nhiều cảnh chỉ xuất hiện LÚC FETCH (400), không phải lúc tạo URL, nên
    probe 32 MiB không phát hiện được; phải chia nhỏ tile sẵn từ đầu theo diện
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
        bands = s2_export_bands(s2_auxiliary_bands, s2_band_preset=s2_band_preset)
    elif dataset == "nicfi":
        bands = ["B", "G", "R", "N"]
    elif dataset.startswith("s5p_"):
        from .s5p import product_metadata
        bands = [product_metadata(dataset.removeprefix("s5p_"))["band"]]
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
        """1 URL cho 1 band trong 1 region; nếu vượt 32 MiB thì raise EEException."""
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
            if dataset in ("dem", "sentinel2") or dataset.startswith("s5p_"):
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
        # Probe eager để phát hiện vượt 32 MiB cần chia tile; cũng có thể
        # dính RefreshError/EEException tạm thời do gateway chặn — retry.
        for k in range(1, 6):
            try:
                url.url()
                break
            except ee.EEException:
                raise  # lỗi thật của graph (vd vượt 32 MiB) — nhường cho caller
            except Exception:
                if k == 5:
                    raise
                time.sleep(2.0 * k)
        url.discard()  # ...rồi bỏ: URL có thời hạn, tạo lại ngay trước khi tải
        return [(f"{band}{tag}", url)]

    # Ước lượng tổng số pixel của bbox (1° ≈ 111 km, đủ chính xác để chọn n).
    # Nếu caller giới hạn kích thước tile thì chia sẵn từ đầu thay vì chỉ
    # dựa vào probe 32 MiB (không bắt được lỗi bộ nhớ lúc fetch).
    approx_px = (
        (bbox.east - bbox.west) * 111320.0 / scale
        * (bbox.north - bbox.south) * 111320.0 / scale
    )
    # A Float32 tile stays below GEE's 32 MiB request limit, even for huge
    # envelopes that need more than eight tiles per axis. Callers may request
    # smaller tiles for expensive computation graphs.
    tile_pixel_limit = min(max_tile_pixels or 8_000_000, 8_000_000)
    min_n = max(1, math.ceil(math.sqrt(approx_px / tile_pixel_limit)))

    band_urls: list[tuple[str, _DownloadURL]] = []
    for band in bands:
        # Thử nguyên vùng; nếu vượt giới hạn 32 MiB thì chia lưới tile tăng dần.
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
        for n in range(max(2, min_n), max(8, min_n + 2) + 1):
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
            # fallback về lỗi của vùng nguyên (vd vượt 32 MiB).
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


def _mosaic_placements(sources, profile):
    """Anchor source sampling to the whole mosaic, independent of block edges.

    Match the full Rasterio merge's global intersection and pixel rounding.
    Recomputing these against every output block shifts fractional tile origins.
    """
    from rasterio.windows import Window, bounds, from_bounds

    transform = profile["transform"]
    west, south, east, north = bounds(
        Window(0, 0, profile["width"], profile["height"]), transform)
    placements = []
    for source in sources:
        overlap = (max(west, source.bounds.left), max(south, source.bounds.bottom),
                   min(east, source.bounds.right), min(north, source.bounds.top))
        if overlap[0] >= overlap[2] or overlap[1] >= overlap[3]:
            continue
        source_window = from_bounds(*overlap, source.transform)
        target = from_bounds(*overlap, transform)
        target = Window(math.floor(target.col_off + 0.1), math.floor(target.row_off + 0.1),
                        math.floor(target.width + 0.5), math.floor(target.height + 0.5))
        if target.width and target.height:
            placements.append((source, source_window, target))
    return placements


def _read_mosaic_window(placements, window, dtype, nodata):
    """Read one first-valid-pixel mosaic block using globally anchored sampling."""
    import numpy as np
    from rasterio.windows import Window

    fill = 0 if nodata is None else nodata
    result = np.full((int(window.height), int(window.width)), fill, dtype=dtype)
    for source, source_window, target in placements:
        left, top = max(window.col_off, target.col_off), max(window.row_off, target.row_off)
        right = min(window.col_off + window.width, target.col_off + target.width)
        bottom = min(window.row_off + window.height, target.row_off + target.height)
        if left >= right or top >= bottom:
            continue
        width, height = int(right - left), int(bottom - top)
        scale_x, scale_y = source_window.width / target.width, source_window.height / target.height
        sampling = Window(source_window.col_off + (left - target.col_off) * scale_x,
                          source_window.row_off + (top - target.row_off) * scale_y,
                          width * scale_x, height * scale_y)
        data = source.read(1, window=sampling, out_shape=(height, width), masked=True)
        row, column = int(top - window.row_off), int(left - window.col_off)
        region = result[row:row + height, column:column + width]
        if np.isnan(fill):
            empty = np.isnan(region)
        elif np.issubdtype(region.dtype, np.floating):
            empty = np.isclose(region, fill)
        else:
            empty = region == fill
        np.copyto(region, data.data, where=empty & ~np.ma.getmaskarray(data), casting="unsafe")
    return result


@bulk_export
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
    if not band_urls:
        raise ValueError("No band URLs to merge")

    # Local imports: heavy deps only needed for the merge step.
    import numpy as np
    import rasterio

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
                    data = measure_download('transfer', _fetch_with_retry, url)
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

        merge_local_band_tiffs(paths, out_path, categorical=categorical, preserve_masks=preserve_masks)
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


def merge_local_band_tiffs(paths, out_path, *, categorical=False, preserve_masks=False, check=lambda: None):
    """Mosaic verified local transport tiles using the established block writer."""
    import numpy as np
    import rasterio
    order = list(paths)
    if not order or any(not values for values in paths.values()):
        raise ValueError("No local band tiles to merge")
    # Mosaic and stack in bounded windows; never allocate a full band/image.
    from contextlib import ExitStack
    from rasterio.transform import from_origin
    from .raster_grid import GEOTIFF_OPTIONS, atomic_raster_path

    dtype = "uint8" if categorical else "float32"
    with atomic_raster_path(out_path) as temporary, ExitStack() as output_context:
        dst = None
        for index, base in enumerate(order, 1):
            with ExitStack() as sources_context:
                srcs = [sources_context.enter_context(rasterio.open(p)) for p in paths[base]]
                ref = srcs[0]
                profile = ref.profile.copy()
                if len(srcs) > 1:
                    if any(s.crs != ref.crs or s.transform.b or s.transform.d
                           or s.transform.a <= 0 or s.transform.e >= 0 for s in srcs):
                        raise RuntimeError(f"Band {base} tiles must share a north-up CRS/grid")
                    west = min(s.bounds.left for s in srcs)
                    south = min(s.bounds.bottom for s in srcs)
                    east = max(s.bounds.right for s in srcs)
                    north = max(s.bounds.top for s in srcs)
                    profile.update(
                        width=int(round((east - west) / ref.res[0])),
                        height=int(round((north - south) / ref.res[1])),
                        transform=from_origin(west, north, *ref.res),
                    )
                if dst is None:
                    if categorical:
                        profile.update(nodata=DYNAMIC_WORLD_NODATA)
                    elif preserve_masks:
                        profile.update(nodata=float("nan"))
                    profile.update(count=len(order), dtype=dtype, **GEOTIFF_OPTIONS)
                    dst = output_context.enter_context(rasterio.open(temporary, "w", **profile))
                elif (profile["width"] != dst.width or profile["height"] != dst.height
                      or ref.crs != dst.crs
                      or not profile["transform"].almost_equals(dst.transform, precision=1e-9)):
                    raise RuntimeError(f"Band {base} grid does not match the first band")

                placements = _mosaic_placements(srcs, profile) if len(srcs) > 1 else None
                for _, window in dst.block_windows(index):
                    check()
                    if len(srcs) == 1:
                        data = ref.read(1, window=window, masked=preserve_masks)
                        if preserve_masks:
                            data = data.astype(dtype).filled(dst.nodata)
                    else:
                        mosaic_nodata = float("nan") if preserve_masks and not categorical else ref.nodata
                        data = _read_mosaic_window(placements, window, dtype, mosaic_nodata)
                    dst.write(data.astype(dtype, copy=False), index, window=window)
                dst.set_band_description(index, base)
                dst.update_tags(index, **ref.tags(1))
                scales, offsets, units = list(dst.scales), list(dst.offsets), list(dst.units)
                scales[index - 1], offsets[index - 1], units[index - 1] = ref.scales[0], ref.offsets[0], ref.units[0]
                dst.scales, dst.offsets, dst.units = scales, offsets, units
                if index == 1:
                    dst.update_tags(**ref.tags())
