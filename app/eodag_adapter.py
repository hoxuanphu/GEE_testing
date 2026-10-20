"""Lazy, provider-specific EODAG 4.9.2 catalogue search (no downloads)."""
from __future__ import annotations

import datetime as dt
import math
import os
import re
import time
from pathlib import Path

from .catalog_models import CatalogFailure, CatalogGateway, CatalogResult, CatalogUnavailable

COLLECTIONS = {'earth_search': 'S2_MSI_L2A_COG', 'planetary_computer': 'S2_MSI_L2A'}
NATIVE_COLLECTION = 'sentinel-2-l2a'
CAPABILITY = {'status': 'unsupported', 'reason': 'eodag_mvp_catalogue_only'}


class DeadlineExceeded(Exception):
    pass


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded()
    return min(30, remaining)


def configured_providers():
    from .ee_session import _load_dotenv
    _load_dotenv()
    return {name.strip() for name in os.getenv('EODAG_PROVIDERS', 'earth_search,planetary_computer').split(',')} & COLLECTIONS.keys()


def validate_selection(provider, collection=None):
    if provider not in configured_providers():
        raise ValueError('An allowlisted EODAG provider is required')
    if collection is not None and collection != COLLECTIONS[provider]:
        raise ValueError('Unsupported EODAG collection for provider')
    return COLLECTIONS[provider]


def _gateway(directory: Path, provider: str, deadline: float) -> CatalogGateway:
    try:
        from eodag import EODataAccessGateway
        from eodag.config import EODAGSettings
        from .ee_session import _load_extra_cas
        _load_extra_cas()

        class BoundedGateway(EODataAccessGateway):
            last_page = None

            def fetch_collections_list(self, *args, **kwargs):
                raise CatalogUnavailable('EODAG discovery is not supported in the MVP')

            def _fetch_external_collection(self, *args, **kwargs):
                raise CatalogUnavailable('EODAG discovery is not supported in the MVP')

            def _do_search(self, search_plugin, *args, **kwargs):
                search_plugin.config.timeout = _remaining(deadline)
                search_plugin.config.retry_total = 0
                search_plugin.config.ssl_verify = True
                sorting = getattr(search_plugin.config, 'sort', {})
                if 'start_datetime' not in sorting.get('sort_param_mapping', {}):
                    raise CatalogFailure('EODAG provider cannot guarantee newest-first ordering')
                result = super()._do_search(search_plugin, *args, **kwargs)
                self.last_page = result
                return result

        directory.mkdir(parents=True, exist_ok=True)
        config = os.getenv('EODAG_CONFIG_FILE') or None
        if config and not Path(config).is_file():
            raise CatalogUnavailable('EODAG configuration file is unavailable')
        settings = EODAGSettings(cfg_dir=directory, cfg_file=config,
                                 locations_cfg_file=directory / 'locations.yml',
                                 providers_whitelist=[provider])
        gateway = BoundedGateway(settings=settings)
        _remaining(deadline)
        return gateway
    except DeadlineExceeded:
        raise
    except Exception as exc:
        raise CatalogUnavailable('EODAG SDK or configuration is unavailable') from exc


def _text(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.upper().replace(' ', '_') in ('NOT_AVAILABLE', 'NOT_MAPPED', 'NONE', 'NULL', 'N/A'):
        return None
    if any(x in value.lower() for x in ('/', 'bearer ', '?', '&', '=', '\\')) or value.startswith('.'):
        return None
    return value


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _time(value):
    value = _text(value)
    if value is None:
        return None, None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return dt.date.fromisoformat(value).isoformat(), None
        stamp = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None, None
        stamp = stamp.astimezone(dt.timezone.utc)
        return stamp.date().isoformat(), stamp.isoformat().replace('+00:00', 'Z')
    except ValueError:
        return None, None


def _coordinates(value):
    if isinstance(value, (list, tuple)) and value:
        result = [_coordinates(v) for v in value]
        return result if all(v is not None for v in result) else None
    return _number(value)


def normalize(product, provider):
    if getattr(product, 'provider', None) != provider:
        raise CatalogFailure('EODAG returned a different provider')
    props = product.properties
    native_collection = _text(props.get('_collection'))
    if native_collection is not None and native_collection != NATIVE_COLLECTION:
        raise CatalogFailure('EODAG returned a different native collection')
    identifier = _text(props.get('id'))
    if not identifier:
        return None
    date = timestamp = None
    for key in ('start_datetime', 'startTimeFromAscendingNode', 'datetime'):
        date, timestamp = _time(props.get(key))
        if date is not None:
            break
    cloud = _number(props.get('eo:cloud_cover', props.get('cloudCover')))
    if cloud is not None and not 0 <= cloud <= 100:
        cloud = None
    geometry = getattr(product, 'geometry', None)
    geo = getattr(geometry, '__geo_interface__', geometry)
    footprint = None
    bbox = None
    if isinstance(geo, dict) and geo.get('type') in ('Polygon', 'MultiPolygon'):
        coordinates = _coordinates(geo.get('coordinates'))
        if coordinates:
            footprint = {'type': geo['type'], 'coordinates': coordinates}
            def points(coords):
                if isinstance(coords[0], (float, int)):
                    yield coords
                else:
                    for child in coords:
                        yield from points(child)
            try:
                vertices = list(points(coordinates))
            except (TypeError, IndexError):
                vertices = []
            if vertices and all(len(p) >= 2 and -180 <= p[0] <= 180 and -90 <= p[1] <= 90 for p in vertices):
                bbox = [min(p[0] for p in vertices), min(p[1] for p in vertices),
                        max(p[0] for p in vertices), max(p[1] for p in vertices)]
            else:
                footprint = None
    tile = _text(props.get('grid:code') or props.get('tileIdentifier') or props.get('s2:mgrs_tile'))
    if tile and tile.startswith('MGRS-'):
        tile = tile[5:]
    metadata = {}
    for key in ('s2:processing_baseline', 's2:product_uri', 'sat:relative_orbit',
                'sat:orbit_state', 'sar:polarizations'):
        value = props.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = _number(value)
        elif isinstance(value, list):
            value = [s for item in value if (s := _text(item)) is not None]
        else:
            value = _text(value)
        if value is not None:
            metadata[key] = value
    instruments = props.get('instruments') or props.get('instrument')
    if isinstance(instruments, list):
        instruments = ', '.join(s for item in instruments if (s := _text(item))) or None
    return dict(id=identifier, name=_text(props.get('title')), date=date, timestamp=timestamp,
                cloud_pct=cloud, tile=tile, platform=_text(props.get('platform')),
                instrument=_text(instruments),
                processing_level=_text(props.get('processing:level') or props.get('processingLevel')),
                product_type=_text(props.get('productType')), provider=provider,
                collection=NATIVE_COLLECTION, eodag_collection=COLLECTIONS[provider],
                bbox=bbox, footprint=footprint, source_metadata=metadata,
                asset_keys=[s for key in (getattr(product, 'assets', {}) or {}) if (s := _text(key))],
                quicklook_available=False, download_capabilities=dict(CAPABILITY))


def search(bounds, start_date, end_date, *, provider, eodag_collection=None,
           cloud_min=0, cloud_max=100, directory=None):
    collection = validate_selection(provider, eodag_collection)
    if os.getenv('EODAG_ENABLED', 'false').lower() != 'true':
        raise CatalogUnavailable('EODAG catalogue is disabled')
    try:
        cap = int(os.getenv('EODAG_MAX_RESULTS', '4000'))
        if not 1 <= cap <= 4000:
            raise ValueError()
    except ValueError as exc:
        raise CatalogUnavailable('EODAG_MAX_RESULTS must be between 1 and 4000') from exc
    deadline = time.monotonic() + 120
    start = dt.date.fromisoformat(start_date)
    end = dt.date.fromisoformat(end_date) + dt.timedelta(days=1)
    rows, seen, pages = [], set(), set()
    missing_id = missing_cloud = 0
    reason = None
    usable = False
    try:
        gateway = _gateway(directory or Path(__file__).resolve().parent.parent / 'downloads' / 'eodag', provider, deadline)
        _remaining(deadline)
        current = gateway.search(collection=collection, provider=provider, raise_errors=True, validate=False,
                                 start=f'{start.isoformat()}T00:00:00Z', end=f'{end.isoformat()}T00:00:00Z',
                                 geom=dict(zip(('lonmin', 'latmin', 'lonmax', 'latmax'), bounds)),
                                 limit=100, sort_by=[('start_datetime', 'DESC')])
        for page_index in range(200):
            _remaining(deadline)
            fingerprint = tuple(getattr(p, 'properties', {}).get('id') for p in current)
            if fingerprint and fingerprint in pages:
                reason = 'repeated_page'
                break
            pages.add(fingerprint)
            for product in current:
                row = normalize(product, provider)
                if row is None:
                    missing_id += 1
                    continue
                identity = (provider, row['collection'], row['id'])
                if identity in seen:
                    continue
                seen.add(identity)
                if row['cloud_pct'] is None:
                    missing_cloud += 1
                    continue
                if not cloud_min <= row['cloud_pct'] <= cloud_max:
                    continue
                if row['date'] and not start.isoformat() <= row['date'] < end.isoformat():
                    continue
                rows.append(row)
                if len(rows) == cap:
                    reason = 'result_cap'
                    break
            usable = True
            if reason:
                break
            if current.next_page_token is None:
                break
            if page_index == 199:
                reason = 'page_budget'
                break
            _remaining(deadline)
            # Consume one transition only: the SDK iterator otherwise owns an unbounded loop.
            iterator = current.next_page(update=False)
            following = next(iterator, None)
            _remaining(deadline)
            if following is None:
                last = getattr(gateway, 'last_page', None)
                if last:
                    reason = 'repeated_page'
                break
            current = following
    except CatalogUnavailable:
        raise
    except Exception as exc:
        if not usable:
            if isinstance(exc, CatalogFailure):
                raise
            raise CatalogFailure('EODAG provider search failed') from exc
        reason = 'time_budget' if isinstance(exc, DeadlineExceeded) else 'provider_error'
    rows.sort(key=lambda row: (row['timestamp'] or row['date'] or '', row['id']), reverse=True)
    warnings = []
    if reason:
        warnings.append(f'EODAG search is partial: {reason}')
    if missing_id:
        warnings.append(f'Skipped {missing_id} products without native item IDs')
    if missing_cloud:
        warnings.append(f'Excluded {missing_cloud} products with missing cloud metadata')
    return CatalogResult(rows, dict(provider_limit=cap, truncated=bool(reason),
        completion='partial' if reason else 'complete', truncation_reason=reason,
        warnings=warnings, missing_id_count=missing_id, missing_cloud_count=missing_cloud,
        eodag_collection=collection))
