"""Chuyển ảnh SAR (Sentinel-1 GeoTIFF, đơn vị dB) thành PNG xem được.

Các chế độ hiển thị:
  - VV hoặc VH đơn lẻ (grayscale) — chuẩn hay dùng nhất
  - Màu giả (false color) R=VH, G=(VV+VH)/2, B=VV — nhìn sinh động hơn
  - Composite VV+VH+VV/VH (RGB) — phân biệt loại đất/nước rõ hơn

Vì giá trị SAR theo dB (thường -30..+10), script tự stretch theo
percentile 2-98% (tùy chỉnh được).

Ví dụ dùng:
    # Grayscale VV (mặc định):
    python sar_to_png.py downloads\\s1gee_xxx.tif

    # Grayscale VH:
    python sar_to_png.py image.tif --pol VH

    # Màu giả:
    python sar_to_png.py image.tif --mode color

    # Sáng/tối hơn:
    python sar_to_png.py image.tif --gamma 1.5

    # Cả thư mục:
    python sar_to_png.py downloads
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image


def stretch_percentile(band: np.ndarray, low: float = 2.0, high: float = 98.0) -> np.ndarray:
    """Kéo giãn tương phản theo percentile -> float 0-1 (NaN giữ nguyên)."""
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros_like(band, dtype=np.float32)
    lo, hi = np.percentile(valid, [low, high])
    if hi <= lo:
        hi = lo + 1e-6
    return np.clip((band - lo) / (hi - lo), 0, 1).astype(np.float32)


def _read_band(src, name: str) -> np.ndarray | None:
    """Đọc band theo tên (mô tả) hoặc số thứ tự."""
    descriptions = src.descriptions
    if name in descriptions:
        return src.read(descriptions.index(name) + 1).astype(np.float32)
    return None


def convert(tif_path: Path, out_dir: Path, mode: str, pol: str,
            gamma: float, low: float, high: float) -> Path:
    """Chuyển 1 file SAR GeoTIFF thành PNG. Trả về đường dẫn file ghi ra."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with rasterio.open(tif_path) as src:
        if src.count == 0:
            raise ValueError("file không có band nào")

        # Stretch chung toàn file theo percentile
        vv = _read_band(src, "VV")
        vh = _read_band(src, "VH")

        if mode == "gray":
            band = _read_band(src, pol)
            if band is None:
                band = src.read(1).astype(np.float32)
            if src.nodata is not None and np.isfinite(src.nodata):
                band = np.where(np.isclose(band, src.nodata), np.nan, band)
            ch = stretch_percentile(band, low, high)
            if gamma != 1.0:
                ch = np.power(ch, 1.0 / gamma)
            gray = (np.nan_to_num(ch) * 255).astype(np.uint8)
            rgb = np.stack([gray] * 3, axis=-1)
            suffix = pol.lower()

        elif mode == "color":
            # False color: R=VH, G=trung bình, B=VV (cần cả 2 pol)
            if vv is None or vh is None:
                raise ValueError(
                    "chế độ --mode color cần cả band VV và VH "
                    "(dùng --mode gray nếu file chỉ có 1 polarization)"
                )
            mean = (vv + vh) / 2.0
            chans = []
            for b in (vh, mean, vv):
                ch = stretch_percentile(b, low, high)
                if gamma != 1.0:
                    ch = np.power(ch, 1.0 / gamma)
                chans.append((np.nan_to_num(ch) * 255).astype(np.uint8))
            rgb = np.stack(chans, axis=-1)
            suffix = "color"

        else:  # composite VV/VH/VV
            if vv is None or vh is None:
                raise ValueError(
                    "chế độ --mode composite cần cả band VV và VH"
                )
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = 10.0 * np.log10(vv / vh)  # dB
            chans = []
            for b in (vv, vh, ratio):
                ch = stretch_percentile(b, low, high)
                if gamma != 1.0:
                    ch = np.power(ch, 1.0 / gamma)
                chans.append((np.nan_to_num(ch) * 255).astype(np.uint8))
            rgb = np.stack(chans, axis=-1)
            suffix = "composite"

    out_path = out_dir / f"{tif_path.stem}_{suffix}.png"
    if mode == "gray":
        # Ảnh xám thật sự — 1 kênh duy nhất (mode L trong PNG)
        Image.fromarray(gray, mode="L").save(out_path)
    else:
        Image.fromarray(rgb, mode="RGB").save(out_path)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chuyển ảnh SAR (dB) GeoTIFF thành PNG xem được.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Ví dụ dùng:")[-1],
    )
    parser.add_argument("input", help="File .tif hoặc thư mục chứa file .tif")
    parser.add_argument(
        "--mode", choices=["gray", "color", "composite"], default="gray",
        help="gray: 1 pol grayscale | color: VH/mixed/VV | composite: VV/VH/ratio",
    )
    parser.add_argument(
        "--pol", choices=["VV", "VH", "HH", "HV"], default="VV",
        help="Polarization cho chế độ gray (mặc định VV)",
    )
    parser.add_argument(
        "--gamma", type=float, default=1.0,
        help="Hiệu chỉnh gamma (>1 sáng hơn, <1 tối hơn)",
    )
    parser.add_argument(
        "--clip", nargs=2, type=float, default=(2.0, 98.0), metavar=("LOW", "HIGH"),
        help="Percentile cắt cho stretch (mặc định 2 98)",
    )
    args = parser.parse_args()

    target = Path(args.input)
    files = sorted(target.glob("*.tif")) if target.is_dir() else [target]
    if not files or not files[0].exists():
        sys.exit(f"Không tìm thấy file .tif: {target}")

    for tif in files:
        try:
            out = convert(tif, tif.parent / "sar_png", args.mode, args.pol,
                          args.gamma, args.clip[0], args.clip[1])
            print(f"{tif.name} [{args.mode}] -> {out}")
        except ValueError as e:
            print(f"BỎ QUA {tif.name}: {e}")


if __name__ == "__main__":
    main()
