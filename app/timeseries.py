"""Persistent, single-worker time-series exports for the local GEE application."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import rasterio
import requests
from rasterio.transform import Affine
from rasterio.warp import calculate_default_transform, reproject, Resampling

from .geo import Bbox
from .imagery import download_geotiff, load_timeseries_scene, merge_band_tiffs

TTL_SECONDS = 24 * 60 * 60
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "partial", "failed", "interrupted")
MIN_FREE_BYTES = 64 * 1024 * 1024


class BusyError(ValueError):
    """Another search already owns the application worker."""


def _safe(value):
    """Remove transport/authentication metadata rather than trying to redact secrets."""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()
                if not any(part in str(k).lower() for part in
                           ("url", "token", "secret", "credential", "password", "authorization", "cookie", "key"))}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if isinstance(value, str) and ("://" in value or "Bearer " in value):
        return "[redacted]"
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


def _error(exc: Exception) -> str:
    # Exception bodies often contain signed URLs, request headers or OAuth errors.
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "Insufficient disk space."
    if isinstance(exc, PermissionError):
        return "Access denied while loading or saving this scene."
    if isinstance(exc, DiskSpaceError):
        return "Insufficient disk space."
    return f"Scene export failed ({type(exc).__name__})."


class DiskSpaceError(OSError):
    pass


def _timestamp(now=None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now.timestamp()
    return float(now)


class TimeSeriesStore:
    """SQLite snapshots and activity timestamps; root is downloads/timeseries."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "jobs.sqlite3"
        self._lock = threading.RLock()
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS searches (
                    id TEXT PRIMARY KEY, parameters TEXT NOT NULL, scenes TEXT NOT NULL,
                    created REAL NOT NULL, activity REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, search_id TEXT NOT NULL,
                    status TEXT NOT NULL, total INTEGER NOT NULL,
                    processed INTEGER NOT NULL DEFAULT 0,
                    succeeded INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    error TEXT, created REAL NOT NULL, activity REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS jobs_search ON jobs(search_id);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_search(self, parameters: dict, scenes: list[dict]) -> dict:
        search_id, now = str(uuid.uuid4()), time.time()
        parameters, scenes = _safe(parameters), _safe(scenes)
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO searches VALUES (?,?,?,?,?)",
                       (search_id, json.dumps(parameters), json.dumps(scenes), now, now))
        return self.get_search(search_id)

    def get_search(self, search_id: str, offset: int = 0, limit: int = 50) -> dict | None:
        if offset < 0 or limit < 1 or limit > 50:
            raise ValueError("offset must be nonnegative and limit must be 1–50")
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE searches SET activity=? WHERE id=?", (time.time(), search_id))
        scenes = json.loads(row["scenes"])
        return {"search_id": search_id, "total": len(scenes), "scenes": scenes[offset:offset + limit],
                "parameters": json.loads(row["parameters"]), "offset": offset, "limit": limit}

    def _snapshot(self, search_id: str):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone()
        if row is None:
            raise ValueError("Search not found or expired.")
        return json.loads(row["parameters"]), json.loads(row["scenes"])

    @staticmethod
    def _job(row) -> dict:
        return {"job_id": row["id"], "search_id": row["search_id"],
                **{key: row[key] for key in
                   ("status", "total", "processed", "succeeded", "failed", "error")}}

    def create_job(self, search_id: str) -> dict:
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            search = db.execute("SELECT scenes FROM searches WHERE id=?", (search_id,)).fetchone()
            if search is None:
                raise ValueError("Search not found or expired.")
            total = len(json.loads(search["scenes"]))
            if not total:
                raise ValueError("Search contains no scenes.")
            existing = db.execute("SELECT * FROM jobs WHERE search_id=? ORDER BY created DESC LIMIT 1",
                                  (search_id,)).fetchone()
            if existing is not None and existing["status"] in (*ACTIVE_STATUSES, "completed", "partial"):
                db.execute("UPDATE jobs SET activity=? WHERE id=?", (time.time(), existing["id"]))
                return self._job(existing)
            if db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone():
                raise BusyError("Another time-series job is active. Try again after it finishes.")
            job_id, now = str(uuid.uuid4()), time.time()
            db.execute("INSERT INTO jobs (id,search_id,status,total,created,activity) VALUES (?,?,?,?,?,?)",
                       (job_id, search_id, "queued", total, now, now))
            db.execute("UPDATE searches SET activity=? WHERE id=?", (now, search_id))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return self._job(row)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return None
            now = time.time()
            db.execute("UPDATE jobs SET activity=? WHERE id=?", (now, job_id))
            db.execute("UPDATE searches SET activity=? WHERE id=?", (now, row["search_id"]))
            return self._job(row)

    def _update_job(self, job_id: str, **updates):
        if not set(updates) <= {"status", "processed", "succeeded", "failed", "error"}:
            raise ValueError("Invalid job update")
        updates["activity"] = time.time()
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE jobs SET {','.join(k + '=?' for k in updates)} WHERE id=?",
                       (*updates.values(), job_id))

    def _job_dir(self, job_id: str) -> Path:
        # IDs never originate from a caller-supplied path.
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError("Invalid job ID")
        return self.root / job_id

    def download_path(self, job_id: str) -> Path | None:
        job = self.get_job(job_id)
        if job is None or job["status"] not in ("completed", "partial"):
            return None
        path = self._job_dir(job_id) / "timeseries.zip"
        return path if path.is_file() else None

    def recover_interrupted_jobs(self) -> int:
        with self._lock, self._connect() as db:
            result = db.execute("UPDATE jobs SET status='interrupted', error=?, activity=? "
                                "WHERE status IN ('queued','running')",
                                ("Application restarted; start a new export to retry.", time.time()))
            return result.rowcount

    def cleanup_expired_jobs(self, now=None) -> int:
        cutoff, removed = _timestamp(now) - TTL_SECONDS, 0
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT id FROM jobs WHERE activity < ? "
                              "AND status NOT IN ('queued','running')", (cutoff,)).fetchall()
            for row in rows:
                directory = self._job_dir(row["id"])
                if directory.exists():
                    shutil.rmtree(directory)
                db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                removed += 1
            db.execute("DELETE FROM searches WHERE activity < ? AND NOT EXISTS "
                       "(SELECT 1 FROM jobs WHERE jobs.search_id=searches.id)", (cutoff,))
        return removed


def align_timeseries_raster(source: Path, destination: Path, grid: dict | None = None,
                            *, categorical: bool = False, bands: list[str] | None = None,
                            nodata=None) -> dict:
    """Warp to the first successful scene grid, retaining descriptions and missing coverage."""
    with rasterio.open(source) as src:
        if not src.crs:
            raise ValueError("Raster CRS is missing")
        if bands is not None and src.count != len(bands):
            raise ValueError("Exported raster bands do not match the search snapshot")
        if grid is None:
            transform, width, height = src.transform, src.width, src.height
            if src.crs != rasterio.crs.CRS.from_epsg(4326):
                transform, width, height = calculate_default_transform(
                    src.crs, "EPSG:4326", src.width, src.height, *src.bounds)
            grid = {"crs": "EPSG:4326", "transform": list(transform)[:6],
                    "width": width, "height": height,
                    "bounds": list(rasterio.transform.array_bounds(height, width, transform))}
        transform = Affine(*grid["transform"])
        fill = 255 if categorical else (src.nodata if src.nodata is not None else nodata)
        if fill is None or (isinstance(fill, float) and not math.isfinite(fill)):
            fill = -9999.0
        profile = src.profile.copy()
        profile.update(driver="GTiff", crs=grid["crs"], transform=transform,
                       width=grid["width"], height=grid["height"], nodata=fill,
                       dtype="uint8" if categorical else "float32")
        with rasterio.open(destination, "w", **profile) as dst:
            for index in range(1, src.count + 1):
                reproject(source=rasterio.band(src, index), destination=rasterio.band(dst, index),
                          src_transform=src.transform, src_crs=src.crs,
                          src_nodata=src.nodata, dst_transform=transform, dst_crs=grid["crs"],
                          dst_nodata=fill, init_dest_nodata=True,
                          resampling=Resampling.nearest if categorical else Resampling.bilinear)
                description = bands[index - 1] if bands and index <= len(bands) else src.descriptions[index - 1]
                if description:
                    dst.set_band_description(index, description)
            dst.update_tags(**_safe(src.tags()))
            dst.scales = src.scales
            dst.offsets = src.offsets
            dst.units = src.units
    return grid


def _scene_filename(dataset: str, scene: dict) -> str:
    stamp = datetime.fromisoformat(scene["timestamp"].replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dataset = "".join(c for c in dataset if c.isascii() and (c.isalnum() or c == "_"))
    token = hashlib.sha256(scene["asset_id"].encode()).hexdigest()[:16]
    return f"{dataset}_{stamp}_{token}.tif"


class TimeSeriesService:
    def __init__(self, store: TimeSeriesStore):
        self.store = store
        self._lock = threading.RLock()
        self._executor = None
        self._futures = {}
        self._closed = False

    def start_job(self, search_id: str) -> dict:
        with self._lock:
            if self._closed:
                raise RuntimeError("Time-series service is shut down.")
            self._futures = {key: future for key, future in self._futures.items() if not future.done()}
            job = self.store.create_job(search_id)
            job_id = job["job_id"]
            if job["status"] == "queued" and job_id not in self._futures:
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="timeseries")
                try:
                    self._futures[job_id] = self._executor.submit(self.run_timeseries_job, job_id)
                except Exception:
                    self.store._update_job(job_id, status="failed", error="Unable to start export worker.")
                    raise RuntimeError("Unable to start export worker.") from None
            return job

    def recover_interrupted_jobs(self) -> int:
        with self._lock:
            if any(not f.done() for f in self._futures.values()):
                raise BusyError("Cannot recover jobs while this worker is active.")
            return self.store.recover_interrupted_jobs()

    def cleanup_expired_jobs(self, now=None) -> int:
        return self.store.cleanup_expired_jobs(now)

    def shutdown(self):
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=True)

    def _check_disk(self, parameters: dict, directory: Path, *, archive: bool = False):
        bbox, scale = parameters["bbox"], float(parameters["scale_m"])
        width = abs(bbox["east"] - bbox["west"]) * 111320 / scale
        height = abs(bbox["north"] - bbox["south"]) * 111320 / scale
        estimate = int(max(1, width * height) * max(1, len(parameters["bands"])) * 4)
        # Raw tiles, stacked TIFF and aligned TIFF can coexist. ZIP creation
        # additionally needs room for an uncompressed copy of all successful files.
        required = max(MIN_FREE_BYTES, estimate * 4)
        if archive:
            required = max(MIN_FREE_BYTES, sum(p.stat().st_size for p in directory.glob("*.tif")) * 2)
        if shutil.disk_usage(directory).free < required:
            raise DiskSpaceError("Insufficient disk space")

    def run_timeseries_job(self, job_id: str):
        job = self.store.get_job(job_id)
        if job is None or job["status"] != "queued":
            return
        directory = self.store._job_dir(job_id)
        with self.store._lock, self.store._connect() as db:
            claimed = db.execute("UPDATE jobs SET status='running', activity=? WHERE id=? AND status='queued'",
                                 (time.time(), job_id))
            if claimed.rowcount != 1:
                return
        records, grid = [], None
        succeeded = failed = processed = 0
        fatal = None
        try:
            parameters, scenes = self.store._snapshot(job["search_id"])
            directory.mkdir(parents=True, exist_ok=True)
            for position, scene in enumerate(scenes):
                record = {**scene, "file": None, "status": "failed", "error": None,
                          "bands": parameters["bands"], "units": parameters.get("units"),
                          "value_scale": parameters.get("value_scale"),
                          "value_offset": parameters.get("value_offset")}
                raw = directory / "scene.raw.tif"
                aligned = directory / "scene.aligned.tif"
                try:
                    self._check_disk(parameters, directory)
                    filename = _scene_filename(parameters["dataset"], scene)
                    for attempt in range(3):
                        try:
                            image = load_timeseries_scene(scene["asset_id"], parameters["dataset"],
                                                          parameters.get("polarization", "vvvh"))
                            urls = download_geotiff(image, parameters["dataset"],
                                                    Bbox(**parameters["bbox"]), parameters["scale_m"])
                            merge_band_tiffs(urls, raw, categorical=parameters["dataset"] == "dynamic_world")
                            break
                        except Exception as exc:
                            # The merge helper already retries HTTP fetches/URL refreshes.
                            # Only retry scene construction/export for transient failures.
                            status = getattr(getattr(exc, "response", None), "status_code", None)
                            transient = isinstance(exc, (TimeoutError, ConnectionError, requests.Timeout,
                                                         requests.ConnectionError)) or status in (429, 500, 502, 503, 504)
                            transient = transient or any(s in str(exc).lower() for s in
                                                        ("temporarily unavailable", "quota exceeded", "timed out"))
                            if not transient or attempt == 2:
                                raise
                            time.sleep(0.25 * (attempt + 1))
                    new_grid = align_timeseries_raster(raw, aligned, grid,
                                categorical=parameters["dataset"] == "dynamic_world",
                                bands=parameters["bands"], nodata=parameters.get("nodata"))
                    with rasterio.open(aligned) as raster:
                        record["nodata"] = raster.nodata
                        record["dtype"] = raster.dtypes[0]
                    os.replace(aligned, directory / filename)
                    grid = new_grid
                    record.update(file=filename, status="succeeded")
                    succeeded += 1
                except Exception as exc:
                    record["error"] = _error(exc)
                    failed += 1
                    if isinstance(exc, DiskSpaceError) or getattr(exc, "errno", None) == 28:
                        fatal = "Insufficient disk space; export stopped safely."
                finally:
                    raw.unlink(missing_ok=True)
                    aligned.unlink(missing_ok=True)
                records.append(record)
                processed += 1
                self.store._update_job(job_id, processed=processed, succeeded=succeeded, failed=failed)
                if fatal:
                    for remainder in scenes[position + 1:]:
                        records.append({**remainder, "file": None, "status": "failed",
                                        "error": "Not attempted: insufficient disk space."})
                    failed += len(scenes) - processed
                    processed = len(scenes)
                    break
            manifest = {"job_id": job_id, "search_id": job["search_id"],
                        "parameters": parameters, "grid": grid, "scenes": records,
                        "total": len(scenes), "processed": processed,
                        "succeeded": succeeded, "failed": failed}
            manifest = _safe(manifest)
            (directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            csv_buffer = io.StringIO(newline="")
            fields = ["asset_id", "timestamp", "date", "cloud_pct", "dataset", "collection",
                      "file", "status", "error", "bands", "units", "value_scale", "value_offset",
                      "nodata", "dtype", "source_metadata", "parameters", "grid"]
            writer = csv.DictWriter(csv_buffer, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                row = {**_safe(record), "parameters": parameters, "grid": grid}
                writer.writerow({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
                                 for k, v in row.items()})
            (directory / "manifest.csv").write_text(csv_buffer.getvalue(), encoding="utf-8")
            if succeeded and not fatal:
                self._check_disk(parameters, directory, archive=True)
                pending = directory / "timeseries.zip.part"
                with zipfile.ZipFile(pending, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
                    for record in records:
                        if record["file"]:
                            archive.write(directory / record["file"], record["file"])
                    for filename in ("manifest.json", "manifest.csv"):
                        archive.write(directory / filename, filename)
                os.replace(pending, directory / "timeseries.zip")
            status = "failed" if fatal or not succeeded else ("partial" if failed else "completed")
            self.store._update_job(job_id, status=status, processed=processed,
                                   succeeded=succeeded, failed=failed,
                                   error=fatal or ("All scenes failed to export." if not succeeded else None))
        except Exception as exc:
            self.store._update_job(job_id, status="failed", processed=processed,
                                   succeeded=succeeded, failed=failed, error=_error(exc))
        finally:
            (directory / "timeseries.zip.part").unlink(missing_ok=True)
