"""Ghép 3 band từ GeoTIFF thành 1 ảnh RGB màu.

Thứ tự band trong file GeoTIFF tải từ app (theo app/imagery.py):
  Sentinel-2 : 1=B2(Blue), 2=B3(Green), 3=B4(Red), 4=B8(NIR), 5=B11(SWIR1), 6=B12(SWIR2)
  Landsat 8/9: 1=SR_B2(Blue), 2=SR_B3(Green), 3=SR_B4(Red), 4=SR_B5(NIR), 5=SR_B6, 6=SR_B7

Ví dụ dùng:
    # Ảnh màu thật (True color: R=Red, G=Green, B=Blue):
    .venv\\Scripts\\python.exe compose_rgb.py downloads\\sentinel2_xxx.tif

    # Ảnh giả màu 1 (False color: R=NIR, G=Red, B=Green - thực vật sáng đỏ):
    .venv\\Scripts\\python.exe compose_rgb.py image.tif --preset falsecolor

    # Chỉ định band tùy ý (1-based), ví dụ SWIR1-NIR-Red:
    .venv\\Scripts\\python.exe compose_rgb.py image.tif --bands 5 4 3

    # Xử lý cả thư mục downloads:
    .venv\\Scripts\\python.exe compose_rgb.py downloads
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image

# Band indices (1-based) theo thứ tự trong file tif
PRESETS = {
    #       (R, G, B)
    "truecolor":  (3, 2, 1),  # Red, Green, Blue  -> màu thật
    "falsecolor": (4, 3, 2),  # NIR, Red, Green   -> thực vật nổi bật màu đỏ
    "swir":       (5, 4, 3),  # SWIR1, NIR, Red   -> cháy rừng, đất trống
}


def stretch_percentile(band: np.ndarray, low: float = 2.0, high: float = 98.0) -> np.ndarray:
    """Kéo giãn tương phản cho một band -> uint8 0-255.

    Hàm này vẫn được giữ cho các caller cũ.  ``compose`` bên dưới dùng một
    mask chung cho cả ba kênh để tránh mỗi kênh có một thang màu khác nhau.
    """
    valid = band[np.isfinite(band)]
    if valid.size == 0:
        return np.zeros_like(band, dtype=np.uint8)
    lo, hi = np.percentile(valid, [low, high])
    if hi <= lo:
        hi = lo + 1e-6
    return (np.clip((band - lo) / (hi - lo), 0, 1) * 255).astype(np.uint8)


def _stretch_rgb(
    data: np.ndarray,
    valid_mask: np.ndarray,
    *,
    mode: str = "fixed",
    display_min: float = 0.0,
    display_max: float = 0.3,
    percentile_low: float = 2.0,
    percentile_high: float = 98.0,
) -> np.ndarray:
    """Stretch 3 channels with one shared spatial-valid mask.

    ``data`` has shape ``(3, height, width)``.  Fixed reflectance limits are
    preferable for comparing dates and prevent an aerosol-heavy channel from
    receiving a different contrast scale.  Percentile mode remains available
    for non-optical products, but its statistics are computed from the same
    physically valid pixels in all three channels.
    """
    if data.shape[0] != 3:
        raise ValueError(f"RGB data must have three channels, got {data.shape[0]}")
    if display_max <= display_min:
        raise ValueError("display_max must be greater than display_min")
    if mode not in {"fixed", "percentile"}:
        raise ValueError(f"Unknown stretch mode: {mode}")

    finite = np.isfinite(data).all(axis=0)
    valid = np.asarray(valid_mask, dtype=bool) & finite
    # Negative values are possible in old exports with the former offset.  Do
    # not let them set percentile limits; fixed mode still clips them cleanly.
    stats_mask = valid & np.all(data >= display_min, axis=0)
    if mode == "percentile":
        values = data[:, stats_mask]
        if values.size:
            lo, hi = np.percentile(values, [percentile_low, percentile_high])
            # One range is used for all channels, preserving neutral colours.
            lo = float(max(display_min, lo))
            hi = float(min(display_max, hi))
            if hi <= lo:
                lo, hi = display_min, display_max
        else:
            lo, hi = display_min, display_max
    else:
        lo, hi = display_min, display_max

    stretched = np.clip((data - lo) / (hi - lo), 0.0, 1.0)
    stretched[:, ~valid] = 0.0
    return stretched


def compose(tif_path: Path, rgb_idx: tuple[int, int, int], out_dir: Path,
            suffix: str, gamma: float = 1.5, brightness: int = 0,
            contrast: float = 1.0, saturation: float = 1.0,
            auto_color: bool = False, stretch_mode: str = "fixed",
            display_min: float = 0.0, display_max: float = 0.3) -> Path:
    """Ghép 3 band (chỉ số 1-based) thành ảnh RGB PNG.

    GeoTIFF nodata và các giá trị không hữu hạn được loại khỏi mask chung.
    Mặc định dùng cùng dải reflectance 0–0.3 cho cả R/G/B, giống preview
    Sentinel-2 của app, thay vì stretch độc lập từng kênh.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with rasterio.open(tif_path) as src:
        if max(rgb_idx) > src.count:
            raise ValueError(
                f"{tif_path.name}: chỉ có {src.count} band, không có band {max(rgb_idx)}"
            )
        masked = src.read(list(rgb_idx), masked=True).astype(np.float32)
        data = masked.filled(np.nan)
        valid_mask = ~np.ma.getmaskarray(masked).any(axis=0)

    rgb_float = _stretch_rgb(
        data,
        valid_mask,
        mode=stretch_mode,
        display_min=display_min,
        display_max=display_max,
    )
    if gamma <= 0:
        raise ValueError("gamma must be greater than zero")
    if gamma != 1.0:
        rgb_float = np.power(rgb_float, 1.0 / gamma)  # gamma > 1 -> sáng hơn
    # Rasterio/``_stretch_rgb`` dùng (kênh, cao, rộng), còn PIL dùng
    # (cao, rộng, kênh).
    rgb = np.moveaxis(np.round(rgb_float * 255).astype(np.uint8), 0, -1)
    valid_output = (
        np.isfinite(data).all(axis=0)
        & valid_mask
        & np.all(data >= display_min, axis=0)
    )

    # --- Tinh chỉnh độ sáng / tương phản / màu (sau stretch + gamma) ---
    if brightness != 0.0:
        # brightness: -100..100, dịch toàn bộ kênh
        rgb = rgb.astype(np.int16)
        rgb = np.clip(rgb + brightness, 0, 255).astype(np.uint8)
    if contrast != 1.0:
        # contrast: 0.5..2.0, đẩy xa/gần điểm giữa 128
        rgb = rgb.astype(np.float32)
        rgb = np.clip((rgb - 128.0) * contrast + 128.0, 0, 255).astype(np.uint8)
    if saturation != 1.0:
        # saturation: 0 = xám, 1 = gốc, 2 = màu đậm
        gray = rgb.mean(axis=-1, keepdims=True)
        rgb = rgb.astype(np.float32)
        rgb = np.clip(gray + (rgb - gray) * saturation, 0, 255).astype(np.uint8)
    if auto_color:
        # Cân bằng màu tự động chỉ trên pixel hợp lệ; nền đen không được kéo lên.
        for c in range(3):
            values = rgb[..., c][valid_output]
            med = np.median(values) if values.size else 0.0
            shift = 128.0 - med
            rgb[..., c] = np.clip(rgb[..., c].astype(np.float32) + shift, 0, 255).astype(np.uint8)

    # Brightness/contrast/saturation/auto-color không được làm nền nodata sáng lên.
    rgb[~valid_output] = 0
    out_path = out_dir / f"{tif_path.stem}_{suffix}.png"
    Image.fromarray(rgb, mode="RGB").save(out_path)
    return out_path


def compose_geotiff(tif_path: Path, rgb_idx: tuple[int, int, int], out_path: Path,
                    stretch_mode: str = "fixed", display_min: float = 0.0,
                    display_max: float = 0.3, gamma: float = 1.0,
                    brightness: int = 0, contrast: float = 1.0,
                    saturation: float = 1.0, auto_color: bool = False) -> Path:
    """Ghép 3 band thành GeoTIFF RGB 8-bit, GIỮ NGUYÊN georeference (CRS +
    transform) của file gốc — dùng làm texture cho DEM_to_3D (--texture).

    Khác PNG: file .tif xuất ra mang tọa độ nên dem_to_3d.py tự căn khớp
    lên lưới DEM, không cần file phụ (.tfw/.aux).
    """
    with rasterio.open(tif_path) as src:
        if max(rgb_idx) > src.count:
            raise ValueError(
                f"{tif_path.name}: chỉ có {src.count} band, không có band {max(rgb_idx)}"
            )
        masked = src.read(list(rgb_idx), masked=True).astype(np.float32)
        data = masked.filled(np.nan)
        valid_mask = ~np.ma.getmaskarray(masked).any(axis=0)
        profile = src.profile.copy()

    rgb_float = _stretch_rgb(
        data, valid_mask, mode=stretch_mode,
        display_min=display_min, display_max=display_max,
    )
    if gamma <= 0:
        raise ValueError("gamma must be greater than zero")
    if gamma != 1.0:
        rgb_float = np.power(rgb_float, 1.0 / gamma)
    rgb = np.moveaxis(np.round(rgb_float * 255).astype(np.uint8), 0, -1)
    valid_output = (
        np.isfinite(data).all(axis=0)
        & valid_mask
        & np.all(data >= display_min, axis=0)
    )

    if brightness != 0.0:
        rgb = rgb.astype(np.int16)
        rgb = np.clip(rgb + brightness, 0, 255).astype(np.uint8)
    if contrast != 1.0:
        rgb = rgb.astype(np.float32)
        rgb = np.clip((rgb - 128.0) * contrast + 128.0, 0, 255).astype(np.uint8)
    if saturation != 1.0:
        gray = rgb.mean(axis=-1, keepdims=True)
        rgb = rgb.astype(np.float32)
        rgb = np.clip(gray + (rgb - gray) * saturation, 0, 255).astype(np.uint8)
    if auto_color:
        for c in range(3):
            values = rgb[..., c][valid_output]
            med = np.median(values) if values.size else 0.0
            shift = 128.0 - med
            rgb[..., c] = np.clip(
                rgb[..., c].astype(np.float32) + shift, 0, 255
            ).astype(np.uint8)
    rgb[~valid_output] = 0

    # rasterio: (kênh, cao, rộng); nén LZW cho file nhẹ hơn
    rgb_chw = np.moveaxis(rgb, -1, 0)
    profile.update(
        driver="GTiff", count=3, dtype="uint8", compress="lzw",
        photometric="RGB", nodata=None,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(rgb_chw)
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ghép 3 band GeoTIFF thành ảnh RGB màu.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Ví dụ dùng:")[-1],
    )
    parser.add_argument("input", help="File .tif hoặc thư mục chứa file .tif")
    parser.add_argument(
        "--preset", choices=list(PRESETS), default="truecolor",
        help="Bộ band dựng sẵn: truecolor (mặc định), falsecolor, swir",
    )
    parser.add_argument(
        "--bands", nargs=3, type=int, metavar=("R", "G", "B"),
        help="Chỉ định band tùy ý (1-based), vd: --bands 5 4 3. Ghi đè --preset",
    )
    parser.add_argument(
        "--gamma", type=float, default=1.0,
        help="Hiệu chỉnh gamma (>1 sáng hơn, <1 tối hơn), mặc định 1.0",
    )
    parser.add_argument(
        "--stretch", choices=("fixed", "percentile"), default="fixed",
        help="Cách kéo giãn: fixed (mặc định 0..0.3) hoặc percentile chung 3 kênh",
    )
    parser.add_argument(
        "--display-min", type=float, default=0.0,
        help="Reflectance tối thiểu khi stretch fixed, mặc định 0.0",
    )
    parser.add_argument(
        "--display-max", type=float, default=0.3,
        help="Reflectance tối đa khi stretch fixed, mặc định 0.3",
    )
    parser.add_argument(
        "--brightness", type=int, default=0,
        help="Độ sáng -100..100 (dương = sáng hơn), mặc định 0",
    )
    parser.add_argument(
        "--contrast", type=float, default=1.0,
        help="Tương phản 0.5..2.0 (>1 = đậm nét hơn), mặc định 1.0",
    )
    parser.add_argument(
        "--saturation", type=float, default=1.0,
        help="Độ rực màu 0..2 (0 = xám, 2 = màu đậm), mặc định 1.0",
    )
    parser.add_argument(
        "--auto-color", action="store_true",
        help="Cân bằng màu tự động (khử ảnh lệch xanh/vàng)",
    )
    parser.add_argument(
        "--geotiff", action="store_true",
        help="Ngoài PNG, xuất thêm GeoTIFF RGB 8-bit kèm tọa độ (dùng làm "
             "texture cho DEM_to_3D --texture). Thứ tự band R,G,B đúng chuẩn.",
    )
    args = parser.parse_args()

    rgb_idx = tuple(args.bands) if args.bands else PRESETS[args.preset]
    suffix = f"rgb{''.join(map(str, rgb_idx))}"

    target = Path(args.input)
    files = sorted(target.glob("*.tif")) if target.is_dir() else [target]
    if not files or not files[0].exists():
        sys.exit(f"Không tìm thấy file .tif: {target}")

    for tif in files:
        try:
            out = compose(tif, rgb_idx, tif.parent / "rgb", suffix, args.gamma,
                          args.brightness, args.contrast, args.saturation,
                          args.auto_color, args.stretch, args.display_min,
                          args.display_max)
            print(f"{tif.name} -> band R,G,B = {rgb_idx} -> {out}")
            if args.geotiff:
                out_gt = compose_geotiff(
                    tif, rgb_idx,
                    tif.parent / f"{tif.stem}_{suffix}_geo.tif",
                    stretch_mode=args.stretch, display_min=args.display_min,
                    display_max=args.display_max, gamma=args.gamma,
                    brightness=args.brightness, contrast=args.contrast,
                    saturation=args.saturation, auto_color=args.auto_color,
                )
                print(f"{tif.name} -> GeoTIFF texture -> {out_gt}")
        except ValueError as e:
            print(f"BỎ QUA {tif.name}: {e}")


if __name__ == "__main__":
    main()
