"""Normalize bounded, untrusted vector uploads without network or file extraction.

All coordinates returned here are WGS84 longitude/latitude. Invalid input rejects
the entire upload; topology is checked, never repaired.
"""
from __future__ import annotations

import io
import json
import math
from pathlib import PurePosixPath
import stat
import zipfile

from defusedxml import ElementTree as SafeET
from rasterio.crs import CRS
from rasterio.warp import transform
import shapefile
from shapely.geometry import shape
from shapely.validation import explain_validity

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 100
MAX_EXPANDED_BYTES = 80 * 1024 * 1024
MAX_NORMALIZED_BYTES = 80 * 1024 * 1024
MAX_FEATURES = 10_000
MAX_COORDINATES = 500_000
MAX_DEPTH = 64
MAX_LATITUDE = 85.0511287798066
_GEOMETRIES = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon", "GeometryCollection"}


class VectorImportError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _fail(message, code="invalid_vector", status=422):
    raise VectorImportError(code, message, status)


def _json_tree(value, depth=0):
    """Validate JSON values and depth, including otherwise unused properties."""
    if depth > MAX_DEPTH:
        _fail("Vector nesting exceeds the supported limit.", "vector_limit")
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            _fail("Property names must be strings.")
        for child in value.values():
            _json_tree(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _json_tree(child, depth + 1)
    elif isinstance(value, float) and not math.isfinite(value):
        _fail("Vector values must be finite.", "invalid_coordinates")
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        _fail("Properties must contain JSON values.")


def _bounded_json_size(value, limit):
    """Count conservative ASCII JSON bytes without constructing a large encoding.

    Strings are encoded in small chunks, so even a single oversized property is
    rejected before its escaped serialization or deep copy is allocated.
    """
    used = 0

    def charge(size):
        nonlocal used
        used += size
        if used > limit:
            _fail("Normalized vector output exceeds the 80 MiB budget.", "vector_limit")

    def walk(item):
        if isinstance(item, str):
            charge(2)
            # ASCII JSON uses at least one byte per input character.
            if len(item) > limit - used:
                _fail("Normalized vector output exceeds the 80 MiB budget.", "vector_limit")
            for start in range(0, len(item), 4096):
                charge(len(json.dumps(item[start:start+4096], ensure_ascii=True)) - 2)
        elif isinstance(item, dict):
            charge(2)
            for index, (key, child) in enumerate(item.items()):
                if index:
                    charge(2)
                walk(key)
                charge(2)
                walk(child)
        elif isinstance(item, (list, tuple)):
            charge(2)
            for index, child in enumerate(item):
                if index:
                    charge(2)
                walk(child)
        else:
            charge(len(json.dumps(item, allow_nan=False)))

    walk(value)
    return used


def _position(value):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        _fail("Each coordinate position must have two or three numbers.", "invalid_coordinates")
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in value):
        _fail("Coordinates must contain finite numbers.", "invalid_coordinates")
    if not (-180 <= value[0] <= 180 and -MAX_LATITUDE <= value[1] <= MAX_LATITUDE):
        _fail("Coordinates exceed the supported WGS84 map extent.", "invalid_coordinates")


def _sequence(value, minimum=1, ring=False):
    if not isinstance(value, (list, tuple)) or len(value) < minimum:
        _fail("Geometry has too few coordinate positions.", "invalid_geometry")
    for pos in value:
        _position(pos)
    if ring and value[0] != value[-1]:
        _fail("Polygon rings must be explicitly closed.", "invalid_geometry")
    if any(abs(a[0] - b[0]) > 180 for a, b in zip(value, value[1:])):
        _fail("Geometry crossing the antimeridian is unsupported.", "unsupported_antimeridian")
    return len(value)


def _wgs84_crs(obj):
    crs = obj.get("crs")
    if crs is None:
        return
    props = crs.get("properties") if isinstance(crs, dict) else None
    name = props.get("name") if isinstance(props, dict) else None
    if not isinstance(name, str) or name.upper() not in ("EPSG:4326", "URN:OGC:DEF:CRS:OGC:1.3:CRS84", "URN:OGC:DEF:CRS:EPSG::4326", "OGC:CRS84", "CRS84"):
        _fail("GeoJSON must declare WGS84/CRS84 or omit CRS under RFC 7946.", "invalid_crs")


def validate_geometry(geometry: dict) -> int:
    """Validate one non-collection geometry; return its coordinate-position count."""
    if not isinstance(geometry, dict) or geometry.get("type") not in _GEOMETRIES - {"GeometryCollection"}:
        _fail("Unsupported or malformed geometry.", "invalid_geometry")
    kind, coords = geometry["type"], geometry.get("coordinates")
    if kind == "Point":
        _position(coords)
        count = 1
    elif kind == "MultiPoint":
        if not isinstance(coords, (list, tuple)) or not coords:
            _fail("MultiPoint has no positions.", "invalid_geometry")
        for pos in coords:
            _position(pos)
        count = len(coords)
    elif kind == "LineString":
        count = _sequence(coords, 2)
    else:
        if not isinstance(coords, (list, tuple)) or not coords:
            _fail("Geometry has no coordinates.", "invalid_geometry")
        if kind == "MultiLineString":
            count = sum(_sequence(line, 2) for line in coords)
        elif kind == "Polygon":
            count = sum(_sequence(ring, 4, ring=True) for ring in coords)
        else:
            count = 0
            for poly in coords:
                if not isinstance(poly, (list, tuple)) or not poly:
                    _fail("Polygon has no rings.", "invalid_geometry")
                count += sum(_sequence(ring, 4, ring=True) for ring in poly)
    if count > MAX_COORDINATES:
        _fail("Vector exceeds 500,000 coordinate positions.", "vector_limit")
    try:
        geom = shape(geometry)
        if geom.is_empty or not geom.is_valid:
            _fail("Invalid geometry topology: " + explain_validity(geom), "invalid_geometry")
    except VectorImportError:
        raise
    except Exception as exc:
        raise VectorImportError("invalid_geometry", "Malformed geometry coordinates.") from exc
    return count


class _Builder:
    def __init__(self, filename):
        self.filename = filename
        self.features = []
        self.warnings = []
        self.coordinates = 0
        self.nulls = 0
        # Fixed wrapper, deduplicated warnings and bounded counters fit within
        # 1 KiB; filename and each complete feature are accounted separately.
        self.normalized_bytes = 1024 + _bounded_json_size(filename, MAX_NORMALIZED_BYTES - 1024)

    def add(self, geometry, properties=None, layer=None, name=None, depth=0):
        if depth > MAX_DEPTH:
            _fail("Geometry nesting exceeds the supported limit.", "vector_limit")
        if geometry is None:
            self.nulls += 1
            self.warnings.append("A feature without geometry was omitted.")
            return
        if not isinstance(geometry, dict):
            _fail("Geometry must be an object.", "invalid_geometry")
        _wgs84_crs(geometry)
        if geometry.get("type") == "GeometryCollection":
            children = geometry.get("geometries")
            if not isinstance(children, list):
                _fail("GeometryCollection requires a geometries array.", "invalid_geometry")
            for child in children:
                self.add(child, properties, layer, name, depth + 1)
            return
        if len(self.features) >= MAX_FEATURES:
            _fail("Vector exceeds 10,000 features.", "vector_limit")
        self.coordinates += validate_geometry(geometry)
        if self.coordinates > MAX_COORDINATES:
            _fail("Vector exceeds 500,000 coordinate positions.", "vector_limit")
        if properties is None:
            properties = {}
        if not isinstance(properties, dict):
            _fail("Feature properties must be an object or null.")
        _json_tree(properties)
        label = name if name is not None else properties.get("name", properties.get("Name", properties.get("NAME")))
        if not isinstance(label, (str, int, float)) or isinstance(label, bool) or not str(label).strip():
            label = f"{PurePosixPath(self.filename).stem} {len(self.features)+1}"
        feature = {"type": "Feature", "id": f"f{len(self.features)+1}", "geometry": geometry,
                   "properties": properties, "name": str(label), "layer": layer or PurePosixPath(self.filename).stem}
        # Charge inherited properties, geometry, name and layer for every leaf,
        # including array separators, before making any independent property copy.
        feature_bytes = _bounded_json_size(feature, MAX_NORMALIZED_BYTES - self.normalized_bytes - 2)
        self.normalized_bytes += feature_bytes + 2
        # Round-trip copies only JSON values; strings are kept as text, never HTML.
        feature["properties"] = json.loads(json.dumps(properties, allow_nan=False))
        self.features.append(feature)

    def result(self):
        kinds = [f["geometry"]["type"] for f in self.features]
        return {"filename": self.filename, "feature_collection": {"type": "FeatureCollection", "features": self.features},
                "warnings": list(dict.fromkeys(self.warnings)), "counts": {"features": len(kinds),
                "points": sum(k in ("Point", "MultiPoint") for k in kinds),
                "lines": sum(k in ("LineString", "MultiLineString") for k in kinds),
                "polygons": sum(k in ("Polygon", "MultiPolygon") for k in kinds),
                "coordinates": self.coordinates, "null_geometries": self.nulls}}


def _geojson(data, builder):
    def bad_constant(value):
        _fail("JSON contains a non-finite number.", "invalid_coordinates")

    obj = json.loads(data.decode("utf-8-sig"), parse_constant=bad_constant)
    _json_tree(obj)
    if not isinstance(obj, dict):
        _fail("GeoJSON must be an object.")
    _wgs84_crs(obj)
    kind = obj.get("type")
    if kind == "FeatureCollection":
        items = obj.get("features")
        if not isinstance(items, list):
            _fail("FeatureCollection requires a features array.")
        if len(items) > MAX_FEATURES:
            _fail("Vector exceeds 10,000 features.", "vector_limit")
    elif kind == "Feature":
        items = [obj]
    elif kind in _GEOMETRIES:
        builder.add(obj)
        return
    else:
        _fail("Unrecognized GeoJSON object.")
    for item in items:
        if not isinstance(item, dict) or item.get("type") != "Feature" or "geometry" not in item:
            _fail("FeatureCollection contains an invalid Feature.")
        _wgs84_crs(item)
        builder.add(item["geometry"], item.get("properties"))


def _tag(node):
    return node.tag.rsplit("}", 1)[-1]


def _children(node, tag):
    return [child for child in node if _tag(child) == tag]


def _text(node, tag):
    children = _children(node, tag)
    return (children[0].text or "").strip() if children else None


def _xml(data):
    depth, nodes = 0, 0
    parser = SafeET.iterparse(io.BytesIO(data), events=("start", "end"), forbid_dtd=True, forbid_entities=True, forbid_external=True)
    for event, _node in parser:
        if event == "start":
            depth += 1
            nodes += 1
            if depth > MAX_DEPTH or nodes > 2_000_000:
                _fail("XML nesting or element count exceeds the supported limit.", "vector_limit")
        else:
            depth -= 1
    return parser.root


def _kml_coordinates(node):
    text = _text(node, "coordinates")
    if not text:
        _fail("KML geometry is missing coordinates.", "invalid_geometry")
    result = []
    for token in text.split():
        result.append([float(v) for v in token.split(",")])
        if len(result) > MAX_COORDINATES:
            _fail("Vector exceeds 500,000 coordinate positions.", "vector_limit")
    return result


def _kml_geometry(node):
    kind = _tag(node)
    if kind == "Point":
        points = _kml_coordinates(node)
        if len(points) != 1:
            _fail("KML Point must contain exactly one position.", "invalid_geometry")
        return {"type": "Point", "coordinates": points[0]}
    if kind == "LineString":
        return {"type": "LineString", "coordinates": _kml_coordinates(node)}
    if kind == "Polygon":
        outer = _children(node, "outerBoundaryIs")
        if len(outer) != 1:
            _fail("KML Polygon must have one outer boundary.", "invalid_geometry")
        rings = []
        for boundary in outer + _children(node, "innerBoundaryIs"):
            ring = _children(boundary, "LinearRing")
            if len(ring) != 1:
                _fail("KML Polygon boundary must contain one LinearRing.", "invalid_geometry")
            rings.append(_kml_coordinates(ring[0]))
        return {"type": "Polygon", "coordinates": rings}
    if kind == "MultiGeometry":
        children = [_kml_geometry(child) for child in node if _tag(child) in ("Point", "LineString", "Polygon", "MultiGeometry")]
        if not children:
            _fail("KML MultiGeometry has no supported geometry.", "invalid_geometry")
        return {"type": "GeometryCollection", "geometries": children}
    _fail("Unsupported KML geometry.", "invalid_geometry")


def _kml(data, builder, layer=None):
    root = _xml(data)
    if _tag(root) != "kml":
        _fail("KML document requires a kml root element.")
    for node in root.iter():
        if _tag(node) in ("NetworkLink", "GroundOverlay"):
            builder.warnings.append("External KML resources and raster overlays are not loaded.")

    def walk(node, folder):
        kind = _tag(node)
        if kind == "Folder":
            folder = _text(node, "name") or folder
        if kind == "Placemark":
            props = {tag: _text(node, tag) for tag in ("name", "description", "address") if _text(node, tag) is not None}
            for ext in _children(node, "ExtendedData"):
                for item in ext.iter():
                    if _tag(item) == "Data" and item.get("name"):
                        props[item.get("name")] = _text(item, "value") or ""
                    elif _tag(item) == "SimpleData" and item.get("name"):
                        props[item.get("name")] = item.text or ""
            geoms = [child for child in node if _tag(child) in ("Point", "LineString", "Polygon", "MultiGeometry")]
            if not geoms:
                builder.add(None, props, folder)
            for geom in geoms:
                builder.add(_kml_geometry(geom), props, folder)
            return
        for child in node:
            if _tag(child) in ("Document", "Folder", "Placemark"):
                walk(child, folder)

    walk(root, layer or PurePosixPath(builder.filename).stem)


def _gpx(data, builder):
    root = _xml(data)
    if _tag(root) != "gpx":
        _fail("GPX document requires a gpx root element.")

    def position(node):
        pos = [float(node.attrib["lon"]), float(node.attrib["lat"])]
        elevation = _text(node, "ele")
        if elevation:
            pos.append(float(elevation))
        return pos

    for node in root:
        kind = _tag(node)
        if kind not in ("wpt", "rte", "trk"):
            continue
        props = {tag: _text(node, tag) for tag in ("name", "desc", "cmt", "type", "time") if _text(node, tag) is not None}
        if kind == "wpt":
            geom = {"type": "Point", "coordinates": position(node)}
        elif kind == "rte":
            geom = {"type": "LineString", "coordinates": [position(p) for p in _children(node, "rtept")]}
        else:
            segments = [[position(p) for p in _children(seg, "trkpt")] for seg in _children(node, "trkseg")]
            geom = {"type": "LineString", "coordinates": segments[0]} if len(segments) == 1 else {"type": "MultiLineString", "coordinates": segments}
        builder.add(geom, props)


def _archive(data):
    members = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            _fail("Archive exceeds 100 entries.", "vector_limit")
        total_declared, total_actual = 0, 0
        names = set()
        for entry in entries:
            raw = entry.filename.replace("\\", "/")
            path = PurePosixPath(raw)
            if not raw or raw.startswith("/") or ":" in raw or ".." in path.parts or "\x00" in raw:
                _fail("Archive contains an unsafe member path.", "invalid_archive")
            key = str(path).casefold()
            if key in names:
                _fail("Archive contains duplicate member names.", "invalid_archive")
            names.add(key)
            if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                _fail("Encrypted or symbolic-link archive members are unsupported.", "invalid_archive")
            total_declared += entry.file_size
            if total_declared > MAX_EXPANDED_BYTES:
                _fail("Archive exceeds 80 MiB expanded data.", "vector_limit")
            chunks = []
            with archive.open(entry) as source:
                while True:
                    block = source.read(min(64 * 1024, MAX_EXPANDED_BYTES - total_actual + 1))
                    if not block:
                        break
                    total_actual += len(block)
                    if total_actual > MAX_EXPANDED_BYTES:
                        _fail("Archive exceeds 80 MiB actual expanded data.", "vector_limit")
                    chunks.append(block)
            if not entry.is_dir():
                members[key] = (str(path), b"".join(chunks))
    return members


def _project_geometry(geometry, crs):
    # Transform positions with rasterio/GDAL without cutting or repairing topology.
    kind = geometry.get("type")
    if kind == "GeometryCollection":
        return {"type": kind, "geometries": [_project_geometry(g, crs) for g in geometry["geometries"]]}
    levels = {"Point": 0, "MultiPoint": 1, "LineString": 1, "MultiLineString": 2, "Polygon": 2, "MultiPolygon": 3}
    if kind not in levels:
        _fail("Unsupported Shapefile geometry.", "invalid_geometry")
    positions = []

    def collect(value, depth):
        if depth:
            return [collect(v, depth-1) for v in value]
        positions.append(value)

    collect(geometry["coordinates"], levels[kind])
    if len(positions) > MAX_COORDINATES:
        _fail("Vector exceeds 500,000 coordinate positions.", "vector_limit")
    if any(not math.isfinite(n) for p in positions for n in p):
        _fail("Coordinates must be finite.", "invalid_coordinates")
    xs, ys = transform(crs, "EPSG:4326", [p[0] for p in positions], [p[1] for p in positions])
    converted = iter([[x, y, *p[2:]] for x, y, p in zip(xs, ys, positions)])

    def rebuild(value, depth):
        return [rebuild(v, depth-1) for v in value] if depth else next(converted)

    return {"type": kind, "coordinates": rebuild(geometry["coordinates"], levels[kind])}


def _shape_geometry(source):
    if source.shapeType == shapefile.NULL:
        return None
    if len(source.points) > MAX_COORDINATES:
        _fail("Vector exceeds 500,000 coordinate positions.", "vector_limit")
    if source.shapeType not in (shapefile.POLYGON, shapefile.POLYGONM, shapefile.POLYGONZ):
        return source.__geo_interface__
    starts = list(source.parts)
    if not starts or starts[0] != 0 or starts != sorted(set(starts)) or starts[-1] >= len(source.points):
        _fail("Shapefile polygon has invalid ring offsets.", "invalid_geometry")
    rings = [list(source.points[start:end]) for start, end in zip(starts, starts[1:] + [len(source.points)])]
    # pyshp's default __geo_interface__ silently promotes orphan holes to shells.
    # Use its explicit error channel and reject that repair before projection.
    errors = {}
    polygons = shapefile.organize_polygon_rings(rings, errors)
    if errors:
        _fail("Shapefile contains orphan holes or invalid polygon ring orientation.", "invalid_geometry")
    return {"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1 else {"type": "MultiPolygon", "coordinates": polygons}


def _shapefile(members, builder):
    shp_names = sorted(key for key in members if key.endswith(".shp"))
    if not shp_names:
        _fail("ZIP has no Shapefile .shp members.", "invalid_archive")
    for key in shp_names:
        base = key[:-4]
        for suffix in (".shx", ".dbf", ".prj"):
            if base+suffix not in members:
                _fail("Shapefile requires matching .shp/.shx/.dbf/.prj members.", "missing_crs" if suffix == ".prj" else "invalid_archive")
        try:
            crs = CRS.from_wkt(members[base+".prj"][1].decode("utf-8-sig").strip())
            if not crs:
                _fail("Shapefile CRS is empty.", "invalid_crs")
        except Exception as exc:
            raise VectorImportError("invalid_crs", "Shapefile .prj CRS is invalid or unsupported.") from exc
        encoding = "utf-8"
        if base+".cpg" in members:
            encoding = members[base+".cpg"][1].decode("ascii").strip()
            if encoding.isdigit():
                encoding = "utf-8" if encoding == "65001" else "cp"+encoding
        with shapefile.Reader(shp=io.BytesIO(members[key][1]), shx=io.BytesIO(members[base+".shx"][1]), dbf=io.BytesIO(members[base+".dbf"][1]), encoding=encoding) as reader:
            if reader.numRecords > MAX_FEATURES:
                _fail("Vector exceeds 10,000 features.", "vector_limit")
            if reader.numRecords != reader.numShapes:
                _fail("Shapefile geometry and attribute record counts differ.")
            for record in reader.iterShapeRecords():
                source_geometry = _shape_geometry(record.shape)
                geom = None if source_geometry is None else _project_geometry(source_geometry, crs)
                # pyshp may return DBF date values; represent those as ISO text.
                props = {k: v.isoformat() if hasattr(v, "isoformat") else v for k, v in record.record.as_dict().items()}
                builder.add(geom, props, members[key][0][:-4])


def read_vector(data: bytes, filename: str) -> dict:
    """Read one upload atomically; errors expose only safe, actionable messages."""
    if not isinstance(data, bytes) or not data:
        _fail("Vector upload is empty.")
    if len(data) > MAX_FILE_BYTES:
        _fail("Vector upload exceeds 20 MiB.", "vector_limit", 413)
    if not isinstance(filename, str) or not filename.strip():
        _fail("A vector filename is required.")
    filename = PurePosixPath(filename.replace("\\", "/")).name
    builder = _Builder(filename)
    suffix = PurePosixPath(filename).suffix.lower()
    try:
        if suffix in (".json", ".geojson"):
            _geojson(data, builder)
        elif suffix == ".kml":
            _kml(data, builder)
        elif suffix == ".gpx":
            _gpx(data, builder)
        elif suffix in (".kmz", ".zip"):
            members = _archive(data)
            if suffix == ".kmz":
                kmls = [value for key, value in sorted(members.items()) if key.endswith(".kml")]
                if not kmls:
                    _fail("KMZ has no KML documents.", "invalid_archive")
                for name, content in kmls:
                    _kml(content, builder, PurePosixPath(name).stem)
            else:
                _shapefile(members, builder)
        else:
            _fail("Supported vector formats: GeoJSON, JSON, KML, KMZ, GPX and Shapefile ZIP.", "unsupported_format")
    except VectorImportError:
        raise
    except RecursionError as exc:
        raise VectorImportError("vector_limit", "Vector nesting exceeds the supported limit.") from exc
    except Exception as exc:
        raise VectorImportError("invalid_vector", "Vector file is malformed or uses unsupported content.") from exc
    return builder.result()
