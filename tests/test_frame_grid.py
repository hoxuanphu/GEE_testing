"""Exact-bound and exact-affine frame grid regression cases."""
import unittest

from affine import Affine
import numpy as np

from app.raster_grid import build_reference_grid
from app.frame_models import FrameError


class FrameGridTests(unittest.TestCase):
    def frame(self):
        return {'crs': 'EPSG:32648', 'transform': [3, 0, 398712, 0, -3, 2406162],
                'width': 1508, 'height': 1157}

    def test_known_exact_extent(self):
        frame = self.frame()
        grid = build_reference_grid(frame)
        self.assertEqual((grid['width'], grid['height']), (453, 348))
        self.assertAlmostEqual(grid['actual_pixel_width_m'], 9.986754966887418)
        self.assertAlmostEqual(grid['actual_pixel_height_m'], 9.974137931034482)
        self.assertEqual(Affine(*grid['transform']) * (453, 348), (403236, 2402691))

    def test_rotated_sheared_extent_and_exact_grid(self):
        frame = self.frame()
        t = Affine.translation(398712, 2406162) * Affine.rotation(31) * Affine.shear(8) * Affine.scale(3, -3)
        frame['transform'] = list(t)[:6]
        grid = build_reference_grid(frame)
        out = Affine(*grid['transform'])
        for source, destination in zip([(0, 0), (1508, 0), (1508, 1157), (0, 1157)],
                                       [(0, 0), (grid['width'], 0), (grid['width'], grid['height']), (0, grid['height'])]):
            np.testing.assert_allclose(t * source, out * destination, rtol=0, atol=1e-8)
        exact = build_reference_grid(frame, 'match_grid')
        self.assertEqual(exact['transform'], frame['transform'])
        self.assertEqual((exact['width'], exact['height']), (1508, 1157))

    def test_invalid_and_geographic_constraints(self):
        frame = self.frame()
        frame['crs'] = 'EPSG:4326'
        with self.assertRaises(FrameError):
            build_reference_grid(frame)
        self.assertEqual(build_reference_grid(frame, 'match_grid')['pixel_unit'], 'degree')
        frame = self.frame()
        frame['width'] = 30001
        with self.assertRaises(FrameError):
            build_reference_grid(frame, 'match_grid')
        frame['transform'] = [0] * 6
        with self.assertRaises(ValueError):
            build_reference_grid(frame, 'match_grid')
