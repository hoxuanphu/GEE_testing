#!/usr/bin/env python
"""Download exact commune imagery masks, including holes.

--pad expands the output grid only; pixels outside the boundary remain invalid.
Verified disk cache enables retries; use --overwrite to replace an output.
"""
from __future__ import annotations
import argparse
import re
import sys
import unicodedata
from pathlib import Path
import shapefile
from app.esri import (TILE_SIZE, ORIGIN_SHIFT, MAX_LAT, TILE_URL, _load_cas,
                      deg2tile, make_plan, download)
SHP_PATH = Path(__file__).resolve().parent / 'Shapefile_PX_VN_34' / 'Việt Nam (phường xã) - 34.shp'


def _no_diacritics(s):
    s = s.replace('Đ', 'D').replace('đ', 'd')
    return ''.join(c for c in unicodedata.normalize('NFD', s)
                   if unicodedata.category(c) != 'Mn').lower().strip()


def _slug(s):
    return re.sub(r'[^a-z0-9]+', '_', _no_diacritics(s)).strip('_')


def find_communes(reader, names, province):
    fields = [f[0] for f in reader.fields[1:]]
    wanted = list(dict.fromkeys(_no_diacritics(n) for n in names if n.strip()))
    if not wanted:
        raise ValueError('At least one commune name is required')
    matches = {key: [] for key in wanted}
    for i, record in enumerate(reader.records()):
        props = dict(zip(fields, record))
        key = _no_diacritics(str(props.get('ten_xa', '')))
        if key in matches and (province is None or _no_diacritics(province) in
                               _no_diacritics(str(props.get('ten_tinh', '')))):
            matches[key].append((i, props))
    for key, items in matches.items():
        if not items:
            raise ValueError(f'Commune not found: {key}; check --tinh')
        if len(items) > 1:
            raise ValueError(f'Ambiguous commune: {key}; specify --tinh (matches: '
                             + ', '.join(str(p.get('ten_tinh')) for _, p in items) + ')')
    return {key: items[0] for key, items in matches.items()}


def tile_grid_mask(shape, zoom, pad=1):
    import numpy as np
    plan = make_plan(list(shape.bbox), zoom, geometry=shape.__geo_interface__, pad=pad)
    mask = np.zeros((plan.height//256, plan.width//256), dtype=bool)
    for x, y in plan.tiles:
        mask[y-plan.y0, x-plan.x0] = True
    return plan.x0, plan.y0, mask


def download_commune(session, idx, props, shape, zoom, output, workers, pad=1,
                     max_tiles=4000, overwrite=False):
    # Legacy session argument is accepted; engine owns and closes worker sessions.
    plan = make_plan(list(shape.bbox), zoom, max_tiles,
                     geometry=shape.__geo_interface__, pad=pad, workers=workers)
    output = Path(output)
    stats = download(plan, output, output.parent / '.esri_cache', workers=workers,
                     overwrite=overwrite)
    print(f"Saved {props['ten_xa']}: {output}; {stats}")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--names', required=True)
    ap.add_argument('--tinh', default=None)
    ap.add_argument('--zoom', type=int, default=18)
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--pad', type=int, default=1)
    ap.add_argument('--out-dir', default='downloads')
    ap.add_argument('--max-tiles', type=int, default=4000)
    ap.add_argument('--overwrite', action='store_true')
    args = ap.parse_args()
    try:
        with shapefile.Reader(str(SHP_PATH), encoding='utf-8') as reader:
            communes = find_communes(reader, args.names.split(','), args.tinh)
            for _, (idx, props) in communes.items():
                out = Path(args.out_dir) / f"esri_{_slug(props['ten_xa'])}_z{args.zoom}.tif"
                download_commune(None, idx, props, reader.shape(idx), args.zoom, out,
                                 args.workers, args.pad, args.max_tiles, args.overwrite)
    except (ValueError, RuntimeError, OSError, shapefile.ShapefileException) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
