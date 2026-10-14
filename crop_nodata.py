"""Cắt bỏ vùng không có giá trị (nodata/-inf, hiển thị đen) khỏi GeoTIFF.

Tìm hình chữ nhật nhỏ nhất bao quanh toàn bộ pixel có giá trị ở TẤT CẢ các band,
rồi cắt file theo vùng đó — giữ nguyên số band, mô tả band, hệ tọa độ.

Ví dụ dùng:
    .venv\\Scripts\\python.exe crop_nodata.py downloads\\sentinel2_xxx.tif
    .venv\\Scripts\\python.exe crop_nodata.py downloads              # xử lý cả thư mục
    .venv\\Scripts\\python.exe crop_nodata.py image.tif -o downloads\\cropped.tif
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window


def crop_one(tif_path: Path, out_path: Path, padding: int = 0) -> tuple[Path, int, int]:
    """Cắt một file. Trả về (đường dẫn ghi ra, chiều rộng mới, chiều cao mới)."""
    with rasterio.open(tif_path) as src:
        nodata = src.nodata

        # Tìm mask hợp lệ: pixel có giá trị ở TẤT CẢ các band
        valid = np.ones((src.height, src.width), dtype=bool)
        for i in range(1, src.count + 1):
            band = src.read(i)
            ok = np.isfinite(band) if np.issubdtype(band.dtype, np.floating) else np.ones_like(valid)
            if nodata is not None:
                ok &= band != nodata
            valid &= ok

        if valid.all():
            raise ValueError("file đã đầy 100% pixel có giá trị, không cần cắt")

        rows = np.any(valid, axis=1)
        cols = np.any(valid, axis=0)
        if not rows.any():
            raise ValueError("file không có pixel có giá trị nào")

        r0, r1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]) - 1)
        c0, c1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]) - 1)

        # Thêm padding (nếu có) nhưng không vượt mép ảnh
        r0 = max(0, r0 - padding)
        c0 = max(0, c0 - padding)
        r1 = min(src.height - 1, r1 + padding)
        c1 = min(src.width - 1, c1 + padding)

        win = Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1)
        transform = src.window_transform(win)

        profile = src.profile.copy()
        profile.update(
            width=int(win.width),
            height=int(win.height),
            transform=transform,
            compress="deflate",
        )

        with rasterio.open(out_path, "w", **profile) as dst:
            for i in range(1, src.count + 1):
                dst.write(src.read(i, window=win), i)
            dst.descriptions = src.descriptions
            dst.update_tags(**src.tags())

    return out_path, int(win.width), int(win.height)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cắt bỏ vùng nodata (đen) khỏi GeoTIFF")
    parser.add_argument("input", type=Path, help="File .tif hoặc thư mục chứa .tif")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="File ghi ra (khi xử lý 1 file). Mặc định: <tên>_crop.tif")
    parser.add_argument("-p", "--padding", type=int, default=0,
                        help="Thêm viền (pixel) quanh vùng có giá trị, mặc định 0")
    args = parser.parse_args()

    if args.input.is_dir():
        files = sorted(args.input.glob("*.tif"))
        if not files:
            sys.exit(f"Không tìm thấy file .tif nào trong {args.input}")
        for f in files:
            out = f.with_name(f.stem + "_crop.tif")
            try:
                path, w, h = crop_one(f, out, args.padding)
                print(f"{f.name} -> {path.name} ({w} x {h})")
            except ValueError as e:
                print(f"BỎ QUA {f.name}: {e}")
    else:
        out = args.output or args.input.with_name(args.input.stem + "_crop.tif")
        path, w, h = crop_one(args.input, out, args.padding)
        print(f"{args.input.name} -> {path} ({w} x {h})")


if __name__ == "__main__":
    main()
