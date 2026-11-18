"""Tải composite Sentinel-2 SR (10 m) cho tỉnh An Giang (VN44).

Chạy đúng code path của /api/download: best_scene_for_roi(composite) ->
download_geotiff -> merge_band_tiffs. Method ghép lấy từ argv (mặc định
medoid — màu trung thực hơn median): python download_angiang_median.py [medoid|median|mosaic|base-score]
"""
import datetime as dt
import sys
import time
import traceback
from pathlib import Path

# rasterio thỉnh thoảng fail import kiểu flaky trong môi trường detached
# ("DLL load failed ... The handle is invalid") — import sớm để fail nhanh
# thay vì phát hiện sau khi đã tải xong toàn bộ tile.
import rasterio  # noqa: F401

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from app.admin_boundaries import get_province
from app.ee_session import init_ee
from app.imagery import (
    MEDOID_MAX_TILE_PIXELS,
    S2_SCALE,
    best_scene_for_roi,
    download_geotiff,
    merge_band_tiffs,
)

START = "2026-03-20"
END = "2026-05-19"  # inclusive
END_EXCLUSIVE = (dt.date.fromisoformat(END) + dt.timedelta(days=1)).isoformat()
METHOD = sys.argv[1] if len(sys.argv) > 1 else "medoid"
OUT = BASE_DIR / "downloads" / f"sentinel2_VN44_{START}_{END}_{METHOD}.tif"


def main() -> int:
    t0 = time.time()
    print("init_ee()...", flush=True)
    init_ee()
    print(f"EE ok ({time.time()-t0:.1f}s)", flush=True)

    province = get_province("VN44")
    if province is None:
        print("ERROR: province VN44 not found")
        return 1
    roi = province.ee_geometry()
    print(f"ROI: {province.name} bbox={province.bbox}", flush=True)

    print(f"building {METHOD} composite...", flush=True)
    t1 = time.time()
    scene = best_scene_for_roi(
        "sentinel2",
        roi,
        START,
        END_EXCLUSIVE,
        composite=True,
        composite_method=METHOD,
    )
    if scene is None:
        print("ERROR: no scene found in date range")
        return 1
    print(
        f"{METHOD} ok ({time.time()-t1:.1f}s): {scene.scene_count} scenes, "
        f"date={scene.date}, mean cloud={scene.cloud_pct:.1f}%",
        flush=True,
    )

    print("requesting download URLs (per band, tiled)...", flush=True)
    t2 = time.time()
    band_urls = download_geotiff(
        scene.ee_object, "sentinel2", province.bbox, S2_SCALE, roi,
        max_tile_pixels=(
            MEDOID_MAX_TILE_PIXELS
            if METHOD in ("medoid", "base-score")
            else None
        ),
    )
    n_tiles = sum(1 for b, _ in band_urls if "_t" in b)
    print(
        f"got {len(band_urls)} URLs ({n_tiles} tiled) ({time.time()-t2:.1f}s)",
        flush=True,
    )

    print("downloading + merging...", flush=True)
    t3 = time.time()
    merge_band_tiffs(band_urls, OUT)
    size_mb = OUT.stat().st_size / 1e6
    print(f"DONE: {OUT} ({size_mb:.1f} MB) in {time.time()-t3:.1f}s", flush=True)
    print(f"total {time.time()-t0:.1f}s", flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        print("FAILED:", flush=True)
        traceback.print_exc()
        sys.exit(2)
