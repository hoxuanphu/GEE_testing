"""Mask final raster blocks against an exact WGS84 polygon without losing band masks."""
from pathlib import Path
import os
import uuid

import numpy as np
import rasterio
from rasterio.features import geometry_mask
from rasterio.warp import transform_geom

from .raster_grid import GEOTIFF_OPTIONS


def mask_raster_to_polygon(path: Path, geometry: dict) -> None:
    path = Path(path)
    temporary = path.with_name(f'.{path.stem}.{uuid.uuid4().hex}.mask.tif')
    try:
        with rasterio.open(path) as source:
            if source.crs is None:
                raise ValueError('Raster has no CRS for polygon masking')
            projected = transform_geom('EPSG:4326', source.crs, geometry)
            dtype = np.dtype(source.dtypes[0])
            nodata = source.nodata
            if nodata is None:
                nodata = float('nan') if np.issubdtype(dtype, np.floating) else np.iinfo(dtype).max
            profile = source.profile.copy()
            # Dataset profiles do not retain the BigTIFF creation option.
            profile.update(nodata=nodata, **GEOTIFF_OPTIONS)
            with rasterio.open(temporary, 'w', **profile) as target:
                target.update_tags(**source.tags())
                for band in range(1, source.count + 1):
                    target.update_tags(band, **source.tags(band))
                    if source.descriptions[band - 1]:
                        target.set_band_description(band, source.descriptions[band - 1])
                    if source.units[band - 1]:
                        target.set_band_unit(band, source.units[band - 1])
                for _, window in source.block_windows(1):
                    data = source.read(window=window, masked=True)
                    outside = geometry_mask([projected], out_shape=(int(window.height), int(window.width)),
                                            transform=source.window_transform(window), all_touched=False)
                    data.mask = np.ma.getmaskarray(data) | outside[None, :, :]
                    target.write(data.filled(nodata), window=window)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
