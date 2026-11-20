import tempfile
from dataclasses import replace
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app.download_models import TileReceipt
from app.download_store import SelectionStore
from tests.test_download_jobs import selection


class PipelineTests(unittest.TestCase):
    def test_rgbnir_reduces_frozen_download_plan_to_four_bands(self):
        from app.download_pipeline import export_plan
        from app.download_models import DownloadSettings
        with tempfile.TemporaryDirectory() as root:
            snapshot = selection(SelectionStore(root))
            full = export_plan(snapshot, DownloadSettings())
            reduced = export_plan(replace(snapshot, request={**snapshot.request, 's2_band_preset': 'rgbnir'}),
                                  DownloadSettings())
        self.assertEqual(reduced['bands'], ['B2', 'B3', 'B4', 'B8'])
        self.assertEqual(reduced['logical_bytes'] * 3, full['logical_bytes'])
        self.assertNotEqual(reduced['key'], full['key'])

    def test_recover_renamed_complete_artifact_without_provider_or_download(self):
        from app.download_pipeline import execute_selection,export_plan
        from app.download_models import DownloadSettings
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root); snapshot=selection(store)
            settings=DownloadSettings()
            plan=export_plan(snapshot,settings)
            target=store.root/'artifacts'/(plan['key']+'.tif')
            target.parent.mkdir()
            grid=plan['expected_grid']
            from affine import Affine
            with rasterio.open(target,'w',driver='GTiff',width=grid['width'],height=grid['height'],count=12,dtype='float32',
                crs=grid['crs'],transform=Affine(*grid['transform']),BIGTIFF='YES') as dst:
                dst.descriptions=tuple(plan['bands'])
                dst.write(np.zeros((12,grid['height'],grid['width']),dtype='float32'))
            from app.download_pipeline import validate_artifact
            attempt=store.claim_attempt(plan['key'])
            store.prepare_publication(attempt,validate_artifact(target,plan),target)
            store.release_attempt(attempt)
            with mock.patch('app.ee_session.init_ee',side_effect=AssertionError('no provider')), \
                 mock.patch('app.download_pipeline.download_tiles',side_effect=AssertionError('no transfer')):
                receipt=execute_selection(snapshot,store,settings=settings)
            self.assertEqual(receipt.filename,str(target))
            self.assertEqual(store.get_artifact(plan['key']).sha256,receipt.sha256)

    def test_recorded_corruption_rebuilds_and_wrong_point_grid_is_rejected(self):
        from app.download_pipeline import execute_selection,export_plan,validate_artifact
        from app.download_models import DownloadSettings
        with tempfile.TemporaryDirectory() as root:
            store=SelectionStore(root);snapshot=selection(store);plan=export_plan(snapshot,DownloadSettings())
            target=store.root/'artifacts'/(plan['key']+'.tif');target.parent.mkdir()
            grid=plan['expected_grid']
            from affine import Affine
            with rasterio.open(target,'w',driver='GTiff',width=grid['width'],height=grid['height'],count=12,
                dtype='float32',crs=grid['crs'],transform=Affine(*grid['transform']),BIGTIFF='YES') as dst:
                dst.descriptions=tuple(plan['bands']);dst.write(np.zeros((12,grid['height'],grid['width']),dtype='float32'))
            receipt=validate_artifact(target,plan)
            attempt=store.claim_attempt(plan['key']);store.save_artifact(attempt,receipt);store.release_attempt(attempt)
            with rasterio.open(target,'r+') as source:
                source.write(np.ones((grid['height'],grid['width']),dtype='float32'),1)
            with mock.patch('app.ee_session.init_ee',side_effect=AssertionError('rebuild_requested')):
                with self.assertRaisesRegex(AssertionError,'rebuild_requested'):
                    execute_selection(snapshot,store)
            self.assertIsNone(store.get_artifact(plan['key']))
            with rasterio.open(target,'r+') as source: source.transform=from_origin(100,20,.001,.001)
            with self.assertRaisesRegex(ValueError,'artifact_grid'): validate_artifact(target,plan)
