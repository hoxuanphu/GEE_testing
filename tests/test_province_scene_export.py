"""Native tile selection and exact local masking for one GEE province scene."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
import rasterio
from affine import Affine
from rasterio.crs import CRS
from shapely.geometry import Polygon, box, mapping

from app import full_scene_export as export


class ProvinceSceneExportTests(unittest.TestCase):
    def test_selects_only_intersecting_tiles_and_masks_on_reference_grid(self):
        crs = CRS.from_epsg(4326)
        transform = Affine(1, 0, 0, 0, -1, 8)
        province = mapping(Polygon([(0, 7), (7, 7), (7, 5), (2, 5), (2, 1), (0, 1)]))
        footprint = mapping(box(1, 0, 8, 8))

        def assemble(tiles, path, *, preserve_masks):
            paths = {}
            for name, request in tiles:
                band = name.split('_t')[0]
                width, height = request['dimensions']
                tile = path.with_name(name + '.tif')
                with rasterio.open(tile, 'w', driver='GTiff', width=width, height=height,
                                   count=1, dtype='float32', crs=crs,
                                   transform=Affine(*request['crs_transform'])) as output:
                    output.write(np.full((height, width), 0.25, dtype='float32'), 1)
                paths.setdefault(band, []).append(tile)
            self.assertEqual({band: len(tiles) for band, tiles in paths.items()},
                             {'B2': 5, 'B3': 5, 'B4': 5, 'B8': 5})
            export.imagery.merge_local_band_tiffs(paths, path, preserve_masks=preserve_masks)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(export, '_TILE_SIZE', 2), \
             patch.object(export, '_native_grid', return_value=(8, 8, transform, crs)), \
             patch.object(export.shutil, 'disk_usage', return_value=Mock(free=10**11)), \
             patch.object(export.imagery, 'load_timeseries_scene'), \
             patch.object(export.imagery, '_DownloadURL', side_effect=lambda image, request: request), \
             patch.object(export.imagery, 'merge_band_tiffs', side_effect=assemble):
            path = Path(directory) / 'result.tif'
            export.export_s2_province('test-asset', path, province_geometry=province,
                                      footprint=footprint, s2_band_preset='rgbnir')
            with rasterio.open(path) as result:
                self.assertEqual(result.descriptions, ('B2', 'B3', 'B4', 'B8'))
                self.assertEqual(result.shape, (6, 6))
                self.assertEqual(result.transform, transform * Affine.translation(1, 1))
                self.assertEqual(result.tags()['SOURCE_ASSET'], 'test-asset')
                data = result.read(1, masked=True)
                self.assertEqual(data.count(), 16)
                self.assertEqual(float(data[4, 0]), 0.25)
                self.assertTrue(data.mask[4, 1])

    def test_rejects_boundary_contact_without_overlapping_area(self):
        with self.assertRaises(export.SceneDoesNotCoverProvince):
            export._province_tiles(mapping(box(0, 0, 4, 4)), mapping(box(4, 0, 8, 4)),
                                   8, 8, Affine(1, 0, 0, 0, -1, 8), CRS.from_epsg(4326))
