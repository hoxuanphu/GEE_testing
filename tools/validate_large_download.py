"""Explicit local synthetic >4GiB VRT/BigTIFF validation (never live imagery)."""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import numpy as np
import rasterio
from rasterio.transform import from_origin
from app.download_metrics import resource_usage
from app.raster_mosaic import assemble_to_grid
from app.raster_grid import warp_to_grid


def validate(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(output).free<14*1024**3:
        raise OSError('Need at least 14GiB free for the owned synthetic workspace')
    started=time.perf_counter()
    expected={};paths={}
    grid=dict(crs='EPSG:3857',transform=[1,0,0,0,-1,8192],width=8192,height=8192)
    with tempfile.TemporaryDirectory(prefix='physical-big-',dir=output) as directory, \
         rasterio.Env(GDAL_CACHEMAX=256*1024*1024,GDAL_NUM_THREADS='2'):
        root=Path(directory)
        rng=np.random.default_rng(4242)
        for band in range(20):
            name=f'band{band+1}';paths[name]=[]
            for half in range(2):
                path=root/f'{name}-{half}.tif';paths[name].append(path)
                with rasterio.open(path,'w',driver='GTiff',width=4096,height=8192,count=1,
                     dtype='float32',crs=grid['crs'],transform=from_origin(half*4096,8192,1,1),
                     tiled=True,blockxsize=256,blockysize=256,BIGTIFF='YES',nodata=-9999) as dst:
                    for _,window in dst.block_windows(1):
                        data=rng.random((int(window.height),int(window.width)),dtype=np.float32)
                        dst.write(data,1,window=window)
                        if half==1 and window.row_off==7936 and window.col_off==3840:
                            expected[band+1]=data
            print(f'Synthetic source {band+1}/20 ready',flush=True)
        destination=root/'physical-output.tif'
        assemble_to_grid(paths,destination,grid)
        size=destination.stat().st_size
        if size<=2**32:
            raise AssertionError('Compressed physical file did not exceed4GiB')
        with destination.open('rb') as handle:
            if handle.read(4)!=b'II+\x00': raise AssertionError('Output must be BigTIFF')
        with rasterio.open(destination) as source:
            for band in range(1,21):
                from rasterio.windows import Window
                actual=source.read(band,window=Window(7936,7936,256,256))
                np.testing.assert_array_equal(actual.view('uint32'),expected[band].view('uint32'))
        crop=dict(crs=grid['crs'],transform=[1,0,8064,0,-1,128],width=128,height=128)
        warped=root/'last-block-warp.tif'
        warp_to_grid(destination,warped,crop)
        with rasterio.open(warped) as source:
            for band in range(1,21):
                np.testing.assert_array_equal(source.read(band).view('uint32'),expected[band][-128:,-128:].view('uint32'))
        result=dict(scope='local synthetic VRT writer, not GEE download',status='passed',physical_bytes=size,
            physical_gib=size/1024**3,logical_bytes=8192*8192*20*4,bands=20,width=8192,height=8192,
            last_block_all_bands_verified=True,warp_from_beyond_4gib_verified=True,
            elapsed_s=time.perf_counter()-started,resources=resource_usage(),rasterio=rasterio.__version__,
            gdal=rasterio.__gdal_version__,temporary_rasters_removed_on_exit=True)
    (output/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result,indent=2),flush=True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    validate(parser.parse_args().output)
