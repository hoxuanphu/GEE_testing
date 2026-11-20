"""Offline bounded annual catalogue and actual GeoTIFF transport contracts."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import ee
import numpy as np
import rasterio
from affine import Affine
from ee.apitestcase import ApiTestCase
from google.auth.exceptions import RefreshError
from rasterio.io import MemoryFile


def transport():
    from app import timeseries_transport
    return timeseries_transport


PARAMS = {"dataset": "sentinel2", "bands": ["B2", "B3"], "scale_m": 10,
          "polarization": "vvvh", "platform": None, "selection_mode": "point",
          "lon": 105, "lat": 20, "rotation_deg": 0,
          "bbox": {"west": 105, "south": 20, "east": 105.001, "north": 20.001},
          "roi_geometry": {"type": "Polygon", "coordinates": [
              [[105, 20], [105.001, 20], [105.001, 20.001], [105, 20.001], [105, 20]]]}}


def row(name="a", day="2024-02-29"):
    return {"asset_id": "COPERNICUS/S2_SR_HARMONIZED/" + name,
            "collection": "COPERNICUS/S2_SR_HARMONIZED", "timestamp_ms":
            datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000,
            "source_metadata": {"MGRS_TILE": "48PXS"}, "cloud_pct": 0}


class CatalogueTests(unittest.TestCase):
    def test_module_is_available(self):
        self.assertIsNotNone(importlib.util.find_spec("app.timeseries_transport"))

    def test_monthly_windows_include_leap_day_and_final_day(self):
        t = transport()
        with mock.patch.object(t, "_metadata_window", return_value=[]) as window:
            self.assertEqual(t.list_year_scenes(PARAMS, "2024-01-31", "2024-03-01"), [])
        self.assertEqual([(c.args[1], c.args[2]) for c in window.call_args_list],
                         [("2024-01-31", "2024-02-01"), ("2024-02-01", "2024-03-01"),
                          ("2024-03-01", "2024-03-02")])

    def test_timeout_splits_without_publishing_partial_results(self):
        t = transport()
        calls = []
        def window(p, start, end, **kw):
            calls.append((start, end))
            if (start, end) == ("2024-02-28", "2024-03-01"):
                raise t.BatchTransportError("timeout", "safe")
            return [row(start, start)]
        with mock.patch.object(t, "_metadata_window", side_effect=window):
            result = t.list_year_scenes(PARAMS, "2024-02-28", "2024-02-29")
        self.assertEqual([s["date"] for s in result], ["2024-02-28", "2024-02-29"])
        self.assertEqual(calls, [("2024-02-28", "2024-03-01"),
                                 ("2024-02-28", "2024-02-29"), ("2024-02-29", "2024-03-01")])
        with mock.patch.object(t, "_metadata_window", side_effect=t.BatchTransportError("window_cap", "safe")):
            with self.assertRaises(t.BatchTransportError) as caught:
                t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29")
        self.assertEqual(caught.exception.code, "incomplete_year")

    def test_deduplicates_native_ids_and_rejects_year_cap(self):
        t = transport()
        with mock.patch.object(t, "_metadata_window", return_value=[row(), row()]):
            result = t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["asset_id"], "COPERNICUS/S2_SR_HARMONIZED/a")
        self.assertEqual(result[0]["name"], "a")
        self.assertEqual(result[0]["source_metadata"], {"MGRS_TILE": "48PXS"})
        with mock.patch.object(t, "MAX_SCENES", 1), mock.patch.object(t, "_metadata_window", return_value=[row("a"), row("b")]):
            with self.assertRaises(t.BatchTransportError) as caught:
                t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29")
        self.assertEqual(caught.exception.code, "year_cap")

    def test_pause_exception_propagates_unchanged(self):
        t = transport()
        error = RuntimeError("pause identity")
        def check():
            raise error
        with self.assertRaises(RuntimeError) as caught:
            t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29", check=check)
        self.assertIs(caught.exception, error)

    def test_raster_pixel_limit_rejected_before_initializing_rpc(self):
        t = transport()
        huge = {**PARAMS, "bbox": {"west": 100, "south": 10, "east": 110, "north": 30}}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(t, "_RPC") as rpc:
            with self.assertRaises(t.BatchTransportError) as caught:
                t.export_batch_scene(huge, row(), Path(folder) / "out.tif")
        self.assertEqual(caught.exception.code, "scene_limit")
        rpc.assert_not_called()

    def test_ee_initialization_exception_stops_catalogue_and_raster_fatally(self):
        t = transport()
        with tempfile.TemporaryDirectory() as folder:
            operations = [lambda: t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29"),
                          lambda: t.export_batch_scene(PARAMS, row(), Path(folder) / "out.tif")]
            for operation in operations:
                with self.subTest(operation=operation), \
                        mock.patch("app.frame_search.init_ee", side_effect=ee.EEException("secret credentials/project")), \
                        mock.patch.object(t, "_metadata_graph") as graph:
                    with self.assertRaises(t.BatchTransportError) as caught:
                        operation()
                    self.assertTrue(caught.exception.fatal)
                    self.assertEqual(caught.exception.code, "initialization_failed")
                    self.assertNotIn("secret", str(caught.exception))
                    graph.assert_not_called()
            self.assertEqual(list(Path(folder).iterdir()), [])


class GraphAndRasterTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, "_install_cloud_api_resource", install)

    def test_native_metadata_graph_full_ids_point_and_vector_rules(self):
        t = transport()
        graph = json.dumps(ee.serializer.encode(t._metadata_graph(PARAMS, "2024-02-01", "2024-03-01")))
        self.assertIn("Geometry.contains", graph)
        self.assertIn("COPERNICUS/S2_SR_HARMONIZED/", graph)
        self.assertIn("Element.toDictionary", graph)
        self.assertNotIn("Image.multiply", graph)
        self.assertNotIn("Image.clamp", graph)
        self.assertNotIn("Image.select", graph)
        vector = {**PARAMS, "selection_mode": "vector"}
        graph = json.dumps(ee.serializer.encode(t._metadata_graph(vector, "2024-02-01", "2024-03-01")))
        self.assertNotIn("Geometry.contains", graph)
        self.assertIn("105.001", graph)
        for dataset, prefix in (("landsat", "LANDSAT/LC09/C02/T1_L2/"),
                                ("nicfi", "projects/planet-nicfi/assets/basemaps/asia/"),
                                ("dynamic_world", "GOOGLE/DYNAMICWORLD/V1/")):
            graph = json.dumps(ee.serializer.encode(t._metadata_graph({**PARAMS, "dataset": dataset}, "2024-02-01", "2024-03-01")))
            self.assertIn(prefix, graph)

    def test_window_retries_share_deadline_and_auth_fails_fatally(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        rpc.request_once.side_effect = [TimeoutError("secret"), TimeoutError("secret"), b'{"result":{"rows":[],"count":0}}']
        with mock.patch.object(t, "_RPC", return_value=rpc), mock.patch.object(t.time, "sleep"):
            self.assertEqual(t._metadata_window(PARAMS, "2024-02-01", "2024-03-01"), [])
        self.assertEqual(rpc.request_once.call_count, 3)
        deadlines = [c.kwargs["deadline"] for c in rpc.request_once.call_args_list]
        self.assertEqual(len(set(deadlines)), 1)
        rpc.request_once.side_effect = t.FrameUpstreamError("upstream_unauthenticated", "secret", http_status=401)
        with mock.patch.object(t, "_RPC", return_value=rpc):
            with self.assertRaises(t.BatchTransportError) as caught:
                t._metadata_window(PARAMS, "2024-02-01", "2024-03-01")
        self.assertTrue(caught.exception.fatal)
        self.assertNotIn("secret", str(caught.exception))

    def test_late_window_result_rejected(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        rpc.request_once.return_value = b'{"result":{"rows":[],"count":0}}'
        with mock.patch.object(t, "_RPC", return_value=rpc), mock.patch.object(t.time, "monotonic", side_effect=[0, 0, 121]):
            with self.assertRaises(t.BatchTransportError) as caught:
                t._metadata_window(PARAMS, "2024-02-01", "2024-03-01")
        self.assertEqual(caught.exception.code, "timeout")

    def test_pause_value_error_identity_survives_rpc_scope(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        error = ValueError("pause")
        checks = 0
        def check():
            nonlocal checks
            checks += 1
            if checks == 2:
                raise error
        with mock.patch.object(t, "_RPC", return_value=rpc):
            with self.assertRaises(ValueError) as caught:
                t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29", check=check)
        self.assertIs(caught.exception, error)

    def test_credential_refresh_errors_are_safe_and_fatal(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        rpc.request_once.side_effect = RefreshError("secret credentials")
        with mock.patch.object(t, "_RPC", return_value=rpc):
            with self.assertRaises(t.BatchTransportError) as caught:
                t._metadata_window(PARAMS, "2024-02-01", "2024-03-01")
        self.assertTrue(caught.exception.fatal)
        self.assertNotIn("secret", str(caught.exception))

    def test_ee_graph_exception_after_initialization_is_nonfatal(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        with mock.patch.object(t, "_RPC", return_value=rpc), \
                mock.patch.object(t, "_metadata_graph", side_effect=ee.EEException("secret graph")):
            with self.assertRaises(t.BatchTransportError) as caught:
                t.list_year_scenes(PARAMS, "2024-02-29", "2024-02-29")
        self.assertFalse(caught.exception.fatal)
        self.assertNotIn("secret", str(caught.exception))

    def test_raster_wrong_schema_keeps_existing_file_and_cleans_temporary(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        def thumbnail(image, params, *, deadline):
            with MemoryFile() as memory:
                with memory.open(driver="GTiff", width=params["dimensions"][0], height=params["dimensions"][1],
                                 count=1, dtype="float32", crs=params["crs"],
                                 transform=Affine(*params["crs_transform"])) as dst:
                    dst.write(np.zeros((1, dst.height, dst.width), dtype="float32"))
                return memory.read()
        rpc.thumbnail_once.side_effect = thumbnail
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(t, "_RPC", return_value=rpc), \
                mock.patch.object(t.imagery, "load_timeseries_scene", return_value=ee.Image.constant([0, 0])):
            output = Path(folder) / "raw.tif"
            output.write_bytes(b"previous verified raster")
            with self.assertRaises(t.BatchTransportError) as caught:
                t.export_batch_scene(PARAMS, row(), output)
            self.assertEqual(caught.exception.code, "tile_schema")
            self.assertEqual(output.read_bytes(), b"previous verified raster")
            self.assertEqual([p.name for p in Path(folder).iterdir()], ["raw.tif"])

    def test_raster_pause_cleans_partial_and_preserves_exception(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        calls = 0
        error = OSError("pause identity")
        def check():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(t, "_RPC", return_value=rpc), \
                mock.patch.object(t.imagery, "load_timeseries_scene", return_value=ee.Image.constant([0, 0])):
            with self.assertRaises(OSError) as caught:
                t.export_batch_scene(PARAMS, row(), Path(folder) / "raw.tif", check=check)
            self.assertIs(caught.exception, error)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_raster_timeout_retries_are_bounded_with_one_operation_deadline(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        rpc.thumbnail_once.side_effect = TimeoutError("secret request")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(t, "_RPC", return_value=rpc), \
                mock.patch.object(t.imagery, "load_timeseries_scene", return_value=ee.Image.constant([0, 0])), \
                mock.patch.object(t.time, "sleep"):
            with self.assertRaises(t.BatchTransportError) as caught:
                t.export_batch_scene(PARAMS, row(), Path(folder) / "raw.tif")
            self.assertEqual(list(Path(folder).iterdir()), [])
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(rpc.thumbnail_once.call_count, 3)
        self.assertEqual(len({c.kwargs["deadline"] for c in rpc.thumbnail_once.call_args_list}), 1)

    def test_wire_graph_casts_to_float_after_unmask(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        wire_graphs = []
        def thumbnail(image, params, *, deadline):
            wire_graphs.append(ee.serializer.encode(image, for_cloud_api=True))
            raise TimeoutError()
        rpc.thumbnail_once.side_effect = thumbnail
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(t, "_RPC", return_value=rpc), \
                mock.patch.object(t.imagery, "load_timeseries_scene", return_value=ee.Image.constant([0, 0])), \
                mock.patch.object(t.time, "sleep"):
            with self.assertRaises(t.BatchTransportError):
                t.export_batch_scene(PARAMS, row(), Path(folder) / "raw.tif")
        self.assertTrue(wire_graphs)
        # A Double-valued fill constant promotes unmask's output. The final
        # graph operation sent to the thumbnail transport must restore Float32.
        graph = wire_graphs[0]
        root = graph["values"][graph["result"]]["functionInvocationValue"]
        self.assertEqual(root["functionName"], "Image.toFloat")
        unmask = root["arguments"]["value"]
        if "valueReference" in unmask:
            unmask = graph["values"][unmask["valueReference"]]
        self.assertEqual(unmask["functionInvocationValue"]["functionName"], "Image.unmask")

    def test_actual_tile_assembly_keeps_zero_and_independent_band_nodata(self):
        t = transport()
        rpc = mock.MagicMock()
        rpc.__enter__.return_value = rpc
        requests = []
        def thumbnail(image, params, *, deadline):
            requests.append(params)
            width, height = params["dimensions"]
            data = np.zeros((2, height, width), dtype="float32")
            data[0, 0, 0] = t.WIRE_NODATA
            data[1, -1, -1] = 7
            with MemoryFile() as memory:
                with memory.open(driver="GTiff", width=width, height=height, count=2,
                                 crs=params["crs"], transform=Affine(*params["crs_transform"]), dtype="float32") as dst:
                    dst.write(data)
                    dst.descriptions = ("B2", "B3")
                return memory.read()
        rpc.thumbnail_once.side_effect = thumbnail
        real_tiles = t.iter_grid_tiles
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(t, "_RPC", return_value=rpc), \
                mock.patch.object(t.imagery, "load_timeseries_scene", return_value=ee.Image.constant([0, 0]).rename(["B2", "B3"])), \
                mock.patch.object(t, "iter_grid_tiles", side_effect=lambda g, n, b: real_tiles(g, n, 64)):
            output = Path(folder) / "raw.tif"
            t.export_batch_scene(PARAMS, row(), output)
            with rasterio.open(output) as src:
                data = src.read(masked=True)
                self.assertEqual(src.descriptions, ("B2", "B3"))
                self.assertTrue(data.mask[0, 0, 0])
                self.assertFalse(data.mask[1, 0, 0])
                self.assertEqual(data[1, 0, 0], 0)
                self.assertFalse(list(Path(folder).glob("*.part*")))
        self.assertGreater(len(requests), 1)
        self.assertTrue(all(p["dimensions"][0] * p["dimensions"][1] * 8 <= 64 for p in requests))


if __name__ == "__main__":
    unittest.main()
