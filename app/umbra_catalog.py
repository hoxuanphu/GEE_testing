"""Local search index for the public Umbra SAR bucket."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import logging
import math
from pathlib import Path, PurePosixPath
from queue import SimpleQueue
import re
import sqlite3
from threading import Event, Lock, Thread
from time import monotonic, sleep
from urllib.parse import quote, unquote_plus
from uuid import uuid4

from defusedxml.ElementTree import fromstring
import requests
from shapely.affinity import translate
from shapely.errors import ShapelyError
from shapely.geometry import Polygon, box, mapping, shape
from shapely.ops import unary_union
from shapely.prepared import prep


BUCKET_URL = 'https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog'
PREFIXES = ('open-data/', 'sar-data/tasks/', 'sar-data/task-data/')
ATTRIBUTION = 'Umbra Lab, Inc. · Umbra Open Data · CC BY 4.0'
LICENSE_URL = 'https://creativecommons.org/licenses/by/4.0/'
GEC_NAME = re.compile(r'(?P<stem>.+)_GEC(?:[_-][a-z0-9][a-z0-9_-]*)?\.tiff?$', re.I)
PREVIEW_NAME = re.compile(r'(?P<stem>.+?)_(?:GEC_)?(?:preview|thumbnail|quicklook)\.(?:png|jpe?g)$', re.I)
SIDECAR_SUFFIXES = ('.stac.v2.json', '_metadata.json')
REFRESH_WORKERS = 12
log = logging.getLogger(__name__)


class CatalogNotReady(Exception):
    pass


class RemoteError(OSError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Umbra returned HTTP {status}')


@dataclass(frozen=True)
class Object:
    path: str
    size: int
    etag: str


def object_url(path: str) -> str:
    if (not path.startswith(PREFIXES) or '\\' in path or
            any(part in ('', '.', '..') for part in path.split('/'))):
        raise ValueError('Invalid Umbra object path')
    return BUCKET_URL + '/' + quote(path, safe='/')


def small_get(session, url, *, params=None, headers=None, max_bytes=2 * 1024**2):
    """Bound metadata responses and retry temporary failures only."""
    deadline = monotonic() + 45
    for attempt in range(3):
        try:
            with session.get(url, params=params, headers=headers, timeout=(5, 10),
                             stream=True, allow_redirects=False) as response:
                if response.status_code != 200:
                    raise RemoteError(response.status_code)
                body = bytearray()
                for block in response.iter_content(65536):
                    body.extend(block)
                    if len(body) > max_bytes or monotonic() > deadline:
                        raise ValueError('Umbra metadata response exceeds budget')
                return bytes(body)
        except (requests.ConnectionError, requests.Timeout, RemoteError) as exc:
            if attempt == 2 or monotonic() >= deadline or (
                isinstance(exc, RemoteError) and exc.status not in (429, 500, 502, 503, 504)
            ):
                raise
            sleep(0.25 * 2**attempt)


def list_objects(session, prefix):
    params = {'list-type': '2', 'prefix': prefix, 'encoding-type': 'url', 'max-keys': 1000}
    seen = set()
    for _ in range(500):
        root = fromstring(small_get(session, BUCKET_URL + '/', params=dict(params)))
        if root.tag.rsplit('}', 1)[-1] != 'ListBucketResult':
            raise ValueError('Invalid Umbra listing')
        for item in root.findall('{*}Contents'):
            path = unquote_plus(item.findtext('{*}Key', ''))
            if path.startswith(prefix) and not path.endswith('/'):
                object_url(path)
                yield Object(path, int(item.findtext('{*}Size')), item.findtext('{*}ETag', ''))
        truncated = root.findtext('{*}IsTruncated')
        if truncated == 'false':
            return
        token = root.findtext('{*}NextContinuationToken')
        if truncated != 'true' or not token or token in seen:
            raise ValueError('Invalid Umbra continuation token')
        seen.add(token)
        params['continuation-token'] = token
    raise ValueError('Umbra listing exceeded page budget')


def get_sidecar(obj, *, session=None):
    if session is None:
        with requests.Session() as local:
            return get_sidecar(obj, session=local)
    return json.loads(small_get(session, object_url(obj.path), headers={'If-Match': obj.etag}))


def _unwrap(ring, anchor=None):
    points = []
    for lon, lat, *_ in ring:
        if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError('Invalid geographic coordinate')
        if anchor is not None:
            lon += 360 * round((anchor - lon) / 360)
        points.append((lon, lat))
        anchor = lon
    return points


def normalize_geometry(document):
    """Split rings crossing the antimeridian into polygons in [-180, 180]."""
    geom = shape(document)
    if geom.geom_type not in ('Polygon', 'MultiPolygon') or geom.is_empty:
        raise ValueError('Umbra requires a polygon geometry')
    normalized = []
    for polygon in geom.geoms if geom.geom_type == 'MultiPolygon' else (geom,):
        exterior = _unwrap(polygon.exterior.coords)
        unwrapped = Polygon(exterior, [_unwrap(hole.coords, exterior[0][0]) for hole in polygon.interiors])
        if not unwrapped.is_valid or unwrapped.area <= 0:
            raise ValueError('Invalid Umbra footprint or ROI')
        west, _, east, _ = unwrapped.bounds
        for shift in range(math.floor((west + 180) / 360), math.floor((east + 180) / 360) + 1):
            offset = 360 * shift
            piece = unwrapped.intersection(box(offset - 180, -90, offset + 180, 90))
            pieces = piece.geoms if hasattr(piece, 'geoms') else (piece,)
            normalized.extend(translate(part, xoff=-offset) for part in pieces
                              if part.geom_type == 'Polygon' and part.area > 0)
    result = unary_union(normalized)
    if result.is_empty or result.geom_type not in ('Polygon', 'MultiPolygon'):
        raise ValueError('No polygon area')
    return result


def _resolution(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) and value > 0 else None


def normalize_metadata(doc):
    if doc.get('type') == 'Feature':
        p = doc['properties']
        geometry = doc['geometry']
        captured = p.get('datetime') or p['start_datetime']
        platform, polarization = p.get('platform'), p.get('sar:polarizations')
        collect, item = p.get('umbra:collect_id'), doc.get('id')
        azimuth, ground_range = p.get('sar:resolution_azimuth'), p.get('sar:resolution_range')
    else:
        collects = doc['collects']
        if len(collects) != 1:
            raise ValueError('Ambiguous legacy collection footprint')
        p = collects[0]
        geometry = p['footprintPolygonLla']
        captured = p['startAtUTC']
        platform, polarization = doc.get('umbraSatelliteName'), p.get('polarizations')
        collect, item = p.get('id'), None
        resolution = p.get('maxGroundResolution') or {}
        azimuth, ground_range = resolution.get('azimuthMeters'), resolution.get('rangeMeters')
    t = datetime.fromisoformat(captured)
    if t.tzinfo is None:
        raise ValueError('Umbra timestamp must include a timezone')
    if isinstance(polarization, str):
        polarization = re.findall(r'HH|HV|VH|VV', polarization.upper())
    footprint = normalize_geometry(geometry)
    return {
        'datetime': t.astimezone(timezone.utc).isoformat(timespec='microseconds'),
        'footprint': mapping(footprint),
        'bbox': list(footprint.bounds),
        'platform': platform if isinstance(platform, str) else None,
        'polarizations': [p for p in polarization if p in ('HH', 'HV', 'VH', 'VV')]
        if isinstance(polarization, list) else [],
        'collect_id': collect if isinstance(collect, str) else None,
        'item_id': item if isinstance(item, str) else None,
        'resolution_azimuth_m': _resolution(azimuth),
        'resolution_range_m': _resolution(ground_range),
    }


def _sidecar_rank(sidecar, stem):
    path = PurePosixPath(sidecar.path).name.lower()
    modern = path.endswith('.stac.v2.json')
    source_stem = path.removesuffix('.stac.v2.json') if modern else path.removesuffix('_metadata.json')
    return (source_stem != stem, not modern)


class Catalog:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / 'catalog.sqlite3'
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._running = False
        self._progress = {}
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS version (data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS refresh_failure (failed INTEGER PRIMARY KEY CHECK (failed = 1));
                CREATE TABLE IF NOT EXISTS metadata (path TEXT PRIMARY KEY, etag TEXT, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS scenes (
                    id TEXT PRIMARY KEY, captured TEXT NOT NULL,
                    west REAL, south REAL, east REAL, north REAL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS scenes_by_date ON scenes(captured DESC, id);
            """)

    def _connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _version(db):
        row = db.execute('SELECT data FROM version').fetchone()
        return json.loads(row[0]) if row else None

    def status(self):
        with closing(self._connect()) as db:
            version = self._version(db)
            failed = db.execute('SELECT 1 FROM refresh_failure').fetchone() is not None
        with self._lock:
            state = ('refreshing' if self._running else ('stale' if version else 'failed') if failed
                     else 'ready' if version else 'empty')
            progress = self._progress.copy() if self._running else {}
        return {'state': state, 'prefixes': list(PREFIXES), **(version or {}), **progress}

    def start(self):
        with self._lock:
            if not self._running:
                self._stop.clear()
                self._running = True
                self._progress = {'stage': 'listing'}
                self._thread = Thread(target=self._run_refresh, name='umbra-index', daemon=True)
                self._thread.start()
        return self.status()

    def shutdown(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)

    def refresh(self):
        with self._lock:
            if self._running:
                return False
            self._stop.clear()
            self._running = True
            self._progress = {'stage': 'listing'}
        return self._run_refresh()

    def _check(self):
        if self._stop.is_set():
            raise InterruptedError('Umbra refresh stopped')

    def _run_refresh(self):
        try:
            self._build()
            return True
        except Exception:
            if not self._stop.is_set():
                log.exception('Umbra catalogue refresh failed')
                try:
                    with closing(self._connect()) as db, db:
                        db.execute('INSERT OR IGNORE INTO refresh_failure VALUES (1)')
                except sqlite3.Error:
                    log.exception('Could not record Umbra refresh failure')
            return False
        finally:
            with self._lock:
                self._running = False

    def _scan(self, prefix):
        groups = defaultdict(list)
        with requests.Session() as session:
            for obj in list_objects(session, prefix):
                self._check()
                name = PurePosixPath(obj.path).name
                if obj.size > 0 and obj.etag and (
                    GEC_NAME.fullmatch(name) or name.lower().endswith(SIDECAR_SUFFIXES)
                    or (obj.size <= 512 * 1024 and PREVIEW_NAME.fullmatch(name))
                ):
                    groups[obj.path.rsplit('/', 1)[0]].append(obj)
        return groups

    def _build(self):
        groups = {}
        with ThreadPoolExecutor(max_workers=len(PREFIXES)) as executor:
            for listed in executor.map(self._scan, PREFIXES):
                groups.update(listed)
        with closing(self._connect()) as db:
            cached = {path: (etag, data) for path, etag, data in db.execute('SELECT * FROM metadata')}

        scenes, resolved = [], {}
        skipped = 0
        duplicates = set()
        work = [objects for _, objects in sorted(groups.items())]
        sessions = SimpleQueue()

        def resolve(group):
            session = sessions.get()
            try:
                return self._group(group, cached, session)
            finally:
                sessions.put(session)

        with self._lock:
            self._progress = {'stage': 'metadata', 'processed': 0, 'total_groups': len(work)}
        # Submit a bounded number of acquisitions so cancelling does not drain a large queue.
        with ExitStack() as stack:
            for _ in range(REFRESH_WORKERS):
                sessions.put(stack.enter_context(requests.Session()))
            executor = stack.enter_context(ThreadPoolExecutor(max_workers=REFRESH_WORKERS))
            for start in range(0, len(work), 48):
                self._check()
                batch = work[start:start + 48]
                for found, metadata, missing in executor.map(resolve, batch):
                    skipped += missing
                    resolved.update(metadata)
                    for scene in found:
                        signature = (scene['item_id'] or scene['collect_id'], scene['datetime'],
                                     scene['filename'].lower(), scene['size_bytes'], scene['etag'])
                        if signature[0] is not None:
                            if signature in duplicates:
                                continue
                            duplicates.add(signature)
                        scenes.append(scene)
                with self._lock:
                    self._progress['processed'] += len(batch)
        if skipped and not scenes:
            raise ValueError('No usable metadata in the Umbra catalogue')
        version = {
            'generation': uuid4().hex,
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'count': len(scenes),
            'skipped': skipped,
        }
        self._check()
        with closing(self._connect()) as db, db:
            db.execute('DELETE FROM scenes')
            db.execute('DELETE FROM metadata')
            db.execute('DELETE FROM version')
            db.execute('DELETE FROM refresh_failure')
            db.executemany('INSERT INTO metadata VALUES (?, ?, ?)',
                           ((path, etag, data) for path, (etag, data) in resolved.items()))
            db.executemany('INSERT INTO scenes VALUES (?, ?, ?, ?, ?, ?, ?)',
                           ((s['id'], s['datetime'], *s['bbox'], json.dumps(s)) for s in scenes))
            db.execute('INSERT INTO version VALUES (?)', (json.dumps(version),))

    def _group(self, objects, cached, session):
        found, metadata = [], {}
        invalid = set()
        assets, previews, sidecars = [], {}, []
        for obj in sorted(objects, key=lambda o: o.path):
            name = PurePosixPath(obj.path).name
            if match := GEC_NAME.fullmatch(name):
                assets.append((match['stem'].lower(), obj))
            elif match := PREVIEW_NAME.fullmatch(name):
                previews.setdefault(match['stem'].lower(), obj)
            else:
                sidecars.append(obj)
        single_acquisition = len({stem for stem, _ in assets}) == 1
        for stem, asset in assets:
            self._check()
            filename = PurePosixPath(asset.path).name
            candidates = sorted(sidecars, key=lambda o: (_sidecar_rank(o, stem), o.path))
            for sidecar in candidates:
                rank = _sidecar_rank(sidecar, stem)
                if rank[0] and not (len(candidates) == 1 and single_acquisition):
                    break
                if sidecar.path in invalid:
                    continue
                old_etag, raw = metadata.get(sidecar.path, cached.get(sidecar.path, (None, None)))
                try:
                    if old_etag != sidecar.etag:
                        raw = json.dumps(get_sidecar(sidecar, session=session), allow_nan=False)
                    parsed = normalize_metadata(json.loads(raw))
                except RemoteError as exc:
                    if exc.status not in (403, 404, 412):
                        raise
                    invalid.add(sidecar.path)
                    continue
                except (KeyError, TypeError, ValueError, AttributeError, ShapelyError):
                    invalid.add(sidecar.path)
                    continue
                metadata[sidecar.path] = (sidecar.etag, raw)
                row = dict(parsed, id=sha256(asset.path.encode()).hexdigest(),
                           object_path=asset.path, filename=filename,
                           size_bytes=asset.size, etag=asset.etag,
                           metadata_path=sidecar.path, metadata_etag=sidecar.etag)
                if preview := previews.get(stem):
                    row.update(preview_path=preview.path, preview_etag=preview.etag)
                found.append(row)
                break
        return found, metadata, len(assets) - len(found)

    def metadata(self, path, etag):
        with closing(self._connect()) as db:
            row = db.execute('SELECT data FROM metadata WHERE path=? AND etag=?', (path, etag)).fetchone()
        return json.loads(row[0]) if row else None

    def search(self, geometry, start: str, stop: str):
        roi = normalize_geometry(geometry)
        west, south, east, north = roi.bounds
        prepared = prep(roi)
        with closing(self._connect()) as db:
            db.execute('BEGIN')
            version = self._version(db)
            if version is None:
                raise CatalogNotReady
            rows = db.execute(
                'SELECT data FROM scenes WHERE captured >= ? AND captured < ? '
                'AND east >= ? AND north >= ? AND west <= ? AND south <= ? '
                'ORDER BY captured DESC, id', (start, stop, west, south, east, north))
            scenes = []
            for data, in rows:
                scene = json.loads(data)
                if prepared.intersects(shape(scene['footprint'])):
                    scenes.append(scene)
        return version, scenes
