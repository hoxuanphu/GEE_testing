"""Chuyển shapefile 34 tỉnh thành GeoJSON (dạng vn.json) cho app.

Đọc DBF + SHP thủ công bằng struct (không cần pyshp/geopandas):
- DBF: header + records UTF-8, trường phân tách theo descriptor.
- SHP: main file header + record header + shape (Polygon/MultiPolygon).

Output: shapefile34/vn34.json — FeatureCollection với properties {id, name}.
ID giữ nguyên mã tỉnh (ma_tinh) nếu có, không thì sinh từ tên.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

BASE = Path(__file__).parent / "shapefile34"
SHP = BASE / "Việt Nam (tỉnh thành) - 34.shp"
DBF = BASE / "Việt Nam (tỉnh thành) - 34.dbf"
OUT = BASE / "vn34.json"


def read_dbf_fields(path: Path) -> tuple[list[tuple[str, str, int, int]], int]:
    """Trả về (danh sách (tên, type, length, decimal), độ dài record)."""
    raw = path.read_bytes()
    n_fields = (len(raw) - 33) // 32  # ước lượng; kết thúc header = 0x0D
    fields: list[tuple[str, str, int, int]] = []
    offset = 32
    while raw[offset] != 0x0D:
        desc = raw[offset : offset + 32]
        name = desc[0:11].split(b"\x00")[0].decode("utf-8", "replace").strip()
        ftype = chr(desc[11])
        flen = desc[16]
        fdec = desc[17]
        fields.append((name, ftype, flen, fdec))
        offset += 32
    header_len = struct.unpack("<H", raw[8:10])[0]
    rec_len = struct.unpack("<H", raw[10:12])[0]
    n_records = struct.unpack("<I", raw[4:8])[0]
    return fields, rec_len, n_records, header_len


def read_dbf_records(path: Path) -> list[dict[str, str]]:
    raw = path.read_bytes()
    fields, rec_len, n_records, header_len = read_dbf_fields(path)
    records: list[dict[str, str]] = []
    pos = header_len
    for _ in range(n_records):
        rec = raw[pos : pos + rec_len]
        pos += rec_len
        if not rec or rec[0:1] == b"*":  # deleted
            continue
        item: dict[str, str] = {}
        off = 1  # skip deletion flag
        for name, ftype, flen, _fdec in fields:
            val = rec[off : off + flen].decode("utf-8", "replace").strip()
            off += flen
            item[name] = val
        records.append(item)
    return records


def read_shp_shapes(path: Path) -> list[dict]:
    """Đọc SHP trả về danh sách geometry dict (Polygon/MultiPolygon)."""
    raw = path.read_bytes()
    offset = 100  # main header 100 bytes
    shapes: list[dict] = []
    while offset + 8 <= len(raw):
        rec_num, rec_len = struct.unpack(">II", raw[offset : offset + 8])
        offset += 8
        body = raw[offset : offset + rec_len * 2]
        offset += rec_len * 2
        shape_type = struct.unpack("<I", body[0:4])[0]
        if shape_type == 0:  # null
            shapes.append(None)
            continue
        if shape_type not in (3, 5, 13, 15, 23, 25, 31):
            raise ValueError(f"Unsupported shape type {shape_type}")
        # Polygon (5) / PolygonM (15) / PolygonZ (25): parts + points.
        # NOTE: body chứa bbox (4 double, 32 byte) TRƯỚC numParts/numPoints
        # — layout: [type(4)][bbox(32)][numParts(4)][numPoints(4)][parts][points].
        if shape_type in (5, 15, 25):
            num_parts = struct.unpack("<I", body[36:40])[0]
            num_points = struct.unpack("<I", body[40:44])[0]
            parts = list(struct.unpack(f"<{num_parts}I", body[44 : 44 + 4 * num_parts]))
            pts_start = 44 + 4 * num_parts
            coords = struct.unpack(
                f"<{num_points * 2}d", body[pts_start : pts_start + num_points * 16]
            )
            points = [
                [coords[i * 2], coords[i * 2 + 1]] for i in range(num_points)
            ]
            rings: list[list[list[float]]] = []
            for i, start in enumerate(parts):
                end = parts[i + 1] if i + 1 < len(parts) else num_points
                ring = [list(p) for p in points[start:end]]
                if len(ring) >= 4:
                    rings.append(ring)

            # Phân tách ring ngoài / ring trong bằng diện tích shoe-lace.
            # Ring ngoài có |area| lớn và theo spec là CCW (area > 0);
            # ring trong (hole) là CW (area < 0). Ring nào |area| ~ 0 bỏ qua.
            def ring_area2(ring: list[list[float]]) -> float:
                a = 0.0
                n = len(ring)
                for i in range(n):
                    x1, y1 = ring[i]
                    x2, y2 = ring[(i + 1) % n]
                    a += x1 * y2 - x2 * y1
                return a

            polygons: list[list[list[list[float]]]] = []
            current: list[list[list[float]]] = []
            for ring in rings:
                a2 = ring_area2(ring)
                if abs(a2) < 1e-12:
                    continue
                if a2 > 0:  # outer (CCW)
                    if current:
                        polygons.append(current)
                    current = [ring]
                else:  # hole (CW)
                    if not current:
                        # Hole không có outer đi trước — coi như outer đảo hướng
                        current = [ring]
                    else:
                        current.append(ring)
            if current:
                polygons.append(current)
            if not polygons:
                raise ValueError(f"shape {rec_num}: no valid rings")

            # Hợp nhất các polygon rời (cùng tỉnh, ví dụ 114 phần của An
            # Giang gồm nhiều vùng + đảo) thành MultiPolygon.
            geom: dict = {
                "type": "MultiPolygon",
                "coordinates": polygons,
            }
            shapes.append(geom)
        else:
            raise ValueError(
                f"Shape type {shape_type} (point/polyline) không phải polygon"
            )
    return shapes


def main() -> None:
    records = read_dbf_records(DBF)
    shapes = read_shp_shapes(SHP)
    print(f"{len(records)} records, {len(shapes)} shapes")
    if len(records) != len(shapes):
        print("WARN: số record != số shape", file=sys.stderr)

    # Lọc tên trường hữu ích
    keys = records[0].keys() if records else []
    print("fields:", list(keys))
    for r in records[:3]:
        print("  ", {k: v for k, v in r.items()})

    id_key = next((k for k in keys if "ma_tinh" in k.lower() or k.lower() in ("id", "gid", "fid")), None)
    name_key = next((k for k in keys if "ten_tinh" in k.lower() or k.lower() == "name"), None)
    print("id_key =", id_key, "| name_key =", name_key)

    features = []
    for rec, geom in zip(records, shapes):
        if geom is None:
            continue
        pid = (rec.get(id_key) if id_key else None) or ""
        pname = (rec.get(name_key) if name_key else None) or ""
        features.append(
            {
                "type": "Feature",
                "properties": {"id": f"VN{pid}" if pid else pname, "name": pname},
                "geometry": geom,
            }
        )
    fc = {"type": "FeatureCollection", "features": features}
    OUT.write_text(json.dumps(fc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"OK -> {OUT} ({len(features)} features, {OUT.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
