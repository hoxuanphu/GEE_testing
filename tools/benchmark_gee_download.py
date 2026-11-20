"""Reproducible local assembly benchmark; live runs must be explicitly requested.

Local numbers exclude GEE, URL latency and browser delivery. Both local writers
consume the same deterministic fixture bytes. Cold live means application cache;
the operating-system cache is never claimed to be controlled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import rasterio
from rasterio.transform import from_origin
from app import imagery
from app.download_metrics import DownloadMetrics
from app.raster_grid import warp_to_grid
from app.raster_mosaic import assemble_to_grid


def live_benchmark(args):
    """Explicit live, same frozen graph across repeats, isolated checkpoints."""
    import dataclasses
    from app import main as api
    from app.download_models import DownloadSettings
    from app.download_pipeline import execute_selection,export_plan,validate_artifact
    from app.download_snapshots import restore_selection
    from app.download_store import SelectionStore
    from unittest.mock import patch
    fields=dict(dataset='sentinel2',start_date='2023-01-18',end_date='2023-05-17',merge_scenes=True,
                s2_auxiliary_bands=None if args.case=='user-roi-26' else [],color_balance=False)
    if args.case=='small':
        fields.update(lon=106.4437867045,lat=10.076415139,radius_m=1000)
    elif args.case in ('user-roi-12','user-roi-26','rotated'):
        fields.update(selection_mode='rectangle',center_lon=106.4437867045,center_lat=10.076415139,
                      width_m=83328.8399307535,height_m=76948.81987935984,
                      rotation_deg=15 if args.case=='rotated' else 0)
    else:
        raise ValueError('local-big is a local synthetic case, not a live download')
    args.output.mkdir(parents=True,exist_ok=True)
    search_started=time.perf_counter()
    # No running application/shared session is reconfigured; only this CLI's
    # own process and private data directory use the feature flag.
    os.environ['GEE_DOWNLOAD_SNAPSHOTS']='1'
    # Do not restore the whole environment after provider initialization:
    # init_ee installs the corporate trust bundle in this CLI process.
    with patch.object(api,'_DOWNLOADS_DIR',args.output.resolve()):
        response=api.find_scene(api.SceneRequest(**fields))
        snapshot=api._get_download_store().get(response.selection_token)
    search_s=time.perf_counter()-search_started
    durations=[]
    settings=DownloadSettings(vrt=True)
    plan=export_plan(snapshot,settings)
    for run in range(args.repeat):
        root=args.output/('warm-cache' if args.cache=='warm' else f'cold-cache-{run}')
        store=SelectionStore(root)
        try:
            store.create(snapshot)
        except __import__('sqlite3').IntegrityError:
            pass
        metrics=DownloadMetrics(f'live-{run}')
        started=time.perf_counter()
        if args.mode=='optimized':
            receipt=execute_selection(snapshot,store,settings=settings,metrics=metrics)
        else:
            image=restore_selection(snapshot)
            source,out=root/'source.tif',root/'legacy.tif'
            with metrics.phase('planning_probe'):
                urls=imagery.download_geotiff(image,'sentinel2',plan['bbox'],10,
                                             s2_auxiliary_bands=fields['s2_auxiliary_bands'])
            with metrics.phase('transfer_merge_compress'):
                imagery.merge_band_tiffs(urls,source if plan['grid'] else out,preserve_masks=True)
            if plan['grid']:
                with metrics.phase('warp_compress'):
                    warp_to_grid(source,out,plan['grid'])
                source.unlink()
            receipt=validate_artifact(out,plan)
        durations.append(time.perf_counter()-started)
        metrics.write_report(args.output/f'live-{run}.json')
        print(f'Live run {run+1}/{args.repeat}: {durations[-1]:.3f}s, {receipt.size} bytes',flush=True)
    report=dict(case=args.case,mode=args.mode,cache=args.cache,os_cache='uncontrolled',
                scope='live GEE server export; excludes search and browser delivery',selection_hash=snapshot.selection_hash,
                request=snapshot.request,search_s=search_s,recipe=snapshot.recipe,
                durations_s=durations,median_s=statistics.median(durations),repeats=args.repeat,
                rasterio=rasterio.__version__,gdal=rasterio.__gdal_version__)
    (args.output/'summary.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({key:report[key] for key in ('case','mode','cache','median_s','selection_hash')},indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=['user-roi-12', 'user-roi-26', 'small', 'rotated', 'local-big'])
    parser.add_argument('--mode', required=True, choices=['legacy', 'optimized'])
    parser.add_argument('--repeat', type=int, default=5)
    parser.add_argument('--cache', choices=['cold', 'warm'], default='warm')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    if args.live:
        if args.repeat<1:
            parser.error('repeat must be positive')
        return live_benchmark(args)
    if args.case=='local-big':
        if args.mode!='optimized' or args.repeat!=1:
            parser.error('Physical BigTIFF validation requires --mode optimized --repeat 1')
        from tools.validate_large_download import validate
        return validate(args.output)
    if args.repeat < 1:
        parser.error('repeat must be positive')
    if args.cache == 'cold':
        parser.error('Cannot guarantee OS cold cache without a controlled restart; use warm and report it honestly.')
    args.output.mkdir(parents=True, exist_ok=True)
    size = 256 if args.case == 'small' else 1536
    count = 26 if args.case == 'user-roi-26' else 12
    digests = []
    durations = []
    with tempfile.TemporaryDirectory(dir=args.output) as directory:
        root = Path(directory)
        fixtures = []
        rng = np.random.default_rng(42)
        for band in range(count):
            for tile in range(2):
                path = root / f'band{band}_{tile}.tif'
                data = rng.random((size, size), dtype=np.float32)
                data[:3, :3] = -9999
                with rasterio.open(path, 'w', driver='GTiff', width=size, height=size, count=1,
                                   dtype='float32', nodata=-9999, crs='EPSG:4326',
                                   transform=from_origin(106 + tile * size * .0001, 11, .0001, .0001)) as dst:
                    dst.write(data, 1)
                digests.append(hashlib.sha256(path.read_bytes()).hexdigest())
                fixtures.append((f'B{band}_t0{tile}', path))
        groups = {}
        for name, path in fixtures:
            groups.setdefault(name.split('_t')[0], []).append(path)
        grid = dict(crs='EPSG:4326', transform=[.0001, 0, 106, 0, -.0001, 11], width=size * 2, height=size)
        for run in range(args.repeat):
            metrics = DownloadMetrics(f'local-{run}')
            source, output = root / 'source.tif', root / 'output.tif'
            started = time.perf_counter()
            if args.mode == 'legacy':
                with metrics.phase('legacy_merge_compress'):
                    imagery.merge_local_band_tiffs(groups, source, preserve_masks=True)
                with metrics.phase('warp_compress'):
                    warp_to_grid(source, output, grid)
            else:
                with metrics.phase('vrt_warp_compress'):
                    assemble_to_grid(groups, output, grid)
            durations.append(time.perf_counter() - started)
            metrics.write_report(args.output / f'run-{run}.json')
            source.unlink(missing_ok=True)
            output.unlink()
    report = dict(case=args.case, mode=args.mode, cache=args.cache, scope='local synthetic; no GEE/network/delivery',
                  dimensions=[size * 2, size], bands=count, input_digest=hashlib.sha256(''.join(digests).encode()).hexdigest(),
                  repeats=args.repeat, durations_s=durations, median_s=statistics.median(durations),
                  python=platform.python_version(), rasterio=rasterio.__version__, gdal=rasterio.__gdal_version__,
                  platform=platform.platform(), free_disk_bytes=shutil.disk_usage(args.output).free)
    (args.output / 'summary.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
