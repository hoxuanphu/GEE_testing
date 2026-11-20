r"""Burn province boundaries and names into a displayed RGB(A) GeoTIFF.

Example:
  .venv\Scripts\python.exe add_province_boundaries.py "C:\path\image.tif"

Requires rasterio, numpy, Pillow, pyshp and shapely. The default vector is
the repository's 34-province shapefile; the output is a new adjacent TIFF.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import transform_geom
import shapefile
from shapely.geometry import Point, box, mapping, shape
from PIL import Image, ImageDraw, ImageFont

DEFAULT_SHAPE = Path(__file__).resolve().parent / 'shapefile34' / 'Việt Nam (tỉnh thành) - 34.shp'


def lines(geometry):
    if geometry.geom_type in ('LineString', 'LinearRing'):
        yield geometry
    elif hasattr(geometry, 'geoms'):
        for part in geometry.geoms:
            yield from lines(part)


def label_point(geom, bounds, valid):
    """Pick a point away from province/frame edges on visible source pixels."""
    left, bottom, right, top = bounds
    best, best_score = None, -1.0
    clipped = geom.intersection(box(*bounds))
    if clipped.is_empty:
        return None
    xmin, ymin, xmax, ymax = clipped.bounds
    for y in np.linspace(ymin, ymax, 45):
        for x in np.linspace(xmin, xmax, 45):
            point = Point(x, y)
            if not clipped.contains(point):
                continue
            col = min(valid.shape[1] - 1, max(0, int((x-left)/(right-left)*valid.shape[1])))
            row = min(valid.shape[0] - 1, max(0, int((top-y)/(top-bottom)*valid.shape[0])))
            if not valid[row, col]:
                continue
            score = clipped.boundary.distance(point)
            if score > best_score:
                best, best_score = point, score
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--shapefile', type=Path, default=DEFAULT_SHAPE)
    parser.add_argument('--font', type=Path, default=Path('C:/Windows/Fonts/arialbd.ttf'))
    parser.add_argument('--font-size', type=int, default=100, help='Label height in pixels (default: 100)')
    parser.add_argument('--line-width', type=int, default=7, help='Boundary width in pixels (default: 7)')
    args = parser.parse_args()
    output = args.output or args.input.with_name(args.input.stem + '_province_boundaries.tif')
    if output.resolve() == args.input.resolve():
        parser.error('Output must be different from input')
    if output.exists():
        parser.error(f'Output already exists: {output}')
    if args.font_size < 1 or args.line_width < 1:
        parser.error('Font size and line width must be positive')
    font = ImageFont.truetype(str(args.font), args.font_size)
    prj = args.shapefile.with_suffix('.prj')
    vector_crs = rasterio.crs.CRS.from_wkt(prj.read_text(encoding='utf-8'))

    with rasterio.open(args.input) as src:
        if (src.count not in (3, 4) or any(d != 'uint8' for d in src.dtypes)
                or src.colorinterp[:3] != (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
                or (src.count == 4 and src.colorinterp[3] != ColorInterp.alpha)):
            raise ValueError('Input must be a displayed uint8 RGB or RGBA GeoTIFF')
        if src.crs is None or src.transform.b != 0 or src.transform.d != 0:
            raise ValueError('Input must have a CRS and a north-up, unrotated grid')
        valid = src.dataset_mask(out_shape=(512, 512), resampling=Resampling.nearest) > 0
        frame = box(*src.bounds)
        provinces = []
        with shapefile.Reader(str(args.shapefile), encoding='utf-8') as reader:
            for record in reader.iterShapeRecords():
                geometry = transform_geom(vector_crs, src.crs, record.shape.__geo_interface__)
                geom = shape(geometry)
                if geom.intersects(frame):
                    provinces.append((record.record.as_dict()['ten_tinh'], geom))
        if not provinces:
            raise ValueError('No province intersects the image')

        overlay = Image.new('RGBA', (src.width, src.height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        inverse = ~src.transform
        for _, geom in provinces:
            for line in lines(geom.boundary.intersection(frame)):
                coords = [inverse * (x, y) for x, y, *_ in line.coords]
                if len(coords) >= 2:
                    draw.line(coords, fill=(0, 0, 0, 255), width=args.line_width + 6, joint='curve')
                    draw.line(coords, fill=(255, 220, 0, 255), width=args.line_width, joint='curve')
        names = []
        for name, geom in provinces:
            point = label_point(geom, src.bounds, valid)
            if point is None:
                continue
            x, y = inverse * (point.x, point.y)
            bb = draw.textbbox((0, 0), name, font=font, stroke_width=3)
            x = max(4-bb[0], min(x-(bb[2]-bb[0])/2-bb[0], src.width-bb[2]-4))
            y = max(4-bb[1], min(y-(bb[3]-bb[1])/2-bb[1], src.height-bb[3]-4))
            draw.text((x, y), name, font=font, fill=(255, 255, 255, 255), stroke_width=3, stroke_fill=(0, 0, 0, 255))
            names.append(name)
        profile = src.profile.copy()
        profile.update(driver='GTiff', compress='deflate', tiled=True,
                       blockxsize=512, blockysize=512, BIGTIFF='IF_SAFER')
        output.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(output, 'w', **profile) as dst:
            dst.colorinterp = src.colorinterp
            dst.update_tags(**src.tags())
            for i, desc in enumerate(src.descriptions, 1):
                if desc:
                    dst.set_band_description(i, desc)
            for _, window in dst.block_windows(1):
                data = src.read(window=window)
                x, y = int(window.col_off), int(window.row_off)
                over = np.asarray(overlay.crop((x, y, x + int(window.width), y + int(window.height))))
                mask = (over[:, :, 3] > 0) & (src.dataset_mask(window=window) > 0)
                for band in range(3):
                    data[band][mask] = over[:, :, band][mask]
                dst.write(data, window=window)
                if src.count == 3:
                    dst.write_mask(src.dataset_mask(window=window), window=window)
    print('Output:', output)
    print('Province labels:', ', '.join(names) or '(none on valid pixels)')


if __name__ == '__main__':
    main()
