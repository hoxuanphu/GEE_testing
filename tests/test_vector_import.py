"""Bounded vector normalization using real in-memory vector fixtures."""
import importlib
import io
import json
import unittest
from unittest.mock import patch
import zipfile

import shapefile
from rasterio.crs import CRS


def polygon(x=0, y=0):
    return {"type": "Polygon", "coordinates": [[[x, y], [x+2, y], [x+2, y+2], [x, y+2], [x, y]]]}


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return out.getvalue()


class VectorImportTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("app.vector_import"), "Vector normalization API is not implemented")
        self.mod = importlib.import_module("app.vector_import")

    def read(self, obj, filename="layer.geojson"):
        return self.mod.read_vector(json.dumps(obj).encode(), filename)

    def test_geojson_collection_holes_disjoint_parts_and_duplicate_ids(self):
        hole = [[.5, .5], [.5, 1], [1, 1], [1, .5], [.5, .5]]
        p = polygon()
        p["coordinates"].append(hole)
        result = self.read({"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": "same", "properties": {"name": "<b>water</b>", "v": [1, "<&>"]}, "geometry": p},
            {"type": "Feature", "id": "same", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": [polygon(4)["coordinates"], polygon(8)["coordinates"]]}},
        ]})
        features = result["feature_collection"]["features"]
        self.assertEqual(len(features), 2)
        self.assertNotEqual(features[0]["id"], features[1]["id"])
        self.assertTrue(all(isinstance(f["id"], str) for f in features))
        self.assertEqual(features[0]["geometry"], p)
        self.assertEqual(features[0]["name"], "<b>water</b>")
        self.assertEqual(features[0]["properties"]["v"], [1, "<&>"])
        self.assertEqual(features[1]["geometry"]["type"], "MultiPolygon")

    def test_geometry_collections_flatten_and_null_is_warned(self):
        result = self.read({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "group"}, "geometry": {"type": "GeometryCollection", "geometries": [polygon(), {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [1, 1]}]}]}},
            {"type": "Feature", "properties": {}, "geometry": None}]})
        features = result["feature_collection"]["features"]
        self.assertEqual([f["geometry"]["type"] for f in features], ["Polygon", "Point"])
        self.assertTrue(result["warnings"])
        self.assertEqual([f["name"] for f in features], ["group", "group"])
        self.assertEqual(len({f["id"] for f in features}), 2)

    def test_geojson_feature_and_raw_geometry_are_accepted(self):
        for obj in [polygon(), {"type": "Feature", "properties": {}, "geometry": polygon()}]:
            self.assertEqual(len(self.read(obj)["feature_collection"]["features"]), 1)

    def test_kml_multi_geometry_properties_and_folder(self):
        data = b'''<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Folder><name>Survey</name><Placemark><name>Site</name><ExtendedData><Data name="category"><value>river</value></Data></ExtendedData><MultiGeometry><Point><coordinates>106,20,3</coordinates></Point><LineString><coordinates>106,20 107,21</coordinates></LineString><Polygon><outerBoundaryIs><LinearRing><coordinates>106,20 108,20 108,22 106,22 106,20</coordinates></LinearRing></outerBoundaryIs><innerBoundaryIs><LinearRing><coordinates>106.5,20.5 106.5,21 107,21 107,20.5 106.5,20.5</coordinates></LinearRing></innerBoundaryIs></Polygon></MultiGeometry></Placemark></Folder></Document></kml>'''
        features = self.mod.read_vector(data, "sites.kml")["feature_collection"]["features"]
        self.assertEqual([f["geometry"]["type"] for f in features], ["Point", "LineString", "Polygon"])
        self.assertTrue(all(f["layer"] == "Survey" for f in features))
        self.assertEqual(features[0]["properties"]["category"], "river")
        self.assertEqual(len(features[2]["geometry"]["coordinates"]), 2)

    def test_kmz_multiple_documents_and_network_links_not_loaded(self):
        kml = b'<kml><NetworkLink><Link><href>http://127.0.0.1:1/private</href></Link></NetworkLink><Placemark><Point><coordinates>1,2</coordinates></Point></Placemark></kml>'
        result = self.mod.read_vector(archive([("a.kml", kml), ("nested/b.kml", kml)]), "both.kmz")
        self.assertEqual(len(result["feature_collection"]["features"]), 2)
        self.assertTrue(result["warnings"])

    def test_gpx_waypoints_routes_and_multi_segment_tracks(self):
        data = b'<gpx xmlns="http://www.topografix.com/GPX/1/1"><wpt lat="20" lon="106"><name>W</name></wpt><rte><name>R</name><rtept lat="20" lon="106"/><rtept lat="21" lon="107"/></rte><trk><name>T</name><trkseg><trkpt lat="20" lon="106"/><trkpt lat="21" lon="107"/></trkseg><trkseg><trkpt lat="22" lon="108"/><trkpt lat="23" lon="109"/></trkseg></trk></gpx>'
        features = self.mod.read_vector(data, "track.gpx")["feature_collection"]["features"]
        self.assertEqual([f["geometry"]["type"] for f in features], ["Point", "LineString", "MultiLineString"])
        self.assertEqual([f["name"] for f in features], ["W", "R", "T"])

    def shape_entries(self, prefix="a", prj=True):
        shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
        with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
            writer.field("name", "C")
            writer.point(1113194.9079327357, 1118889.9748579594)
            writer.record("Projected")
        entries = [(prefix+".shp", shp.getvalue()), (prefix+".shx", shx.getvalue()), (prefix+".dbf", dbf.getvalue())]
        if prj:
            entries.append((prefix+".prj", CRS.from_epsg(3857).to_wkt()))
        return entries

    def test_shapefile_zip_multiple_sets_and_reprojection(self):
        result = self.mod.read_vector(archive(self.shape_entries()+self.shape_entries("folder/B")), "projected.zip")
        features = result["feature_collection"]["features"]
        self.assertEqual(len(features), 2)
        self.assertAlmostEqual(features[0]["geometry"]["coordinates"][0], 10, places=6)
        self.assertAlmostEqual(features[0]["geometry"]["coordinates"][1], 10, places=6)
        self.assertEqual({f["layer"] for f in features}, {"a", "folder/B"})

    def test_shapefile_missing_or_invalid_crs_rejected(self):
        for entries in [self.shape_entries(prj=False), self.shape_entries(prj=False)+[("a.prj", "bogus")]]:
            with self.assertRaises(self.mod.VectorImportError):
                self.mod.read_vector(archive(entries), "shape.zip")

    def test_shapefile_preserves_holes_and_rejects_orphan_hole_repair(self):
        outer = [[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]]  # Shapefile shells run clockwise.
        hole = [[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]
        other = [[8, 0], [8, 4], [12, 4], [12, 0], [8, 0]]
        orphan = [[20, 1], [21, 1], [21, 2], [20, 2], [20, 1]]

        def shape_zip(rings):
            shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
            with shapefile.Writer(shp=shp, shx=shx, dbf=dbf) as writer:
                writer.field("name", "C")
                writer.poly(rings)
                writer.record("Area")
            return archive([("a.shp", shp.getvalue()), ("a.shx", shx.getvalue()), ("a.dbf", dbf.getvalue()), ("a.prj", CRS.from_epsg(4326).to_wkt())])

        features = self.mod.read_vector(shape_zip([outer, hole, other]), "hole.zip")["feature_collection"]["features"]
        self.assertEqual(features[0]["geometry"]["type"], "MultiPolygon")
        self.assertEqual([len(p) for p in features[0]["geometry"]["coordinates"]], [2, 1])
        for rings in ([outer, other, orphan], [hole]):
            with self.subTest(rings=rings), self.assertRaises(self.mod.VectorImportError):
                self.mod.read_vector(shape_zip(rings), "invalid.zip")

    def test_invalid_topology_coordinates_crs_and_shapes_reject_atomically(self):
        invalid = [
            {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]},
            {"type": "Point", "coordinates": [float("nan"), 0]},
            {"type": "Point", "coordinates": [181, 0]},
            {"type": "Point", "coordinates": [1, 86]},
            {"type": "LineString", "coordinates": [[179, 0], [-179, 1]]},
            {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
            {"type": "LineString", "coordinates": [[1, 2]]},
            {"type": "Point", "coordinates": [True, 1]},
            {"type": "Point", "coordinates": [1, 2, float("inf")]},
        ]
        for geom in invalid:
            with self.subTest(geom=geom), self.assertRaises(self.mod.VectorImportError):
                self.read({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": polygon()}, {"type": "Feature", "properties": {}, "geometry": geom}]})
        with self.assertRaises(self.mod.VectorImportError):
            self.read({"type": "FeatureCollection", "crs": {"type": "name", "properties": {"name": "EPSG:3857"}}, "features": []})

    def test_empty_unsupported_corrupt_xml_and_entity_attack_rejected(self):
        samples = [(b"{}", "a.geojson"), (b"no", "a.zip"), (b"{}", "a.exe"), (b"<kml>", "a.kml"), (b'<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///secret">]><kml>&x;</kml>', "a.kml")]
        for data, name in samples:
            with self.subTest(name=name), self.assertRaises(self.mod.VectorImportError):
                self.mod.read_vector(data, name)

    def test_archive_traversal_duplicates_and_member_limit(self):
        for entries in [[("../evil.kml", "<kml/>")], [("/evil.kml", "<kml/>")], [("C:\\evil.kml", "<kml/>")], [("x.kml", "<kml/>"), ("X.kml", "<kml/>")], [(f"{i}.kml", "<kml/>") for i in range(101)]]:
            with self.assertRaises(self.mod.VectorImportError):
                self.mod.read_vector(archive(entries), "a.kmz")

    def test_file_feature_coordinate_and_depth_limits(self):
        with self.assertRaises(self.mod.VectorImportError):
            self.mod.read_vector(b" " * (20*1024*1024+1), "a.json")
        with self.assertRaises(self.mod.VectorImportError):
            self.read({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [0, 0]}}] * 10001})
        with self.assertRaises(self.mod.VectorImportError):
            self.read({"type": "MultiPoint", "coordinates": [[0, 0]] * 500001})
        geom = polygon()
        for _ in range(70):
            geom = {"type": "GeometryCollection", "geometries": [geom]}
        with self.assertRaises(self.mod.VectorImportError):
            self.read(geom)

    def test_nested_geojson_crs_cannot_override_wgs84(self):
        feature = {"type": "Feature", "properties": {}, "geometry": {"type": "Point", "coordinates": [10, 10]}}
        crs = {"type": "name", "properties": {"name": "EPSG:3857"}}
        for target in ("feature", "geometry"):
            obj = json.loads(json.dumps(feature))
            (obj if target == "feature" else obj["geometry"])["crs"] = crs
            with self.subTest(target=target), self.assertRaises(self.mod.VectorImportError):
                self.read({"type": "FeatureCollection", "features": [obj]})

    def test_encrypted_archive_and_expanded_data_limit(self):
        data = bytearray(archive([("a.kml", "<kml/>")]))
        # Set the encryption bit in both real ZIP headers.
        for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
            start = data.index(signature)
            data[start+offset] |= 1
        with self.assertRaises(self.mod.VectorImportError) as ctx:
            self.mod.read_vector(bytes(data), "encrypted.kmz")
        self.assertEqual(ctx.exception.code, "invalid_archive")
        with patch("app.vector_import.MAX_EXPANDED_BYTES", 100):
            with self.assertRaises(self.mod.VectorImportError) as ctx:
                self.mod.read_vector(archive([("a.kml", "x"*101)]), "expanded.kmz")
            self.assertEqual(ctx.exception.code, "vector_limit")

    def test_deep_xml_and_invalid_kml_reject_whole_file(self):
        deep = b"<kml>" + b"<Folder>"*70 + b"</Folder>"*70 + b"</kml>"
        with self.assertRaises(self.mod.VectorImportError):
            self.mod.read_vector(deep, "deep.kml")
        invalid = b'<kml><Placemark><Point><coordinates>1,2</coordinates></Point></Placemark><Placemark><Point><coordinates>NaN,2</coordinates></Point></Placemark></kml>'
        with self.assertRaises(self.mod.VectorImportError):
            self.mod.read_vector(invalid, "atomic.kml")

    def test_normalized_budget_bounds_inherited_and_ordinary_properties(self):
        point = {"type": "Point", "coordinates": [1, 2]}
        props = {"description": "x" * 600}
        collection = {"type": "Feature", "properties": props, "geometry": {
            "type": "GeometryCollection", "geometries": [point] * 8}}
        ordinary = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": props, "geometry": point} for _ in range(8)]}
        kml = ('<kml><Placemark><description>' + 'x' * 600 +
               '</description><MultiGeometry>' +
               '<Point><coordinates>1,2</coordinates></Point>' * 8 +
               '</MultiGeometry></Placemark></kml>').encode()
        fixtures = [(json.dumps(collection).encode(), "collection.geojson"),
                    (json.dumps(ordinary).encode(), "ordinary.geojson"), (kml, "collection.kml")]
        for data, filename in fixtures:
            with self.subTest(filename=filename):
                with patch.object(self.mod, "MAX_NORMALIZED_BYTES", 2_000, create=True):
                    with self.assertRaises(self.mod.VectorImportError) as ctx:
                        self.mod.read_vector(data, filename)
                self.assertEqual(ctx.exception.code, "vector_limit")
                self.assertIn("normalized", ctx.exception.message.lower())

    def test_normalized_budget_checks_oversized_properties_before_copy(self):
        # The valid input parse is allowed; a property round-trip must never start.
        data = json.dumps({"type": "Feature", "properties": {"description": "x" * 2_000},
                           "geometry": {"type": "Point", "coordinates": [1, 2]}}).encode()
        original_loads = json.loads
        with patch.object(self.mod, "MAX_NORMALIZED_BYTES", 1_200, create=True):
            with patch("app.vector_import.json.loads", wraps=original_loads) as loads:
                with self.assertRaises(self.mod.VectorImportError) as ctx:
                    self.mod.read_vector(data, "large.geojson")
                self.assertEqual(ctx.exception.code, "vector_limit")
                self.assertEqual(loads.call_count, 1)

    def test_normalized_budget_counts_escaped_bytes_and_accepts_bounded_children(self):
        point = {"type": "Point", "coordinates": [1, 2]}
        # Escaped JSON bytes exceed the allowance even though 100 characters fit.
        with patch.object(self.mod, "MAX_NORMALIZED_BYTES", 1_400):
            with self.assertRaises(self.mod.VectorImportError) as ctx:
                self.read({"type": "Feature", "properties": {"description": "\u2603" * 100}, "geometry": point})
            self.assertEqual(ctx.exception.code, "vector_limit")
        with patch.object(self.mod, "MAX_NORMALIZED_BYTES", 4_000):
            result = self.read({"type": "Feature", "properties": {"description": "x" * 600},
                                "geometry": {"type": "GeometryCollection", "geometries": [point, point]}})
        features = result["feature_collection"]["features"]
        self.assertEqual(len(features), 2)
        self.assertEqual(features[1]["properties"]["description"], "x" * 600)
        features[0]["properties"]["description"] = "changed"
        self.assertEqual(features[1]["properties"]["description"], "x" * 600)


if __name__ == "__main__":
    unittest.main()
