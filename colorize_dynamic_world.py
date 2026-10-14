"""Colorize a Dynamic World label GeoTIFF for viewing.

Dynamic World downloads from the application contain one categorical ``label``
band.  The values are class IDs (0--8), not RGB reflectance values, so a
generic image viewer may display them as grayscale.  This script converts the
labels to the official Dynamic World colours while keeping the input file
unchanged.

The script writes, next to the input by default:

* ``*_color.tif`` - georeferenced RGBA GeoTIFF;
* ``*_color.png`` - a viewable PNG preview; and
* ``*_color_legend.csv`` - class names and colours.

Examples::

    .venv\\Scripts\\python.exe colorize_dynamic_world.py \\
        downloads\\dynamic_world_label.tif

    .venv\\Scripts\\python.exe colorize_dynamic_world.py \\
        downloads\\dynamic_world_label.tif --nodata black

The default NoData rendering is transparent.  Use ``--nodata black`` when a
viewer does not display transparent pixels as desired.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Literal

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import ColorInterp, Resampling
from rasterio.windows import Window


NODATA_LABEL = 255

# Official Dynamic World colours, in label order.  Keep this mapping local to
# the standalone script so it can be used without initializing Earth Engine.
DYNAMIC_WORLD_CLASSES: dict[int, tuple[str, str]] = {
    0: ("Water", "419bdf"),
    1: ("Trees", "397d49"),
    2: ("Grass", "88b053"),
    3: ("Flooded vegetation", "7a87c6"),
    4: ("Crops", "e49635"),
    5: ("Shrub and scrub", "dfc35a"),
    6: ("Built", "c4281b"),
    7: ("Bare", "a59b8f"),
    8: ("Snow and ice", "b39fe1"),
}

NODATA_MODES = ("transparent", "black")


def _hex_rgb(value: str) -> tuple[int, int, int]:
    """Convert a six-character RGB hex value to integer channels."""
    if len(value) != 6:
        raise ValueError(f"Expected a six-character RGB value, got {value!r}")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def _windows(width: int, height: int, block_size: int):
    """Yield windows covering a raster without loading it all at once."""
    for row_off in range(0, height, block_size):
        for col_off in range(0, width, block_size):
            yield Window(
                col_off=col_off,
                row_off=row_off,
                width=min(block_size, width - col_off),
                height=min(block_size, height - row_off),
            )


def _rgba_block(
    labels: np.ndarray,
    source_mask: np.ndarray,
    *,
    nodata_mode: Literal["transparent", "black"],
) -> np.ndarray:
    """Convert one label block to an array shaped ``(4, height, width)``."""
    if labels.ndim != 2:
        raise ValueError(f"Expected a two-dimensional label block, got {labels.shape}")
    if source_mask.shape != labels.shape:
        raise ValueError("Source mask and label block must have the same shape")
    if nodata_mode not in NODATA_MODES:
        raise ValueError(f"Unknown NoData mode: {nodata_mode}")

    rgba = np.zeros((4, *labels.shape), dtype=np.uint8)
    # A valid Dynamic World label is exactly one of the nine class IDs.  This
    # also rejects explicit 255 fill values and any corrupt/out-of-range data.
    valid = source_mask & (labels >= 0) & (labels <= 8)

    for class_id, (_name, hex_colour) in DYNAMIC_WORLD_CLASSES.items():
        pixels = valid & (labels == class_id)
        red, green, blue = _hex_rgb(hex_colour)
        rgba[0, pixels] = red
        rgba[1, pixels] = green
        rgba[2, pixels] = blue
        rgba[3, pixels] = 255

    if nodata_mode == "black":
        # RGB is already zero for invalid pixels; only their alpha changes.
        rgba[3, ~valid] = 255

    return rgba


def _output_profile(src: rasterio.DatasetReader) -> dict:
    """Build an RGBA profile while retaining the input spatial grid."""
    profile = src.profile.copy()
    profile.update(
        driver="GTiff",
        count=4,
        dtype="uint8",
        nodata=None,
        compress="deflate",
        BIGTIFF="IF_SAFER",
    )

    # Small synthetic/non-tiled inputs are useful for tests and can be smaller
    # than GDAL's minimum tile size.  Preserve valid source tiling, but do not
    # force tiling on an input that did not use it.
    if not profile.get("tiled", False):
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
    return profile


def _write_legend(path: Path) -> None:
    """Write the Dynamic World class palette and NoData semantics."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["class_id", "class_name", "hex_colour", "red", "green", "blue"])
        for class_id, (name, hex_colour) in DYNAMIC_WORLD_CLASSES.items():
            red, green, blue = _hex_rgb(hex_colour)
            writer.writerow([class_id, name, f"#{hex_colour}", red, green, blue])
        writer.writerow([NODATA_LABEL, "NoData", "transparent or black", "", "", ""])


def _write_png(
    color_path: Path,
    png_path: Path,
    *,
    max_size: int,
) -> None:
    """Write a bounded-size RGBA preview from the colorized GeoTIFF."""
    if max_size <= 0:
        raise ValueError("max_size must be positive")

    with rasterio.open(color_path) as src:
        scale = min(1.0, max_size / max(src.width, src.height))
        out_width = max(1, round(src.width * scale))
        out_height = max(1, round(src.height * scale))
        data = src.read(
            out_shape=(4, out_height, out_width),
            resampling=Resampling.nearest,
        )

    rgba = np.moveaxis(data, 0, -1)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, mode="RGBA").save(png_path)


def colorize(
    input_path: Path,
    output_path: Path | None = None,
    *,
    nodata_mode: Literal["transparent", "black"] = "transparent",
    png_path: Path | None = None,
    legend_path: Path | None = None,
    block_size: int = 512,
    preview_max_size: int = 2000,
) -> dict[str, Path]:
    """Create colorized Dynamic World products and return their paths.

    The first band of ``input_path`` is interpreted as the Dynamic World
    ``label`` band.  Source masks, the Dynamic World fill value 255, and every
    value outside 0--8 become NoData in the output.  The source raster is read
    and never modified.
    """
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    if preview_max_size <= 0:
        raise ValueError("preview_max_size must be positive")
    if nodata_mode not in NODATA_MODES:
        raise ValueError(f"Unknown NoData mode: {nodata_mode}")

    output_path = output_path or input_path.with_name(f"{input_path.stem}_color.tif")
    output_path = Path(output_path)
    png_path = Path(png_path) if png_path is not None else output_path.with_suffix(".png")
    legend_path = (
        Path(legend_path)
        if legend_path is not None
        else output_path.with_name(f"{output_path.stem}_legend.csv")
    )

    if output_path.resolve() == input_path.resolve():
        raise ValueError("Output path must differ from the input path")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with rasterio.open(input_path) as src:
        if src.count < 1:
            raise ValueError("Input raster has no label band")
        label_dtype = np.dtype(src.dtypes[0])
        if not np.issubdtype(label_dtype, np.integer):
            raise ValueError(
                f"Dynamic World label band must use an integer dtype, got {src.dtypes[0]}"
            )

        profile = _output_profile(src)
        with rasterio.open(output_path, "w", **profile) as dst:
            dst.colorinterp = (
                ColorInterp.red,
                ColorInterp.green,
                ColorInterp.blue,
                ColorInterp.alpha,
            )
            dst.set_band_description(1, "Dynamic World color red")
            dst.set_band_description(2, "Dynamic World color green")
            dst.set_band_description(3, "Dynamic World color blue")
            dst.set_band_description(4, "Dynamic World alpha")
            dst.update_tags(
                source_input=str(input_path),
                source_band="label",
                dataset="GOOGLE/DYNAMICWORLD/V1",
                classes="0-8",
                nodata_label=str(NODATA_LABEL),
                nodata_mode=nodata_mode,
                note="RGBA visualization; preserve the original label TIFF for analysis.",
            )

            for window in _windows(src.width, src.height, block_size):
                masked = src.read(1, window=window, masked=True)
                labels = np.asarray(masked.data)
                source_mask = ~np.ma.getmaskarray(masked)
                rgba = _rgba_block(
                    labels,
                    source_mask,
                    nodata_mode=nodata_mode,
                )
                dst.write(rgba, window=window)

    _write_png(output_path, png_path, max_size=preview_max_size)
    _write_legend(legend_path)
    return {"color_tif": output_path, "png": png_path, "legend": legend_path}


# Descriptive alias for callers that prefer a name matching the script.
colorize_dynamic_world = colorize


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a Dynamic World label GeoTIFF to a colourized RGBA image.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("input", type=Path, help="Dynamic World label GeoTIFF")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output RGBA GeoTIFF (default: <input>_color.tif)",
    )
    parser.add_argument(
        "--nodata",
        choices=NODATA_MODES,
        default="transparent",
        dest="nodata_mode",
        help="Render invalid/masked labels as transparent or opaque black",
    )
    parser.add_argument("--png", type=Path, help="PNG preview path")
    parser.add_argument("--legend", type=Path, help="CSV legend path")
    parser.add_argument(
        "--block-size",
        type=int,
        default=512,
        help="Rows/columns processed per window",
    )
    parser.add_argument(
        "--preview-max-size",
        type=int,
        default=2000,
        help="Maximum PNG preview dimension",
    )
    args = parser.parse_args()
    if args.block_size <= 0:
        parser.error("--block-size must be positive")
    if args.preview_max_size <= 0:
        parser.error("--preview-max-size must be positive")

    result = colorize(
        args.input,
        args.output,
        nodata_mode=args.nodata_mode,
        png_path=args.png,
        legend_path=args.legend,
        block_size=args.block_size,
        preview_max_size=args.preview_max_size,
    )
    print(f"Colorized GeoTIFF: {result['color_tif']}")
    print(f"PNG preview:       {result['png']}")
    print(f"Legend CSV:        {result['legend']}")


if __name__ == "__main__":
    main()
