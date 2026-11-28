"""Exact-scene downloads overlap tile fetch and local assembly."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import numpy as np
from fastapi import FastAPI
from rasterio.crs import CRS
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app import full_scene_export, imagery, main
from app.scene_catalog import register_scene_catalog
from app.task_center import DownloadTransferMiddleware, DownloadTransfers


class ScenePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_scene_fetches_during_assembly_and_can_be_cancelled_before_assembly(self):
        with tempfile.TemporaryDirectory() as directory:
            await self.exercise_queue(Path(directory))

    async def exercise_queue(self, directory):
        with MemoryFile() as tile:
            with tile.open(driver='GTiff', width=2, height=2, count=1, dtype='float32',
                           crs='EPSG:4326', transform=from_origin(105, 11, .1, .1)) as dst:
                dst.write(np.array([[0, 1], [2, 3]], dtype='float32'), 1)
            payload = tile.read()

        first_assembling = threading.Event()
        release_first = threading.Event()
        started = {asset: threading.Event() for asset in 'ABC'}
        real_merge = imagery.merge_local_band_tiffs

        def plan(asset, *args, **kwargs):
            return [('B2', Mock(url=Mock(return_value=asset)))]

        def fetch(asset, *args, **kwargs):
            started[asset].set()
            return payload

        def assemble(paths, output, **kwargs):
            if not first_assembling.is_set():
                first_assembling.set()
                if not release_first.wait(8):
                    raise AssertionError('test did not release the first assembly')
            return real_merge(paths, output, **kwargs)

        api = FastAPI()
        transfers = DownloadTransfers()
        api.add_middleware(DownloadTransferMiddleware, transfers=transfers)
        with patch.object(main, '_DOWNLOADS_DIR', directory), patch.object(main, '_ensure_ee'), \
             patch.object(main, '_resolve_selection', return_value=(object(), main.Bbox(105, 9, 107, 11), None)), \
             patch.object(imagery, 'load_timeseries_scene', side_effect=lambda asset, dataset: asset), \
             patch.object(imagery, 's2_export_bands', return_value=['B2']), \
             patch.object(full_scene_export, '_native_grid', return_value=(
                 2, 2, from_origin(105, 11, .1, .1), CRS.from_epsg(4326))), \
             patch.object(full_scene_export, '_band_tiles', side_effect=plan), \
             patch.object(main, 'download_geotiff', side_effect=plan), \
             patch.object(imagery, '_fetch_with_retry', side_effect=fetch), \
             patch.object(imagery, 'merge_local_band_tiffs', side_effect=assemble):
            catalog = register_scene_catalog(api)
            search = catalog.create_search(
                {'source': 'gee', 'dataset': 'sentinel2', 'selection_mode': 'point',
                 'lon': 106, 'lat': 10, 'radius_m': 100, 'rotation_deg': 0,
                 'province_id': None, 'commune_id': None},
                [{'asset_id': asset} for asset in 'ABC'],
            )
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api),
                                         base_url='http://test') as client:
                url = f'/api/scene-catalog/{search["search_id"]}/download'
                first = asyncio.create_task(client.get(f'{url}/0?extent=scene'))
                others = []
                try:
                    self.assertTrue(await asyncio.to_thread(first_assembling.wait, 2))
                    second = asyncio.create_task(client.get(f'{url}/1'))
                    others.append(second)
                    self.assertTrue(await asyncio.to_thread(started['B'].wait, 2),
                                    'scene B should fetch while scene A is still assembling')
                    third = asyncio.create_task(client.get(f'{url}/2'))
                    others.append(third)
                    async with asyncio.timeout(2):
                        while [item['stage'] for item in transfers.snapshot()] != [
                                'assembling', 'waiting_assembly', 'queued']:
                            await asyncio.sleep(.01)
                    self.assertFalse(started['C'].is_set())
                    transfers.cancel(transfers.snapshot()[1]['id'])
                    self.assertEqual((await asyncio.wait_for(second, 2)).status_code, 409)
                    self.assertTrue(await asyncio.to_thread(started['C'].wait, 2))
                finally:
                    release_first.set()
                    responses = await asyncio.gather(first, *others)

                self.assertEqual([item['status'] for item in transfers.snapshot()],
                                 ['completed', 'cancelled', 'completed'])
                for response in (responses[0], responses[2]):
                    with MemoryFile(response.content) as raster, raster.open() as image:
                        self.assertEqual(image.descriptions, ('B2',))
                        self.assertEqual(image.read(1).tolist(), [[0, 1], [2, 3]])
                with MemoryFile(responses[0].content) as raster, raster.open() as image:
                    self.assertEqual(image.tags()['SOURCE_ASSET'], 'A')
                self.assertEqual(len(list(directory.glob('*.tif'))), 1)
                self.assertFalse(list(directory.glob('*_bands_tmp')))
