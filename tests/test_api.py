"""API tests cho endpoints CDSE Sentinel-1 (GRD cũ + SLC/pairs mới).

Mock tầng client (search_s1_slc / open_product / get_token) — không gọi
mạng thật, không cần credentials.
Chạy: .venv/Scripts/python.exe -m unittest discover tests
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app import main  # noqa: E402


class TestS3OnlyAPI(unittest.TestCase):
    def test_missing_keys_fail_before_network_or_password_auth(self):
        with mock.patch.object(main, "get_s3_credentials", side_effect=RuntimeError("S3 credentials missing")), \
                mock.patch.object(main, "get_token") as token, \
                mock.patch.object(main, "get_product_s3_path") as path:
            response = TestClient(main.app).get(
                "/api/s1/s3/download", params={"product_id": "12345678-1234-1234-1234-123456789abc"}
            )
        self.assertEqual(response.status_code, 400)
        token.assert_not_called()
        path.assert_not_called()

    def test_zip_uses_s3_keys_without_password_auth(self):
        import io
        import zipfile

        with mock.patch.object(main, "get_s3_credentials", return_value=("access", "secret")), \
                mock.patch.object(main, "get_token") as token, \
                mock.patch.object(main, "get_product_s3_path", return_value="/eodata/example.SAFE"), \
                mock.patch.object(main, "s3_list_product_files", return_value=[]):
            response = TestClient(main.app).get(
                "/api/s1/s3/download", params={"product_id": "12345678-1234-1234-1234-123456789abc"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(response.content)))
        token.assert_not_called()


class TestTimeSeriesAPI(unittest.TestCase):
    def setUp(self):
        import uuid
        self.root = Path(__file__).resolve().parent.parent / "downloads" / ("test-api-timeseries-" + str(uuid.uuid4()))
        self.store = main.TimeSeriesStore(self.root)
        self.service = main.TimeSeriesService(self.store)
        self.patches = [mock.patch.object(main, "_timeseries_store", self.store),
                        mock.patch.object(main, "_timeseries_service", self.service),
                        mock.patch.object(main, "_ensure_ee"),
                        mock.patch.object(main, "_resolve_selection", return_value=(object(), __import__("app.geo", fromlist=["Bbox"]).Bbox(106, 9, 108, 11), None))]
        for patch in self.patches:
            patch.start()
        self.client = TestClient(main.app)
        self.body = dict(dataset="sentinel2", lon=107, lat=10, radius_m=2000,
                         start_date="2026-01-01", end_date="2026-01-01")

    def tearDown(self):
        import shutil
        self.service.shutdown()
        self.client.close()
        for patch in reversed(self.patches):
            patch.stop()
        shutil.rmtree(self.root)

    def search(self, scenes):
        with mock.patch.object(main, "list_timeseries_scenes", return_value=scenes) as listing:
            response = self.client.post("/api/timeseries/search", json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(listing.call_args.args[4], "2026-01-02")
        return response.json()

    def test_snapshot_pagination_and_empty_job(self):
        scenes = [dict(asset_id=f"asset/{i}", timestamp="2026-01-01T23:59:59Z", date="2026-01-01", cloud_pct=100) for i in range(73)]
        data = self.search(scenes)
        self.assertEqual(data["total"], 73)
        self.assertEqual(len(data["scenes"]), 50)
        with mock.patch.object(main, "list_timeseries_scenes", side_effect=AssertionError("must not relist")):
            page = self.client.get(f"/api/timeseries/search/{data['search_id']}?offset=50&limit=50").json()
        self.assertEqual(len(page["scenes"]), 23)
        self.assertEqual(page["scenes"][0]["cloud_pct"], 100)
        empty = self.search([])
        self.assertEqual(self.client.post("/api/timeseries/jobs", json={"search_id": empty["search_id"]}).status_code, 400)

    def test_reject_invalid_and_untrusted_requests(self):
        for override in [dict(selection_mode="province"), dict(dataset="aws"), dict(lon=181), dict(lat=-91), dict(radius_m=0), dict(radius_m=45001), dict(lon=float("inf")), dict(start_date="20260101"), dict(start_date="2026-02-30"), dict(end_date="2025-01-01"), dict(polarization="invalid"), dict(asset_id="arbitrary")]:
            body = {**self.body, **override}
            if override.get("lon") == float("inf"):
                with self.assertRaises(ValueError):
                    main.TimeSeriesRequest(**body)
            else:
                self.assertEqual(self.client.post("/api/timeseries/search", json=body).status_code, 422, override)
        self.assertEqual(self.client.post("/api/timeseries/jobs", json={"search_id": "none", "url": "https://bad"}).status_code, 422)
        self.assertEqual(self.client.get("/api/timeseries/search/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/timeseries/search/missing?limit=51").status_code, 400)
        self.assertEqual(self.client.get("/api/timeseries/jobs/missing").status_code, 404)
        self.assertEqual(self.client.get("/api/timeseries/jobs/missing/download").status_code, 404)

    def test_job_idempotency_busy_status_and_download(self):
        scene = dict(asset_id="asset/a", timestamp="2026-01-01T00:00:00Z", date="2026-01-01")
        first, second = self.search([scene]), self.search([scene])
        with mock.patch.object(self.service, "run_timeseries_job"):
            job = self.client.post("/api/timeseries/jobs", json={"search_id": first["search_id"]}).json()
            duplicate = self.client.post("/api/timeseries/jobs", json={"search_id": first["search_id"]}).json()
            self.assertEqual(job["job_id"], duplicate["job_id"])
            self.assertEqual(self.client.post("/api/timeseries/jobs", json={"search_id": second["search_id"]}).status_code, 409)
        self.assertEqual(self.client.get(f"/api/timeseries/jobs/{job['job_id']}/download").status_code, 409)
        directory = self.store._job_dir(job["job_id"])
        directory.mkdir()
        (directory / "timeseries.zip").write_bytes(b"mock-zip")
        self.store._update_job(job["job_id"], status="partial", processed=1, succeeded=1)
        self.assertEqual(self.client.get(f"/api/timeseries/jobs/{job['job_id']}").json()["status"], "partial")
        self.assertEqual(self.client.get(f"/api/timeseries/jobs/{job['job_id']}/download").content, b"mock-zip")

    def test_lifespan_recovers_and_allows_repeated_startup(self):
        scene = dict(asset_id="asset/a", timestamp="2026-01-01T00:00:00Z", date="2026-01-01")
        data = self.search([scene])
        queued = self.store.create_job(data["search_id"])
        with TestClient(main.app) as client:
            self.assertEqual(client.get(f"/api/timeseries/jobs/{queued['job_id']}").json()["status"], "interrupted")
        with TestClient(main.app) as client:
            self.assertEqual(client.get("/api/health").status_code, 200)
        main._timeseries_service.shutdown()

    def test_collection_permission_failure_is_sanitized(self):
        with mock.patch.object(main, "list_timeseries_scenes", side_effect=RuntimeError("https://signed-secret")):
            response = self.client.post("/api/timeseries/search", json=self.body)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("signed-secret", response.text)


class _FakeResp:
    """Fake requests.Response cho iter_open_product (đã đọc chunk đầu)."""

    closed = False

    def iter_content(self, chunk_size=None):
        return iter([])

    def close(self):
        self.closed = True


def _open_ok(chunks=(b"zip",)):
    """mock open_product trả (resp, first_chunk) như thật."""
    first = chunks[0] if chunks else b""
    return mock.patch.object(
        main, "open_product", return_value=(_FakeResp(), first)
    )


class TestCompositeMethodValidation(unittest.TestCase):
    def _request(self, **overrides):
        values = {
            "selection_mode": "province",
            "province_id": "VN44",
            "dataset": "sentinel2",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
        }
        values.update(overrides)
        return main.SceneRequest(**values)

    def test_base_score_is_accepted(self):
        request = self._request(composite_method="base-score")
        self.assertEqual(request.composite_method, "base-score")

    def test_dynamic_world_is_accepted(self):
        request = self._request(dataset="dynamic_world")
        self.assertEqual(request.dataset, "dynamic_world")

    def test_unknown_composite_method_is_rejected(self):
        with self.assertRaises(ValidationError):
            self._request(composite_method="quality-mosaic")

    def test_dynamic_world_uses_native_scale(self):
        self.assertEqual(main._scale_for("dynamic_world"), 10.0)



class TestDynamicWorldSceneResponse(unittest.TestCase):
    def test_response_is_categorical_and_has_no_cloud_percentage(self):
        from app.geo import Bbox
        from app.imagery import SceneInfo

        request = main.SceneRequest(
            selection_mode="point",
            lon=107.0,
            lat=10.0,
            dataset="dynamic_world",
            start_date="2026-01-01",
            end_date="2026-01-31",
        )
        scene = SceneInfo(
            dataset="dynamic_world",
            collection="GOOGLE/DYNAMICWORLD/V1",
            scene_id="20260101T000000_20260101T000000_T48PVS",
            date="2026-01-01",
            cloud_pct=-1.0,
            ee_object=object(),
        )
        bbox = Bbox(west=106.9, south=9.9, east=107.1, north=10.1)
        with mock.patch.object(main, "_ensure_ee"), \
             mock.patch.object(main, "_resolve_selection", return_value=(object(), bbox, None)), \
             mock.patch.object(main, "best_scene", return_value=scene), \
             mock.patch.object(main, "preview_rgb", return_value="preview-url"):
            response = main.find_scene(request)

        self.assertEqual(response.dataset, "dynamic_world")
        self.assertEqual(response.bands, ["label"])
        self.assertEqual(response.scale_m, 10.0)
        self.assertEqual(response.cloud_pct, -1.0)
        self.assertIsNone(response.composite_method)



def _slc(pid: str, start: str, orbit=151, direction="DESCENDING") -> dict:
    return {
        "id": pid,
        "name": f"S1A_IW_SLC__1SDV_{pid}",
        "date": start[:10],
        "timestamp": start,
        "size_gb": 7.8,
        "online": True,
        "cog": False,
        "product_type": "IW_SLC__1S",
        "operational_mode": "IW",
        "relative_orbit": orbit,
        "track": orbit,
        "orbit_direction": direction,
        "pass": direction,
        "polarization": "VV",
        "slice_number": 5,
        "total_slices": 8,
    }


class TestS1SlcScenes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def _get(self, **params):
        base = {"lon": 107.0, "lat": 16.0, "start_date": "2026-01-01", "end_date": "2026-03-01"}
        base.update(params)
        return self.client.get("/api/s1/slc/scenes", params=base)

    def test_ok(self):
        with mock.patch.object(main, "search_s1_slc", return_value=[_slc("a", "2026-01-01T01:30:02.000Z")]):
            res = self._get()
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["product_type"], "SLC")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["products"][0]["track"], 151)

    def test_invalid_date_format(self):
        self.assertEqual(self._get(start_date="01-01-2026").status_code, 400)

    def test_start_after_end(self):
        self.assertEqual(self._get(start_date="2026-03-01", end_date="2026-01-01").status_code, 400)

    def test_lon_out_of_range(self):
        self.assertEqual(self._get(lon=200).status_code, 400)

    def test_limit_too_large(self):
        self.assertEqual(self._get(limit=500).status_code, 400)

    def test_catalog_error_502(self):
        with mock.patch.object(main, "search_s1_slc", side_effect=RuntimeError("boom")):
            self.assertEqual(self._get().status_code, 502)

    def test_no_products_404(self):
        with mock.patch.object(main, "search_s1_slc", return_value=[]):
            self.assertEqual(self._get().status_code, 404)


class TestS1Pairs(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def _get(self, **params):
        base = {"lon": 107.0, "lat": 16.0, "start_date": "2026-01-01", "end_date": "2026-03-01"}
        base.update(params)
        return self.client.get("/api/s1/pairs", params=base)

    def test_ok_pair(self):
        products = [
            _slc("a", "2026-01-01T01:30:02.000Z"),
            _slc("b", "2026-01-13T01:30:02.000Z"),
        ]
        with mock.patch.object(main, "search_s1_slc", return_value=products):
            res = self._get()
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["count"], 1)
        pair = data["pairs"][0]
        self.assertEqual(pair["master"]["id"], "a")
        self.assertEqual(pair["secondary"]["id"], "b")
        self.assertAlmostEqual(pair["temporal_separation_days"], 12.0, places=3)
        self.assertIsNone(pair["baseline"]["perpendicular_m"])
        self.assertFalse(pair["interferogram_ready"])

    def test_bad_days_range_400(self):
        self.assertEqual(self._get(min_days=30, max_days=10).status_code, 400)

    def test_bad_orbit_direction_400(self):
        self.assertEqual(self._get(orbit_direction="SIDEWAYS").status_code, 400)

    def test_max_pairs_bounds_400(self):
        self.assertEqual(self._get(max_pairs=0).status_code, 400)

    def test_incompatible_no_pairs_404(self):
        products = [
            _slc("a", "2026-01-01T01:30:02.000Z", orbit=151),
            _slc("b", "2026-01-13T01:30:02.000Z", orbit=99),
        ]
        with mock.patch.object(main, "search_s1_slc", return_value=products):
            self.assertEqual(self._get().status_code, 404)


class TestS1Download(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)

    def test_invalid_product_id_400(self):
        res = self.client.get("/api/s1/download", params={"product_id": "../etc/passwd"})
        self.assertEqual(res.status_code, 400)

    def test_filename_traversal_sanitized(self):
        with mock.patch.object(main, "get_token", return_value="tok"), \
             _open_ok():
            res = self.client.get(
                "/api/s1/download",
                params={"product_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                        "filename": "..\\..\\evil.zip"},
            )
        self.assertEqual(res.status_code, 200)
        disposition = res.headers["content-disposition"]
        self.assertIn("evil.zip", disposition)
        self.assertNotIn("..", disposition)

    def test_newline_in_filename_sanitized(self):
        with mock.patch.object(main, "get_token", return_value="tok"), \
             _open_ok():
            res = self.client.get(
                "/api/s1/download",
                params={"product_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                        "filename": "x.zip\r\nSet-Cookie: evil=1"},
            )
        self.assertEqual(res.status_code, 200)
        disposition = res.headers["content-disposition"]
        self.assertNotIn("\r", disposition)
        self.assertNotIn("\n", disposition)

    def test_missing_creds_400(self):
        with mock.patch.object(
            main, "get_token", side_effect=RuntimeError("CDSE credentials missing")
        ):
            res = self.client.get(
                "/api/s1/download",
                params={"product_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
            )
        self.assertEqual(res.status_code, 400)

    def test_proxy_block_error_502_not_empty_zip(self):
        # open_product nêu lỗi (vd proxy chặn >300MB) TRƯỚC khi header 200
        # được gửi — người dùng nhận 502 có thông điệp, không phải ZIP rỗng.
        with mock.patch.object(main, "get_token", return_value="tok"), \
             mock.patch.object(
                 main, "open_product",
                 side_effect=RuntimeError(
                     "Proxy công ty chặn file > 300 MB (Vingroup Web Gateway). "
                     "Hãy tải qua mạng khác (nhà/4G) hoặc liên hệ IT."
                 ),
             ):
            res = self.client.get(
                "/api/s1/download",
                params={"product_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
            )
        self.assertEqual(res.status_code, 502)
        self.assertIn("300 MB", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
