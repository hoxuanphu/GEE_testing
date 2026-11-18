"""Actual final raster masking preserves holes, components, zeros and band NoData."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin


class VectorMaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'result.tif'

    def raster(self, nodata=-9999):
        with rasterio.open(self.path, 'w', driver='GTiff', width=4, height=4, count=2,
                           dtype='float32', nodata=nodata, crs='EPSG:4326',
                           transform=from_origin(0, 4, 1, 1)) as dst:
            first = np.ones((4, 4), dtype='float32')
            first[0, 0] = 0
            if nodata is not None:
                first[0, 1] = nodata
            dst.write(first, 1)
            dst.write(np.full((4, 4), 7, dtype='float32'), 2)
            dst.set_band_description(1, 'B4')
            dst.set_band_description(2, 'B8')
            dst.update_tags(source='fixture')
            dst.update_tags(1, units='reflectance')

    def test_hole_is_nodata_and_valid_zero_and_per_band_nodata_survive(self):
        from app.vector_mask import mask_raster_to_polygon
        self.raster()
        geometry = {'type': 'Polygon', 'coordinates': [
            [[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]],
            [[1, 1], [1, 3], [3, 3], [3, 1], [1, 1]]]}
        mask_raster_to_polygon(self.path, geometry)
        with rasterio.open(self.path) as src:
            data = src.read(masked=True)
            self.assertTrue(data.mask[:, 1:3, 1:3].all())
            self.assertFalse(data.mask[0, 0, 0])
            self.assertEqual(data[0, 0, 0], 0)
            self.assertTrue(data.mask[0, 0, 1])
            self.assertFalse(data.mask[1, 0, 1])
            self.assertEqual(src.descriptions, ('B4', 'B8'))
            self.assertEqual(src.tags()['source'], 'fixture')
            self.assertEqual(src.tags(1)['units'], 'reflectance')

    def test_disconnected_components_leave_gap_nodata_without_nodata_in_source(self):
        from app.vector_mask import mask_raster_to_polygon
        self.raster(nodata=None)
        geometry = {'type': 'MultiPolygon', 'coordinates': [
            [[[0, 0], [1, 0], [1, 4], [0, 4], [0, 0]]],
            [[[3, 0], [4, 0], [4, 4], [3, 4], [3, 0]]]]}
        mask_raster_to_polygon(self.path, geometry)
        with rasterio.open(self.path) as src:
            data = src.read(masked=True)
            self.assertTrue(data.mask[:, :, 1:3].all())
            self.assertFalse(data.mask[:, :, [0, 3]].any())
            self.assertEqual(data[0, 0, 0], 0)


if __name__ == '__main__':
    unittest.main()
