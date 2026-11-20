"""Bounded mask-only preflight on the same final grid used by the exporter."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import threading
import time

from affine import Affine
import ee
import numpy as np
import rasterio
from rasterio.crs import CRS
from rasterio.enums import Resampling
from rasterio.io import MemoryFile
from rasterio.vrt import WarpedVRT

from .frame_models import FrameError
from .frame_search import _RPC, _remaining, _retry, _TransientError, validate_asset

_MEASURE_LOCK = threading.Lock()


def _load_mask_image(asset_id, dataset, bands):
    validate_asset(asset_id, dataset, bands)
    return ee.Image(asset_id).select(bands)


def _mask_graph(image, bands, grid):
    # Keep each band's mask independent until after the final nearest warp.
    return ee.Image.cat([
        image.select([band]).mask().gt(0).unmask(0, sameFootprint=False).reproject(
            crs=grid['crs'], crsTransform=grid['transform'])
        for band in bands
    ]).rename(bands).toByte()


def _fetch_mask_tile(image, bands, grid, window, rpc, deadline):
    from .frame_export import _tile_grid
    target = _tile_grid(grid, window)
    graph = _mask_graph(image, bands, target)
    params = {'crs': target['crs'], 'crs_transform': target['transform'],
              'dimensions': [target['width'], target['height']], 'format': 'GEO_TIFF',
              'filePerBand': False}

    def operation():
        raw = rpc.thumbnail_once(graph, params, deadline=deadline)
        try:
            with MemoryFile(raw) as memory, memory.open() as source:
                if (source.width != target['width'] or source.height != target['height']
                        or source.count != len(bands) or source.crs != CRS.from_user_input(target['crs'])
                        or any(abs(a-b) > 1e-8 for a,b in zip(list(source.transform)[:6], target['transform']))):
                    raise FrameError('invalid_mask', 'Mask tải về không khớp lưới kiểm tra.')
                data = source.read(masked=True)
                if not np.isin(data.compressed(), [0, 1]).all():
                    raise FrameError('invalid_mask', 'Mask tải về có giá trị không hợp lệ.')
                return data.filled(0).astype(np.uint8)
        except rasterio.errors.RasterioError:
            raise _TransientError() from None
    return _retry(operation, deadline)


def measure_frame_selection(scenes: list[dict], parameters: dict, *, deadline: float,
                            work_directory: Path | None = None) -> dict:
    from .frame_export import _grid, iter_grid_tiles
    from .frame_projection import plan_transport_grid, mask_warp_options
    _remaining(deadline)
    if not 1 <= len(scenes) <= 10:
        raise FrameError('invalid_selection', 'Chọn từ 1 đến 10 nguồn để kiểm tra.')
    grid = _grid(parameters['output_grid'])
    bands, dataset = parameters['bands'], parameters['dataset']
    custom = CRS.from_user_input(grid['crs']).to_epsg() is None
    transport = plan_transport_grid(grid, dataset, bands)['grid'] if custom else grid
    root = Path(work_directory) if work_directory is not None else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    reserve = (transport['width'] * transport['height'] * len(bands)
               + grid['width'] * grid['height'] + 64 * 1024**2)
    if shutil.disk_usage(root).free < reserve:
        raise FrameError('disk_space', 'Không đủ đĩa để kiểm tra mask nhóm.')
    if not _MEASURE_LOCK.acquire(blocking=False):
        raise FrameError('busy', 'Một lượt kiểm tra độ phủ nhóm đang chạy.', 409)
    try:
        with tempfile.TemporaryDirectory(prefix='frame-mask-', dir=root) as temporary:
            folder = Path(temporary)
            union_path = folder / 'union.tif'
            profile = dict(driver='GTiff', width=grid['width'], height=grid['height'], count=1,
                           dtype='uint8', crs=grid['crs'], transform=Affine(*grid['transform']),
                           tiled=True, blockxsize=256, blockysize=256, compress='deflate')
            with rasterio.Env(GDAL_PAM_ENABLED='NO', GDAL_CACHEMAX=64*1024**2), \
                    rasterio.open(union_path, 'w+', **profile) as union, \
                    _RPC(min(120, _remaining(deadline)), deadline) as rpc:
                for window in iter_grid_tiles(grid, 1):
                    union.write(np.zeros((int(window.height), int(window.width)), dtype=np.uint8),
                                indexes=1, window=window)
                for source in scenes:
                    _remaining(deadline)
                    image = _load_mask_image(source['asset_id'], dataset, bands)
                    mask_path = folder / 'source.tif'
                    mask_profile = {**profile, 'width': transport['width'], 'height': transport['height'],
                                    'count': len(bands), 'crs': transport['crs'],
                                    'transform': Affine(*transport['transform'])}
                    with rasterio.open(mask_path, 'w', **mask_profile) as masks:
                        for window in iter_grid_tiles(transport, len(bands)):
                            _remaining(deadline)
                            data = _fetch_mask_tile(image, bands, transport, window, rpc, deadline)
                            masks.write(data, window=window)
                    with rasterio.open(mask_path) as masks:
                        with WarpedVRT(masks, **mask_warp_options(grid)) as target:
                            for window in iter_grid_tiles(grid, len(bands)):
                                _remaining(deadline)
                                valid = target.read(window=window, masked=True).filled(0).all(axis=0)
                                union.write((union.read(1, window=window).astype(bool) | valid).astype(np.uint8),
                                            indexes=1, window=window)
                    mask_path.unlink()
                count = 0
                for _, window in union.block_windows(1):
                    _remaining(deadline)
                    count += int(np.count_nonzero(union.read(1, window=window)))
            total = grid['width'] * grid['height']
            _remaining(deadline)
            return {'measurement_version': 1, 'method': 'output_grid_mask', 'estimated': True,
                    'band_coverage_pct': count * 100.0 / total, 'valid_pixel_count': count,
                    'total_pixels': total, 'ordered_asset_ids': [s['asset_id'] for s in scenes],
                    'roi_fingerprint': parameters['roi_fingerprint'],
                    'merge_policy': 'primary_first_fill_nodata'}
    finally:
        _MEASURE_LOCK.release()
