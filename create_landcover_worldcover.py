"""Create an ESA WorldCover land-cover map for an existing Sentinel-2 GeoTIFF.

The input image is used only as the area/grid reference. The land-cover values
come from ``ESA/WorldCover/v200`` in Google Earth Engine and are categorical
class IDs (not reflectance values).

Example:
    .venv\\Scripts\\python.exe create_landcover_worldcover.py \
        downloads\\sentinel2_20250228T031721_20250228T032919_T48PWS_2025-02-28_crop.tif

Outputs are written next to the input by default:
    *_worldcover.tif       categorical GeoTIFF (nearest-neighbour aligned)
    *_worldcover.png       colourized preview
    *_worldcover_legend.csv

Earth Engine credentials are loaded using the same ``.env`` and
``ee_private_key.json`` conventions as the FastAPI application.
"""
from __future__ import annotations

import argparse
import csv
import math
import shutil
import tempfile
from pathlib import Path

import ee
import numpy as np
import requests
import rasterio
from PIL import Image
from rasterio.merge import merge as rio_merge
from rasterio.warp import Resampling, reproject

from app.ee_session import init_ee


COLLECTION = "ESA/WorldCover/v200"
BAND = "Map"
SCALE_M = 10

# ESA WorldCover v200 classes and the official-ish WorldCover colours.
CLASSES: dict[int, tuple[str, str]] = {
    10: ("Tree cover", "006400"),
    20: ("Shrubland", "FFBB22"),
    30: ("Grassland", "FFFF4C"),
    40: ("Cropland", "F096FF"),
    50: ("Built-up", "FA0000"),
    60: ("Bare / sparse vegetation", "B4B4B4"),
    70: ("Snow and ice", "F0F0F0"),
    80: ("Permanent water bodies", "0064C8"),
    90: ("Herbaceous wetland", "0096A0"),
    95: ("Mangroves", "00CF75"),
    100: ("Moss and lichen", "FAE6A0"),
}


def _hex_rgb(value: str) -> tuple[int, int, int]:
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _download_bytes(url: str, timeout: int = 600) -> bytes:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content


def _tile_count(width: int, height: int, max_pixels: int) -> int:
    """Return an n for an n x n grid whose tiles fit max_pixels."""
    return max(1, math.ceil(math.sqrt((width * height) / max_pixels)))


def _tile_bounds(bounds, n: int, col: int, row: int) -> tuple[float, float, float, float]:
    dx = (bounds.right - bounds.left) / n
    dy = (bounds.top - bounds.bottom) / n
    left = bounds.left + col * dx
    right = bounds.left + (col + 1) * dx
    # Earth Engine accepts west, south, east, north.
    bottom = bounds.bottom + row * dy
    top = bounds.bottom + (row + 1) * dy
    return left, bottom, right, top


def _worldcover_image() -> ee.Image:
    # WorldCover v200 is a single global 2021 map image collection.
    return ee.ImageCollection(COLLECTION).first().select(BAND)


def _download_tiles(input_path: Path, tmp_dir: Path, max_pixels: int) -> tuple[list[Path], int]:
    with rasterio.open(input_path) as src:
        width, height, bounds = src.width, src.height, src.bounds

    n = _tile_count(width, height, max_pixels)
    image = _worldcover_image()
    tile_paths: list[Path] = []

    for row in range(n):
        for col in range(n):
            west, south, east, north = _tile_bounds(bounds, n, col, row)
            region = ee.Geometry.Rectangle([west, south, east, north], geodesic=False)
            params = {
                "name": f"worldcover_{row}_{col}",
                "scale": SCALE_M,
                "crs": "EPSG:4326",
                "region": region,
                "format": "GEO_TIFF",
                "filePerBand": False,
            }
            print(f"Downloading tile {row + 1}/{n} x {col + 1}/{n} ...", flush=True)
            url = image.getDownloadURL(params)
            tile_path = tmp_dir / f"tile_{row:03d}_{col:03d}.tif"
            tile_path.write_bytes(_download_bytes(url))
            tile_paths.append(tile_path)
    return tile_paths, n


def _write_outputs(input_path: Path, tile_paths: list[Path], output_path: Path) -> None:
    with rasterio.open(input_path) as ref:
        profile = ref.profile.copy()
        dst_transform = ref.transform
        dst_crs = ref.crs
        dst_height, dst_width = ref.height, ref.width
        dst_bounds = ref.bounds

    # Mosaic tiles first; then reproject using nearest-neighbour to the exact
    # dimensions/transform of the Sentinel-2 input grid.
    sources = [rasterio.open(path) for path in tile_paths]
    try:
        mosaic, mosaic_transform = rio_merge(sources, method="first")
        mosaic_crs = sources[0].crs
        source = mosaic[0]
    finally:
        for src in sources:
            src.close()

    result = np.zeros((dst_height, dst_width), dtype=np.uint8)
    reproject(
        source=source,
        destination=result,
        src_transform=mosaic_transform,
        src_crs=mosaic_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        resampling=Resampling.nearest,
        src_nodata=0,
        dst_nodata=0,
    )

    profile.update(
        driver="GTiff",
        height=dst_height,
        width=dst_width,
        count=1,
        dtype="uint8",
        nodata=0,
        compress="deflate",
        tiled=True,
        transform=dst_transform,
        crs=dst_crs,
    )
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(result, 1)
        dst.set_band_description(1, "WorldCover class")
        dst.update_tags(
            dataset=COLLECTION,
            source_input=str(input_path),
            class_values=",".join(str(k) for k in CLASSES),
            note="Categorical classes; use nearest-neighbour resampling.",
        )

    _write_png(result, output_path.with_suffix(".png"))
    _write_legend(output_path.with_suffix(".legend.csv"))


def _write_png(data: np.ndarray, path: Path) -> None:
    rgb = np.zeros((*data.shape, 3), dtype=np.uint8)
    for class_id, (_name, colour) in CLASSES.items():
        rgb[data == class_id] = _hex_rgb(colour)
    Image.fromarray(rgb, mode="RGB").save(path)


def _write_legend(path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["class_id", "class_name", "hex_colour"])
        for class_id, (name, colour) in CLASSES.items():
            writer.writerow([class_id, name, f"#{colour}"])


def create_landcover(input_path: Path, output_path: Path, max_pixels: int) -> None:
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if output_path.resolve() == input_path.resolve():
        raise ValueError("Output path must differ from the input path")

    init_ee()
    tmp_root = Path(tempfile.mkdtemp(prefix="worldcover_", dir=input_path.parent))
    try:
        tile_paths, n = _download_tiles(input_path, tmp_root, max_pixels)
        print(f"Downloaded {len(tile_paths)} tile(s) using a {n}x{n} grid.")
        _write_outputs(input_path, tile_paths, output_path)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Existing Sentinel-2 GeoTIFF")
    parser.add_argument(
        "-o", "--output", type=Path,
        help="Output categorical GeoTIFF (default: <input>_worldcover.tif)",
    )
    parser.add_argument(
        "--max-pixels", type=int, default=5_000_000,
        help="Maximum pixels per Earth Engine tile (default: 5,000,000)",
    )
    args = parser.parse_args()
    if args.max_pixels <= 0:
        parser.error("--max-pixels must be positive")

    output = args.output or args.input.with_name(f"{args.input.stem}_worldcover.tif")
    create_landcover(args.input, output, args.max_pixels)
    print(f"Land-cover GeoTIFF: {output}")
    print(f"PNG preview:        {output.with_suffix('.png')}")
    print(f"Legend CSV:         {output.with_suffix('.legend.csv')}")


if __name__ == "__main__":
    main()
