#!/usr/bin/env python
"""Download masked Esri imagery. Zoom is sampling, not native resolution.

Existing outputs require --overwrite. Verified JPEG cache is retained for retries.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from app.esri import (TILE_SIZE, ORIGIN_SHIFT, MAX_LAT, TILE_URL, _load_cas,
                      deg2tile, tile_x_to_merc, tile_y_to_merc, make_plan, download)
EARTH_R = 6378137.0


def download_bbox(west, south, east, north, zoom, output, max_tiles=4000,
                  workers=4, overwrite=False):
    plan = make_plan([west, south, east, north], zoom, max_tiles, workers=workers)
    output = Path(output)
    stats = download(plan, output, output.parent / '.esri_cache', workers=workers,
                     overwrite=overwrite)
    print(f"Saved {output}: {stats}")
    return stats


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bbox', nargs=4, type=float, required=True,
                    metavar=('WEST','SOUTH','EAST','NORTH'))
    ap.add_argument('--zoom', type=int, default=17)
    ap.add_argument('--output', default='downloads/esri_world_imagery.tif')
    ap.add_argument('--max-tiles', type=int, default=4000)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--overwrite', action='store_true')
    args = ap.parse_args()
    try:
        download_bbox(*args.bbox, args.zoom, args.output, args.max_tiles,
                      workers=args.workers, overwrite=args.overwrite)
    except (ValueError, RuntimeError, OSError) as exc:
        ap.error(str(exc))


if __name__ == '__main__':
    sys.exit(main())
