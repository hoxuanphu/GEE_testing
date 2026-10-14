"""Bounded, resumable Esri World Imagery downloads (no Earth Engine initialization).

Limits: 20,000 requested tiles, 500 million bounding-raster pixels, 8 workers.
Disk reservations conservatively allow 5 bytes/output pixel + 2 MB/requested tile,
with 256 MiB free-space reserve, 6 GiB/download and 12 GiB/service storage caps.
These additional budgets may reject requests below the tile/pixel ceilings.
No automatic deletion: operators explicitly remove old owned job/cache folders.
Masks use pixel centres, including polygon holes; missing imagery is invalid.
Zoom controls sampling, not native imagery resolution. Acquisition date is unknown.
Only Esri's blank-tile header identifies placeholders; unflagged valid JPEG
placeholders cannot reliably be distinguished from imagery without verified hashes.
Shutdown signals cancellation and waits at most one second; in-flight HTTP reads
finish under connect/read timeouts and a streamed size/time budget.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
import requests
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.warp import transform_geom
from rasterio.windows import Window, transform as window_transform

TILE_SIZE = 256
ORIGIN_SHIFT = math.pi * 6378137.0
MAX_LAT = 85.0511287798066
MAX_PIXELS = 500_000_000
MAX_JOB_DISK = 6 * 1024 ** 3
MAX_SERVICE_DISK = 12 * 1024 ** 3
DISK_RESERVE = 256 * 1024 ** 2
MAX_TILE_BYTES = 2_000_000


def _disk_usage(root):
    return sum(p.stat().st_size for p in Path(root).rglob('*') if p.is_file())


def _check_disk(plan, root, budget=MAX_JOB_DISK):
    # RGB + internal mask, TIFF block overhead, and maximum verified JPEG bytes.
    estimate = plan.width * plan.height * 5 + len(plan.tiles) * MAX_TILE_BYTES
    existing = _disk_usage(root)
    if existing + estimate > budget:
        raise ValueError('Disk budget exceeded; reduce area/zoom or remove owned job/cache files')
    if shutil.disk_usage(root).free < estimate + DISK_RESERVE:
        raise ValueError('Insufficient free disk space for imagery and cache')
TILE_URL = ('https://server.arcgisonline.com/ArcGIS/rest/services/'
            'World_Imagery/MapServer/tile/{z}/{y}/{x}')
ATTRIBUTION = 'Esri, Maxar, Earthstar Geographics, and the GIS User Community'


class BusyError(RuntimeError):
    """A download is already active."""


class CancelledError(RuntimeError):
    pass


class EsriRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bbox: list[float] | None = None
    commune_id: str | None = None
    zoom: int = Field(default=18, ge=1, le=19)
    workers: int = Field(default=4, ge=1, le=8)
    max_tiles: int = Field(default=4000, ge=1, le=20000)

    @model_validator(mode='after')
    def validate_area(self):
        if (self.bbox is None) == (self.commune_id is None):
            raise ValueError('Specify exactly one of bbox or commune_id')
        if self.bbox is not None:
            validate_bbox(self.bbox)
        if self.commune_id is not None and not self.commune_id.strip():
            raise ValueError('commune_id must not be empty')
        return self


def validate_bbox(bbox):
    if len(bbox) != 4 or not all(math.isfinite(v) for v in bbox):
        raise ValueError('bbox must contain four finite W S E N coordinates')
    w, s, e, n = bbox
    if not (-180 <= w < e <= 180 and -MAX_LAT <= s < n <= MAX_LAT):
        raise ValueError('bbox must be ordered and inside Web Mercator bounds')


def deg2tile(lon, lat, z):
    n = 2 ** z
    return ((lon + 180) / 360 * n,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def tile_x_to_merc(x, z):
    return x / 2 ** z * 2 * ORIGIN_SHIFT - ORIGIN_SHIFT


def tile_y_to_merc(y, z):
    return ORIGIN_SHIFT - y / 2 ** z * 2 * ORIGIN_SHIFT


def _load_cas():
    from .ee_session import _load_extra_cas
    _load_extra_cas()


def _pairs(coords):
    if isinstance(coords, (list, tuple)) and coords:
        if isinstance(coords[0], (int, float)):
            if len(coords) < 2:
                raise ValueError('Invalid polygon coordinate')
            yield coords[:2]
        else:
            for child in coords:
                yield from _pairs(child)


@dataclass
class Plan:
    zoom: int
    x0: int
    y0: int
    width: int
    height: int
    geometry: dict
    tiles: list[tuple[int, int]]
    transform: Any

    def window(self, x, y):
        return Window((x-self.x0)*256, (y-self.y0)*256, 256, 256)

    def mask(self, x, y):
        return rasterize([(self.geometry, 255)], out_shape=(256, 256),
                         transform=window_transform(self.window(x, y), self.transform),
                         fill=0, dtype='uint8')


def make_plan(bbox, zoom=18, max_tiles=4000, geometry=None, pad=0, workers=4):
    validate_bbox(bbox)
    if not isinstance(zoom, int) or not 1 <= zoom <= 19:
        raise ValueError('zoom must be 1..19')
    if not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError('workers must be 1..8')
    if not isinstance(pad, int) or not 0 <= pad <= 8:
        raise ValueError('pad must be 0..8')
    if not isinstance(max_tiles, int) or not 1 <= max_tiles <= 20000:
        raise ValueError('max_tiles must be 1..20000')
    w, s, e, n = bbox
    if geometry is None:
        geometry = {'type': 'Polygon', 'coordinates': [[[w,s],[e,s],[e,n],[w,n],[w,s]]]}
    if geometry.get('type') not in ('Polygon', 'MultiPolygon'):
        raise ValueError('A polygon or multipolygon is required')
    coords = list(_pairs(geometry.get('coordinates', [])))
    if not coords or any(not math.isfinite(v) for p in coords for v in p):
        raise ValueError('Invalid polygon coordinates')
    if any(not (-180 <= p[0] <= 180 and -MAX_LAT <= p[1] <= MAX_LAT) for p in coords):
        raise ValueError('Polygon outside Web Mercator bounds')
    if any(not (w <= p[0] <= e and s <= p[1] <= n) for p in coords):
        raise ValueError('Polygon outside bbox')
    xa, ya = deg2tile(w, n, zoom)
    xb, yb = deg2tile(e, s, zoom)
    last = 2 ** zoom - 1
    x0, y0 = max(0, math.floor(xa)-pad), max(0, math.floor(ya)-pad)
    x1, y1 = min(last, math.ceil(xb)-1+pad), min(last, math.ceil(yb)-1+pad)
    width, height = (x1-x0+1)*256, (y1-y0+1)*256
    if width * height > MAX_PIXELS:
        raise ValueError(f'Output exceeds {MAX_PIXELS} raster pixels; reduce zoom/area/pad')
    res = 2 * ORIGIN_SHIFT / (2 ** zoom * 256)
    plan = Plan(zoom, x0, y0, width, height,
                transform_geom('EPSG:4326', 'EPSG:3857', geometry), [],
                from_origin(tile_x_to_merc(x0, zoom), tile_y_to_merc(y0, zoom), res, res))
    for y in range(y0, y1+1):
        for x in range(x0, x1+1):
            if plan.mask(x, y).any():
                plan.tiles.append((x, y))
                if len(plan.tiles) > max_tiles:
                    raise ValueError(f'Area exceeds {max_tiles} tiles; reduce zoom/area')
    if not plan.tiles:
        raise ValueError('Polygon has no pixels at this zoom')
    return plan


def request_plan(body):
    if body.commune_id is not None:
        from .commune_boundaries import get_commune
        commune = get_commune(body.commune_id)
        if commune is None:
            raise ValueError('Unknown commune_id')
        b = commune.bbox
        return make_plan([b.west,b.south,b.east,b.north], body.zoom, body.max_tiles,
                         commune.geometry, workers=body.workers)
    return make_plan(body.bbox, body.zoom, body.max_tiles, workers=body.workers)


def _atomic_json(path, value):
    part = path.with_suffix('.json.part')
    with part.open('w', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, path)


def _decode(data):
    with Image.open(BytesIO(data)) as image:
        if image.format != 'JPEG' or image.size != (256, 256):
            raise ValueError('Expected a 256x256 JPEG tile')
        image.load()
        return np.asarray(image.convert('RGB')).copy()


class TileDownloadError(RuntimeError):
    """All retry attempts failed for an individual tile."""


class DownloadError(RuntimeError):
    def __init__(self, message, progress):
        super().__init__(message)
        self.progress = dict(progress)


def _tile(plan, x, y, cache, cancel):
    path = cache / f'{plan.zoom}_{x}_{y}.jpg'
    if cancel.is_set():
        raise CancelledError('Download cancelled')
    if path.exists():
        try:
            if path.stat().st_size > 2_000_000:
                raise ValueError('Cached tile exceeds size budget')
            return _decode(path.read_bytes()), True
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
    with requests.Session() as session:
        session.headers['User-Agent'] = 'GEE_testing Esri downloader (research)'
        last_error = None
        for attempt in range(3):
            if cancel.is_set():
                raise CancelledError('Download cancelled')
            try:
                with session.get(TILE_URL.format(z=plan.zoom, x=x, y=y),
                                 timeout=(5, 15), stream=True) as response:
                    # Esri explicitly flags unavailable tiles; uniform imagery is valid.
                    blank = response.headers.get('blank-tile', '').strip().lower()
                    if response.status_code == 404 or blank in ('true', '1', 'yes'):
                        return None, False
                    response.raise_for_status()
                    data = bytearray()
                    deadline = time.monotonic() + 20
                    for chunk in response.iter_content(chunk_size=65536):
                        if cancel.is_set():
                            raise CancelledError('Download cancelled')
                        if time.monotonic() > deadline or len(data) + len(chunk) > 2_000_000:
                            raise ValueError('Tile response exceeds size/time budget')
                        data.extend(chunk)
                    data = bytes(data)
                    arr = _decode(data)
                part = path.with_suffix('.jpg.part')
                try:
                    part.write_bytes(data)
                    os.replace(part, path)
                finally:
                    part.unlink(missing_ok=True)
                return arr, False
            except (requests.RequestException, OSError, ValueError) as exc:
                last_error = exc
                if attempt < 2 and cancel.wait(0.5 * (attempt + 1)):
                    raise CancelledError('Download cancelled')
    raise TileDownloadError(f'Tile {plan.zoom}/{y}/{x}: {type(last_error).__name__}: {last_error}') from last_error


def download(plan, output, cache, workers=4, cancel=None, progress=None, overwrite=False):
    """Publish a masked TIFF atomically. Reuse only fully decoded cached JPEGs."""
    if not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError('workers must be 1..8')
    output, cache = Path(output), Path(cache)
    if output.exists() and not overwrite:
        raise FileExistsError(f'{output} exists; use --overwrite')
    output.parent.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    _check_disk(plan, cache)
    output_estimate = plan.width * plan.height * 5 + DISK_RESERVE
    if shutil.disk_usage(output.parent).free < output_estimate:
        raise ValueError('Insufficient free disk space for output')
    cancel = cancel or threading.Event()
    stats = dict(processed=0, total=len(plan.tiles), succeeded=0, failed=0, cached=0)
    if progress:
        progress(dict(stats))
    _load_cas()
    tile_errors = []
    part = output.with_name(output.name + '.' + uuid.uuid4().hex + '.part')
    try:
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True, GDAL_CACHEMAX=32 * 1024 * 1024):
            with rasterio.open(part, 'w', driver='GTiff', width=plan.width, height=plan.height,
                               count=3, dtype='uint8', crs='EPSG:3857', transform=plan.transform,
                               tiled=True, blockxsize=256, blockysize=256, compress='deflate',
                               BIGTIFF='IF_SAFER') as dst:
                # Initialize every window explicitly, including omitted polygon tiles.
                zero = np.zeros((3, 256, 256), dtype='uint8')
                for row in range(plan.height//256):
                    for col in range(plan.width//256):
                        if cancel.is_set():
                            raise CancelledError('Download cancelled')
                        window = Window(col*256, row*256, 256, 256)
                        dst.write(zero, window=window)
                        dst.write_mask(zero[0], window=window)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    tiles = iter(plan.tiles)
                    pending = {}
                    def submit():
                        item = next(tiles, None)
                        if item is not None:
                            x, y = item
                            pending[pool.submit(_tile, plan, x, y, cache, cancel)] = item
                    for _ in range(workers):
                        submit()
                    while pending:
                        if cancel.is_set():
                            raise CancelledError('Download cancelled')
                        ready, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
                        for future in ready:
                            x, y = pending.pop(future)
                            try:
                                arr, cached = future.result()
                            except TileDownloadError as exc:
                                tile_errors.append(str(exc))
                                arr, cached = None, False
                            stats['processed'] += 1
                            if arr is None:
                                stats['failed'] += 1
                            else:
                                stats['succeeded'] += 1
                                stats['cached'] += int(cached)
                                dst.write(arr.transpose(2,0,1), window=plan.window(x,y))
                                dst.write_mask(plan.mask(x,y), window=plan.window(x,y))
                            if progress:
                                progress(dict(stats))
                            if not cancel.is_set():
                                submit()
                if not stats['succeeded']:
                    detail = tile_errors[0] if tile_errors else 'Esri returned unavailable (404/blank-tile) imagery'
                    raise DownloadError('No usable imagery tiles were downloaded: ' + detail, stats)
                dst.update_tags(source=TILE_URL, attribution=ATTRIBUTION,
                                acquisition_date='unknown', zoom=str(plan.zoom),
                                native_resolution='unknown; zoom is sampling resolution',
                                missing_tiles=str(stats['failed']), mask_semantics='pixel centre',
                                succeeded_tiles=str(stats['succeeded']), total_tiles=str(stats['total']),
                                geometry_mercator=json.dumps(plan.geometry),
                                sampling_resolution_m=str(abs(plan.transform.a)),
                                tile_errors=json.dumps(tile_errors[:20]),
                                usage='Subject to Esri and imagery provider terms; attribution required')
        if cancel.is_set():
            raise CancelledError('Download cancelled')
        if overwrite:
            os.replace(part, output)
        else:
            os.link(part, output)
            part.unlink()
        return stats
    finally:
        part.unlink(missing_ok=True)
        Path(str(part) + '.msk').unlink(missing_ok=True)


class EsriService:
    """Single active job. Atomic job.json plus verified cache survive restart."""
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs = {}
        self._thread = None
        self._cancel = threading.Event()
        self._closed = False
        self.recover()

    def _save(self, job):
        _atomic_json(self.root / job['job_id'] / 'job.json', job)

    def recover(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise BusyError('A job is running')
            for folder in self.root.iterdir():
                try:
                    if str(uuid.UUID(folder.name)) != folder.name:
                        continue
                    job = json.loads((folder / 'job.json').read_text(encoding='utf-8'))
                    if job['job_id'] != folder.name:
                        continue
                    if job['status'] in ('queued', 'running'):
                        job['status'] = 'interrupted'
                        self._save(job)
                    self._jobs[folder.name] = job
                except (ValueError, OSError, KeyError, TypeError):
                    continue

    def _available(self):
        if self._closed:
            raise RuntimeError('Service is closed')
        if self._thread and self._thread.is_alive():
            raise BusyError('Another Esri job is active')

    def start(self, body: EsriRequest):
        with self._lock:
            self._available()
            plan = request_plan(body)
            _check_disk(plan, self.root, MAX_SERVICE_DISK)
            job_id = str(uuid.uuid4())
            (self.root / job_id).mkdir()
            job = dict(job_id=job_id, status='queued', request=body.model_dump(),
                       progress=dict(processed=0, total=len(plan.tiles), succeeded=0, failed=0, cached=0),
                       error=None, attribution=ATTRIBUTION, source=TILE_URL,
                       acquisition_date=None, native_resolution=None)
            self._jobs[job_id] = job
            self._launch(job, plan)
            return self.get(job_id)

    def _launch(self, job, plan):
        job['status'] = 'queued'
        job['error'] = None
        job['progress'] = dict(processed=0,total=len(plan.tiles),succeeded=0,failed=0,cached=0)
        self._save(job)
        self._cancel = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(job, plan, self._cancel), daemon=True)
        self._thread.start()

    def _run(self, job, plan, cancel):
        folder = self.root / job['job_id']
        def progress(stats):
            with self._lock:
                job['progress'] = stats
                self._save(job)
        try:
            with self._lock:
                job['status'] = 'running'
                self._save(job)
            stats = download(plan, folder / 'imagery.tif', folder / 'cache',
                             workers=job['request']['workers'], cancel=cancel,
                             progress=progress, overwrite=True)
            with self._lock:
                job['progress'] = stats
                job['status'] = 'partial' if stats['failed'] else 'completed'
        except Exception as exc:
            with self._lock:
                job['status'] = 'cancelled' if cancel.is_set() else 'failed'
                if isinstance(exc, DownloadError):
                    job['progress'] = exc.progress
                job['error'] = str(exc)
        finally:
            with self._lock:
                self._save(job)

    def get(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            return json.loads(json.dumps(job)) if job else None

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job['status'] in ('queued', 'running'):
                self._cancel.set()
            return self.get(job_id)

    def resume(self, job_id):
        with self._lock:
            self._available()
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job['status'] == 'completed':
                return self.get(job_id)
            plan = request_plan(EsriRequest(**job['request']))
            _check_disk(plan, self.root, MAX_SERVICE_DISK)
            self._launch(job, plan)
            return self.get(job_id)

    def download_path(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job['status'] in ('completed', 'partial'):
                path = self.root / job_id / 'imagery.tif'
                if path.is_file():
                    return path
            return None

    def shutdown(self):
        with self._lock:
            self._closed = True
            self._cancel.set()
            thread = self._thread
        if thread:
            thread.join(timeout=1)
