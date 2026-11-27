"""Export one optical Earth Engine asset on its full native reference grid."""
from pathlib import Path
import shutil

from affine import Affine
import ee
import rasterio
from rasterio.crs import CRS

from . import imagery
from .download_models import DownloadSettings
from .export_resources import bulk_export


# 2816 x 2816 Float32 pixels fit below GEE's 32 MiB per-request limit.
_TILE_SIZE = 2816
_REFERENCE_BANDS = {'sentinel2': 'B2', 'landsat': 'SR_B2'}


class FullSceneDiskSpaceError(OSError):
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


def _band_tiles(image, bands, width, height, transform, crs):
    options = {'crs': crs.to_string(), 'format': 'GEO_TIFF', 'filePerBand': False}
    for band in bands:
        one_band = image.select(band).unmask(imagery._GEE_FLOAT_NODATA, sameFootprint=False).toFloat()
        for row in range(0, height, _TILE_SIZE):
            for col in range(0, width, _TILE_SIZE):
                tile_transform = transform * Affine.translation(col, row)
                tag = f'_t{row}_{col}'
                yield band + tag, imagery._DownloadURL(one_band, {
                    **options,
                    'crs_transform': list(tile_transform)[:6],
                    'dimensions': [min(_TILE_SIZE, width - col), min(_TILE_SIZE, height - row)],
                })


@bulk_export
def export_full_scene(asset_id, dataset, destination: Path, *, s2_auxiliary_bands=None, s2_band_preset=None):
    reference_band = _REFERENCE_BANDS[dataset]
    image = imagery.load_timeseries_scene(asset_id, dataset)
    width, height, transform, crs = _native_grid(asset_id, reference_band)
    bands = (imagery.s2_export_bands(s2_auxiliary_bands, s2_band_preset=s2_band_preset)
             if dataset == 'sentinel2' else imagery.timeseries_source_metadata(dataset)['bands'])
    raw_bytes = width * height * len(bands) * 4
    if shutil.disk_usage(destination.parent).free < DownloadSettings().disk_reserve(raw_bytes):
        raise FullSceneDiskSpaceError('Insufficient disk space for full scene')

    tiles = list(_band_tiles(image, bands, width, height, transform, crs))
    imagery.merge_band_tiffs(tiles, destination, preserve_masks=True)
    with rasterio.open(destination, 'r+') as output:
        if (output.shape != (height, width) or output.crs != crs or
                not output.transform.almost_equals(transform, precision=1e-9) or
                output.descriptions != tuple(bands)):
            raise ValueError('Exported GeoTIFF does not match the complete scene grid')
        output.update_tags(SOURCE_ASSET=asset_id, EXPORT_EXTENT='full_scene', GRID_REFERENCE_BAND=reference_band)
