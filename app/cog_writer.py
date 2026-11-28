"""Run GDAL's COG copy in a child process so a large local write can be cancelled."""
from pathlib import Path
import subprocess
import sys


def write_cog(source, destination, *, check):
    check()
    worker = Path(__file__).resolve()
    with subprocess.Popen([sys.executable, str(worker), str(source), str(destination)],
                          stdin=subprocess.DEVNULL,
                          creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as process:
        try:
            while True:
                check()
                try:
                    exit_code = process.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        except BaseException:
            if process.poll() is None:
                process.kill()
            raise
    check()
    if exit_code:
        raise RuntimeError('GDAL could not create the Cloud Optimized GeoTIFF')


if __name__ == '__main__':
    from rasterio import Env
    from rasterio.shutil import copy

    with Env(GDAL_CACHEMAX=256 * 1024**2):
        copy(sys.argv[1], sys.argv[2], driver='COG', BIGTIFF='YES', BLOCKSIZE=512,
             COMPRESS='DEFLATE', PREDICTOR='FLOATING_POINT', RESAMPLING='NEAREST', NUM_THREADS=2)
