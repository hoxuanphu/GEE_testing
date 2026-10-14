"""Classify a six-band Sentinel-2 GeoTIFF using spectral rules.

This is a deliberately simple, label-free experiment: no WorldCover data and
no machine-learning model are used.  The six input bands (B2, B3, B4, B8, B11,
B12) are converted to spectral indices and a decision tree assigns a class to
each pixel.  Thresholds are exposed as command-line options because they are
scene/region dependent.

The default output directory is ``downloads/landcover_spectral``.  The script
creates:

* ``*_spectral_landcover.tif`` - categorical classes (uint8, NoData=0),
* ``*_spectral_landcover.png`` - a colour preview,
* ``*_spectral_stats.csv`` - pixel/area statistics, and
* ``*_ndvi.tif``, ``*_mndwi.tif``, ``*_ndmi.tif``, ``*_ndbi.tif`` and
  ``*_bsi.tif`` - float32 diagnostic index rasters.

Example::

    .venv\\Scripts\\python.exe create_landcover_spectral.py \\
      downloads\\sentinel2_20250228T031721T032919_T48PWS_2025-02-28_crop.tif

The classifier is intended as a baseline.  A single acquisition cannot
reliably separate all detailed classes (for example forest versus cropland),
so inspect the diagnostic rasters and tune thresholds for the study area.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image
from rasterio.enums import Resampling


# Output class id -> (name, RGB colour).  0 is reserved for NoData.
CLASSES: dict[int, tuple[str, tuple[int, int, int]]] = {
    0: ("NoData", (0, 0, 0)),
    1: ("Water", (0, 84, 230)),
    2: ("Cloud (heuristic)", (255, 255, 255)),
    3: ("Built-up", (220, 30, 30)),
    4: ("Bare / sparse vegetation", (190, 170, 110)),
    5: ("Dense vegetation / tree", (0, 125, 40)),
    6: ("Moderate vegetation", (140, 200, 60)),
    7: ("Wetland vegetation", (0, 155, 155)),
    8: ("Unclassified", (128, 128, 128)),
}

INDEX_NAMES = ("ndvi", "mndwi", "ndmi", "ndbi", "bsi")


def _norm_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Calculate (a-b)/(a+b), returning NaN where the denominator is zero."""
    denominator = a + b
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.divide(a - b, denominator, out=np.full_like(a, np.nan), where=denominator != 0)


def _find_bands(src: rasterio.DatasetReader) -> dict[str, int]:
    """Find required bands by descriptions, falling back to documented order."""
    names = {str(desc).upper(): i for i, desc in enumerate(src.descriptions, 1) if desc}
    required = ("B2", "B3", "B4", "B8", "B11", "B12")
    if all(name in names for name in required):
        return {name: names[name] for name in required}
    if src.count >= 6:
        # App downloads use [Blue, Green, Red, NIR, SWIR1, SWIR2].
        return dict(zip(required, range(1, 7)))
    raise ValueError(
        "Input must contain six Sentinel-2 bands B2, B3, B4, B8, B11 and B12 "
        f"(found {src.count} band(s): {src.descriptions})"
    )


def _pixel_area_m2(src: rasterio.DatasetReader) -> float:
    """Approximate area of one pixel, including geographic (EPSG:4326) grids."""
    x_size, y_size = abs(src.transform.a), abs(src.transform.e)
    if src.crs is not None and src.crs.is_projected:
        return x_size * y_size
    # The input test image is EPSG:4326.  Convert degree dimensions at the
    # centre latitude; this is adequate for per-scene summary statistics.
    from math import cos, radians

    latitude = (src.bounds.top + src.bounds.bottom) / 2.0
    return x_size * 111_320.0 * cos(radians(latitude)) * y_size * 111_320.0


def _detect_input_offset(src: rasterio.DatasetReader, band_ids: dict[str, int]) -> float:
    """Detect the -0.1 offset present in the current app Sentinel-2 export.

    The application currently subtracts 1000 before dividing by 10000.  A
    conservative detection lets this standalone script also work with normal
    (already-corrected) reflectance GeoTIFFs.  ``--input-offset`` can override
    the detection when processing another export format.
    """
    sample = src.read(
        band_ids["B2"],
        out_shape=(1, min(src.height, 128), min(src.width, 128)),
    )
    finite = sample[np.isfinite(sample)]
    if finite.size == 0:
        return 0.0
    low_fraction = float(np.mean(finite <= -0.09))
    median = float(np.median(finite))
    return 0.1 if median < -0.02 and low_fraction > 0.01 else 0.0


def _classify_block(
    bands: dict[str, np.ndarray],
    *,
    min_reflectance: float,
    ndvi_threshold: float,
    water_threshold: float,
    ndmi_threshold: float,
    ndbi_threshold: float,
    bsi_threshold: float,
    wetland_mndwi: float,
    cloud_threshold: float,
    cloud_enabled: bool,
    input_offset: float,
) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Compute indices and labels for one raster block."""
    # Undo a known export offset before calculating indices.  The current app
    # export has reflectance values shifted by -0.1; corrected products use 0.
    b2 = bands["B2"] + input_offset
    b3 = bands["B3"] + input_offset
    b4 = bands["B4"] + input_offset
    b8 = bands["B8"] + input_offset
    b11 = bands["B11"] + input_offset
    b12 = bands["B12"] + input_offset

    valid = np.ones(b2.shape, dtype=bool)
    for band in (b2, b3, b4, b8, b11, b12):
        valid &= np.isfinite(band) & (band > min_reflectance)

    ndvi = _norm_diff(b8, b4)
    mndwi = _norm_diff(b3, b11)
    ndmi = _norm_diff(b8, b11)
    ndbi = _norm_diff(b11, b8)
    bsi = _norm_diff((b11 + b4), (b8 + b2))

    for index in (ndvi, mndwi, ndmi, ndbi, bsi):
        index[~valid] = np.nan

    # Keep mutually competing masks explicit so precedence is easy to inspect.
    water = valid & (mndwi > water_threshold) & (ndvi < 0.30)
    cloud = np.zeros(valid.shape, dtype=bool)
    if cloud_enabled:
        brightness = (b2 + b3 + b4) / 3.0
        cloud = valid & ~water & (brightness > cloud_threshold) & (ndvi < 0.40)
    builtup = valid & ~water & ~cloud & (ndbi > ndbi_threshold) & (ndvi < 0.35)
    bare = valid & ~water & ~cloud & ~builtup & (bsi > bsi_threshold) & (ndvi < 0.35)
    wetland = (
        valid & ~water & ~cloud & ~builtup & ~bare
        & (ndvi > 0.30) & (ndmi > ndmi_threshold) & (mndwi > wetland_mndwi)
    )
    dense = valid & ~water & ~cloud & ~builtup & ~bare & ~wetland \
        & (ndvi > ndvi_threshold) & (ndmi > ndmi_threshold)
    moderate = valid & ~water & ~cloud & ~builtup & ~bare & ~wetland & ~dense \
        & (ndvi > 0.35)

    labels = np.full(valid.shape, 8, dtype=np.uint8)
    labels[water] = 1
    labels[cloud] = 2
    labels[builtup] = 3
    labels[bare] = 4
    labels[dense] = 5
    labels[moderate] = 6
    labels[wetland] = 7
    labels[~valid] = 0
    return labels, {"ndvi": ndvi, "mndwi": mndwi, "ndmi": ndmi, "ndbi": ndbi, "bsi": bsi}


def _write_preview(labels_path: Path, output_path: Path, max_size: int = 1800) -> None:
    with rasterio.open(labels_path) as src:
        scale = min(1.0, max_size / max(src.width, src.height))
        out_h, out_w = max(1, round(src.height * scale)), max(1, round(src.width * scale))
        labels = src.read(1, out_shape=(out_h, out_w), resampling=Resampling.nearest)
    lut = np.zeros((256, 3), dtype=np.uint8)
    for class_id, (_, colour) in CLASSES.items():
        lut[class_id] = colour
    Image.fromarray(lut[labels], mode="RGB").save(output_path)


def classify(
    input_path: Path,
    output_dir: Path,
    *,
    min_reflectance: float = -0.02,
    ndvi_threshold: float = 0.60,
    water_threshold: float = 0.10,
    ndmi_threshold: float = 0.20,
    ndbi_threshold: float = 0.05,
    bsi_threshold: float = 0.10,
    wetland_mndwi: float = -0.05,
    cloud_threshold: float = 0.35,
    cloud_enabled: bool = True,
    block_size: int = 512,
    input_offset: float | None = None,
) -> dict[str, Path]:
    """Classify an input image and return paths of generated products."""
    if not input_path.exists():
        raise FileNotFoundError(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = input_path.stem
    labels_path = output_dir / f"{stem}_spectral_landcover.tif"
    preview_path = output_dir / f"{stem}_spectral_landcover.png"
    stats_path = output_dir / f"{stem}_spectral_stats.csv"

    with rasterio.open(input_path) as src:
        band_ids = _find_bands(src)
        detected_offset = _detect_input_offset(src, band_ids) if input_offset is None else input_offset
        profile = src.profile.copy()
        profile.update(count=1, dtype="uint8", nodata=0, compress="deflate", tiled=True)
        index_profile = src.profile.copy()
        index_profile.update(count=1, dtype="float32", nodata=np.nan, compress="deflate", tiled=True)
        index_paths = {name: output_dir / f"{stem}_{name}.tif" for name in INDEX_NAMES}
        counts = np.zeros(max(CLASSES) + 1, dtype=np.int64)
        pixel_area = _pixel_area_m2(src)

        with rasterio.open(labels_path, "w", **profile) as label_dst:
            index_dsts = {name: rasterio.open(path, "w", **index_profile) for name, path in index_paths.items()}
            try:
                for row_off in range(0, src.height, block_size):
                    for col_off in range(0, src.width, block_size):
                        window = rasterio.windows.Window(
                            col_off, row_off,
                            min(block_size, src.width - col_off),
                            min(block_size, src.height - row_off),
                        )
                        arrays = {
                            name: src.read(band_id, window=window).astype(np.float32, copy=False)
                            for name, band_id in band_ids.items()
                        }
                        labels, indices = _classify_block(
                            arrays,
                            min_reflectance=min_reflectance,
                            ndvi_threshold=ndvi_threshold,
                            water_threshold=water_threshold,
                            ndmi_threshold=ndmi_threshold,
                            ndbi_threshold=ndbi_threshold,
                            bsi_threshold=bsi_threshold,
                            wetland_mndwi=wetland_mndwi,
                            cloud_threshold=cloud_threshold,
                            cloud_enabled=cloud_enabled,
                            input_offset=detected_offset,
                        )
                        label_dst.write(labels, 1, window=window)
                        counts += np.bincount(labels.ravel(), minlength=len(counts))
                        for name, dst in index_dsts.items():
                            dst.write(indices[name].astype(np.float32), 1, window=window)
            finally:
                for dst in index_dsts.values():
                    dst.close()

            label_dst.set_band_description(1, "Spectral rule land-cover class")
            label_dst.write_colormap(1, {k: (*colour, 255) for k, (_, colour) in CLASSES.items()})
            label_dst.update_tags(
                method="Rule-based Sentinel-2 spectral indices (no WorldCover)",
                input_bands="B2,B3,B4,B8,B11,B12",
                thresholds=(
                    f"ndvi={ndvi_threshold};water_mndwi={water_threshold};ndmi={ndmi_threshold};"
                    f"ndbi={ndbi_threshold};bsi={bsi_threshold};wetland_mndwi={wetland_mndwi};"
                    f"cloud_brightness={cloud_threshold};min_reflectance={min_reflectance};"
                    f"input_offset={detected_offset}"
                ),
            )

        for name, path in index_paths.items():
            with rasterio.open(path, "r+") as dst:
                dst.set_band_description(1, name.upper())
                dst.update_tags(method="Sentinel-2 spectral index", source_input=str(input_path))

    total_valid = int(counts[1:].sum())
    with stats_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["class_id", "class_name", "pixel_count", "area_ha", "percent_valid"])
        for class_id, (name, _) in CLASSES.items():
            pixels = int(counts[class_id])
            area_ha = pixels * pixel_area / 10_000.0
            percent = 100.0 * pixels / total_valid if total_valid else 0.0
            writer.writerow([class_id, name, pixels, f"{area_ha:.3f}", f"{percent:.3f}"])

    _write_preview(labels_path, preview_path)
    return {"labels": labels_path, "preview": preview_path, "stats": stats_path, **index_paths}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Six-band Sentinel-2 GeoTIFF")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("downloads/landcover_spectral"))
    parser.add_argument("--ndvi", type=float, default=0.60, help="Dense vegetation NDVI threshold (default: 0.60)")
    parser.add_argument("--water-mndwi", type=float, default=0.10, help="Water MNDWI threshold (default: 0.10)")
    parser.add_argument("--ndmi", type=float, default=0.20, help="Vegetation/wetland NDMI threshold (default: 0.20)")
    parser.add_argument("--ndbi", type=float, default=0.05, help="Built-up NDBI threshold (default: 0.05)")
    parser.add_argument("--bsi", type=float, default=0.10, help="Bare-land BSI threshold (default: 0.10)")
    parser.add_argument("--wetland-mndwi", type=float, default=-0.05, help="Wetland MNDWI lower bound (default: -0.05)")
    parser.add_argument("--cloud-threshold", type=float, default=0.35, help="Mean B2/B3/B4 brightness for heuristic cloud (default: 0.35)")
    parser.add_argument("--min-reflectance", type=float, default=-0.02, help="Mask pixels at/below this value in any band (default: -0.02)")
    parser.add_argument("--input-offset", type=float, default=None, help="Add this value to every band before indices (default: auto-detect current app's -0.1 offset)")
    parser.add_argument("--no-cloud", action="store_true", help="Disable heuristic cloud class (no SCL is present in this six-band file)")
    parser.add_argument("--block-size", type=int, default=512, help="Processing block size for non-tiled inputs (default: 512; currently informational)")
    args = parser.parse_args()
    if args.block_size <= 0:
        parser.error("--block-size must be positive")
    result = classify(
        args.input,
        args.output_dir,
        min_reflectance=args.min_reflectance,
        ndvi_threshold=args.ndvi,
        water_threshold=args.water_mndwi,
        ndmi_threshold=args.ndmi,
        ndbi_threshold=args.ndbi,
        bsi_threshold=args.bsi,
        wetland_mndwi=args.wetland_mndwi,
        cloud_threshold=args.cloud_threshold,
        cloud_enabled=not args.no_cloud,
        block_size=args.block_size,
        input_offset=args.input_offset,
    )
    print("Created spectral land-cover products:")
    for key, path in result.items():
        print(f"  {key:8s} {path}")


if __name__ == "__main__":
    main()
