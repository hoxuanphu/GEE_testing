"""Metadata-only scene enumeration without cloud thresholds or silent page caps."""
from __future__ import annotations

import datetime as dt
import math
import time
from urllib.parse import urljoin, urlsplit

import ee
import requests

from . import aws_stac, cdse, imagery
from .platforms import apply_platform_filter, validate_platform

MAX_SCENES = 20_000
MAX_PAGES = 200
QUERY_SECONDS = 120


class InventoryIncomplete(ValueError):
    """The provider did not supply a complete, bounded inventory."""


def _wkt(geometry):
    """Serialize trusted WGS84 polygon rings, retaining holes and multipolygons."""
    def polygon(rings):
        return '(' + ','.join('(' + ','.join(f'{float(p[0])} {float(p[1])}' for p in ring) + ')'
                              for ring in rings) + ')'
    if geometry['type'] == 'Polygon':
        return 'POLYGON' + polygon(geometry['coordinates'])
    if geometry['type'] == 'MultiPolygon':
        return 'MULTIPOLYGON(' + ','.join(polygon(p) for p in geometry['coordinates']) + ')'
    raise ValueError('ROI must be a polygon or multipolygon')


def _timeout(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise InventoryIncomplete('Hết thời gian lấy danh sách đầy đủ; hãy thu hẹp khoảng ngày.')
    return min(45, remaining)


def _next_url(href, current, origin):
    url = urljoin(current, href)
    parsed, expected = urlsplit(url), urlsplit(origin)
    if (parsed.scheme != 'https' or parsed.netloc != expected.netloc or
            parsed.path.rstrip('/') != expected.path.rstrip('/') or parsed.fragment):
        raise InventoryIncomplete('Nguồn trả về liên kết phân trang không hợp lệ.')
    return url


def _row(asset_id, timestamp, name=None, cloud=None):
    if not isinstance(asset_id, str) or not asset_id or not isinstance(timestamp, str):
        raise InventoryIncomplete('Nguồn trả về cảnh thiếu mã hoặc thời gian chụp.')
    acquired = dt.datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    if acquired.tzinfo is None:
        raise InventoryIncomplete('Nguồn trả về thời gian chụp không có múi giờ.')
    acquired = acquired.astimezone(dt.timezone.utc)
    try:
        cloud = float(cloud) if cloud is not None else None
    except (ValueError, TypeError):
        cloud = None
    if cloud is not None and (not math.isfinite(cloud) or not 0 <= cloud <= 100):
        cloud = None
    return dict(asset_id=asset_id, name=name or asset_id.rsplit('/', 1)[-1],
                timestamp=acquired.isoformat().replace('+00:00', 'Z'),
                date=acquired.date().isoformat(), cloud_pct=cloud)


def _compute_gee(expression, deadline):
    # Reuse the existing isolated REST transport: no process-wide EE deadline changes.
    from .frame_search import _RPC
    with _RPC(min(45, _timeout(deadline)), deadline=deadline) as rpc:
        return rpc.compute(expression)


def _gee(dataset, geometry, start, end, platform, polarization, product, orbit_pass, relative_orbit):
    from . import main
    deadline = time.monotonic() + QUERY_SECONDS
    main._ensure_ee()
    validate_platform(dataset, platform)
    exclusive = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
    if dataset == 's5p':
        from .s5p import product_metadata
        collection_ids = [product_metadata(product)['collection']]
    else:
        collection_ids = {'sentinel2': [imagery.S2_COLLECTION],
                          'landsat': [imagery.L8_COLLECTION, imagery.L9_COLLECTION],
                          's1gee': [imagery.S1_COLLECTION], 'nicfi': [imagery.NICFI_COLLECTION],
                          'dynamic_world': [imagery.DYNAMIC_WORLD_COLLECTION]}[dataset]
    roi = ee.Geometry(geometry, geodesic=False)
    collections = []
    for collection_id in collection_ids:
        native = ee.ImageCollection(collection_id).filterDate(start, exclusive).filterBounds(roi)
        native = apply_platform_filter(native, dataset, platform)
        if dataset == 's1gee':
            native = native.filter(ee.Filter.eq('instrumentMode', 'IW'))
            for band in {'vv': ['VV'], 'vh': ['VH'], 'vvvh': ['VV', 'VH'],
                         'hh': ['HH'], 'hv': ['HV'], 'hhhv': ['HH', 'HV']}[polarization]:
                native = native.filter(ee.Filter.listContains('transmitterReceiverPolarisation', band))
            if orbit_pass is not None:
                native = native.filter(ee.Filter.eq('orbitProperties_pass', orbit_pass))
            if relative_orbit is not None:
                native = native.filter(ee.Filter.eq('relativeOrbitNumber_start', relative_orbit))

        def tag(image):
            # Image.id() may be relative and merge() rewrites indexes. Tag the full ID first.
            return image.set('inventory:asset_id', ee.String(collection_id + '/').cat(
                ee.String(image.get('system:index'))))

        collections.append(native.map(tag))
    collection = collections[0]
    for following in collections[1:]:
        collection = collection.merge(following)
    cloud_property = {'sentinel2': 'CLOUDY_PIXEL_PERCENTAGE', 'landsat': 'CLOUD_COVER'}.get(dataset)

    def metadata(value):
        image = ee.Image(value)
        return ee.Dictionary({
            'asset_id': image.get('inventory:asset_id'),
            'timestamp_ms': image.get('system:time_start'),
            'cloud_pct': image.get(cloud_property) if cloud_property else None,
        })

    # Limit the metadata response, never turn a truncated set into a reported total.
    values = _compute_gee(collection.toList(MAX_SCENES + 1).map(metadata), deadline)
    if len(values) > MAX_SCENES:
        raise InventoryIncomplete(f'Có hơn {MAX_SCENES:,} cảnh; hãy thu hẹp khoảng ngày để lấy đầy đủ danh sách.')
    return [_row(item['asset_id'], dt.datetime.fromtimestamp(
        item['timestamp_ms'] / 1000, tz=dt.timezone.utc).isoformat(), cloud=item.get('cloud_pct')) for item in values]


def _cdse(dataset, geometry, start, end, product_type):
    cdse._ensure_env()
    selections = {
        's2': ("Collection/Name eq 'SENTINEL-2'", "contains(Name,'MSIL2A')"),
        's1': ("Collection/Name eq 'SENTINEL-1'", "contains(Name,'_IW_GRDH_1')"),
        's1slc': ("Collection/Name eq 'SENTINEL-1'", "contains(Name,'_IW_SLC')"),
        's3': ("Collection/Name eq 'SENTINEL-3'", f"contains(Name,'_{product_type}_')"),
    }
    exclusive = (dt.date.fromisoformat(end) + dt.timedelta(days=1)).isoformat()
    polygon = _wkt(geometry)
    filt = ' and '.join((*selections[dataset],
        f'ContentDate/Start ge {start}T00:00:00.000Z',
        f'ContentDate/Start lt {exclusive}T00:00:00.000Z',
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon}')"))
    params = {'$filter': filt, '$orderby': 'ContentDate/Start asc,Id asc', '$top': 1000,
              '$expand': 'Attributes', '$count': 'true'}
    url, seen_pages, rows = cdse._CATALOG_URL, set(), {}
    expected, deadline = None, time.monotonic() + QUERY_SECONDS
    for _ in range(MAX_PAGES):
        page_key = (url, str(params))
        if page_key in seen_pages:
            raise InventoryIncomplete('Nguồn lặp trang; chưa lấy được danh sách đầy đủ.')
        seen_pages.add(page_key)
        response = requests.get(url, params=params, timeout=_timeout(deadline))
        response.raise_for_status()
        payload = response.json()
        _timeout(deadline)
        if '@odata.count' in payload:
            expected = int(payload['@odata.count'])
        previous_count = len(rows)
        for item in payload['value']:
            cloud = next((a.get('Value') for a in item.get('Attributes', []) if a.get('Name') == 'cloudCover'), None)
            row = _row(item['Id'], item.get('ContentDate', {}).get('Start'), item.get('Name'), cloud)
            rows.setdefault(row['asset_id'], row)
        if payload['value'] and len(rows) == previous_count:
            raise InventoryIncomplete('Nguồn lặp nội dung trang; chưa lấy được danh sách đầy đủ.')
        if len(rows) > MAX_SCENES or expected is not None and expected > MAX_SCENES:
            raise InventoryIncomplete(f'Danh sách vượt {MAX_SCENES:,} cảnh; hãy thu hẹp khoảng ngày.')
        following = payload.get('@odata.nextLink')
        if not following:
            if expected is not None and expected != len(rows):
                raise InventoryIncomplete('Catalogue trả thiếu cảnh; hãy thu hẹp khoảng ngày và đếm lại.')
            return list(rows.values())
        url, params = _next_url(following, url, cdse._CATALOG_URL), None
    raise InventoryIncomplete('Quá nhiều trang catalogue; hãy thu hẹp khoảng ngày.')


def _aws(geometry, start, end):
    aws_stac._load_extra_cas()
    original = {'collections': [aws_stac._COLLECTION], 'intersects': geometry,
                'datetime': f'{start}T00:00:00Z/{end}T23:59:59.999999Z', 'limit': 1000}
    body, url, method = original, aws_stac._STAC_URL, 'POST'
    deadline, seen_pages, rows, expected = time.monotonic() + QUERY_SECONDS, set(), {}, None
    for _ in range(MAX_PAGES):
        page_key = (method, url, str(body))
        if page_key in seen_pages:
            raise InventoryIncomplete('Nguồn lặp trang; chưa lấy được danh sách đầy đủ.')
        seen_pages.add(page_key)
        response = (requests.post(url, json=body, timeout=_timeout(deadline)) if method == 'POST'
                    else requests.get(url, timeout=_timeout(deadline)))
        response.raise_for_status()
        payload = response.json()
        _timeout(deadline)
        matched = payload.get('numberMatched', payload.get('context', {}).get('matched'))
        if matched is not None:
            expected = int(matched)
        previous_count = len(rows)
        for item in payload['features']:
            properties = item['properties']
            row = _row(item['id'], properties.get('datetime'), cloud=properties.get('eo:cloud_cover'))
            rows.setdefault(row['asset_id'], row)
        if payload['features'] and len(rows) == previous_count:
            raise InventoryIncomplete('Nguồn lặp nội dung trang; chưa lấy được danh sách đầy đủ.')
        if len(rows) > MAX_SCENES or expected is not None and expected > MAX_SCENES:
            raise InventoryIncomplete(f'Danh sách vượt {MAX_SCENES:,} cảnh; hãy thu hẹp khoảng ngày.')
        following = next((link for link in payload.get('links', []) if link.get('rel') == 'next'), None)
        if following is None:
            if expected is not None and expected != len(rows):
                raise InventoryIncomplete('STAC trả thiếu cảnh; hãy thu hẹp khoảng ngày và đếm lại.')
            return list(rows.values())
        url = _next_url(following['href'], url, aws_stac._STAC_URL)
        method = following.get('method', 'GET').upper()
        if method not in ('GET', 'POST'):
            raise InventoryIncomplete('Phương thức phân trang không được hỗ trợ.')
        body = {**original, **following.get('body', {})} if method == 'POST' else None
    raise InventoryIncomplete('Quá nhiều trang STAC; hãy thu hẹp khoảng ngày.')


def list_scenes(*, dataset, source, geometry, start, end, platform=None,
                polarization='vvvh', product='NO2', product_type='OL_2_LFR',
                orbit_pass=None, relative_orbit=None):
    if source == 'gee':
        rows = _gee(dataset, geometry, start, end, platform, polarization, product, orbit_pass, relative_orbit)
    elif source == 'aws' and dataset in ('s2', 's2aws'):
        rows = _aws(geometry, start, end)
    elif source == 'cdse' and dataset in ('s2', 's1', 's1slc', 's3'):
        rows = _cdse(dataset, geometry, start, end, product_type)
    else:
        raise ValueError('Nguồn này chưa hỗ trợ đếm ảnh.')
    unique = {row['asset_id']: row for row in rows}
    return sorted(unique.values(), key=lambda row: (row['timestamp'], row['asset_id']))
