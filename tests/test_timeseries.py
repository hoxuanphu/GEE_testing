"""Offline persistence, worker, archive and raster alignment regression tests."""
from __future__ import annotations

import json
import shutil
import threading
import unittest
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app import timeseries as ts


class TimeSeriesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / (".timeseries-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.store = ts.TimeSeriesStore(self.root)
        self.service = ts.TimeSeriesService(self.store)
        self.parameters = {"dataset": "dynamic_world", "polarization": "vvvh",
                           "bbox": {"west": 0, "south": 0, "east": 0.0004, "north": 0.0004},
                           "scale_m": 10, "bands": ["label"], "units": "class", "nodata": 255,
                           "value_scale": 1, "value_offset": 0, "lon": 0.0002, "lat": 0.0002,
                           "radius_m": 20, "start_date": "2026-01-01", "end_date": "2026-01-01"}
        self.scenes = [{"asset_id": "GOOGLE/DYNAMICWORLD/V1/scene-a", "timestamp": "2026-01-01T10:00:00Z",
                        "date": "2026-01-01", "cloud_pct": 100, "dataset": "dynamic_world",
                        "collection": "GOOGLE/DYNAMICWORLD/V1", "source_metadata": {"orbit": 42}},
                       {"asset_id": "GOOGLE/DYNAMICWORLD/V1/scene-b", "timestamp": "2026-01-01T10:00:00Z",
                        "date": "2026-01-01", "cloud_pct": None, "dataset": "dynamic_world",
                        "collection": "GOOGLE/DYNAMICWORLD/V1", "source_metadata": {"track": 3}}]

    def tearDown(self):
        self.service.shutdown()
        shutil.rmtree(self.root)

    def search(self, scenes=None):
        return self.store.create_search(self.parameters, self.scenes if scenes is None else scenes)["search_id"]

    @staticmethod
    def raster(path, *, second=False, categorical=True, projected=False):
        with rasterio.open(path, "w", driver="GTiff", height=2 if second else 4,
                           width=2 if second else 4, count=1,
                           dtype="uint8" if categorical else "float32",
                           nodata=255 if categorical else -9999,
                           crs="EPSG:3857" if projected else "EPSG:4326",
                           transform=from_origin(0, 40 if projected else 0.0004,
                                                 10 if projected else 0.0001,
                                                 10 if projected else 0.0001)) as dst:
            data = np.array([[0, 4], [255, 6]], dtype="uint8") if categorical else np.array([[1, 2], [-9999, 4]], dtype="float32")
            dst.write(data if second else np.tile(data, (2, 2)), 1)
            dst.set_band_description(1, "label" if categorical else "VV")

    def run_job(self, *, fail_ids=(), transient=False):
        job = self.store.create_job(self.search())
        seen = []
        attempts = {}

        def load(asset_id, dataset, polarization):
            seen.append(asset_id)
            attempts[asset_id] = attempts.get(asset_id, 0) + 1
            if asset_id in fail_ids:
                raise PermissionError("Authorization: Bearer TOPSECRET https://host?token=SECRET")
            if transient and attempts[asset_id] == 1:
                raise TimeoutError("https://host?secret=SECRET")
            return asset_id

        def merge(urls, path, *, categorical):
            self.assertTrue(categorical)
            self.raster(path, second=urls.endswith("scene-b"))

        with mock.patch.object(ts, "load_timeseries_scene", side_effect=load), \
             mock.patch.object(ts, "download_geotiff", side_effect=lambda image, *a: image), \
             mock.patch.object(ts, "merge_band_tiffs", side_effect=merge), \
             mock.patch.object(ts.time, "sleep"):
            self.service.run_timeseries_job(job["job_id"])
        return self.store.get_job(job["job_id"]), seen

    def test_immutable_snapshot_and_paging_survive_reopen(self):
        scenes = [dict(self.scenes[0], asset_id=f"collection/{i}") for i in range(123)]
        result = self.store.create_search(self.parameters, scenes)
        scenes[0]["asset_id"] = "changed"
        self.parameters["bands"].append("changed")
        result["scenes"][0]["asset_id"] = "also-changed"
        reopened = ts.TimeSeriesStore(self.root)
        self.assertEqual(reopened.get_search(result["search_id"])["scenes"][0]["asset_id"], "collection/0")
        self.assertEqual(reopened.get_search(result["search_id"])["parameters"]["bands"], ["label"])
        pages = [reopened.get_search(result["search_id"], offset=i)["scenes"] for i in (0, 50, 100)]
        self.assertEqual([len(p) for p in pages], [50, 50, 23])
        self.assertEqual(result["total"], 123)
        self.assertIsNone(reopened.get_search("missing"))
        for offset, limit in ((-1, 50), (0, 0), (0, 51)):
            with self.assertRaises(ValueError):
                reopened.get_search(result["search_id"], offset, limit)

    def test_empty_unknown_no_job(self):
        for search_id in (self.search([]), "unknown"):
            with self.assertRaises(ValueError):
                self.store.create_job(search_id)
        self.assertIsNone(self.store.get_job("../../anything"))
        self.assertIsNone(self.store.download_path("../../anything"))
        self.assertIsNone(self.service._executor)

    def test_concurrent_idempotent_job_and_busy(self):
        search_id, other = self.search(), self.search()
        with ThreadPoolExecutor(max_workers=8) as executor:
            jobs = list(executor.map(lambda _: self.store.create_job(search_id), range(16)))
        self.assertEqual(len({j["job_id"] for j in jobs}), 1)
        with self.assertRaises(ts.BusyError):
            ts.TimeSeriesStore(self.root).create_job(other)

    def test_zip_grid_metadata_cloud_and_unique_same_timestamp(self):
        job, seen = self.run_job()
        self.assertEqual(job["status"], "completed")
        self.assertEqual((job["total"], job["processed"], job["succeeded"], job["failed"]), (2, 2, 2, 0))
        self.assertEqual(seen, [s["asset_id"] for s in self.scenes])
        path = self.store.download_path(job["job_id"])
        self.assertTrue(path.is_file())
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            tiffs = [n for n in names if n.endswith(".tif")]
            self.assertEqual(len(set(tiffs)), 2)
            self.assertTrue(all("/" not in n and ".." not in n for n in names))
            self.assertEqual(archive.testzip(), None)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["parameters"], self.parameters)
            self.assertEqual(manifest["scenes"][0]["cloud_pct"], 100)
            self.assertEqual(manifest["scenes"][0]["source_metadata"]["orbit"], 42)
            self.assertIn(b"grid", archive.read("manifest.csv"))
            grids = []
            for filename in tiffs:
                with rasterio.MemoryFile(archive.read(filename)) as memory, memory.open() as src:
                    grids.append((src.crs, src.transform, src.shape))
                    self.assertEqual(src.nodata, 255)
                    self.assertEqual(src.descriptions, ("label",))
                    self.assertEqual(src.dtypes, ("uint8",))
                    self.assertTrue(set(np.unique(src.read(1))) <= {0, 4, 6, 255})
                    self.assertEqual(src.read(1)[0, 0], 0)
                    if filename == manifest["scenes"][1]["file"]:
                        self.assertTrue(np.all(src.read(1)[2:, :] == 255))
            self.assertEqual(grids[0], grids[1])
        self.assertFalse((path.parent / "timeseries.zip.part").exists())
        self.assertEqual(self.store.create_job(job["search_id"])["job_id"], job["job_id"])

    def test_partial_errors_sanitized_and_next_scene_runs(self):
        job, seen = self.run_job(fail_ids=[self.scenes[0]["asset_id"]])
        self.assertEqual(job["status"], "partial")
        self.assertEqual((job["succeeded"], job["failed"], job["processed"]), (1, 1, 2))
        self.assertEqual(len(seen), 2)
        with zipfile.ZipFile(self.store.download_path(job["job_id"])) as archive:
            body = archive.read("manifest.json") + archive.read("manifest.csv")
            self.assertNotIn(b"TOPSECRET", body)
            self.assertNotIn(b"https://", body)
            self.assertNotIn(b"Bearer", body)
            self.assertEqual(len([n for n in archive.namelist() if n.endswith(".tif")]), 1)

    def test_all_failure_no_zip_retry_new_job(self):
        job, _ = self.run_job(fail_ids=[s["asset_id"] for s in self.scenes])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failed"], 2)
        self.assertIsNone(self.store.download_path(job["job_id"]))
        manifest = self.store._job_dir(job["job_id"]) / "manifest.json"
        self.assertTrue(manifest.is_file())
        self.assertNotEqual(self.store.create_job(job["search_id"])["job_id"], job["job_id"])

    def test_transient_retries_bounded(self):
        job, seen = self.run_job(transient=True)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(len(seen), 4)
        job2 = self.store.create_job(self.search())
        with mock.patch.object(ts, "load_timeseries_scene", side_effect=TimeoutError("secret")) as load, \
             mock.patch.object(ts.time, "sleep"):
            self.service.run_timeseries_job(job2["job_id"])
        self.assertEqual(load.call_count, 6)
        self.assertEqual(self.store.get_job(job2["job_id"])["status"], "failed")

    def test_disk_exhaustion_stops_and_never_completed(self):
        job = self.store.create_job(self.search())
        with mock.patch.object(ts.shutil, "disk_usage", return_value=mock.Mock(free=0)), \
             mock.patch.object(ts, "load_timeseries_scene") as load:
            self.service.run_timeseries_job(job["job_id"])
        result = self.store.get_job(job["job_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["processed"], 2)
        self.assertEqual(result["failed"], 2)
        load.assert_not_called()
        self.assertIsNone(self.store.download_path(job["job_id"]))
        self.assertIn("disk", result["error"])

    def test_archive_disk_failure_retains_tiffs_no_download(self):
        original = self.service._check_disk
        def check(parameters, directory, *, archive=False):
            if archive:
                raise ts.DiskSpaceError()
            return original(parameters, directory)
        with mock.patch.object(self.service, "_check_disk", side_effect=check):
            job, _ = self.run_job()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["succeeded"], 2)
        self.assertEqual(len(list(self.store._job_dir(job["job_id"]).glob("*.tif"))), 2)
        self.assertIsNone(self.store.download_path(job["job_id"]))

    def test_archive_write_failure_is_atomic(self):
        with mock.patch.object(ts.zipfile.ZipFile, "write", side_effect=OSError("credentials SECRET")):
            job, _ = self.run_job()
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(self.store.download_path(job["job_id"]))
        self.assertFalse((self.store._job_dir(job["job_id"]) / "timeseries.zip.part").exists())
        self.assertNotIn("SECRET", job["error"])

    def test_restart_marks_queued_running_interrupted_and_allows_retry(self):
        job = self.store.create_job(self.search())
        reopened = ts.TimeSeriesStore(self.root)
        self.assertEqual(reopened.recover_interrupted_jobs(), 1)
        self.assertEqual(reopened.get_job(job["job_id"])["status"], "interrupted")
        retry = reopened.create_job(job["search_id"])
        reopened._update_job(retry["job_id"], status="running")
        self.assertEqual(reopened.recover_interrupted_jobs(), 1)
        self.assertEqual(reopened.recover_interrupted_jobs(), 0)
        self.assertIsNone(reopened.download_path(retry["job_id"]))

    def test_ttl_activity_and_active_protection(self):
        job, _ = self.run_job()
        active_search = self.search()
        active = self.store.create_job(active_search)
        unused_search = self.search([])
        old = ts.time.time() - ts.TTL_SECONDS - 60
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET activity=?", (old,))
            db.execute("UPDATE searches SET activity=?", (old,))
        self.assertEqual(self.store.cleanup_expired_jobs(), 1)
        self.assertIsNone(self.store.get_job(job["job_id"]))
        self.assertFalse(self.store._job_dir(job["job_id"]).exists())
        self.assertIsNone(self.store.get_search(job["search_id"]))
        self.assertIsNone(self.store.get_search(unused_search))
        self.assertEqual(self.store.get_job(active["job_id"])["status"], "queued")
        self.assertIsNotNone(self.store.get_search(active_search))
        self.store._update_job(active["job_id"], status="failed")
        with self.store._connect() as db:
            db.execute("UPDATE jobs SET activity=?", (old,))
        self.store.get_job(active["job_id"])
        self.assertEqual(self.service.cleanup_expired_jobs(datetime.now(timezone.utc)), 0)

    def test_service_lazy_executor_single_submission_and_shutdown(self):
        started, release = threading.Event(), threading.Event()
        def worker(job_id):
            self.store._update_job(job_id, status="running")
            started.set()
            release.wait()
            self.store._update_job(job_id, status="failed")
        search_id, other = self.search(), self.search()
        self.assertIsNone(self.service._executor)
        with mock.patch.object(self.service, "run_timeseries_job", side_effect=worker) as run:
            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    jobs = list(executor.map(lambda _: self.service.start_job(search_id), range(16)))
                self.assertTrue(started.wait(2))
                self.assertEqual(len({j["job_id"] for j in jobs}), 1)
                self.assertEqual(run.call_count, 1)
                with self.assertRaises(ts.BusyError):
                    self.service.start_job(other)
                with self.assertRaises(ts.BusyError):
                    self.service.recover_interrupted_jobs()
            finally:
                release.set()
                self.service.shutdown()
        with self.assertRaises(RuntimeError):
            self.service.start_job(search_id)

    def test_manifest_transport_metadata_removed(self):
        self.parameters["credentials"] = {"secret": "SECRET"}
        self.scenes[0]["download_url"] = "https://host?signed=SECRET"
        self.scenes[0]["source_metadata"]["access_token"] = "SECRET"
        job, _ = self.run_job()
        with zipfile.ZipFile(self.store.download_path(job["job_id"])) as archive:
            self.assertNotIn(b"SECRET", archive.read("manifest.json"))
            self.assertNotIn(b"SECRET", archive.read("manifest.csv"))

    def test_actual_merge_download_helper_and_snapshot_parameters(self):
        from app import imagery
        input_path = self.root / "input.tif"
        with rasterio.open(input_path, "w", driver="GTiff", height=16, width=16,
                           count=1, dtype="uint8", nodata=255, crs="EPSG:4326",
                           transform=from_origin(0, 0.0004, 0.000025, 0.000025)) as dst:
            dst.write(np.tile(np.array([[0, 4], [255, 6]], dtype="uint8"), (8, 8)), 1)
        payload = input_path.read_bytes()
        search_id = self.search()
        self.parameters["dataset"] = "landsat"
        job = self.store.create_job(search_id)
        url = mock.Mock()
        url.url.return_value = "https://mock.invalid?signed=SECRET"
        with mock.patch.object(ts, "load_timeseries_scene", return_value=object()) as load, \
             mock.patch.object(ts, "download_geotiff", return_value=[("label", url)]) as download, \
             mock.patch.object(imagery, "_fetch_with_retry", return_value=payload) as fetch:
            self.service.run_timeseries_job(job["job_id"])
        result = self.store.get_job(job["job_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(load.call_args_list[0].args,
                         (self.scenes[0]["asset_id"], "dynamic_world", "vvvh"))
        self.assertEqual(download.call_args.args[1], "dynamic_world")
        self.assertEqual(download.call_args.args[3], 10)
        self.assertEqual(fetch.call_count, 2)
        self.assertFalse(list(self.store._job_dir(job["job_id"]).glob("*_bands_tmp")))
        with zipfile.ZipFile(self.store.download_path(job["job_id"])) as archive:
            self.assertNotIn(b"SECRET", archive.read("manifest.json"))

    def test_first_failed_scene_does_not_establish_grid(self):
        job, _ = self.run_job(fail_ids=[self.scenes[0]["asset_id"]])
        with zipfile.ZipFile(self.store.download_path(job["job_id"])) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["grid"]["width"], 2)
            self.assertEqual(manifest["grid"]["height"], 2)

    def test_store_returns_copy_of_progress_and_unknown_path_rejected(self):
        job = self.store.create_job(self.search())
        job["status"] = "completed"
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "queued")
        with self.assertRaises(ValueError):
            self.store._job_dir("../../outside")

    def test_concurrent_worker_claim_downloads_only_once(self):
        job = self.store.create_job(self.search([self.scenes[0]]))
        entered, release = threading.Event(), threading.Event()
        def load(*args):
            entered.set()
            release.wait()
            return object()
        def merge(urls, path, *, categorical):
            self.raster(path)
        with mock.patch.object(ts, "load_timeseries_scene", side_effect=load) as loader, \
             mock.patch.object(ts, "download_geotiff", return_value=[]), \
             mock.patch.object(ts, "merge_band_tiffs", side_effect=merge):
            with ThreadPoolExecutor(max_workers=2) as executor:
                first = executor.submit(self.service.run_timeseries_job, job["job_id"])
                try:
                    self.assertTrue(entered.wait(30))
                    second = executor.submit(self.service.run_timeseries_job, job["job_id"])
                    second.result(timeout=60)
                finally:
                    release.set()
                first.result(timeout=120)
            self.assertEqual(loader.call_count, 1)
        self.assertEqual(self.store.get_job(job["job_id"])["status"], "completed")

    def test_continuous_sources_keep_band_order_scale_and_s1_db(self):
        sources = {"sentinel2": ["B2", "B3", "B4", "B8", "B11", "B12"],
                   "landsat": ["SR_B2", "SR_B3", "SR_B4", "SR_B5", "SR_B6", "SR_B7"],
                   "nicfi": ["B", "G", "R", "N"], "s1gee": ["VV", "VH"]}
        for dataset, bands in sources.items():
            with self.subTest(dataset=dataset):
                parameters = dict(self.parameters, dataset=dataset, bands=bands,
                                  units="dB" if dataset == "s1gee" else "reflectance",
                                  nodata=-9999)
                search = self.store.create_search(parameters, self.scenes[:1])
                job = self.store.create_job(search["search_id"])
                value = -12.5 if dataset == "s1gee" else 0.25
                def merge(urls, path, *, categorical):
                    self.assertFalse(categorical)
                    with rasterio.open(path, "w", driver="GTiff", height=4, width=4,
                                       count=len(bands), dtype="float32", nodata=-9999,
                                       crs="EPSG:4326", transform=from_origin(0, 0.0004, 0.0001, 0.0001)) as dst:
                        dst.write(np.full((len(bands), 4, 4), value, dtype="float32"))
                with mock.patch.object(ts, "load_timeseries_scene", return_value=object()) as load, \
                     mock.patch.object(ts, "download_geotiff", return_value=[]), \
                     mock.patch.object(ts, "merge_band_tiffs", side_effect=merge):
                    self.service.run_timeseries_job(job["job_id"])
                load.assert_called_once_with(self.scenes[0]["asset_id"], dataset, "vvvh")
                with zipfile.ZipFile(self.store.download_path(job["job_id"])) as archive:
                    filename = next(n for n in archive.namelist() if n.endswith(".tif"))
                    with rasterio.MemoryFile(archive.read(filename)) as memory, memory.open() as src:
                        self.assertEqual(src.descriptions, tuple(bands))
                        self.assertTrue(np.all(src.read() == value))

    def test_mid_job_disk_failure_records_remaining_and_keeps_success(self):
        original = self.service._check_disk
        calls = 0
        def check(parameters, directory, *, archive=False):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ts.DiskSpaceError()
            return original(parameters, directory, archive=archive)
        with mock.patch.object(self.service, "_check_disk", side_effect=check):
            job, seen = self.run_job()
        self.assertEqual(job["status"], "failed")
        self.assertEqual((job["succeeded"], job["failed"], job["processed"]), (1, 1, 2))
        self.assertEqual(len(seen), 1)
        self.assertIsNone(self.store.download_path(job["job_id"]))
        directory = self.store._job_dir(job["job_id"])
        self.assertEqual(len(list(directory.glob("*.tif"))), 1)
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["scenes"]), 2)

    def test_filename_normalizes_dataset_and_asset_path(self):
        filename = ts._scene_filename("../../evil\\\\dataset", self.scenes[0])
        self.assertNotIn("/", filename)
        self.assertNotIn("\\\\", filename)
        self.assertNotIn("..", filename)
        self.assertNotEqual(filename, ts._scene_filename("../../evil\\\\dataset", self.scenes[1]))

    def test_band_count_mismatch_rejected(self):
        source = self.root / "source.tif"
        self.raster(source)
        with self.assertRaises(ValueError):
            ts.align_timeseries_raster(source, self.root / "aligned.tif", bands=["label", "other"])

    def test_continuous_alignment_preserves_missing_coverage(self):
        first, second = self.root / "first.tif", self.root / "second.tif"
        aligned_first, aligned_second = self.root / "aligned1.tif", self.root / "aligned2.tif"
        self.raster(first, categorical=False)
        self.raster(second, categorical=False, second=True)
        grid = ts.align_timeseries_raster(first, aligned_first)
        ts.align_timeseries_raster(second, aligned_second, grid)
        with rasterio.open(aligned_second) as src:
            self.assertEqual(src.descriptions, ("VV",))
            self.assertTrue(np.all(src.read(1)[2:, :] == -9999))
            self.assertEqual(src.dtypes, ("float32",))

    def test_projected_first_raster_grid_defaults_geographic(self):
        source, target = self.root / "projected.tif", self.root / "geographic.tif"
        self.raster(source, categorical=False, projected=True)
        grid = ts.align_timeseries_raster(source, target)
        self.assertEqual(grid["crs"], "EPSG:4326")
        with rasterio.open(target) as src:
            self.assertEqual(src.crs.to_epsg(), 4326)


if __name__ == "__main__":
    unittest.main()
