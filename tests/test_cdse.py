"""Unit tests cho app.cdse — OData query, normalization, pair matching.

Không gọi mạng thật: mock requests.get của module app.cdse.
Chạy: .venv/Scripts/python.exe -m unittest discover tests
"""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import cdse  # noqa: E402


def _slc_raw(
    pid: str,
    name: str,
    start: str,
    online: bool = True,
    length: int = 8_000_000_000,
    attributes: list | None = None,
) -> dict:
    if attributes is None:
        attributes = [
            {"@odata.type": "#OData.CSC.StringAttribute", "Name": "productType", "Value": "IW_SLC__1S"},
            {"@odata.type": "#OData.CSC.StringAttribute", "Name": "orbitDirection", "Value": "DESCENDING"},
            {"@odata.type": "#OData.CSC.IntegerAttribute", "Name": "relativeOrbitNumber", "Value": 151},
            {"@odata.type": "#OData.CSC.StringAttribute", "Name": "polarisationChannels", "Value": "VV"},
            {"@odata.type": "#OData.CSC.StringAttribute", "Name": "operationalMode", "Value": "IW"},
        ]
    return {
        "Id": pid,
        "Name": name,
        "ContentDate": {"Start": start},
        "ContentLength": length,
        "Online": online,
        "Attributes": attributes,
    }


def _slc_norm(pid: str, start: str, orbit=151, direction="DESCENDING",
              mode="IW", pol="VV") -> dict:
    return {
        "id": pid,
        "name": f"S1A_IW_SLC__1SDV_{pid}",
        "date": start[:10],
        "timestamp": start,
        "size_gb": 8.0,
        "online": True,
        "cog": False,
        "product_type": "IW_SLC__1S",
        "operational_mode": mode,
        "relative_orbit": orbit,
        "track": orbit,
        "orbit_direction": direction,
        "pass": direction,
        "polarization": pol,
        "slice_number": None,
        "total_slices": None,
    }


def _resp(payload: dict) -> mock.Mock:
    resp = mock.Mock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestParseIsoUtc(unittest.TestCase):
    def test_full_timestamp(self):
        ts = cdse._parse_iso_utc("2026-01-05T01:30:02.178000Z")
        self.assertEqual(ts, dt.datetime(2026, 1, 5, 1, 30, 2, 178000, tzinfo=dt.timezone.utc))

    def test_invalid(self):
        self.assertIsNone(cdse._parse_iso_utc("not-a-date"))
        self.assertIsNone(cdse._parse_iso_utc(None))
        self.assertIsNone(cdse._parse_iso_utc(""))


class TestSearchGrd(unittest.TestCase):
    def test_query_and_shape_backward_compatible(self):
        with mock.patch.object(cdse.requests, "get", return_value=_resp({"value": [
            {"Id": "g1", "Name": "S1A_IW_GRDH_1SDV_20260101", "ContentDate": {"Start": "2026-01-01T01:30:02.178Z"}, "ContentLength": 1_500_000_000, "Online": True},
        ]})) as get:
            products = cdse.search_s1(107.0, 16.0, "2026-01-01", "2026-01-31")

        params = get.call_args.kwargs["params"]
        self.assertIn("_IW_GRDH_1", params["$filter"])
        self.assertNotIn("_IW_SLC", params["$filter"])
        self.assertNotIn("$expand", params)
        self.assertEqual(params["$orderby"], "ContentDate/Start desc")
        self.assertEqual(get.call_args.args[0], cdse._CATALOG_URL)

        self.assertEqual(len(products), 1)
        # Đúng shape cũ — không thêm/bớt field
        self.assertEqual(set(products[0]), {"id", "name", "date", "size_gb", "online", "cog"})
        self.assertEqual(products[0]["id"], "g1")
        self.assertEqual(products[0]["date"], "2026-01-01")


class TestSearchSlc(unittest.TestCase):
    def test_query_expand_and_normalization(self):
        raw = _slc_raw("s1", "S1A_IW_SLC__1SDV_20260101T013002", "2026-01-01T01:30:02.178000Z")
        with mock.patch.object(cdse.requests, "get", return_value=_resp({"value": [raw]})) as get:
            products = cdse.search_s1_slc(107.0, 16.0, "2026-01-01", "2026-01-31")

        params = get.call_args.kwargs["params"]
        self.assertIn("_IW_SLC", params["$filter"])
        self.assertIn("Attributes", params["$expand"])
        self.assertEqual(len(products), 1)
        p = products[0]
        self.assertEqual(p["timestamp"], "2026-01-01T01:30:02.178000Z")
        self.assertEqual(p["product_type"], "IW_SLC__1S")
        self.assertEqual(p["relative_orbit"], 151)
        self.assertEqual(p["track"], 151)
        self.assertEqual(p["orbit_direction"], "DESCENDING")
        self.assertEqual(p["pass"], "DESCENDING")
        self.assertEqual(p["polarization"], "VV")
        self.assertEqual(p["operational_mode"], "IW")
        self.assertEqual(p["date"], "2026-01-01")
        self.assertIsNone(p["slice_number"])

    def test_missing_attributes_are_none(self):
        raw = _slc_raw("s2", "S1A_IW_SLC__1SDV_20260101", "2026-01-02T01:30:02.000Z", attributes=[])
        with mock.patch.object(cdse.requests, "get", return_value=_resp({"value": [raw]})):
            products = cdse.search_s1_slc(107.0, 16.0, "2026-01-01", "2026-01-31")
        p = products[0]
        self.assertIsNone(p["relative_orbit"])
        self.assertIsNone(p["track"])
        self.assertIsNone(p["orbit_direction"])
        self.assertIsNone(p["product_type"])

    def test_expand_fallback_without_attributes(self):
        raw = _slc_raw("s3", "S1A_IW_SLC__1SDV_20260101", "2026-01-03T01:30:02.000Z", attributes=[])
        fail = mock.Mock()
        fail.raise_for_status.side_effect = cdse.requests.HTTPError("400")
        ok = _resp({"value": [raw]})
        with mock.patch.object(cdse.requests, "get", side_effect=[fail, ok]) as get:
            products = cdse.search_s1_slc(107.0, 16.0, "2026-01-01", "2026-01-31")
        self.assertEqual(get.call_count, 2)
        self.assertIn("Attributes", get.call_args_list[0].kwargs["params"]["$expand"])
        self.assertNotIn("$expand", get.call_args_list[1].kwargs["params"])
        self.assertEqual(len(products), 1)
        self.assertIsNone(products[0]["relative_orbit"])

    def test_nextlink_dedup_and_cap(self):
        p1 = _slc_raw("a", "S1A_IW_SLC__1SDV_A", "2026-01-01T01:30:02.000Z")
        p1_dup = _slc_raw("a", "S1A_IW_SLC__1SDV_A", "2026-01-01T01:30:02.000Z")
        p2 = _slc_raw("b", "S1A_IW_SLC__1SDV_B", "2026-01-05T01:30:02.000Z")
        page1 = _resp({"value": [p1], "@odata.nextLink": "https://catalogue.example/next"})
        page2 = _resp({"value": [p1_dup, p2]})
        with mock.patch.object(cdse.requests, "get", side_effect=[page1, page2]) as get:
            products = cdse.search_s1_slc(107.0, 16.0, "2026-01-01", "2026-01-31", max_results=3)
        self.assertEqual([p["id"] for p in products], ["a", "b"])
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].args[0], "https://catalogue.example/next")
        self.assertIsNone(get.call_args_list[1].kwargs.get("params"))


class TestBuildPairs(unittest.TestCase):
    def test_same_track_pass_pairs(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = _slc_norm("b", "2026-01-13T01:30:02.000Z")  # 12 ngày
        pairs = cdse.build_pairs([b, a])
        self.assertEqual(len(pairs), 1)
        pair = pairs[0]
        self.assertEqual(pair["master"]["id"], "a")
        self.assertEqual(pair["secondary"]["id"], "b")
        self.assertAlmostEqual(pair["temporal_separation_days"], 12.0, places=3)
        self.assertEqual(pair["relative_orbit"], 151)
        self.assertEqual(pair["orbit_direction"], "DESCENDING")
        self.assertEqual(pair["polarization"], "VV")
        self.assertIsNone(pair["baseline"]["perpendicular_m"])
        self.assertFalse(pair["baseline"]["available"])
        self.assertFalse(pair["interferogram_ready"])

    def test_different_track_or_pass_excluded(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = _slc_norm("b", "2026-01-13T01:30:02.000Z", orbit=152)
        c = _slc_norm("c", "2026-01-13T01:30:02.000Z", direction="ASCENDING")
        self.assertEqual(cdse.build_pairs([a, b, c]), [])

    def test_missing_metadata_excluded(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = dict(_slc_norm("b", "2026-01-13T01:30:02.000Z"), relative_orbit=None, track=None)
        c = dict(_slc_norm("c", "2026-01-25T01:30:02.000Z"), orbit_direction=None, pass_=None)
        self.assertEqual(cdse.build_pairs([a, b, c]), [])

    def test_mode_polarization_mismatch_excluded(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = _slc_norm("b", "2026-01-13T01:30:02.000Z", mode="EW")
        c = _slc_norm("c", "2026-01-25T01:30:02.000Z", pol="HH")
        self.assertEqual(cdse.build_pairs([a, b, c]), [])

    def test_one_side_missing_mode_still_pairs(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = dict(_slc_norm("b", "2026-01-13T01:30:02.000Z"), operational_mode=None)
        pairs = cdse.build_pairs([a, b])
        self.assertEqual(len(pairs), 1)

    def test_temporal_bounds(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = _slc_norm("b", "2026-01-04T01:30:02.000Z")  # 3 ngày — dưới min 6
        c = _slc_norm("c", "2026-03-01T01:30:02.000Z")  # 59 ngày — trên max 48
        d = _slc_norm("d", "2026-02-01T01:30:02.000Z")  # 31 ngày — hợp lệ
        # Quá ngắn và quá dài đều bị loại, chỉ còn cặp trong khoảng [6, 48] ngày
        self.assertEqual(cdse.build_pairs([a, b]), [])
        self.assertEqual(cdse.build_pairs([a, c]), [])
        pairs = cdse.build_pairs([a, d])
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["master"]["id"], "a")
        self.assertEqual(pairs[0]["secondary"]["id"], "d")
        self.assertAlmostEqual(pairs[0]["temporal_separation_days"], 31.0, places=3)

    def test_track_and_direction_filters(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z", orbit=151, direction="DESCENDING")
        b = _slc_norm("b", "2026-01-13T01:30:02.000Z", orbit=151, direction="DESCENDING")
        c = _slc_norm("c", "2026-01-13T01:30:02.000Z", orbit=99, direction="ASCENDING")
        d = _slc_norm("d", "2026-01-25T01:30:02.000Z", orbit=99, direction="ASCENDING")
        e = _slc_norm("e", "2026-04-30T01:30:02.000Z", orbit=99, direction="ASCENDING")
        # Lọc track=99 + ASCENDING: chỉ c-d (12 ngày) nằm trong khoảng,
        # c-e/d-e quá 48 ngày; a/b (track 151, DESCENDING) bị loại
        pairs = cdse.build_pairs([a, b, c, d, e], track=99, orbit_direction="ASCENDING")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["relative_orbit"], 99)
        self.assertEqual(pairs[0]["orbit_direction"], "ASCENDING")
        self.assertEqual(pairs[0]["master"]["id"], "c")
        self.assertEqual(pairs[0]["secondary"]["id"], "d")

    def test_max_pairs_and_sort(self):
        products = [_slc_norm(f"p{i}", f"2026-01-{i:02d}T01:30:02.000Z") for i in range(1, 13)]
        pairs = cdse.build_pairs(products, max_pairs=3)
        self.assertEqual(len(pairs), 3)
        # Sắp theo temporal separation tăng dần: các cặp liên tiếp (1 ngày)
        self.assertLessEqual(
            pairs[0]["temporal_separation_days"], pairs[1]["temporal_separation_days"]
        )

    def test_sorted_by_temporal_separation(self):
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        b = _slc_norm("b", "2026-01-05T01:30:02.000Z")
        c = _slc_norm("c", "2026-01-13T01:30:02.000Z")
        # Mọi tổ hợp trong khoảng: a-b=4, b-c=8, a-c=12 — sắp tăng dần
        pairs = cdse.build_pairs([a, b, c], min_days=1, max_days=30)
        self.assertEqual([p["temporal_separation_days"] for p in pairs], [4.0, 8.0, 12.0])

    def test_master_is_earlier_regardless_of_input_order(self):
        b = _slc_norm("b", "2026-01-13T01:30:02.000Z")
        a = _slc_norm("a", "2026-01-01T01:30:02.000Z")
        pairs = cdse.build_pairs([b, a])
        self.assertEqual(pairs[0]["master"]["id"], "a")
        self.assertEqual(pairs[0]["secondary"]["id"], "b")


if __name__ == "__main__":
    unittest.main()
