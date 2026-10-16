"""Offline contracts for the shared clockwise local-square geometry."""
import math
import unittest

from app.geo import point_bbox, point_roi


class TestPointROI(unittest.TestCase):
    def test_zero_and_full_turn_are_exactly_legacy(self):
        for angle in (0, 360):
            geometry, bbox = point_roi(107, 10, 2000, angle)
            self.assertEqual(bbox, point_bbox(107, 10, 2000))
            self.assertEqual(geometry["coordinates"][0][0], [bbox.west, bbox.south])
            self.assertEqual(geometry["coordinates"][0][0], geometry["coordinates"][0][-1])

    def test_clockwise_vertices_and_envelope(self):
        for angle in (30, 45, 90, 135, 270):
            geometry, bbox = point_roi(107, 10, 2000, angle)
            ring = geometry["coordinates"][0]
            for lon, lat in ring:
                self.assertTrue(bbox.west <= lon <= bbox.east)
                self.assertTrue(bbox.south <= lat <= bbox.north)
                x = (lon - 107) * 111320 * math.cos(math.radians(10))
                y = (lat - 10) * 111320
                self.assertAlmostEqual(math.hypot(x, y), math.sqrt(2)*2000, places=6)
            expected = 2000 * (abs(math.cos(math.radians(angle))) + abs(math.sin(math.radians(angle))))
            self.assertAlmostEqual((bbox.north-10)*111320, expected, places=6)
        ring = point_roi(0, 0, 100, 90)[0]["coordinates"][0]
        self.assertLess(ring[0][0], 0)
        self.assertGreater(ring[0][1], 0)

    def test_invalid_values_and_crossings(self):
        invalid = [(float("nan"), 0, 1, 0), (0, float("inf"), 1, 0), (0, 0, 0, 0),
                   (0, 0, 45001, 0), (0, 0, 1, -1), (0, 0, 1, 361),
                   (0, 0, 1, float("nan")), (180, 0, 1000, 30),
                   (-180, 0, 1000, 0), (0, 85.05112878, 1000, 45), (181, 0, 1, 0)]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                point_roi(*args)
