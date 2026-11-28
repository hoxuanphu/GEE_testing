"""Assemble tiled Earth Engine exports and apply province boundaries locally."""
import math
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from affine import Affine
import ee
import rasterio
from rasterio.crs import CRS
from rasterio.warp import transform_geom
from shapely import get_parts, make_valid, union_all
from shapely.geometry import box, mapping, shape

from . import imagery
from .cog_writer import write_cog
from .download_models import DownloadSettings
from .export_resources import (
    CURRENT_SCENE, ExportDiskSpaceError as FullSceneDiskSpaceError, bulk_export, scene_bulk_export,
)
from .raster_grid import atomic_raster_path
from .vector_mask import mask_raster_to_polygon


# 2816 x 2816 Float32 pixels fit below GEE's 32 MiB per-request limit.
_TILE_SIZE = 2816
_REFERENCE_BANDS = {'sentinel2': 'B2', 'landsat': 'SR_B2'}


class SceneDoesNotCoverProvince(ValueError):
    pass


def _native_grid(asset_id, reference_band):
    band = ee.Image(asset_id).select(reference_band).getInfo()['bands'][0]
    width, height = band['dimensions']
    transform = Affine(*band['crs_transform'])
    crs = CRS.from_user_input(band['crs'])
    if (not width > 0 or not height > 0 or transform.b or transform.d or
            not transform.a > 0 or not transform.e < 0 or crs.to_epsg() is None):
        raise ValueError('Invalid native Earth Engine scene grid')
    return width, height, transform, crs


def _tile_windows(col_start, row_start, col_stop, row_stop):
    for row in range(row_start, row_stop, _TILE_SIZE):
        for col in range(col_start, col_stop, _TILE_SIZE):
            yield col, row, min(_TILE_SIZE, col_stop - col), min(_TILE_SIZE, row_stop - row)


def _band_tiles(image, bands, windows, transform, crs):
    options = {'crs': crs.to_string(), 'format': 'GEO_TIFF', 'filePerBand': False}
    for band in bands:
        one_band = image.select(band).unmask(imagery._GEE_FLOAT_NODATA, sameFootprint=False).toFloat()
        for col, row, width, height in windows:
            tile_transform = transform * Affine.translation(col, row)
            tag = f'_t{row}_{col}'
            yield band + tag, imagery._DownloadURL(one_band, {
                **options,
                'crs_transform': list(tile_transform)[:6],
                'dimensions': [width, height],
            })


def _province_tiles(province_geometry, footprint, width, height, transform, crs):
    province = make_valid(shape(province_geometry))
    scene = make_valid(shape(footprint))
    overlap = union_all([part for part in get_parts(province.intersection(scene)) if part.area > 0])
    if overlap.is_empty:
        raise SceneDoesNotCoverProvince('Scene footprint does not overlap the province')

    geometry = mapping(overlap)
    projected = shape(transform_geom('EPSG:4326', crs, geometry))
    upper_left = transform * (0, 0)
    lower_right = transform * (width, height)
    scene_grid = box(*upper_left, *lower_right)
    projected = projected.intersection(scene_grid)
    if projected.area <= 0:
        raise SceneDoesNotCoverProvince('Scene grid does not overlap the province')

    west, south, east, north = projected.bounds
    left, top = ~transform * (west, north)
    right, bottom = ~transform * (east, south)
    col_start, row_start = max(0, math.floor(left)), max(0, math.floor(top))
    col_stop, row_stop = min(width, math.ceil(right)), min(height, math.ceil(bottom))
    windows = []
    for col, row, w, h in _tile_windows(col_start, row_start, col_stop, row_stop):
        west, north = transform * (col, row)
        east, south = transform * (col + w, row + h)
        if projected.relate_pattern(box(west, south, east, north), 'T********'):
            windows.append((col, row, w, h))
    cropped_transform = transform * Affine.translation(col_start, row_start)
    return geometry, windows, (col_stop - col_start, row_stop - row_start, cropped_transform)


def _validate_and_tag(path, *, width, height, transform, crs, bands, **tags):
    with rasterio.open(path, 'r+') as output:
        if (output.shape != (height, width) or output.crs != crs or
                not output.transform.almost_equals(transform, precision=1e-9) or
                output.descriptions != tuple(bands)):
            raise ValueError('Exported GeoTIFF does not match the requested grid')
        output.update_tags(**tags)


def _check_scene_disk(destination, raw_bytes, message):
    scene = CURRENT_SCENE.get()
    if scene is not None:
        scene.reserve(destination, raw_bytes)
    elif shutil.disk_usage(destination.parent).free < DownloadSettings().disk_reserve(raw_bytes):
        raise FullSceneDiskSpaceError(message)


def _export_native_scene(asset_id, image, destination, bands, reference_band, **tags):
    width, height, transform, crs = _native_grid(asset_id, reference_band)
    raw_bytes = width * height * len(bands) * 4
    _check_scene_disk(destination, raw_bytes, 'Insufficient disk space for full scene')

    windows = list(_tile_windows(0, 0, width, height))
    tiles = list(_band_tiles(image, bands, windows, transform, crs))
    imagery.merge_band_tiffs(tiles, destination, preserve_masks=True)
    tags.update(SOURCE_ASSET=asset_id, EXPORT_EXTENT='full_scene', GRID_REFERENCE_BAND=reference_band)
    _validate_and_tag(destination, width=width, height=height, transform=transform, crs=crs,
                      bands=bands, **tags)


@scene_bulk_export
def export_full_scene(asset_id, dataset, destination: Path, *, s2_auxiliary_bands=None, s2_band_preset=None):
    image = imagery.load_timeseries_scene(asset_id, dataset)
    bands = (imagery.s2_export_bands(s2_auxiliary_bands, s2_band_preset=s2_band_preset)
             if dataset == 'sentinel2' else imagery.timeseries_source_metadata(dataset)['bands'])
    _export_native_scene(asset_id, image, destination, bands, _REFERENCE_BANDS[dataset])


@scene_bulk_export
def export_s1_full_scene(asset_id, scene, destination: Path, *, polarization):
    from .s1_mosaic import s1_export_tags

    bands = imagery.S1_POLARIZATIONS[polarization]
    transfer = CURRENT_SCENE.get()
    with TemporaryDirectory(prefix='.s1-cog-', dir=destination.parent) as work:
        merged = Path(work) / 'merged.tif'
        cog = Path(work) / 'output.tif'
        _export_native_scene(asset_id, scene.ee_object, merged, bands, bands[0],
                             **s1_export_tags(scene, polarization, extent='full_scene'))
        if transfer is not None:
            transfer.check()
            transfer.update(stage='writing_cog')
        write_cog(merged, cog, check=transfer.check if transfer is not None else lambda: None)
        with rasterio.open(cog) as result:
            if result.tags(ns='IMAGE_STRUCTURE').get('LAYOUT') != 'COG':
                raise ValueError('GDAL did not produce a Cloud Optimized GeoTIFF')
        if transfer is not None:
            transfer.check()
        cog.replace(destination)


@scene_bulk_export
def export_s2_province(asset_id, destination: Path, *, province_geometry, footprint,
                       s2_auxiliary_bands=None, s2_band_preset=None):
    reference_band = _REFERENCE_BANDS['sentinel2']
    width, height, transform, crs = _native_grid(asset_id, reference_band)
    if footprint is None:
        # Older search snapshots might not contain a footprint.
        footprint = ee.Image(asset_id).geometry().getInfo()
    geometry, windows, (out_width, out_height, out_transform) = _province_tiles(
        province_geometry, footprint, width, height, transform, crs)
    bands = imagery.s2_export_bands(s2_auxiliary_bands, s2_band_preset=s2_band_preset)
    raw_bytes = out_width * out_height * len(bands) * 4
    _check_scene_disk(destination, raw_bytes, 'Insufficient disk space for province export')

    image = imagery.load_timeseries_scene(asset_id, 'sentinel2')
    tiles = list(_band_tiles(image, bands, windows, transform, crs))
    imagery.merge_band_tiffs(tiles, destination, preserve_masks=True)
    scene = CURRENT_SCENE.get()
    check = {'check': scene.check} if scene is not None else {}
    mask_raster_to_polygon(destination, geometry, **check)
    _validate_and_tag(destination, width=out_width, height=out_height, transform=out_transform, crs=crs,
                      bands=bands, SOURCE_ASSET=asset_id, EXPORT_EXTENT='province',
                      GRID_REFERENCE_BAND=reference_band)


@bulk_export
def export_s1_province(image, destination: Path, *, province_geometry, footprint, bbox, bands):
    # The legacy SAR export requests 10 m in EPSG:4326. Snap to that scale's
    # geographic grid so tiles can be written without a second resampling.
    step = imagery.S1_SCALE / 111319.49079327358
    west, south = math.floor(bbox.west / step), math.floor(bbox.south / step)
    east, north = math.ceil(bbox.east / step), math.ceil(bbox.north / step)
    transform = Affine(step, 0, west * step, 0, -step, north * step)
    crs = CRS.from_epsg(4326)
    geometry, windows, (out_width, out_height, out_transform) = _province_tiles(
        province_geometry, footprint, east - west, north - south, transform, crs)
    raw_bytes = out_width * out_height * len(bands) * 4
    if shutil.disk_usage(destination.parent).free < DownloadSettings().disk_reserve(raw_bytes):
        raise FullSceneDiskSpaceError('Insufficient disk space for SAR province export')
    tiles = list(_band_tiles(image, bands, windows, transform, crs))
    with atomic_raster_path(destination) as temporary:
        imagery.merge_band_tiffs(tiles, temporary, preserve_masks=True)
        mask_raster_to_polygon(temporary, geometry)
        _validate_and_tag(temporary, width=out_width, height=out_height, transform=out_transform,
                          crs=crs, bands=bands, EXPORT_EXTENT='province')
