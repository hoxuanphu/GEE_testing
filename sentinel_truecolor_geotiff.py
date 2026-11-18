"""Create an 8-bit RGBA true-color GeoTIFF from Sentinel-2 reflectance.

Uses B4/B3/B2 with a shared fixed 0-0.3 reflectance display range, matching
compose_rgb.py's default stretch. Spectral values in the input stay unchanged.
"""
import argparse
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--display-max', type=float, default=0.3)
    parser.add_argument('--gamma', type=float, default=1.0)
    args = parser.parse_args()
    if args.display_max <= 0 or args.gamma <= 0:
        parser.error('display-max and gamma must be positive')
    if args.output.resolve() == args.input.resolve() or args.output.exists():
        parser.error('Output must be a new file distinct from the input')
    with rasterio.open(args.input) as src:
        try:
            indexes = [src.descriptions.index(name) + 1 for name in ('B4', 'B3', 'B2')]
        except ValueError as exc:
            raise ValueError('Input must contain bands named B4, B3 and B2') from exc
        profile = src.profile.copy()
        profile.update(driver='GTiff', count=4, dtype='uint8', nodata=None,
                       compress='deflate', tiled=True, blockxsize=512,
                       blockysize=512, BIGTIFF='IF_SAFER', photometric='RGB')
        with rasterio.open(args.output, 'w', **profile) as dst:
            dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
            dst.update_tags(display='B4/B3/B2', display_min='0',
                            display_max=str(args.display_max), gamma=str(args.gamma))
            for _, window in dst.block_windows(1):
                bands = src.read(indexes, window=window, masked=True)
                data = bands.filled(np.nan)
                valid = (~np.ma.getmaskarray(bands).any(axis=0)) & np.isfinite(data).all(axis=0)
                data = np.nan_to_num(data, nan=0, posinf=0, neginf=0)
                rgb = np.power(np.clip(data / args.display_max, 0, 1), 1 / args.gamma)
                rgb = np.rint(rgb * 255).astype('uint8')
                rgb[:, ~valid] = 0
                dst.write(rgb, indexes=[1, 2, 3], window=window)
                dst.write(valid.astype('uint8') * 255, 4, window=window)
    print(f'Created {args.output}')


if __name__ == '__main__':
    main()
