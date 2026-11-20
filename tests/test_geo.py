"""Offline contracts for the shared clockwise local-square geometry."""
import math
import unittest

from app.geo import RectangleROI, point_bbox, point_roi
from app.raster_grid import build_rectangle_grid


class TestRectangleROI(unittest.TestCase):
    def test_rotated_polygon_and_envelope_preserve_dimensions(self):
        roi = RectangleROI(center_lon=107, center_lat=10, width_m=200, height_m=100, rotation_deg=30)
        ring = roi.geometry["coordinates"][0][:-1]
        self.assertEqual(roi.geometry["coordinates"][0][0], roi.geometry["coordinates"][0][-1])
        self.assertEqual(roi.bbox.west, min(point[0] for point in ring))
        self.assertEqual(roi.bbox.north, max(point[1] for point in ring))
        for first, second in zip(ring, ring[1:] + ring[:1]):
            east = (second[0] - first[0]) * 111320 * math.cos(math.radians(10))
            north = (second[1] - first[1]) * 111320
            length = math.hypot(east, north)
            self.assertIn(round(length), (100, 200))
        self.assertEqual(roi.model_dump(), {
            "center_lon": 107.0, "center_lat": 10.0,
            "width_m": 200.0, "height_m": 100.0, "rotation_deg": 30.0,
        })

    def test_zero_and_full_turn_match_axis_aligned_rectangle(self):
        zero = RectangleROI(center_lon=107, center_lat=10, width_m=200, height_m=100, rotation_deg=0)
        full = RectangleROI(center_lon=107, center_lat=10, width_m=200, height_m=100, rotation_deg=360)
        self.assertEqual(zero.geometry, full.geometry)
        self.assertEqual(zero.bbox, full.bbox)
        self.assertEqual(zero.bbox.west, 107 - 100 / (111320 * math.cos(math.radians(10))))
        self.assertEqual(zero.bbox.south, 10 - 50 / 111320)

    def test_grid_inputs_follow_rectangle_dimensions(self):
        roi = RectangleROI(center_lon=107, center_lat=10, width_m=205, height_m=95, rotation_deg=45)
        grid = roi.grid_inputs(10)
        self.assertEqual(grid, build_rectangle_grid(roi, 10))
        self.assertEqual((grid["width"], grid["height"]), (21, 10))
        self.assertEqual(grid["actual_pixel_width_m"], 205 / 21)
        self.assertEqual(grid["actual_pixel_height_m"], 9.5)

    def test_invalid_values_and_domain_crossings(self):
        invalid = [
            dict(center_lon=0, center_lat=0, width_m=0, height_m=1),
            dict(center_lon=0, center_lat=0, width_m=1, height_m=-1),
            dict(center_lon=0, center_lat=0, width_m=90001, height_m=1),
            dict(center_lon=0, center_lat=0, width_m=1, height_m=1, rotation_deg=361),
            dict(center_lon=0, center_lat=0, width_m=1, height_m=1, rotation_deg=float("nan")),
            dict(center_lon=181, center_lat=0, width_m=1, height_m=1),
            dict(center_lon=0, center_lat=85.05112878, width_m=1, height_m=1),
        ]
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValueError):
                RectangleROI(**values)
        with self.assertRaises(ValueError):
            RectangleROI(center_lon=180, center_lat=0, width_m=1000, height_m=1000)


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
