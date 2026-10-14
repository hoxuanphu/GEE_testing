"""Tests for the standalone Dynamic World colorization utility."""
from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from colorize_dynamic_world import (  # noqa: E402
    DYNAMIC_WORLD_CLASSES,
    colorize,
)


class TestColorizeDynamicWorld(unittest.TestCase):
    def _input_raster(self, directory: str) -> Path:
        path = Path(directory) / "labels.tif"
        labels = np.array(
            [
                [0, 1, 2, 3],
                [4, 5, 6, 7],
                [8, 255, 99, 4],
                [0, 1, 255, 8],
            ],
            dtype=np.uint8,
        )
        with rasterio.open(
            path,
            "w",
            driver="GTiff",
            height=labels.shape[0],
            width=labels.shape[1],
            count=1,
            dtype="uint8",
            crs="EPSG:4326",
            transform=from_origin(107.0, 11.0, 0.0001, 0.0001),
            nodata=255,
        ) as dst:
            dst.write(labels, 1)
            dst.set_band_description(1, "label")
        return path

    def test_colorized_geotiff_preserves_grid_palette_and_transparency(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = self._input_raster(tmp)
            output_path = Path(tmp) / "colorized.tif"
            result = colorize(input_path, output_path, block_size=2)

            with rasterio.open(input_path) as source, rasterio.open(output_path) as dst:
                self.assertEqual(dst.count, 4)
                self.assertEqual(dst.dtypes, ("uint8",) * 4)
                self.assertEqual(dst.crs, source.crs)
                self.assertEqual(dst.transform, source.transform)
                self.assertEqual((dst.width, dst.height), (source.width, source.height))
                self.assertEqual(dst.nodata, None)
                self.assertEqual(
                    dst.colorinterp,
                    (
                        ColorInterp.red,
                        ColorInterp.green,
                        ColorInterp.blue,
                        ColorInterp.alpha,
                    ),
                )
                rgba = dst.read()

            for class_id, (_name, hex_colour) in DYNAMIC_WORLD_CLASSES.items():
                row, col = np.argwhere(
                    np.array(
                        [
                            [0, 1, 2, 3],
                            [4, 5, 6, 7],
                            [8, 255, 99, 4],
                            [0, 1, 255, 8],
                        ],
                        dtype=np.uint8,
                    )
                    == class_id
                )[0]
                rgb = tuple(int(hex_colour[index : index + 2], 16) for index in (0, 2, 4))
                self.assertEqual(tuple(rgba[:3, row, col]), rgb)
                self.assertEqual(rgba[3, row, col], 255)

            # Class 0 is water and remains a visible blue pixel, not NoData.
            self.assertEqual(tuple(rgba[:3, 0, 0]), (65, 155, 223))
            self.assertEqual(rgba[3, 0, 0], 255)

            # Explicit 255 and out-of-range 99 are transparent by default.
            self.assertEqual(tuple(rgba[:3, 2, 1]), (0, 0, 0))
            self.assertEqual(rgba[3, 2, 1], 0)
            self.assertEqual(tuple(rgba[:3, 2, 2]), (0, 0, 0))
            self.assertEqual(rgba[3, 2, 2], 0)

            self.assertEqual(result["color_tif"], output_path)
            self.assertTrue(result["png"].exists())
            self.assertTrue(result["legend"].exists())

            with Image.open(result["png"]) as preview:
                self.assertEqual(preview.mode, "RGBA")
                self.assertEqual(preview.size, (4, 4))
                self.assertEqual(preview.getpixel((0, 0)), (65, 155, 223, 255))
                self.assertEqual(preview.getpixel((1, 2)), (0, 0, 0, 0))

            with result["legend"].open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 10)
            self.assertEqual(
                {int(row["class_id"]) for row in rows},
                set(DYNAMIC_WORLD_CLASSES) | {255},
            )
            self.assertEqual(rows[0]["hex_colour"], "#419bdf")
            self.assertEqual(rows[-1]["class_name"], "NoData")

    def test_black_nodata_is_opaque_without_changing_valid_colors(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_path = self._input_raster(tmp)
            output_path = Path(tmp) / "black.tif"
            colorize(input_path, output_path, nodata_mode="black", block_size=1)

            with rasterio.open(output_path) as dst:
                rgba = dst.read()

            self.assertEqual(tuple(rgba[:, 2, 1]), (0, 0, 0, 255))
            self.assertEqual(tuple(rgba[:, 2, 2]), (0, 0, 0, 255))
            self.assertEqual(tuple(rgba[:, 0, 0]), (65, 155, 223, 255))

    def test_rejects_non_integer_label_band(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "float.tif"
            with rasterio.open(
                path,
                "w",
                driver="GTiff",
                height=2,
                width=2,
                count=1,
                dtype="float32",
            ) as dst:
                dst.write(np.ones((2, 2), dtype=np.float32), 1)

            with self.assertRaisesRegex(ValueError, "integer dtype"):
                colorize(path, Path(tmp) / "out.tif")


if __name__ == "__main__":
    unittest.main()
