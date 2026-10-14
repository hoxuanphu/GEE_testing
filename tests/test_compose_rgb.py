"""Tests for common-mask RGB rendering."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compose_rgb import _stretch_rgb, compose  # noqa: E402


class TestStretchRgb(unittest.TestCase):
    def test_fixed_range_uses_one_scale_and_blackens_invalid_pixels(self):
        data = np.array(
            [
                [[0.00, 0.15, np.nan]],
                [[0.15, 0.30, np.nan]],
                [[0.30, 0.00, np.nan]],
            ],
            dtype=np.float32,
        )
        valid = np.array([[True, True, False]])

        result = _stretch_rgb(data, valid)

        np.testing.assert_allclose(
            result[:, 0, :2],
            np.array(
                [
                    [0.0, 0.5],
                    [0.5, 1.0],
                    [1.0, 0.0],
                ],
                dtype=np.float32,
            ),
        )
        np.testing.assert_array_equal(result[:, 0, 2], 0.0)

    def test_percentile_mode_uses_common_spatial_mask(self):
        data = np.array(
            [
                [[0.10, 0.20, 0.90]],
                [[0.10, 0.20, 0.90]],
                [[0.10, 0.20, 0.90]],
            ],
            dtype=np.float32,
        )
        valid = np.array([[True, True, False]])

        result = _stretch_rgb(
            data,
            valid,
            mode="percentile",
            display_min=0.0,
            display_max=1.0,
            percentile_low=0.0,
            percentile_high=100.0,
        )

        self.assertAlmostEqual(float(result[0, 0, 0]), 0.0)
        self.assertAlmostEqual(float(result[0, 0, 1]), 1.0)
        self.assertEqual(float(result[0, 0, 2]), 0.0)


class TestCompose(unittest.TestCase):
    def test_postprocessing_does_not_light_nodata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tif_path = root / "input.tif"
            profile = {
                "driver": "GTiff",
                "height": 1,
                "width": 2,
                "count": 3,
                "dtype": "float32",
                "nodata": -np.inf,
            }
            values = np.array(
                [
                    [[0.10, -np.inf]],
                    [[0.15, -np.inf]],
                    [[0.20, -np.inf]],
                ],
                dtype=np.float32,
            )
            with rasterio.open(tif_path, "w", **profile) as dst:
                dst.write(values)

            out_path = compose(
                tif_path,
                (1, 2, 3),
                root / "rgb",
                "rgb321",
                gamma=1.5,
                brightness=20,
                contrast=1.2,
                saturation=1.5,
                auto_color=True,
            )

            with Image.open(out_path) as image:
                output = np.asarray(image)
            self.assertEqual(tuple(output.shape), (1, 2, 3))
            np.testing.assert_array_equal(output[0, 1], [0, 0, 0])
            self.assertGreater(int(output[0, 0].max()), 0)


if __name__ == "__main__":
    unittest.main()
