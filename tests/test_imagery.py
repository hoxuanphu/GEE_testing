"""Focused tests for Dynamic World categorical raster handling."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import imagery  # noqa: E402


class _Url:
    def url(self):
        return "https://example.test/dynamic-world.tif"

    def refresh(self):
        return self.url()


def _categorical_tiff_bytes() -> bytes:
    profile = {
        "driver": "GTiff",
        "height": 16,
        "width": 16,
        "count": 1,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": from_origin(107.0, 10.0, 0.0001, 0.0001),
        "nodata": 255,
    }
    with MemoryFile() as memory:
        with memory.open(**profile) as dst:
            dst.write(
                np.tile(
                    np.array([[0, 4], [255, 6]], dtype=np.uint8),
                    (8, 8),
                ),
                1,
            )
            dst.set_band_description(1, "label")
        return memory.read()


class TestDynamicWorldDownloadMask(unittest.TestCase):
    def test_expands_footprint_before_filling_nodata(self):
        from app.geo import Bbox

        image = mock.Mock()
        selected = mock.Mock()
        image.select.return_value = selected
        selected.unmask.return_value = selected
        selected.toUint8.return_value = selected
        url = mock.Mock()
        url.url.return_value = "https://example.test/dynamic-world.tif"

        with mock.patch.object(imagery, "_DownloadURL", return_value=url), \
             mock.patch.object(imagery.ee.Geometry, "Rectangle", return_value=object()):
            result = imagery.download_geotiff(
                image,
                "dynamic_world",
                Bbox(west=107.0, south=10.0, east=107.1, north=10.1),
                10.0,
            )

        self.assertEqual(len(result), 1)
        selected.unmask.assert_called_once_with(255, sameFootprint=False)


class TestDynamicWorldCategoricalMerge(unittest.TestCase):
    def test_merge_preserves_water_zero_and_nodata_255(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent) as tmp:
            output = Path(tmp) / "dynamic_world.tif"
            with mock.patch.object(
                imagery, "_fetch_with_retry", return_value=_categorical_tiff_bytes()
            ):
                imagery.merge_band_tiffs(
                    [("label", _Url())], output, categorical=True
                )

            with rasterio.open(output) as src:
                self.assertEqual(src.dtypes, ("uint8",))
                self.assertEqual(src.nodata, 255)
                np.testing.assert_array_equal(
                    src.read(1),
                    np.tile(
                        np.array([[0, 4], [255, 6]], dtype=np.uint8),
                        (8, 8),
                    ),
                )
                self.assertEqual(src.descriptions[0], "label")


class _FakeGeometry:
    def __init__(self, covers=True):
        self.covers = covers

    def contains(self, point, margin):
        return self.covers


class _FakeImage:
    def __init__(self, asset_id, properties=None, covers=True, operations=None):
        self.asset_id = asset_id
        self.properties = dict(properties or {})
        self.covers = covers
        self.operations = list(operations or [])

    def id(self):
        return self.asset_id

    def get(self, name):
        return self.properties.get(name)

    def propertyNames(self):
        return list(self.properties)

    def copyProperties(self, other, names):
        self.properties.update({name: other.properties[name] for name in names if name in other.properties})
        return self

    def set(self, name, value=None):
        self.properties.update(name if isinstance(name, dict) else {name: value})
        return self

    def geometry(self):
        return _FakeGeometry(self.covers)

    def toDictionary(self, names):
        return {name: self.properties[name] for name in names if name in self.properties}

    def __getattr__(self, name):
        def operation(*args):
            self.operations.append((name, args))
            return self
        return operation


class _FakeList:
    def __init__(self, values):
        self.values = values

    def map(self, function):
        return _FakeList([function(value) for value in self.values])

    def getInfo(self):
        return self.values


class _FakeCollection:
    def __init__(self, images, calls):
        self.images = images
        self.calls = calls

    def filterDate(self, start, end):
        self.calls.append(("filterDate", start, end))
        start_ms = imagery.dt.datetime.fromisoformat(start).replace(tzinfo=imagery.dt.timezone.utc).timestamp() * 1000
        end_ms = imagery.dt.datetime.fromisoformat(end).replace(tzinfo=imagery.dt.timezone.utc).timestamp() * 1000
        return _FakeCollection([image for image in self.images if start_ms <= image.get("system:time_start") < end_ms], self.calls)

    def filter(self, condition):
        self.calls.append(("filter", condition))
        op, key, value = condition
        return _FakeCollection([image for image in self.images if
                                (value in (image.get(key) or []) if op == "contains" else image.get(key) == value)], self.calls)

    def filterBounds(self, point):
        self.calls.append(("filterBounds", point))
        return self

    def map(self, function):
        return _FakeCollection([function(image) for image in self.images], self.calls)

    def select(self, bands):
        return self.map(lambda image: image.select(bands))

    def merge(self, collection):
        return _FakeCollection(self.images + collection.images, self.calls)

    def size(self):
        return len(self.images)

    def toList(self, size):
        return _FakeList(self.images[:size])


class TestTimeSeriesImagery(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.images = {}
        self.ee = mock.Mock()
        self.ee.ImageCollection.side_effect = lambda name: _FakeCollection(self.images.get(name, []), self.calls)
        self.ee.Image.side_effect = lambda value: value if isinstance(value, _FakeImage) else _FakeImage(value)
        self.ee.Dictionary.side_effect = lambda value: value
        self.ee.Filter.eq.side_effect = lambda key, value: ("eq", key, value)
        self.ee.Filter.listContains.side_effect = lambda key, value: ("contains", key, value)
        self.ee.Geometry.Point.side_effect = lambda point: point
        self.patch = mock.patch.object(imagery, "ee", self.ee)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def add(self, collection, name, timestamp="2025-01-01T12:00:00+00:00", covers=True, **properties):
        properties.update({"system:time_start": int(imagery.dt.datetime.fromisoformat(timestamp).timestamp() * 1000),
                           "system:index": name})
        image = _FakeImage(collection + "/" + name, properties, covers)
        self.images.setdefault(collection, []).append(image)
        return image

    def scenes(self, dataset, polarization="vvvh"):
        return imagery.list_timeseries_scenes(dataset, 107, 10, "2025-01-01", "2025-01-03", polarization)

    def test_optical_clouds_full_ids_sorting_dedup_and_center(self):
        for collection, dataset, cloud_key in [
            (imagery.S2_COLLECTION, "sentinel2", "CLOUDY_PIXEL_PERCENTAGE"),
            (imagery.L8_COLLECTION, "landsat", "CLOUD_COVER"),
        ]:
            with self.subTest(dataset=dataset):
                self.images = {}
                self.add(collection, "z", **{cloud_key: 100})
                self.add(collection, "a", **{cloud_key: 90})
                self.add(collection, "a", **{cloud_key: 90})
                self.add(collection, "early", "2025-01-01T01:00:00+00:00", **{cloud_key: 60})
                self.add(collection, "edge", covers=False)
                self.add(collection, "last", "2025-01-02T23:59:59+00:00")
                self.add(collection, "after", "2025-01-03T00:00:00+00:00")
                self.add(collection, "before", "2024-12-31T23:59:59+00:00")
                if dataset == "landsat":
                    self.add(imagery.L9_COLLECTION, "a", SPACECRAFT_ID="LANDSAT_9")
                result = self.scenes(dataset)
                expected = [collection + "/" + name for name in ("early", "a", "z")]
                if dataset == "landsat":
                    expected.append(imagery.L9_COLLECTION + "/a")
                expected.append(collection + "/last")
                self.assertEqual([row["asset_id"] for row in result], expected)
                self.assertEqual([row["cloud_pct"] for row in result[:3]], [60, 90, 100])
                self.assertEqual(result[-1]["timestamp"], "2025-01-02T23:59:59Z")
                self.assertEqual(result[-1]["date"], "2025-01-02")
                self.assertIsNone(result[-1]["cloud_pct"])
                self.assertTrue(all(row["dataset"] == dataset for row in result))
                self.ee.Filter.lt.assert_not_called()
                self.assertIn(("filterBounds", [107, 10]), self.calls)

    def test_timestamp_sorting_includes_fractional_seconds_and_negative_cloud_is_null(self):
        self.add(imagery.L8_COLLECTION, "fraction", "2025-01-01T12:00:00.001+00:00", CLOUD_COVER=-1)
        self.add(imagery.L8_COLLECTION, "whole", "2025-01-01T12:00:00+00:00", CLOUD_COVER=0)
        rows = self.scenes("landsat")
        self.assertEqual([row["asset_id"].split("/")[-1] for row in rows], ["whole", "fraction"])
        self.assertEqual(rows[1]["timestamp"], "2025-01-01T12:00:00.001000Z")
        self.assertEqual(rows[0]["cloud_pct"], 0.0)
        self.assertIsNone(rows[1]["cloud_pct"])

    def test_errors_are_not_silently_treated_as_empty_results(self):
        self.ee.ImageCollection.side_effect = RuntimeError("collection unavailable")
        with self.assertRaisesRegex(RuntimeError, "collection unavailable"):
            self.scenes("nicfi")

    def test_existing_single_scene_nicfi_scaling_keeps_original_properties_contract(self):
        image = self.add(imagery.NICFI_COLLECTION, "scene", cadence="monthly")
        imagery._nicfi_collection("2025-01-01", "2025-01-03")
        self.assertNotIn("timeseries:asset_id", image.properties)
        self.assertIn(("divide", (10000.0,)), image.operations)

    def test_nicfi_and_dynamic_world_metadata_and_empty(self):
        for collection, dataset, extra in [
            (imagery.NICFI_COLLECTION, "nicfi", {"cadence": "monthly"}),
            (imagery.DYNAMIC_WORLD_COLLECTION, "dynamic_world", {}),
        ]:
            with self.subTest(dataset=dataset):
                self.images = {}
                self.add(collection, "scene", **extra)
                row = self.scenes(dataset)[0]
                self.assertEqual(row["asset_id"], collection + "/scene")
                self.assertEqual(row["collection"], collection)
                self.assertEqual(row["source_metadata"], extra)
                self.assertIsNone(row["cloud_pct"])
                self.images = {}
                self.assertEqual(self.scenes(dataset), [])

    def test_s1_all_required_bands_iw_and_both_orbits(self):
        for polarization, bands in imagery.S1_POLARIZATIONS.items():
            with self.subTest(polarization=polarization):
                self.images = {}
                common = {"instrumentMode": "IW", "transmitterReceiverPolarisation": bands,
                          "relativeOrbitNumber_start": 42, "orbitNumber_start": 12345,
                          "platform_number": "A"}
                self.add(imagery.S1_COLLECTION, "asc", orbitProperties_pass="ASCENDING", **common)
                self.add(imagery.S1_COLLECTION, "desc", orbitProperties_pass="DESCENDING", **common)
                self.add(imagery.S1_COLLECTION, "wrong-mode", **dict(common, instrumentMode="EW"))
                self.add(imagery.S1_COLLECTION, "no-bands", **dict(common, transmitterReceiverPolarisation=[]))
                if len(bands) > 1:
                    self.add(imagery.S1_COLLECTION, "partial", **dict(common, transmitterReceiverPolarisation=bands[:1]))
                rows = self.scenes("s1gee", polarization)
                self.assertEqual([row["asset_id"].split("/")[-1] for row in rows], ["asc", "desc"])
                self.assertEqual(rows[0]["source_metadata"]["relativeOrbitNumber_start"], 42)
                self.assertEqual(rows[0]["source_metadata"]["orbitProperties_pass"], "ASCENDING")
                self.assertIsNone(rows[0]["cloud_pct"])
                for image in self.images[imagery.S1_COLLECTION][:2]:
                    self.assertIn(("clamp", (-30.0, 10.0)), image.operations)
                    self.assertNotIn("log10", [op[0] for op in image.operations])
                for band in bands:
                    self.assertIn(("filter", ("contains", "transmitterReceiverPolarisation", band)), self.calls)

    def test_exact_asset_loading_all_sources_and_scaling(self):
        cases = [
            ("sentinel2", imagery.S2_COLLECTION, ("divide", (10000.0,))),
            ("landsat", imagery.L8_COLLECTION, ("multiply", (0.0000275,))),
            ("landsat", imagery.L9_COLLECTION, ("add", (-0.2,))),
            ("nicfi", imagery.NICFI_COLLECTION, ("divide", (10000.0,))),
            ("dynamic_world", imagery.DYNAMIC_WORLD_COLLECTION, ("toUint8", ())),
            ("s1gee", imagery.S1_COLLECTION, ("clamp", (-30.0, 10.0))),
        ]
        for dataset, collection, operation in cases:
            with self.subTest(dataset=dataset, collection=collection):
                asset_id = collection + "/exact-scene"
                image = imagery.load_timeseries_scene(asset_id, dataset)
                self.ee.Image.assert_any_call(asset_id)
                self.assertEqual([call.args[0] for call in self.ee.Image.call_args_list
                                  if isinstance(call.args[0], str)][-1], asset_id)
                self.assertIn(operation, image.operations)
                self.assertIn(("select", (imagery.timeseries_source_metadata(dataset)["bands"],)), image.operations)
                self.assertFalse({"log10", "updateMask", "mosaic", "median"} & {op[0] for op in image.operations})
        self.ee.ImageCollection.assert_not_called()

    def test_source_metadata_scales_bands_and_polarizations(self):
        for dataset, scale, factor, count in [
            ("sentinel2", 10, 0.0001, 6), ("landsat", 30, 0.0000275, 6),
            ("nicfi", 4.77, 0.0001, 4), ("dynamic_world", 10, 1, 1),
        ]:
            metadata = imagery.timeseries_source_metadata(dataset)
            self.assertEqual(metadata["scale_m"], scale)
            self.assertEqual(metadata["value_scale"], factor)
            self.assertEqual(len(metadata["bands"]), count)
        self.assertEqual(imagery.timeseries_source_metadata("dynamic_world")["nodata"], 255)
        self.assertEqual(imagery.timeseries_source_metadata("nicfi")["composite"], "source-period")
        self.assertEqual(imagery.timeseries_source_metadata("landsat")["value_offset"], -0.2)
        for polarization, bands in imagery.S1_POLARIZATIONS.items():
            metadata = imagery.timeseries_source_metadata("s1gee", polarization)
            self.assertEqual(metadata["bands"], bands)
            self.assertEqual(metadata["units"], "dB")
            image = imagery.load_timeseries_scene(imagery.S1_COLLECTION + "/scene", "s1gee", polarization)
            self.assertEqual(image.operations[0], ("select", (bands,)))

    def test_invalid_dataset_polarization_and_mismatched_asset(self):
        for function in (imagery.timeseries_source_metadata, imagery.timeseries_collection):
            args = () if function == imagery.timeseries_source_metadata else ("2025-01-01", "2025-01-02")
            with self.assertRaises(ValueError):
                function("aws", *args)
            with self.assertRaises(ValueError):
                function("s1gee", *args, polarization="invalid")
        for asset_id in ["scene", "https://example.test/image", imagery.L8_COLLECTION + "/scene",
                         imagery.S2_COLLECTION + "/../scene", imagery.S2_COLLECTION + "/..",
                         imagery.S2_COLLECTION + "/", imagery.S2_COLLECTION + "/a\\b"]:
            with self.subTest(asset_id=asset_id), self.assertRaises(ValueError):
                imagery.load_timeseries_scene(asset_id, "sentinel2")
        self.ee.Image.assert_not_called()


if __name__ == "__main__":
    unittest.main()
