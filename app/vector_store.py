"""SQLite-backed immutable vector imports and polygon ROI snapshots."""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
import uuid

from shapely.geometry import mapping, shape
from shapely.ops import unary_union

from .vector_import import MAX_COORDINATES, VectorImportError, validate_geometry

TTL_SECONDS = 24 * 60 * 60


class VectorStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "vectors.sqlite3"
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, payload TEXT NOT NULL, accessed REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS rois (id TEXT PRIMARY KEY, payload TEXT NOT NULL, accessed REAL NOT NULL)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _id(value):
        try:
            valid = isinstance(value, str) and str(uuid.UUID(value)) == value
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise VectorImportError("vector_not_found", "Vector import or ROI was not found or has expired.", 404)
        return value

    @staticmethod
    def _cleanup(db, now):
        for table in ("imports", "rois"):
            db.execute(f"DELETE FROM {table} WHERE accessed <= ?", (now-TTL_SECONDS,))

    def _save(self, table, payload, field):
        value = json.loads(json.dumps(payload, allow_nan=False))
        value[field] = str(uuid.uuid4())
        now = time.time()
        with self._connect() as db:
            self._cleanup(db, now)
            db.execute(f"INSERT INTO {table}(id,payload,accessed) VALUES(?,?,?)", (value[field], json.dumps(value, ensure_ascii=False, allow_nan=False), now))
        return value

    def _get(self, table, value):
        value = self._id(value)
        now = time.time()
        with self._connect() as db:
            # Start a write transaction before reading so expiration/touch is atomic.
            db.execute("BEGIN IMMEDIATE")
            self._cleanup(db, now)
            row = db.execute(f"SELECT payload FROM {table} WHERE id=?", (value,)).fetchone()
            if row:
                db.execute(f"UPDATE {table} SET accessed=? WHERE id=?", (now, value))
        if not row:
            raise VectorImportError("vector_not_found", "Vector import or ROI was not found or has expired.", 404)
        return json.loads(row[0])

    def save_import(self, payload: dict) -> dict:
        return self._save("imports", payload, "import_id")

    def get_import(self, import_id: str) -> dict:
        return self._get("imports", import_id)

    def delete_import(self, import_id: str):
        import_id = self._id(import_id)
        with self._connect() as db:
            self._cleanup(db, time.time())
            db.execute("DELETE FROM imports WHERE id=?", (import_id,))

    def create_roi(self, selections: list[dict]) -> dict:
        if not isinstance(selections, list) or not selections:
            raise VectorImportError("invalid_selection", "Select at least one polygon.")
        if len(selections) > 10_000:
            raise VectorImportError("vector_limit", "Too many selected imports.")
        refs = {}
        seen = set()
        geometries = []
        labels = []
        excluded, coordinates = 0, 0
        imports = {}
        for selection in selections:
            if not isinstance(selection, dict):
                raise VectorImportError("invalid_selection", "Each selection requires import_id and feature_ids.")
            import_id = self._id(selection.get("import_id"))
            ids = selection.get("feature_ids")
            if not isinstance(ids, list) or not ids or len(ids) > 10_000 or any(not isinstance(i, str) for i in ids):
                raise VectorImportError("invalid_selection", "feature_ids must be a non-empty list of feature IDs.")
            if import_id not in imports:
                imported = self.get_import(import_id)
                imports[import_id] = {f["id"]: f for f in imported["feature_collection"]["features"]}
            features = imports[import_id]
            for feature_id in ids:
                if feature_id not in features:
                    raise VectorImportError("feature_not_found", "A selected vector feature was not found.", 404)
                key = (import_id, feature_id)
                if key in seen:
                    continue
                seen.add(key)
                feature = features[feature_id]
                geom = feature["geometry"]
                if geom["type"] not in ("Polygon", "MultiPolygon"):
                    excluded += 1
                    continue
                coordinates += validate_geometry(geom)
                if coordinates > MAX_COORDINATES:
                    raise VectorImportError("vector_limit", "Combined ROI exceeds 500,000 coordinate positions.")
                geometries.append(shape(geom))
                labels.append(feature["name"])
                refs.setdefault(import_id, []).append(feature_id)
        if not geometries:
            raise VectorImportError("no_selected_polygons", "Selected features contain no Polygon or MultiPolygon.")
        try:
            merged = unary_union(geometries)
            geometry = json.loads(json.dumps(mapping(merged), allow_nan=False))
            count = validate_geometry(geometry)
            if merged.geom_type not in ("Polygon", "MultiPolygon") or count > MAX_COORDINATES:
                raise VectorImportError("vector_limit", "Combined ROI exceeds the supported geometry limit.")
        except VectorImportError:
            raise
        except Exception as exc:
            raise VectorImportError("invalid_geometry", "Selected polygons could not be combined safely.") from exc
        west, south, east, north = merged.bounds
        name = labels[0] if len(labels) == 1 else f"{len(labels)} polygons"
        return self._save("rois", {"name": name, "geometry": geometry, "bbox": {"west": west, "south": south, "east": east, "north": north},
                                  "feature_refs": [{"import_id": key, "feature_ids": ids} for key, ids in refs.items()],
                                  "excluded_count": excluded}, "roi_id")

    def get_roi(self, roi_id: str) -> dict:
        return self._get("rois", roi_id)
