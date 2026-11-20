"""SQLite ROI immutability, exact union and activity lifetime tests."""
import importlib
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from tests.test_vector_import import polygon


class VectorStoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("app.vector_store"), "Immutable vector storage is not implemented")
        self.mod = importlib.import_module("app.vector_store")
        self.parser = importlib.import_module("app.vector_import")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.mod.VectorStore(self.root)

    def save(self, geoms):
        payload = self.parser.read_vector(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"name": f"feature {i}"}, "geometry": geom} for i, geom in enumerate(geoms)]}).encode(), "area.geojson")
        return self.store.save_import(payload)

    def select(self, item):
        return {"import_id": item["import_id"], "feature_ids": [f["id"] for f in item["feature_collection"]["features"]]}

    def test_persistence_opaque_ids_and_defensive_snapshots(self):
        item = self.save([polygon()])
        self.assertEqual(str(uuid.UUID(item["import_id"])), item["import_id"])
        item["feature_collection"]["features"][0]["geometry"]["coordinates"][0][0][0] = 99
        loaded = self.mod.VectorStore(self.root).get_import(item["import_id"])
        self.assertEqual(loaded["feature_collection"]["features"][0]["geometry"]["coordinates"][0][0], [0, 0])

    def test_union_preserves_hole_disjoint_parts_and_polygon_subset(self):
        p = polygon()
        p["coordinates"].append([[.5, .5], [.5, 1], [1, 1], [1, .5], [.5, .5]])
        first = self.save([p, {"type": "Point", "coordinates": [1, 1]}])
        second = self.save([polygon(5)])
        roi = self.store.create_roi([self.select(first), self.select(second)])
        self.assertEqual(roi["geometry"]["type"], "MultiPolygon")
        self.assertEqual(roi["bbox"], {"west": 0, "south": 0, "east": 7, "north": 2})
        self.assertEqual(sorted(len(p) for p in roi["geometry"]["coordinates"]), [1, 2])
        self.assertEqual(roi["excluded_count"], 1)
        self.assertEqual(len(roi["feature_refs"]), 2)
        self.assertEqual(str(uuid.UUID(roi["roi_id"])), roi["roi_id"])
        self.store.delete_import(first["import_id"])
        self.assertEqual(self.store.get_roi(roi["roi_id"]), roi)
        with self.assertRaises(self.parser.VectorImportError):
            self.store.get_import(first["import_id"])

    def test_overlapping_polygons_are_unioned(self):
        item = self.save([polygon(), polygon(1)])
        roi = self.store.create_roi([self.select(item)])
        from shapely.geometry import shape
        self.assertEqual(shape(roi["geometry"]).area, 6)

    def test_invalid_missing_ids_and_nonpolygon_only_rejected(self):
        item = self.save([{ "type": "Point", "coordinates": [1, 1]}])
        for selections in [[], [self.select(item)], [{"import_id": item["import_id"], "feature_ids": ["missing"]}], [{"import_id": "../bad", "feature_ids": ["a"]}], [{"import_id": str(uuid.uuid4()), "feature_ids": ["a"]}], [{"import_id": item["import_id"], "feature_ids": "bad"}]]:
            with self.subTest(selections=selections), self.assertRaises(self.parser.VectorImportError):
                self.store.create_roi(selections)
        for method in [self.store.get_import, self.store.get_roi]:
            with self.assertRaises(self.parser.VectorImportError) as ctx:
                method(str(uuid.uuid4()))
            self.assertEqual(ctx.exception.status, 404)

    def test_duplicate_selection_is_deduplicated(self):
        item = self.save([polygon(), {"type": "Point", "coordinates": [1, 1]}])
        sel = self.select(item)
        roi = self.store.create_roi([sel, sel])
        self.assertEqual(roi["excluded_count"], 1)
        self.assertEqual(len(roi["feature_refs"]), 1)
        self.assertEqual(len(roi["feature_refs"][0]["feature_ids"]), 1)

    def test_activity_ttl_touch_persists_but_expired_records_fail(self):
        base = time.time()
        with patch("app.vector_store.time.time", return_value=base):
            item = self.save([polygon()])
            roi = self.store.create_roi([self.select(item)])
        with patch("app.vector_store.time.time", return_value=base+23*3600):
            self.store.get_import(item["import_id"])
        with patch("app.vector_store.time.time", return_value=base+25*3600):
            self.store.get_import(item["import_id"])
            with self.assertRaises(self.parser.VectorImportError) as ctx:
                self.store.get_roi(roi["roi_id"])
            self.assertEqual(ctx.exception.status, 404)
        with patch("app.vector_store.time.time", return_value=base+50*3600):
            with self.assertRaises(self.parser.VectorImportError):
                self.mod.VectorStore(self.root).get_import(item["import_id"])

    def test_combined_roi_coordinate_limit(self):
        # Each imported feature is valid alone, but together exceeds the union budget.
        item = self.save([polygon(), polygon(4)])
        with patch("app.vector_store.MAX_COORDINATES", 9):
            with self.assertRaises(self.parser.VectorImportError):
                self.store.create_roi([self.select(item)])

    def test_union_output_coordinate_limit_and_roi_copy(self):
        diamond = {"type": "Polygon", "coordinates": [[[1, -.5], [2.5, 1], [1, 2.5], [-.5, 1], [1, -.5]]]}
        item = self.save([polygon(), diamond])
        # Ten input positions produce 17 union boundary positions at intersections.
        with patch("app.vector_store.MAX_COORDINATES", 10):
            with self.assertRaises(self.parser.VectorImportError):
                self.store.create_roi([self.select(item)])
        roi = self.store.create_roi([self.select(item)])
        snapshot = json.loads(json.dumps(roi))
        roi["name"] = "changed"
        roi["geometry"]["coordinates"][0][0][0] = 99
        self.assertEqual(self.mod.VectorStore(self.root).get_roi(snapshot["roi_id"]), snapshot)


if __name__ == "__main__":
    unittest.main()
