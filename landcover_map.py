"""Tạo bản đồ phân loại đất (land cover) từ GeoTIFF 6-band surface reflectance.

Thứ tự band trong file GeoTIFF tải từ app (cả Sentinel-2 lẫn Landsat):
  1=Blue, 2=Green, 3=Red, 4=NIR, 5=SWIR1, 6=SWIR2

Các chỉ số tính bên trong:
  NDVI  = (NIR - Red)   / (NIR + Red)       -> thảm thực vật
  MNDWI = (Green - SWIR1)/ (Green + SWIR1)   -> mặt nước (tốt hơn NDWI cho nước đục)
  NDBI  = (SWIR1 - NIR) / (SWIR1 + NIR)      -> dân cư / diện tích xây dựng
  BSI   = ((SWIR1+Red) - (NIR+Blue)) / ((SWIR1+Red) + (NIR+Blue)) -> đất trống

Quy tắc phân loại (theo thứ tự ưu tiên):
  nodata (-inf)          -> 0 KHÔNG CÓ DỮ LIỆU
  mây (sáng bất thường)  -> 2 MÂY            (tùy chọn --no-cloud để tắt)
  MNDWI > ngưỡng         -> 1 NƯỚC
  NDVI  > ngưỡng         -> 3 THỰC VẬT
  NDBI  > ngưỡng         -> 4 DÂN CƯ
  BSI   > ngưỡng         -> 5 ĐẤT TRỐNG
  còn lại               -> 6 KHÁC

Output cho mỗi ảnh (vào downloads/landcover/):
  <tên>_landcover.tif  GeoTIFF uint8 kèm color table (mở QGIS hiển thị màu ngay)
  <tên>_landcover.png  bản đồ màu view nhanh + chú giải
  <tên>_stats.csv      thống kê diện tích từng lớp

Ví dụ dùng:
    .venv\\Scripts\\python.exe landcover_map.py downloads\\sentinel2_xxx_crop.tif
    .venv\\Scripts\\python.exe landcover_map.py downloads                    # cả thư mục
    .venv\\Scripts\\python.exe landcover_map.py image.tif --ndvi 0.4 --mndwi 0.15
    .venv\\Scripts\\python.exe landcover_map.py image.tif --smooth --no-cloud
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import rasterio
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Mã lớp -> (tên hiển thị, màu RGB)
CLASSES: dict[int, tuple[str, tuple[int, int, int]]] = {
    0: ("Khong co du lieu", (0, 0, 0)),
    1: ("Nuoc", (0, 84, 230)),
    2: ("May", (255, 255, 255)),
    3: ("Thuc vat", (0, 140, 40)),
    4: ("Dan cu", (230, 30, 30)),
    5: ("Dat trong", (230, 190, 60)),
    6: ("Khac", (128, 128, 128)),
}


def _norm_diff(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """(a-b)/(a+b) an toàn với mẫu số = 0."""
    with np.errstate(divide="ignore", invalid="ignore"):
        return (a - b) / (a + b)


def _pixel_area_m2(src: rasterio.DatasetReader) -> float:
    """Diện tích 1 pixel (m²) từ transform + CRS (xử lý cả EPSG:4326 theo độ)."""
    a, e = abs(src.transform.a), abs(src.transform.e)
    if src.crs is None or src.crs.is_geographic:
        # hệ tọa độ địa lý: quy đổi độ -> mét theo vĩ độ trung tâm ảnh
        from math import cos, radians

        b = src.bounds
        lat = (b.top + b.bottom) / 2.0
        m_per_deg_lat = 111_320.0
        m_per_deg_lon = 111_320.0 * cos(radians(lat))
        return a * m_per_deg_lon * e * m_per_deg_lat
    return a * e  # hệ mét (UTM...) thì đơn vị transform đã là mét


def _majority_smooth(labels: np.ndarray) -> np.ndarray:
    """Lọc majority 3x3 bỏ nhiễu muối tiêu (pixel lẻ bị nuốt bởi láng giềng)."""
    img = Image.fromarray(labels, mode="L")
    return np.array(img.filter(ImageFilter.ModeFilter(size=3)))


def classify(tif_path: Path, out_dir: Path, ndvi_t: float, mndwi_t: float,
             ndbi_t: float, bsi_t: float, cloud_on: bool, cloud_t: float,
             smooth: bool) -> dict[str, object]:
    """Phân loại 1 file. Trả về dict thống kê."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = tif_path.stem

    with rasterio.open(tif_path) as src:
        if src.count < 5:
            raise ValueError(f"cần tối thiểu 5 band (Blue..SWIR1), file chỉ có {src.count}")
        blue = src.read(1).astype(np.float32)
        green = src.read(2).astype(np.float32)
        red = src.read(3).astype(np.float32)
        nir = src.read(4).astype(np.float32)
        swir1 = src.read(5).astype(np.float32)
        profile = src.profile.copy()
        pixel_area = _pixel_area_m2(src)

    # Vùng hợp lệ: tất cả band finite
    valid = (np.isfinite(blue) & np.isfinite(green) & np.isfinite(red)
             & np.isfinite(nir) & np.isfinite(swir1))

    mndwi = _norm_diff(green, swir1)
    ndvi = _norm_diff(nir, red)
    ndbi = _norm_diff(swir1, nir)
    bsi = _norm_diff(swir1 + red, nir + blue)
    is_cloud = (blue > cloud_t) & (red > cloud_t) & (green > cloud_t) if cloud_on else None

    labels = np.full(valid.shape, 6, dtype=np.uint8)  # mặc định: KHÁC
    labels[bsi > bsi_t] = 5                           # đất trống
    labels[ndbi > ndbi_t] = 4                         # dân cư
    labels[ndvi > ndvi_t] = 3                          # thực vật
    if cloud_on:
        labels[is_cloud] = 2                           # mây (đè lên các lớp trên)
    labels[mndwi > mndwi_t] = 1                        # nước (ưu tiên cao nhất)
    labels[~valid] = 0                                 # nodata

    if smooth:
        labels = _majority_smooth(labels)

    # ---- 1. GeoTIFF kèm color table ----
    tif_out = out_dir / f"{stem}_landcover.tif"
    prof = profile.copy()
    prof.update(count=1, dtype="uint8", nodata=0, compress="deflate")
    with rasterio.open(tif_out, "w", **prof) as dst:
        dst.write(labels, 1)
        dst.set_band_description(1, "landcover")
        dst.write_colormap(1, {v: (c[0], c[1], c[2], 255) for v, (_, c) in CLASSES.items()})

    # ---- 2. PNG bản đồ màu + chú giải ----
    lut = np.zeros((256, 3), dtype=np.uint8)
    for v, (_, c) in CLASSES.items():
        lut[v] = c
    rgb = lut[labels]
    png_out = out_dir / f"{stem}_landcover.png"
    img = Image.fromarray(rgb)
    img.thumbnail((1800, 1800))
    img = _add_legend(img, CLASSES)
    img.save(png_out)

    # ---- 3. CSV thống kê ----
    csv_out = out_dir / f"{stem}_stats.csv"
    total_valid = int((labels != 0).sum())
    with open(csv_out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["ma", "lop", "so_pixel", "dien_tich_ha", "phan_tram"])
        for v, (name, _) in CLASSES.items():
            n = int((labels == v).sum())
            ha = n * pixel_area / 10_000.0
            pct = 100.0 * n / total_valid if total_valid else 0.0
            w.writerow([v, name, n, f"{ha:.1f}", f"{pct:.2f}"])

    return {
        "tif": tif_out, "png": png_out, "csv": csv_out,
        "stats": {CLASSES[v][0]: int((labels == v).sum()) for v in CLASSES},
        "total_valid": total_valid,
    }


def _add_legend(img: Image.Image, classes: dict) -> Image.Image:
    """Vẽ chú giải (legend) góc trên trái ảnh."""
    try:
        font = ImageFont.truetype("arial.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    pad, box, line_h = 8, 18, 24
    entries = [(name, color) for v, (name, color) in classes.items() if v != 0]
    w = max(len(name) for name, _ in entries) * 9 + box + pad * 3
    h = len(entries) * line_h + pad * 2
    draw = ImageDraw.Draw(img, "RGBA")
    draw.rectangle([pad, pad, pad + w, pad + h], fill=(255, 255, 255, 210),
                   outline=(0, 0, 0, 255), width=1)
    y = pad + 4
    for name, color in entries:
        draw.rectangle([pad * 2, y + 3, pad * 2 + box, y + 3 + box],
                       fill=tuple(color), outline=(0, 0, 0, 255))
        draw.text((pad * 2 + box + 8, y), name, fill=(0, 0, 0, 255), font=font)
        y += line_h
    return img


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tạo bản đồ phân loại đất từ GeoTIFF 6-band (NDVI/MNDWI/NDBI/BSI)")
    parser.add_argument("input", type=Path, help="File .tif hoặc thư mục chứa .tif")
    parser.add_argument("-o", "--outdir", type=Path, default=Path("downloads/landcover"),
                        help="Thư mục ghi output, mặc định downloads/landcover")
    parser.add_argument("--ndvi", type=float, default=0.35, help="Ngưỡng NDVI thực vật (mặc định 0.35)")
    parser.add_argument("--mndwi", type=float, default=0.10, help="Ngưỡng MNDWI nước (mặc định 0.10)")
    parser.add_argument("--ndbi", type=float, default=0.0, help="Ngưỡng NDBI dân cư (mặc định 0.0)")
    parser.add_argument("--bsi", type=float, default=0.0, help="Ngưỡng BSI đất trống (mặc định 0.0)")
    parser.add_argument("--cloud-thresh", type=float, default=0.2,
                        help="Ngưỡng phản xạ Blue/Green/Red để coi là mây (mặc định 0.2)")
    parser.add_argument("--no-cloud", action="store_true", help="Tắt lớp mây")
    parser.add_argument("--smooth", action="store_true",
                        help="Lọc majority 3x3 bỏ nhiễu muối tiêu")
    args = parser.parse_args()

    files = sorted(args.input.glob("*.tif")) if args.input.is_dir() else [args.input]
    if not files:
        sys.exit(f"Không tìm thấy file .tif: {args.input}")

    for f in files:
        if f.stem.endswith("_landcover"):
            continue
        try:
            r = classify(f, args.outdir, args.ndvi, args.mndwi, args.ndbi,
                         args.bsi, not args.no_cloud, args.cloud_thresh, args.smooth)
            print(f"{f.name}:")
            for name, n in r["stats"].items():
                if n:
                    print(f"  {name:16s}: {n:>10,} pixel")
            print(f"  -> {r['tif'].name} | {r['png'].name} | {r['csv'].name}")
        except (ValueError, rasterio.errors.RasterioError) as e:
            print(f"BỎ QUA {f.name}: {e}")


if __name__ == "__main__":
    main()
