"""Copernicus Data Space Ecosystem (CDSE) client — Sentinel-1 SAR data.

- Search (OData catalogue) is public, no auth needed.
- Download requires a password-grant token (client_id=cdse-public) with the
  CDSE portal username/password (CDSE_USERNAME / CDSE_PASSWORD in .env).
  Dashboard OAuth clients (CDSE_CLIENT_ID/SECRET) are Sentinel Hub credentials
  and are rejected by the OData download endpoint ("Token audience not allowed").

Sản phẩm tải về là file ZIP đầy đủ của cảnh (GRD ~1–2 GB, SLC ~4–8 GB),
không cắt theo vùng như Sentinel-2/Landsat trên GEE.

Ngoài GRD, module còn tìm Sentinel-1 IW SLC ($expand=Attributes để lấy
relative orbit / orbit direction / polarization phục vụ ghép cặp InSAR).
Metadata thiếu là None — không suy diễn track hay baseline từ tên file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator

import requests

from .ee_session import _load_dotenv, _load_extra_cas

_TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE"
    "/protocol/openid-connect/token"
)
_CATALOG_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
_DOWNLOAD_URL = (
    "https://download.dataspace.copernicus.eu/odata/v1/Products({pid})/$value"
)

# Giới hạn an toàn khi đọc nhiều trang catalogue (@odata.nextLink)
_MAX_CATALOG_PAGES = 5
_MAX_CATALOG_PRODUCTS = 500

# Các product type Sentinel-3 thường dùng (kiểm tra input trước khi query)
_S3_PRODUCT_TYPES = {
    "OL_1_EFR",  # OLCI full resolution L1
    "OL_2_LFR",  # OLCI Land reflectance 300 m (phù hợp nhất cho phân tích đất)
    "OL_2_WRR",  # OLCI Water reduced res 1200 m
    "SL_1_RBT",  # SLSTR radiance/brightness temp
    "SL_2_LST",  # SLSTR Land Surface Temperature
    "SR_2_LAN",  # SRAL altimetry land
    "SY_2_SYN",  # Synergy surface reflectance 300 m
}

# OData Attributes -> khóa chuẩn hóa trong response API
_ATTRIBUTE_KEYS = {
    "productType": "product_type",
    "operationalMode": "operational_mode",
    "relativeOrbitNumber": "relative_orbit",
    "orbitDirection": "orbit_direction",
    "polarisationChannels": "polarization",
    "sliceNumber": "slice_number",
    "totalSlices": "total_slices",
}

_token: dict = {"access": None, "expires_at": 0.0}


def _ensure_env() -> None:
    _load_dotenv()
    _load_extra_cas()


def get_token() -> str:
    """Get (and cache) a password-grant access token for downloads."""
    if _token["access"] and time.time() < _token["expires_at"] - 60:
        return _token["access"]

    _ensure_env()
    username = os.getenv("CDSE_USERNAME", "").strip()
    password = os.getenv("CDSE_PASSWORD", "").strip()
    if not username or not password:
        raise RuntimeError(
            "CDSE credentials missing: set CDSE_USERNAME and CDSE_PASSWORD "
            "in .env (email + mật khẩu đăng nhập tại dataspace.copernicus.eu)"
        )

    resp = requests.post(
        _TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "cdse-public",
            "username": username,
            "password": password,
        },
        timeout=60,
    )
    resp.raise_for_status()
    payload = resp.json()
    _token["access"] = payload["access_token"]
    _token["expires_at"] = time.time() + payload.get("expires_in", 600)
    return _token["access"]


def _parse_iso_utc(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_attributes(raw: list | None) -> dict:
    """Map danh sách OData Attributes về dict chuẩn; khóa lạ bỏ qua."""
    out: dict = {}
    for att in raw or []:
        key = _ATTRIBUTE_KEYS.get(att.get("Name"))
        if key is not None and att.get("Value") is not None:
            out[key] = att["Value"]
    return out


def _normalize_product(p: dict, with_metadata: bool) -> dict:
    """Chuẩn hóa 1 product OData.

    with_metadata=True (SLC): thêm các trường Attributes (thiếu -> None).
    with_metadata=False (GRD): giữ đúng response cũ {id, name, date, ...}.
    """
    timestamp = (p.get("ContentDate") or {}).get("Start")
    product = {
        "id": p["Id"],
        "name": p["Name"],
        "date": timestamp[:10] if timestamp else None,
        "size_gb": p.get("ContentLength", 0) / 1e9,
        "online": bool(p.get("Online", False)),
        "cog": "_COG.SAFE" in p["Name"],
    }
    if with_metadata:
        meta = _normalize_attributes(p.get("Attributes"))
        product["timestamp"] = timestamp
        for key in _ATTRIBUTE_KEYS.values():
            product[key] = meta.get(key)
        # Alias rõ ràng cho UI/README
        product["track"] = meta.get("relative_orbit")
        product["pass"] = meta.get("orbit_direction")
    return product


def _fetch_products(params: dict, wanted: int, expand: bool) -> list[dict]:
    """GET catalogue, theo @odata.nextLink tới khi đủ `wanted` product.

    Deduplicate theo Id, hard-cap số trang/số product để tránh query quá rộng.
    """
    out: list[dict] = []
    seen: set[str] = set()
    url: str | None = _CATALOG_URL
    request_params: dict | None = dict(params)
    if expand:
        request_params["$expand"] = "Attributes"
    request_params["$top"] = min(wanted, 1000)

    for _ in range(_MAX_CATALOG_PAGES):
        resp = requests.get(url, params=request_params, timeout=90)
        resp.raise_for_status()
        payload = resp.json()
        for p in payload.get("value", []):
            pid = p.get("Id")
            if pid in seen:
                continue
            seen.add(pid)
            out.append(p)
            if len(out) >= wanted or len(out) >= _MAX_CATALOG_PRODUCTS:
                return out
        url = payload.get("@odata.nextLink")
        if not url:
            break
        # nextLink tự mang query string (skiptoken) — không truyền params nữa
        request_params = None
    return out


def search_s2(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    max_results: int = 50,
    cloud_max: float | None = None,
) -> list[dict]:
    """Search Sentinel-2 L2A (surface reflectance) products covering (lon, lat).

    Ưu tiên sản phẩm _COG.SAFE (GeoTIFF COG, nhỏ ~25%) trong kết quả.
    cloud_max: lọc theo Cloud Cover attribute (0-100) nếu được cung cấp.
    Trả về newest-first: {id, name, date, size_gb, online, cog, tile, cloud_pct}.
    """
    _ensure_env()
    d = 0.02
    west, east = lon - d, lon + d
    south, north = lat - d, lat + d
    polygon = (
        f"POLYGON(({west:.5f} {south:.5f}, {east:.5f} {south:.5f}, "
        f"{east:.5f} {north:.5f}, {west:.5f} {north:.5f}, "
        f"{west:.5f} {south:.5f}))"
    )
    filt = (
        "Collection/Name eq 'SENTINEL-2' and "
        "contains(Name,'MSIL2A') and "
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.999Z and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')"
    )
    if cloud_max is not None:
        filt += f" and Attributes/OData.CSC.Doubles/any(d:d/Name eq 'cloudCover' and d/LessThanOrEqual eq {float(cloud_max)})"
    params = {
        "$filter": filt,
        "$orderby": "ContentDate/Start desc",
    }
    value = _fetch_products(params, max_results, expand=False)
    products = []
    for p in value:
        norm = _normalize_product(p, with_metadata=False)
        # Tile MGRS từ tên: S2A_MSIL2A_YYYYMMDDT...T48PWS_...
        m = re.search(r"_T(\d{2}[A-Z]{3})_", p["Name"])
        norm["tile"] = m.group(1) if m else None
        products.append(norm)
    # COG lên trước trong cùng ngày — tiện chọn để tải
    products.sort(key=lambda p: (p["date"] or "", not p["cog"]), reverse=True)
    return products


def _bbox_wkt(bbox: tuple[float, float, float, float]) -> str:
    """WKT polygon từ bbox (west, south, east, north) — dùng cho OData Intersects."""
    west, south, east, north = bbox
    return (
        f"POLYGON(({west:.5f} {south:.5f}, {east:.5f} {south:.5f}, "
        f"{east:.5f} {north:.5f}, {west:.5f} {north:.5f}, "
        f"{west:.5f} {south:.5f}))"
    )


def search_s2_area(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    cloud_max: float | None = None,
    max_results: int = 300,
) -> list[dict]:
    """Search S2 L2A products intersecting a bbox (province mode).

    Trả về danh sách có trường `tile` (MGRS) để UI nhóm cảnh theo tile.
    """
    _ensure_env()
    filt = (
        "Collection/Name eq 'SENTINEL-2' and "
        "contains(Name,'MSIL2A') and "
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.999Z and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{_bbox_wkt(bbox)}')"
    )
    if cloud_max is not None:
        filt += f" and Attributes/OData.CSC.Doubles/any(d:d/Name eq 'cloudCover' and d/LessThanOrEqual eq {float(cloud_max)})"
    params = {"$filter": filt, "$orderby": "ContentDate/Start desc"}
    value = _fetch_products(params, max_results, expand=False)
    products = []
    for p in value:
        norm = _normalize_product(p, with_metadata=False)
        m = re.search(r"_T(\d{2}[A-Z]{3})_", p["Name"])
        norm["tile"] = m.group(1) if m else None
        products.append(norm)
    products.sort(key=lambda p: (p["date"] or "", not p["cog"]), reverse=True)
    return products


def search_s1_area(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    product_kind: str = "GRD",
    max_results: int = 300,
) -> list[dict]:
    """Search Sentinel-1 GRD/SLC products intersecting a bbox (province mode)."""
    _ensure_env()
    if product_kind not in ("GRD", "SLC"):
        raise ValueError(f"Unknown product_kind: {product_kind}")
    name_filter = "_IW_GRDH_1" if product_kind == "GRD" else "_IW_SLC"
    filt = (
        "Collection/Name eq 'SENTINEL-1' and "
        f"contains(Name,'{name_filter}') and "
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.999Z and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{_bbox_wkt(bbox)}')"
    )
    params = {"$filter": filt, "$orderby": "ContentDate/Start desc"}
    expand = product_kind == "SLC"
    try:
        value = _fetch_products(params, max_results, expand=expand)
    except requests.HTTPError:
        if not expand:
            raise
        value = _fetch_products(params, max_results, expand=False)
    return [
        _normalize_product(p, with_metadata=(product_kind == "SLC")) for p in value
    ]


def search_s3_area(
    bbox: tuple[float, float, float, float],
    start_date: str,
    end_date: str,
    product_type: str = "OL_2_LFR",
    max_results: int = 200,
) -> list[dict]:
    """Search Sentinel-3 products intersecting a bbox (province mode)."""
    _ensure_env()
    if product_type not in _S3_PRODUCT_TYPES:
        raise ValueError(
            f"Unknown S3 product_type: {product_type}. "
            f"Chọn một trong {sorted(_S3_PRODUCT_TYPES)}"
        )
    filt = (
        "Collection/Name eq 'SENTINEL-3' and "
        f"contains(Name,'_{product_type}_') and "
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.999Z and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{_bbox_wkt(bbox)}')"
    )
    params = {"$filter": filt, "$orderby": "ContentDate/Start desc"}
    value = _fetch_products(params, max_results, expand=False)
    return [_normalize_product(p, with_metadata=False) for p in value]


def search_s3(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    product_type: str = "OL_2_LFR",
    max_results: int = 50,
) -> list[dict]:
    """Search Sentinel-3 products covering (lon, lat).

    product_type: 'OL_2_LFR' (Land reflectance, 300 m — thay thế closest
    cho phân tích đất), 'OL_2_WFR' (nước), 'SL_2_LST' (nhiệt độ bề mặt)...
    """
    _ensure_env()
    if product_type not in _S3_PRODUCT_TYPES:
        raise ValueError(
            f"Unknown S3 product_type: {product_type}. "
            f"Chọn một trong {sorted(_S3_PRODUCT_TYPES)}"
        )
    d = 0.05  # footprint S3 lớn (~1400 km swath) nên bbox thoải mái hơn
    west, east = lon - d, lon + d
    south, north = lat - d, lat + d
    polygon = (
        f"POLYGON(({west:.5f} {south:.5f}, {east:.5f} {south:.5f}, "
        f"{east:.5f} {north:.5f}, {west:.5f} {north:.5f}, "
        f"{west:.5f} {south:.5f}))"
    )
    filt = (
        "Collection/Name eq 'SENTINEL-3' and "
        f"contains(Name,'_{product_type}_') and "
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.999Z and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')"
    )
    params = {
        "$filter": filt,
        "$orderby": "ContentDate/Start desc",
    }
    value = _fetch_products(params, max_results, expand=False)
    return [_normalize_product(p, with_metadata=False) for p in value]


def _search_s1_products(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    product_kind: str,
    max_results: int = 50,
) -> list[dict]:
    """Query OData cho GRD/SLC covering (lon, lat), trả product OData thô."""
    _ensure_env()
    if product_kind not in ("GRD", "SLC"):
        raise ValueError(f"Unknown product_kind: {product_kind}")
    # Vùng quan tâm: bbox nhỏ quanh điểm (cảnh S1 rộng ~250 km nên luôn phủ)
    d = 0.02
    west, east = lon - d, lon + d
    south, north = lat - d, lat + d
    polygon = (
        f"POLYGON(({west:.5f} {south:.5f}, {east:.5f} {south:.5f}, "
        f"{east:.5f} {north:.5f}, {west:.5f} {north:.5f}, "
        f"{west:.5f} {south:.5f}))"
    )

    name_filter = "_IW_GRDH_1" if product_kind == "GRD" else "_IW_SLC"
    filt = (
        "Collection/Name eq 'SENTINEL-1' and "
        f"contains(Name,'{name_filter}') and "
        f"ContentDate/Start ge {start_date}T00:00:00.000Z and "
        f"ContentDate/Start le {end_date}T23:59:59.999Z and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')"
    )
    params = {
        "$filter": filt,
        "$orderby": "ContentDate/Start desc",
    }
    # GRD giữ đúng query cũ (không expand); SLC cần Attributes cho pair matching,
    # fallback bỏ expand nếu catalogue từ chối.
    expand = product_kind == "SLC"
    try:
        value = _fetch_products(params, max_results, expand=expand)
    except requests.HTTPError:
        if not expand:
            raise
        value = _fetch_products(params, max_results, expand=False)
    return value


def search_s1(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    max_results: int = 50,
) -> list[dict]:
    """Search Sentinel-1 IW GRDH products covering (lon, lat).

    Dates are inclusive YYYY-MM-DD. Returns newest-first list of dicts:
    {id, name, date, size_gb, online, cog}.
    """
    value = _search_s1_products(lon, lat, start_date, end_date, "GRD", max_results)
    return [_normalize_product(p, with_metadata=False) for p in value]


def search_s1_slc(
    lon: float,
    lat: float,
    start_date: str,
    end_date: str,
    max_results: int = 100,
) -> list[dict]:
    """Search Sentinel-1 IW SLC products covering (lon, lat) — InSAR-ready.

    Trả về newest-first, mỗi product có thêm metadata Attributes:
    timestamp, product_type, operational_mode, relative_orbit/track,
    orbit_direction/pass, polarization, slice_number, total_slices.
    Trường catalogue không trả về là None.
    """
    value = _search_s1_products(lon, lat, start_date, end_date, "SLC", max_results)
    return [_normalize_product(p, with_metadata=True) for p in value]


def _compatible_optional(a: dict, b: dict, field: str) -> bool:
    """Hai giá trị đều có thì phải bằng nhau; một bên None (thiếu metadata)
    thì vẫn tương thích — không coi None == None là 'chắc chắn cùng'."""
    va, vb = a.get(field), b.get(field)
    if va is None or vb is None:
        return True
    return str(va).upper() == str(vb).upper()


def build_pairs(
    products: list[dict],
    min_days: float = 6.0,
    max_days: float = 48.0,
    max_pairs: int = 20,
    track: int | None = None,
    orbit_direction: str | None = None,
) -> list[dict]:
    """Ghép cặp SLC cho InSAR từ danh sách product đã normalize.

    Điều kiện: cùng relative_orbit (track) và orbit_direction, cả hai phải CÓ
    metadata (None bị loại); mode/polarization nếu cả hai có thì phải giống.
    Master = thời điểm sớm hơn. Perpendicular baseline không có trong catalogue
    -> luôn None, không suy diễn.
    """
    eligible: list[tuple[dt.datetime, dict]] = []
    for p in products:
        ts = _parse_iso_utc(p.get("timestamp"))
        if ts is None or p.get("relative_orbit") is None or p.get("orbit_direction") is None:
            continue
        if track is not None and p["relative_orbit"] != track:
            continue
        if orbit_direction and str(p["orbit_direction"]).upper() != orbit_direction.upper():
            continue
        eligible.append((ts, p))

    groups: dict[tuple, list[tuple[dt.datetime, dict]]] = {}
    for ts, p in eligible:
        key = (p["relative_orbit"], str(p["orbit_direction"]).upper())
        groups.setdefault(key, []).append((ts, p))

    pairs: list[dict] = []
    for items in groups.values():
        items.sort(key=lambda x: x[0])
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                master_ts, master = items[i]
                secondary_ts, secondary = items[j]
                sep_days = (secondary_ts - master_ts).total_seconds() / 86400.0
                if sep_days < min_days or sep_days > max_days:
                    continue
                if not _compatible_optional(master, secondary, "operational_mode"):
                    continue
                if not _compatible_optional(master, secondary, "polarization"):
                    continue
                same_pol = (
                    master.get("polarization")
                    if master.get("polarization") == secondary.get("polarization")
                    else None
                )
                pairs.append(
                    {
                        "master": master,
                        "secondary": secondary,
                        "temporal_separation_days": round(sep_days, 3),
                        "relative_orbit": master["relative_orbit"],
                        "track": master["relative_orbit"],
                        "orbit_direction": master["orbit_direction"],
                        "polarization": same_pol,
                        "baseline": {
                            "perpendicular_m": None,
                            "available": False,
                            "reason": (
                                "CDSE catalogue response does not provide "
                                "perpendicular baseline"
                            ),
                        },
                        "interferogram_ready": False,
                    }
                )

    # Ưu tiên temporal separation nhỏ (mất tương quan chậm hơn)
    pairs.sort(key=lambda x: x["temporal_separation_days"])
    return pairs[:max_pairs]


def stream_product(product_id: str) -> Iterator[bytes]:
    """Stream the product ZIP from CDSE (yields chunks)."""
    token = get_token()
    url = _DOWNLOAD_URL.format(pid=product_id)
    with requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=(30, 600),
    ) as resp:
        resp.raise_for_status()
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                yield chunk


def open_product(product_id: str) -> tuple[requests.Response, bytes]:
    """Open the product ZIP and verify the first chunk actually arrives.

    Returns (response, first_chunk); caller must close the response.
    Use instead of stream_product() when you need to fail with a clear
    error *before* sending HTTP 200 to the client (avoids empty downloads).
    """
    token = get_token()
    url = _DOWNLOAD_URL.format(pid=product_id)
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=(30, 300),
    )
    if resp.status_code != 200:
        body = resp.text[:500]
        resp.close()
        if "File Size Block" in body or "Vingroup" in body:
            raise RuntimeError(
                "Proxy công ty chặn file > 300 MB (Vingroup Web Gateway). "
                "Hãy tải qua mạng khác (nhà/4G) hoặc liên hệ IT."
            )
        resp.raise_for_status()  # raises for other statuses
        raise RuntimeError(f"CDSE download failed: HTTP {resp.status_code}")
    try:
        first = next(resp.iter_content(chunk_size=1024 * 1024), b"")
    except Exception:
        resp.close()
        raise
    if not first:
        resp.close()
        raise RuntimeError("CDSE không trả dữ liệu (kết nối bị ngắt giữa chừng)")
    return resp, first


def iter_open_product(resp: requests.Response, first: bytes) -> Iterator[bytes]:
    """Continue yielding chunks from a response opened by open_product()."""
    try:
        if first:
            yield first
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                yield chunk
    finally:
        resp.close()


# ---------------------------------------------------------------------------
# Nodes API — tải TỪNG FILE trong product ZIP (dưới ngưỡng proxy 300 MB)
# ----------------------------------------------------------------------------

_NODES_URL = (
    "https://download.dataspace.copernicus.eu/odata/v1/Products({pid})"
)


def _list_nodes(product_id: str, path: list[str]) -> list[dict]:
    """Liệt kê node con theo đường dẫn bên trong product (mỗi tên URL-encoded).

    path=[] → .../Nodes (gốc product); path=[safe,'GRANULE'] →
    .../Nodes({safe})/Nodes(GRANULE)/Nodes, v.v.
    """
    token = get_token()
    url = _NODES_URL.format(pid=product_id)
    for name in path:
        url += f"/Nodes({urllib.parse.quote(name)})"
    url += "/Nodes"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=90)
    resp.raise_for_status()
    return resp.json().get("result", [])


def _node_value_url(product_id: str, path: list[str]) -> str:
    """URL tải nội dung 1 file node (kết thúc bằng /$value)."""
    token = get_token()  # noqa: F841 — đảm bảo creds sớm
    url = _NODES_URL.format(pid=product_id)
    for name in path:
        url += f"/Nodes({urllib.parse.quote(name)})"
    return url + "/$value"


def list_s2_bands(product_id: str) -> dict:
    """Liệt kê band JP2 của product S2 L2A kèm kích thước + đường dẫn node.

    Trả về {safe: str, granule: str, resolutions: {R10m: [{name, size_mb}], ...}}.
    """
    root = _list_nodes(product_id, [])
    safe = next((n["Name"] for n in root if n["Name"].endswith(".SAFE")), None)
    if not safe:
        raise RuntimeError("Không tìm thấy thư mục .SAFE trong product")

    granules = _list_nodes(product_id, [safe, "GRANULE"])
    if not granules:
        raise RuntimeError("Không tìm thấy GRANULE trong product")
    granule = granules[0]["Name"]

    img = _list_nodes(product_id, [safe, "GRANULE", granule, "IMG_DATA"])
    resolutions: dict[str, list[dict]] = {}
    for res in img:
        if res["Name"].startswith("R") and "m" in res["Name"]:
            bands = _list_nodes(
                product_id, [safe, "GRANULE", granule, "IMG_DATA", res["Name"]]
            )
            resolutions[res["Name"]] = [
                {
                    "name": b["Name"],
                    "size_mb": round((b.get("ContentLength") or 0) / 1e6, 1),
                }
                for b in bands
                if b["Name"].endswith(".jp2")
            ]
    return {"safe": safe, "granule": granule, "resolutions": resolutions}


def download_s2_band_file(
    product_id: str, resolution: str, band_filename: str, out_path: "Path"
) -> int:
    """Tải 1 file band JP2 từ product ZIP qua Nodes API (mỗi file ~100 MB).

    Trả về số byte đã ghi. File lọt dưới ngưỡng proxy 300 MB nên tải được
    qua gateway công ty (khác /$value là cả ZIP ~1.3 GB).
    """
    info = list_s2_bands(product_id)
    known = {
        b["name"]
        for bands in info["resolutions"].values()
        for b in bands
    }
    if band_filename not in known:
        raise ValueError(
            f"Band '{band_filename}' không có trong product (R10m/R20m/R60m)"
        )
    if resolution not in info["resolutions"]:
        raise ValueError(f"Resolution '{resolution}' không hợp lệ")

    url = _node_value_url(
        product_id,
        [info["safe"], "GRANULE", info["granule"], "IMG_DATA", resolution, band_filename],
    )
    token = get_token()
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        stream=True,
        timeout=(30, 600),
    )
    if resp.status_code != 200:
        body = resp.text[:300]
        resp.close()
        raise RuntimeError(f"CDSE node download failed: HTTP {resp.status_code} {body}")
    total = 0
    try:
        with open(out_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
    finally:
        resp.close()
    return total


# ---------------------------------------------------------------------------
# S3 (eodata) — tải product KHÔNG giới hạn dung lượng qua proxy công ty
# ----------------------------------------------------------------------------

_S3_ENDPOINT = "https://eodata.dataspace.copernicus.eu"
_S3_HOST = "eodata.dataspace.copernicus.eu"
_S3_KEYS_URL = "https://s3-keys-manager.cloudferro.com/api/user/credentials"
_S3_REGION = "default"
_S3_SERVICE = "s3"
_S3_BUCKET = "eodata"
_S3_CHUNK = 250 * 1024 * 1024  # mỗi chunk 250 MB — dưới ngưỡng proxy 300 MB


_S3_CREDS_CACHE: tuple[str, str] | None = None


class _S3AccessDenied(Exception):
    """S3 trả 403 — cached credentials có thể đã bị xoá bởi process khác."""


def _reset_s3_creds_cache() -> None:
    """Xoá cache credentials S3 (dùng khi key cũ bị process khác xoá)."""
    global _S3_CREDS_CACHE
    _S3_CREDS_CACHE = None


def get_s3_credentials(force_new: bool = False) -> tuple[str, str]:
    """Lấy credentials S3 (có cache) qua s3-keys-manager.

    Cache trong bộ nhớ process: mỗi request download không tạo key mới
    (tài khoản CDSE giới hạn ~10 key). Nếu POST bị 403 (đầy key) thì tự
    xoá TOÀN BỘ key cũ rồi thử lại.
    """
    global _S3_CREDS_CACHE
    if _S3_CREDS_CACHE and not force_new:
        return _S3_CREDS_CACHE

    token = get_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    resp = requests.post(_S3_KEYS_URL, headers=headers, timeout=60)
    if resp.status_code == 403:
        # Hết chỗ — xoá tất cả key cũ rồi tạo lại
        lst = requests.get(_S3_KEYS_URL, headers=headers, timeout=60)
        if lst.status_code == 200:
            for c in lst.json().get("credentials", []):
                try:
                    requests.delete(
                        f"{_S3_KEYS_URL}/access_id/{c['access_id']}",
                        headers=headers,
                        timeout=60,
                    )
                except Exception:
                    pass  # key nào xoá không được thì bỏ qua
        resp = requests.post(_S3_KEYS_URL, headers=headers, timeout=60)
    resp.raise_for_status()
    d = resp.json()
    _S3_CREDS_CACHE = (d["access_id"], d["secret"])
    return _S3_CREDS_CACHE


def get_product_s3_path(product_id: str) -> str:
    """Lấy S3Path của product từ OData (vd /eodata/Sentinel-1/SAR/...)."""
    url = f"{_CATALOG_URL}({product_id})"
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    path = resp.json().get("S3Path")
    if not path:
        raise RuntimeError("Product không có S3Path (có thể chưa sẵn trên object storage)")
    return path


def _sigv4_headers(
    access_key: str,
    secret: str,
    method: str,
    canonical_uri: str,
    payload_hash: str,
    extra_headers: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Ký AWS SigV4 bằng stdlib (hmac/hashlib) — không cần boto3."""
    amz_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date_stamp = amz_date[:8]

    headers = {"host": _S3_HOST, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    if extra_headers:
        headers.update(extra_headers)
    signed = sorted(headers.items())
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in signed)
    signed_headers = ";".join(k for k, _ in signed)

    canon_params = ""
    if params:
        canon_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))

    canonical_request = "\n".join([
        method, canonical_uri, canon_params, canonical_headers, signed_headers, payload_hash,
    ])
    scope = f"{date_stamp}/{_S3_REGION}/{_S3_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])
    k = hmac.new(("AWS4" + secret).encode(), date_stamp.encode(), hashlib.sha256).digest()
    k = hmac.new(k, _S3_REGION.encode(), hashlib.sha256).digest()
    k = hmac.new(k, _S3_SERVICE.encode(), hashlib.sha256).digest()
    k = hmac.new(k, b"aws4_request", hashlib.sha256).digest()
    signature = hmac.new(k, string_to_sign.encode(), hashlib.sha256).hexdigest()

    return {
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
        **(extra_headers or {}),
    }


def s3_list_product_files(product_id: str, creds: tuple[str, str] | None = None) -> list[dict]:
    """Liệt kê mọi object trong product .SAFE trên S3 (tên + dung lượng).

    Trả về [{key, size, rel}] — rel là đường dẫn tương đối trong .SAFE.
    Nếu S3 trả 403 (key bị process khác xoá do chạm giới hạn 10 key),
    tự tạo lại credentials rồi thử lại một lần.
    """
    global _S3_CREDS_CACHE
    if creds is None:
        creds = get_s3_credentials()
    for attempt in (1, 2):
        try:
            return _s3_list_product_files(product_id, creds)
        except requests.HTTPError as exc:
            if attempt == 1 and exc.response is not None \
                    and exc.response.status_code == 403:
                # Key đã bị xoá (vd: process khác dọn key khi đầy) → tạo mới
                _reset_s3_creds_cache()
                creds = get_s3_credentials(force_new=True)
                continue
            raise
    raise RuntimeError("unreachable")


def _s3_list_product_files(
    product_id: str, creds: tuple[str, str],
) -> list[dict]:
    access_key, secret = creds

    s3_path = get_product_s3_path(product_id)
    # S3Path: /eodata/Sentinel-1/SAR/.../XXX.SAFE
    parts = s3_path.lstrip("/").split("/", 1)
    if len(parts) != 2:
        raise RuntimeError(f"S3Path không hợp lệ: {s3_path}")
    prefix = parts[1].rstrip("/") + "/"

    files: list[dict] = []
    continuation = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "500"}
        if continuation:
            params["continuation-token"] = continuation
        enc = {k: (urllib.parse.quote(v, safe="") if k in ("prefix", "continuation-token") else v)
               for k, v in params.items()}
        payload_hash = hashlib.sha256(b"").hexdigest()
        headers = _sigv4_headers(access_key, secret, "GET", f"/{_S3_BUCKET}",
                                 payload_hash, params=enc)
        resp = requests.get(f"{_S3_ENDPOINT}/{_S3_BUCKET}", headers=headers,
                            params=params, timeout=(15, 120))
        resp.raise_for_status()
        ns = {"s": "http://s3.amazonaws.com/doc/2006-03-01/"}
        root = ET.fromstring(resp.text)
        for content in root.findall(".//s:Contents", ns):
            key = content.find("s:Key", ns).text
            size = int(content.find("s:Size", ns).text)
            files.append({
                "key": key,
                "size": size,
                "rel": key[len(prefix):],  # đường dẫn tương đối trong .SAFE
            })
        is_truncated = root.find("s:IsTruncated", ns)
        if is_truncated is not None and is_truncated.text == "true":
            ct = root.find("s:NextContinuationToken", ns)
            continuation = ct.text if ct is not None else None
            if not continuation:
                break
        else:
            break
    if not files:
        raise RuntimeError("Không tìm thấy object nào trong product trên S3")
    return files


def iter_s3_object(
    key: str,
    creds: tuple[str, str],
    size: int,
    chunk: int = _S3_CHUNK,
    max_attempts: int = 5,
) -> Iterator[bytes]:
    """Stream 1 object S3 theo từng chunk HTTP Range (mỗi chunk < 300 MB).

    Mỗi chunk là 1 request riêng → lọt ngưỡng proxy công ty. Yield các
    block 1 MB để ghép ZIP stream.

    Có retry cho lỗi tạm thời (SSL hostname mismatch do proxy, connection
    reset, timeout, HTTP 429/5xx): resume chính xác từ byte đã yield —
    không lặp lại dữ liệu đã ghi vào ZIP.
    """
    access_key, secret = creds
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_uri = f"/{_S3_BUCKET}/{urllib.parse.quote(key, safe='/')}"

    _transient = (
        requests.exceptions.ConnectionError,  # bao gồm cả SSLError
        requests.exceptions.Timeout,
        requests.exceptions.ChunkedEncodingError,
        _S3AccessDenied,  # 403: key có thể bị xoá bởi process khác
    )
    _backoffs = (3, 7, 15, 30, 60)

    offset = 0
    while offset < size:
        chunk_end = min(offset + chunk, size) - 1
        chunk_len = chunk_end - offset + 1
        emitted = 0
        attempt = 0
        while emitted < chunk_len:
            pos = offset + emitted
            try:
                # 403 giữa chừng = cached key bị xoá → nạp key mới rồi ký lại
                fresh = _S3_CREDS_CACHE
                if fresh is not None and (access_key, secret) != fresh:
                    access_key, secret = fresh
                headers = _sigv4_headers(
                    access_key, secret, "GET", canonical_uri, payload_hash,
                    extra_headers={"range": f"bytes={pos}-{chunk_end}"},
                )
                resp = requests.get(
                    f"{_S3_ENDPOINT}{canonical_uri}",
                    headers=headers,
                    stream=True,
                    timeout=(30, 600),
                )
                if resp.status_code not in (200, 206):
                    body = resp.text[:200]
                    code = resp.status_code
                    resp.close()
                    if code == 403:
                        # key không còn hợp lệ — process khác có thể vừa dọn
                        # key; raise inner riêng để refresh credentials
                        raise _S3AccessDenied(f"HTTP 403 {body}")
                    if code in (429, 500, 502, 503, 504):
                        # lỗi tạm thời phía S3/proxy → cho retry
                        raise requests.exceptions.ConnectionError(f"HTTP {code}")
                    raise RuntimeError(
                        f"S3 GET {key.rsplit('/', 1)[-1]} failed: "
                        f"HTTP {code} {body}"
                    )
                try:
                    for block in resp.iter_content(chunk_size=1024 * 1024):
                        if block:
                            yield block
                            emitted += len(block)
                finally:
                    resp.close()
                if emitted < chunk_len:
                    # connection bị cắt sớm mà không ném exception
                    raise requests.exceptions.ChunkedEncodingError(
                        f"chunk thiếu: {emitted}/{chunk_len} bytes"
                    )
            except _S3AccessDenied:
                # Key cũ bị xoá (process khác dọn key khi đầy) → tạo key mới
                # NGAY rồi thử lại, không ăn backoff dài
                _reset_s3_creds_cache()
                access_key, secret = get_s3_credentials(force_new=True)
                attempt += 1
                if attempt > max_attempts:
                    raise RuntimeError(
                        f"S3 GET {key.rsplit('/', 1)[-1]}: 403 persists "
                        f"sau {max_attempts} lần refresh credentials"
                    )
                print(
                    f"[s3-403] {key.rsplit('/', 1)[-1]}: credentials bị "
                    f"invalid → đã tạo key mới, thử lại (lần {attempt})",
                    flush=True,
                )
            except _transient as exc:
                attempt += 1
                if attempt > max_attempts:
                    raise RuntimeError(
                        f"S3 GET {key.rsplit('/', 1)[-1]} byte {pos}: "
                        f"thất bại sau {max_attempts} lần thử — {exc}"
                    ) from exc
                wait = _backoffs[min(attempt - 1, len(_backoffs) - 1)]
                print(
                    f"[s3-retry] {key.rsplit('/', 1)[-1]} byte {pos}: "
                    f"{type(exc).__name__}: {exc} → thử lại sau {wait}s "
                    f"(lần {attempt}/{max_attempts})",
                    flush=True,
                )
                time.sleep(wait)
        attempt = 0  # chunk hoàn tất → reset bộ đếm retry
        offset = chunk_end + 1
