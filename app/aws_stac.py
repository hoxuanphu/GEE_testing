"""AWS Earth Search STAC client — Sentinel-2 L2A COG (không cần auth).

Nguồn phụ cho S2 L2A: bucket `s3://sentinel-s2-l2a` (element84) với STAC API
công khai tại https://earth-search.aws.element84.com/v1 — search + truy cập
asset đều không cần tài khoản AWS (no-sign-request).

Ưu điểm so với CDSE cho AOI nhỏ: asset là **COG per-band** nên có thể đọc
windowed (chỉ vùng AOI) bằng rasterio mà không tải cả cảnh ~1 GB.

Lưu ý: đây là S2 L2A chuẩn Sen2Cor — có band SCL tương thích pipeline
`app/imagery.py`, nhưng KHÔNG có harmonization offset của GEE
`S2_SR_HARMONIZED`; baseline >= 04.00 cần trừ offset (-1000) khi convert
DN -> reflectance (đã xử lý trong harmonize()).
"""
from __future__ import annotations

import datetime as dt
import time

import requests

from .ee_session import _load_extra_cas

_STAC_URL = "https://earth-search.aws.element84.com/v1/search"
_COLLECTION = "sentinel-2-l2a"

# Asset name trong Earth Search STAC dùng common names:
# blue=B02, green=B03, red=B04, nir=B08, scl=SCL
S2_L2A_BANDS = ("blue", "green", "red", "nir", "scl")

# Ánh xạ common name -> band id GEE-style (để hiển thị nhất quán)
S2_ASSET_TO_BAND = {"blue": "B02", "green": "B03", "red": "B04", "nir": "B08", "scl": "SCL"}

# Offset harmonization của processing baseline >= 04.00 (giống GEE)
_BASELINE_OFFSET = -1000.0
_S2_SCALE = 10000.0

# Giá trị SCL bị coi là mây/bóng mây để mask khi mosaic tỉnh (giống
# _mask_s2_clouds của pipeline GEE trong app/imagery.py).
_SCL_CLOUD_VALUES = (3, 8, 9, 10, 11)
# Nodata cho band SR (float32 reflectance) và SCL (uint8) trong file xuất.
_SR_NODATA = -9999.0
_SCL_NODATA = 255


def _rasterize_province_mask(
    geometry: dict, transform, out_shape: tuple[int, int], crs
) -> "np.ndarray":
    """Rasterize polygon tỉnh (GeoJSON 4326) sang lưới ảnh CRS bất kỳ.

    Trả về mảng uint8: 1 = trong tỉnh, 0 = ngoài. Geometry được
    transform_geom từ EPSG:4326 sang CRS của lưới trước khi rasterize.
    """
    import numpy as np  # noqa: PLC0415
    from rasterio.features import rasterize  # noqa: PLC0415
    from rasterio.warp import transform_geom  # noqa: PLC0415

    geom = transform_geom("EPSG:4326", crs, geometry)
    # CRS dạng chuỗi cũng OK cho rasterize; đảm bảo kiểu CRS chuẩn
    return rasterize(
        [(geom, 1)],
        out_shape=out_shape,
        transform=transform,
        fill=0,
        dtype="uint8",
        all_touched=False,
    )


def _read_tile_stack(
    product: dict,
    bands: list[str],
    bounds_wgs84: tuple[float, float, float, float],
) -> tuple[dict, object, object] | None:
    """Đọc các band của 1 item trong window (tile ∩ bounds_wgs84).

    Trả về (arrays: {band: np.ndarray}, transform, crs) hoặc None nếu
    window không giao tile. Band SR được harmonize DN -> reflectance
    float32; SCL giữ uint8. Pixel mây (SCL 3/8/9/10/11) bị set nodata
    TRƯỚC khi mosaic để tile khác lấp (giống _mask_s2_clouds của GEE).
    """
    import numpy as np  # noqa: PLC0415

    assets = product.get("assets") or {}
    # Đọc SCL trước để có mask mây cho các band SR
    read_order = [b for b in bands if b != "SCL"] + (
        ["SCL"] if "SCL" in bands else []
    )
    arrays: dict = {}
    transform = None
    crs = None
    for band in read_order:
        if band not in assets:
            continue
        arr, tf, crs_ = read_band_window(assets[band], bounds_wgs84)
        if band == "SCL":
            arr = arr.astype("uint8")
        else:
            arr = (arr.astype("float32") + _BASELINE_OFFSET) / _S2_SCALE
        arrays[band] = arr
        if transform is None:
            transform, crs = tf, crs_
    if not arrays:
        return None
    # Mask mây pixel theo SCL (cùng thang 20m lên đệm window SR 10m —
    # SCL window được đọc cùng bbox nên cùng kích thước pixel hoá theo
    # độ phân giải gốc của từng band; khi kích thước lệch, dùng nearest
    # để khớp mảng SR).
    scl = arrays.get("SCL")
    if scl is not None:
        cloud = np.isin(scl, _SCL_CLOUD_VALUES)
        for band, arr in arrays.items():
            if band == "SCL":
                continue
            if arr.shape != scl.shape:
                # Band 10m có gấp đôi cạnh so với SCL 20m — upsample mask
                import numpy as _np  # noqa: PLC0415

                k = arr.shape[0] // max(scl.shape[0], 1)
                if k > 1:
                    cloud_rs = _np.repeat(_np.repeat(cloud, k, axis=0), k, axis=1)
                    if cloud_rs.shape == arr.shape:
                        cloud_m = cloud_rs
                    else:  # lẻ pixel — cắt/pad cho khớp
                        h, w = arr.shape
                        cloud_m = _np.zeros((h, w), dtype=bool)
                        hh = min(h, cloud_rs.shape[0])
                        ww = min(w, cloud_rs.shape[1])
                        cloud_m[:hh, :ww] = cloud_rs[:hh, :ww]
                else:
                    cloud_m = cloud
            else:
                cloud_m = cloud
            arr[cloud_m] = _SR_NODATA
    return arrays, transform, crs


def province_mosaic(
    products: list[dict],
    province_geometry: dict,
    province_bbox: tuple[float, float, float, float],
    out_path: "Path",
) -> list[dict]:
    """Ghép các tile S2 L2A COG thành 1 GeoTIFF cắt đúng biên tỉnh.

    Mỗi item (1 tile × 1 ngày) được đọc windowed trong vùng tile ∩ bbox
    tỉnh, mask pixel mây theo SCL, reproject về lưới đích 10 m UTM (theo
    CRS của tile đầu tiên hợp lệ) rồi mosaic: pixel mới chỉ lấp chỗ còn
    nodata (tile trước đó giữ ưu tiên — thứ tự input quyết định ưu tiên).
    Cuối cùng mask theo polygon tỉnh (rasterize). Band: B02, B03, B04,
    B08, SCL — SR harmonize float32 như merge_all_bands.

    Trả về metadata các tile đã dùng: {id, tile, date, cloud_pct}.
    """
    import numpy as np  # noqa: PLC0415
    import rasterio  # noqa: PLC0415
    from rasterio.enums import Resampling  # noqa: PLC0415
    from rasterio.transform import from_origin  # noqa: PLC0415
    from rasterio.warp import reproject, transform_bounds  # noqa: PLC0415

    band_order = [b for b in ("B02", "B03", "B04", "B08", "SCL") if b]
    used: list[dict] = []
    base_crs = None
    base_transform = None
    base_shape = None
    stack: dict | None = None  # {band: mảng nodata} trên lưới đích
    res = 10.0

    for product in products:
        try:
            got = _read_tile_stack(product, band_order, province_bbox)
        except ValueError:
            # Tile không giao bbox tỉnh (hoặc window rỗng) — bỏ qua
            continue
        if got is None:
            continue
        arrays, tf, crs = got
        if base_crs is None:
            # Lưới đích: dùng thẳng lưới window của tile đầu tiên (CRS UTM
            # của tile, 10 m). Các tile sau reproject về lưới này.
            base_crs, base_transform, base_shape = crs, tf, next(
                iter(arrays.values())
            ).shape
            stack = {
                b: np.full(
                    base_shape,
                    _SCL_NODATA if b == "SCL" else _SR_NODATA,
                    dtype="uint8" if b == "SCL" else "float32",
                )
                for b in band_order
                if b in arrays
            }
        for band in band_order:
            if band not in arrays or band not in stack:
                continue
            src_arr = arrays[band]
            nod = _SCL_NODATA if band == "SCL" else _SR_NODATA
            temp = np.full(base_shape, nod, dtype=stack[band].dtype)
            reproject(
                source=src_arr,
                destination=temp,
                src_transform=tf,
                src_crs=crs,
                dst_transform=base_transform,
                dst_crs=base_crs,
                src_nodata=nod,
                dst_nodata=nod,
                resampling=(
                    Resampling.nearest if band == "SCL" else Resampling.bilinear
                ),
                num_threads=2,
            )
            valid = temp != nod
            target = stack[band]
            # Mosaic: chỉ lấp pixel còn nodata (tile đầu giữ ưu tiên)
            fill = valid & (target == nod)
            target[fill] = temp[fill]
        used.append(
            {
                "id": product.get("id"),
                "tile": product.get("tile"),
                "date": product.get("date"),
                "cloud_pct": product.get("cloud_pct"),
            }
        )

    if not used or stack is None:
        raise ValueError(
            "Không tile nào giao với vùng tỉnh sau khi đọc COG windowed."
        )

    # Mask theo polygon tỉnh — 1 lần sau khi mosaic
    pmask = _rasterize_province_mask(
        province_geometry, base_transform, base_shape, base_crs
    )
    for band, arr in stack.items():
        nod = _SCL_NODATA if band == "SCL" else _SR_NODATA
        arr[pmask == 0] = nod

    bands_out = [b for b in band_order if b in stack]
    profile = {
        "driver": "GTiff",
        "height": base_shape[0],
        "width": base_shape[1],
        "count": len(bands_out),
        "dtype": "float32",
        "crs": base_crs,
        "transform": base_transform,
        "nodata": _SR_NODATA,
        "compress": "deflate",
        "tiled": True,
        "bigtiff": "IF_SAFER",
    }
    with rasterio.open(out_path, "w", **profile) as dst:
        for idx, band in enumerate(bands_out, start=1):
            arr = stack[band].astype("float32")
            if band == "SCL":
                # Nội bộ mosaic SCL dùng 255 làm nodata — chuẩn hoá về
                # nodata chung của file (-9999) khi ghi ra.
                arr[arr == _SCL_NODATA] = _SR_NODATA
            dst.write(arr, idx)
            dst.set_band_description(idx, band)
    return used


def _ensure_cas() -> None:
    _load_extra_cas()


def harmonize(dn: float) -> float:
    """Chuyển DN L2A thành reflectance, xử lý offset baseline >= 04.00."""
    return (dn + _BASELINE_OFFSET) / _S2_SCALE


def get_item(product_id: str) -> dict | None:
    """Lấy 1 item S2 L2A theo ID trực tiếp (không phụ thuộc bbox/điểm).

    Dùng GET /collections/sentinel-2-l2a/items/{id} — trả về None nếu
    item không tồn tại. Tránh lỗi search-theo-điểm bỏ sót item thuộc tile
    khác (điểm chọn nằm ngoài footprint tile đó).
    """
    import requests as _requests  # noqa: PLC0415

    _ensure_cas()
    url = f"https://earth-search.aws.element84.com/v1/collections/{_COLLECTION}/items/{product_id}"
    try:
        resp = _requests.get(url, timeout=60)
    except _requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    return _normalize_features([resp.json()])[0] if resp.json().get("id") else None


# Giới hạn an toàn khi lần theo link next của STAC (mỗi page tối đa 100 item)
_MAX_STAC_PAGES = 40


def _search_stac(body: dict, max_results: int) -> list[dict]:
    """POST /search và lần theo link rel=next tới khi đủ max_results item.

    STAC chỉ trả tối đa 100 item/lần (page). Không phân trang thì khoảng
    thời gian dài (vài năm) sẽ bị cắt mất phần cũ — chỉ còn các cảnh mới.
    """
    features: list[dict] = []
    page_body = dict(body)
    page_body["limit"] = min(max_results, 100)
    url: str = _STAC_URL
    for _ in range(_MAX_STAC_PAGES):
        resp = requests.post(url, json=page_body, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        feats = payload.get("features", [])
        if not feats:
            break
        features.extend(feats)
        if len(features) >= max_results:
            break
        nxt = next(
            (l for l in payload.get("links", []) if l.get("rel") == "next"), None
        )
        if not nxt:
            break
        # stac-fastapi trả body POST sẵn (kèm token) trong link next
        nb = nxt.get("body")
        if isinstance(nb, dict) and nb:
            page_body = dict(nb)
            page_body["limit"] = min(max_results - len(features), 100)
            url = nxt.get("href", _STAC_URL)
        elif nxt.get("token"):
            page_body["token"] = nxt["token"]
            page_body["limit"] = min(max_results - len(features), 100)
        else:
            break
    return features[:max_results]


def search_s2_l2a(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    max_results: int = 50,
    cloud_max: float | None = None,
    cloud_min: float | None = None,
) -> list[dict]:
    """Search S2 L2A items trên AWS Earth Search STAC quanh một điểm.

    Trả về newest-first: {id, name, date, tile, cloud_pct, size_mb (ước
    lượng), assets: {B04: url, ...}, download_url (trang STAC)}.
    """
    _ensure_cas()
    body: dict = {
        "collections": [_COLLECTION],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "limit": min(max_results, 100),
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    if cloud_max is not None:
        body["query"] = {"eo:cloud_cover": {"lte": float(cloud_max)}}
    if cloud_min is not None:
        body.setdefault("query", {}).setdefault("eo:cloud_cover", {})["gte"] = float(cloud_min)

    features = _search_stac(body, max_results)

    out: list[dict] = []
    for f in features:
        props = f.get("properties", {})
        assets = f.get("assets", {})
        band_urls = {
            S2_ASSET_TO_BAND.get(b, b): assets[b]["href"]
            for b in S2_L2A_BANDS
            if b in assets and "href" in assets[b]
        }
        datetime_str = props.get("datetime", "")
        m = None
        # id dạng S2A_..._T48PWS_... hoặc dùng grid:code
        grid = props.get("grid:code", "")  # "MGRS-48PWS"
        if grid.startswith("MGRS-"):
            m = grid[5:]
        out.append(
            {
                "id": f.get("id"),
                "name": f.get("id"),
                "date": datetime_str[:10] if datetime_str else None,
                "tile": m,
                "cloud_pct": props.get("eo:cloud_cover"),
                "size_mb": None,  # COG per-band: không có size tổng
                "cog": True,
                "assets": band_urls,
                "download_url": f.get("links", [{}])[0].get("href"),
                "self_href": f.get("links", [{}])[0].get("href"),
            }
        )
    out.sort(key=lambda p: p["date"] or "", reverse=True)
    return out


def search_s2_l2a_bbox(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    # Chế độ tỉnh: 4 tile × ~73 lượt/năm ≈ 300 scene/năm — có thể cần >1000
    # cho khoảng nhiều năm, nên mặc định 2000 (20 trang STAC).
    max_results: int = 2000,
    cloud_max: float | None = None,
    cloud_min: float | None = None,
) -> list[dict]:
    """Search S2 L2A items intersecting bbox (west, south, east, north)."""
    _ensure_cas()
    west, south, east, north = bbox
    body: dict = {
        "collections": [_COLLECTION],
        "bbox": [west, south, east, north],
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "limit": min(max_results, 100),
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    if cloud_max is not None:
        body["query"] = {"eo:cloud_cover": {"lte": float(cloud_max)}}
    if cloud_min is not None:
        body.setdefault("query", {}).setdefault("eo:cloud_cover", {})["gte"] = float(cloud_min)

    features = _search_stac(body, max_results)
    return _normalize_features(features)


def _normalize_features(features: list[dict]) -> list[dict]:
    out: list[dict] = []
    for f in features:
        props = f.get("properties", {})
        assets = f.get("assets", {})
        band_urls = {
            S2_ASSET_TO_BAND.get(b, b): assets[b]["href"]
            for b in S2_L2A_BANDS
            if b in assets and "href" in assets[b]
        }
        grid = props.get("grid:code", "")
        out.append(
            {
                "id": f.get("id"),
                "name": f.get("id"),
                "date": (props.get("datetime") or "")[:10] or None,
                "tile": grid[5:] if grid.startswith("MGRS-") else None,
                "cloud_pct": props.get("eo:cloud_cover"),
                "size_mb": None,
                "cog": True,
                "assets": band_urls,
            }
        )
    out.sort(key=lambda p: p["date"] or "", reverse=True)
    return out


def read_band_window(
    asset_url: str,
    bbox: tuple[float, float, float, float] | None = None,
    halo: int = 0,
    masked: bool = False,
) -> "tuple":
    """Đọc COG band từ S3 (no-sign-request) chỉ trong vùng bbox.

    Trả về (array, transform, crs). Cần rasterio + GDAL với VSIS3 hỗ trợ
    anonymous access (gdal.SetConfigOption('AWS_NO_SIGN_REQUEST', 'YES')).
    """
    try:
        import rasterio  # noqa: PLC0415
        from rasterio.windows import from_bounds  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "Cần cài rasterio để đọc COG windowed từ AWS S3"
        ) from exc

    # Chuyển s3://... hoặc https://...bucket.s3... về URL công khai
    url = asset_url
    if url.startswith("s3://"):
        url = url.replace("s3://sentinel-s2-l2a/", "").lstrip("/")
        url = f"https://sentinel-s2-l2a.s3.eu-central-1.amazonaws.com/{url}"
    elif ".s3." in url and "amazonaws.com" in url:
        pass  # đã là https URL công khai
    else:
        raise ValueError(f"Unsupported asset URL: {asset_url}")

    # rasterio đọc HTTP Range trực tiếp (COG) — không cần AWS SDK cho GET.
    # Retry: CDN/proxy đôi khi reset stream giữa chừng (đặc biệt qua mạng
    # công ty), 1 lần fail không có nghĩa dữ liệu hỏng.
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            with rasterio.open(url) as src:
                if bbox is not None:
                    # COG S2 ở UTM (EPSG:326xx/327xx) — tính window bằng cách
                    # chiếu bbox lon/lat sang CRS của ảnh trước, tránh mismatch
                    # hệ tọa độ.
                    from rasterio.warp import transform_bounds  # noqa: PLC0415
                    west, south, east, north = transform_bounds(
                        "EPSG:4326", src.crs, *bbox, densify_pts=21
                    )
                    # Clamp vào extent của ảnh: bbox có thể chỉ giao một phần
                    # tile (vd điểm chọn nằm sát mép tỉnh, tile kề chỉ phủ
                    # một dải hẹp).
                    full = from_bounds(*src.bounds, transform=src.transform)
                    west = max(west, src.bounds.left)
                    east = min(east, src.bounds.right)
                    south = max(south, src.bounds.bottom)
                    north = min(north, src.bounds.top)
                    if east <= west or north <= south:
                        raise ValueError(
                            "Vùng chọn (bbox) không giao với ảnh COG này — "
                            "hãy kiểm tra lại tọa độ điểm."
                        )
                    window = from_bounds(
                        west, south, east, north, transform=src.transform
                    )
                    if halo:
                        import math
                        from rasterio.windows import Window
                        left, top = math.floor(window.col_off) - halo, math.floor(window.row_off) - halo
                        right, bottom = math.ceil(window.col_off + window.width) + halo, math.ceil(window.row_off + window.height) + halo
                        window = Window(left, top, right-left, bottom-top)
                    else:
                        window = window.round_lengths().round_offsets()
                    # Giới hạn trong ảnh (tránh đọc vượt extent -> dataset 0 chiều)
                    window = window.intersection(full)
                    if window.width < 1 or window.height < 1:
                        raise ValueError(
                            "Vùng chọn (bbox) không giao với ảnh COG này — "
                            "hãy kiểm tra lại tọa độ điểm."
                        )
                    data = src.read(1, window=window, masked=masked)
                    transform = src.window_transform(window)
                    crs = src.crs
                else:
                    data = src.read(1, masked=masked)
                    transform = src.transform
                    crs = src.crs
                return data, transform, crs
        except ValueError:
            raise
        except Exception as exc:  # rasterio/RasterioIOError, network
            last_exc = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(
        f"Không đọc được band COG từ AWS sau 3 lần thử: {last_exc}"
    )


def merge_all_bands(
    assets: dict[str, str],
    out_path: "Path",
    bbox: tuple[float, float, float, float] | None = None,
    output_grid: dict | None = None,
) -> list[str]:
    """Ghép các band COG thành một GeoTIFF đa lớp (kiểu GEE band order).

    Band SR (B02/B03/B04/B08) được harmonize DN -> reflectance float32
    (trừ offset -1000, chia 10000) giống pipeline GEE S2_SR_HARMONIZED;
    SCL giữ nguyên uint8. Mỗi lớp có band description để dễ phân biệt.

    Trả về danh sách tên band theo thứ tự lớp trong file xuất.
    """
    import numpy as np  # noqa: PLC0415

    band_order = [b for b in ("B02", "B03", "B04", "B08", "SCL") if b in assets]
    if not band_order:
        raise ValueError("Không có band COG nào khả dụng trong assets")

    if output_grid is not None:
        import rasterio
        from .raster_grid import grid_profile, write_grid_band
        with rasterio.open(out_path, "w", **grid_profile(output_grid, len(band_order))) as dst:
            for index, band in enumerate(band_order, 1):
                arr, transform, crs = read_band_window(assets[band], bbox, halo=1, masked=True)
                arr = np.ma.asarray(arr, dtype="float32")
                if band != "SCL":
                    arr = (arr + _BASELINE_OFFSET) / _S2_SCALE
                with rasterio.MemoryFile() as memory:
                    with memory.open(driver="GTiff", width=arr.shape[1], height=arr.shape[0],
                                     count=1, dtype="float32", crs=crs, transform=transform,
                                     nodata=float("nan")) as source:
                        source.write(arr.filled(float("nan")), 1)
                    with memory.open() as source:
                        write_grid_band(source, dst, output_grid, 1, index, band == "SCL")
                dst.set_band_description(index, band)
        return band_order

    layers = []
    profile = None
    for band in band_order:
        arr, transform, crs = read_band_window(assets[band], bbox)
        if band != "SCL":
            arr = (arr.astype("float32") + _BASELINE_OFFSET) / _S2_SCALE
        else:
            arr = arr.astype("uint8")
        layers.append((band, arr))
        if profile is None:
            profile = {
                "driver": "GTiff",
                "height": arr.shape[0],
                "width": arr.shape[1],
                "count": len(band_order),
                "dtype": "float32",
                "crs": crs,
                "transform": transform,
                "compress": "deflate",
            }

    import rasterio  # noqa: PLC0415

    with rasterio.open(out_path, "w", **profile) as dst:
        for idx, (band, arr) in enumerate(layers, start=1):
            dst.write(arr, idx)
            dst.set_band_description(idx, band)
    return band_order
