"""Tách từng band trong file GeoTIFF (đa băng) thành ảnh RGB grayscale.

Ví dụ dùng:
    # Tách tất cả các band của 1 file:
    .venv\\Scripts\\python.exe split_bands_to_rgb.py downloads\\sentinel2_xxx.tif

    # Tách nhiều file (hoặc cả thư mục downloads):
    .venv\\Scripts\\python.exe split_bands_to_rgb.py downloads

Yêu cầu: rasterio (pip install rasterio) — file .tif do GEE export về
đã chứa georeference nên rasterio đọc trực tiếp được.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    import rasterio
    import numpy as np
    from PIL import Image
except ImportError as e:
    sys.exit(f"Thiếu dependency: {e}. Cài bằng: pip install rasterio pillow numpy")


def stretch_percentile(band: np.ndarray, low: float = 2.0, high: float = 98.0) -> np.ndarray:
    """Kéo giãn tương phản theo percentile để hiển thị (0-255)."""
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros_like(band, dtype=np.uint8)
    lo, hi = np.percentile(valid, [low, high])
    if hi <= lo:
        hi = lo + 1e-6
    stretched = np.clip((band - lo) / (hi - lo), 0, 1)
    return (stretched * 255).astype(np.uint8)


def band_dtype_suffix(band: "rasterio.Band" | None, idx: int, descriptions) -> str:
    if descriptions and idx < len(descriptions) and descriptions[idx]:
        return str(descriptions[idx]).replace(" ", "_")
    return str(idx + 1)


def split_file(tif_path: Path, out_dir: Path) -> list[Path]:
    """Tách file .tif thành nhiều ảnh PNG grayscale (mỗi band 1 file).

    Trả về danh sách file đã ghi ra.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = tif_path.stem
    written: list[Path] = []

    with rasterio.open(tif_path) as src:
        print(f"{tif_path.name}: {src.width} x {src.height} px, "
              f"{src.count} band(s), dtype={src.dtypes[0]}, CRS={src.crs}")
        descs = src.descriptions
        for idx in range(1, src.count + 1):
            data = src.read(idx).astype(np.float32)
            # bỏ vùng nodata nếu có
            if src.nodata is not None and np.isfinite(src.nodata):
                data = np.where(np.isclose(data, src.nodata), np.nan, data)

            gray = stretch_percentile(data)
            img8 = np.stack([gray] * 3, axis=-1)  # RGB: R=G=B=gray

            name = band_dtype_suffix(None, idx - 1, descs)
            out_path = out_dir / f"{stem}_b{idx:02d}_{name}.png"
            Image.fromarray(img8, mode="RGB").save(out_path)
            written.append(out_path)
            print(f"  -> band {idx} ({name}): {out_path.name}")

    return written


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        sys.exit(1)

    targets: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if p.is_dir():
            targets.extend(sorted(p.glob("*.tif")))
        else:
            targets.append(p)

    if not targets:
        sys.exit("Không tìm thấy file .tif nào.")

    total = 0
    for tif in targets:
        if not tif.exists():
            print(f"Bỏ qua (không tồn tại): {tif}")
            continue
        total += len(split_file(tif, tif.parent / "bands"))

    print(f"\nHoàn tất: đã ghi {total} ảnh band ra các thư mục 'bands/'.")


if __name__ == "__main__":
    main(sys.argv[1:])
