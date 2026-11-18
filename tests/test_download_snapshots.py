import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ee
from ee.apitestcase import ApiTestCase
from app import imagery


class SnapshotGraphTests(ApiTestCase):
    def setUp(self):
        install = ee.data._install_cloud_api_resource
        super().setUp()
        self.addCleanup(setattr, ee.data, '_install_cloud_api_resource', install)

    def test_raw_trace_keeps_selected_noncontributors_and_direct_assets(self):
        from app.download_models import SelectionTrace
        rows = [dict(**{'system:index': f'scene-{i}', 'system:time_start': 1767225600000},
                     MGRS_TILE=f'tile-{i % 8}', cloud=10) for i in range(16)]
        collection = ee.ImageCollection(imagery.S2_COLLECTION)
        trace = SelectionTrace()
        # Primary coverage 95%, then all 16 selected but only eight contribute.
        with mock.patch.object(ee.List, 'getInfo', return_value=rows), mock.patch.object(
                ee.Dictionary, 'getInfo', side_effect=[{'-1': 5, '15': 95},
                {str(i): 12.5 for i in range(8, 16)}]):
            image, picked = imagery._raw_gap_fill(collection, collection, 'sentinel2', 'cloud',
                ee.Geometry.Rectangle([106, 10, 106.1, 10.1]), selection_trace=trace)
        self.assertEqual(len(trace.assets), 16)
        self.assertEqual(len(picked), 8)
        self.assertEqual(trace.priority, list(range(8)) + list(range(8, 16)))
        self.assertEqual(len(trace.layers), 2)
        graph = image.serialize()
        self.assertIn(imagery.S2_COLLECTION + '/scene-0', graph)
        self.assertNotIn('ImageCollection.load', graph)
        self.assertNotIn('Collection.first', graph)
        self.assertIn('Image.gt', graph)

    def test_freeze_restore_persistence_mismatch_expiry_and_pinning(self):
        from app.download_snapshots import freeze_selection, restore_selection
        from app.download_store import SelectionStore, SelectionExpired, SelectionMismatch
        scene = imagery.SceneInfo('sentinel2', imagery.S2_COLLECTION, 'a', '2026-01-01', 1,
                                  imagery.load_timeseries_scene(imagery.S2_COLLECTION + '/a', 'sentinel2'))
        with tempfile.TemporaryDirectory() as root:
            store = SelectionStore(Path(root))
            request = {'bands': [], 'grid': {'width': 2}}
            snapshot = freeze_selection(scene, request, provider_scope='test-project', now=100, ttl=10)
            store.create(snapshot)
            request['bands'].append('QA60')
            loaded = SelectionStore(Path(root)).get(snapshot.token, now=101)
            self.assertEqual(loaded.request['bands'], [])
            self.assertEqual(restore_selection(loaded).serialize(), scene.ee_object.serialize())
            with self.assertRaises(SelectionMismatch):
                store.get(snapshot.token, request={'bands': None}, now=101)
            with self.assertRaises(SelectionMismatch):
                store.get(snapshot.token, provider_scope='other', now=101)
            store.pin(snapshot.token, 'attempt')
            store.cleanup(now=200)
            self.assertEqual(store.get(snapshot.token, now=200, pinned_by='attempt').token, snapshot.token)
            with self.assertRaises(SelectionExpired):
                store.get(snapshot.token, now=200)
            store.unpin('attempt')
            store.cleanup(now=200)
            with self.assertRaises(SelectionExpired):
                store.get(snapshot.token, now=200)

    def test_mutable_catalog_graph_cannot_be_frozen(self):
        from app.download_snapshots import freeze_selection
        scene = imagery.SceneInfo('sentinel2', imagery.S2_COLLECTION, 'a', '2026', 1,
                                  ee.Image(ee.ImageCollection(imagery.S2_COLLECTION).first()))
        with self.assertRaisesRegex(ValueError, 'bound'):
            freeze_selection(scene, {}, provider_scope='test')


class SnapshotAPITests(unittest.TestCase):
    def test_older_snapshot_accepts_new_default_options_but_rejects_changed_policy(self):
        from dataclasses import replace
        from app import main
        from app.download_store import SelectionStore, SelectionMismatch
        from tests.test_download_jobs import selection
        request = main.SceneRequest(dataset='sentinel2', lon=107, lat=10,
            start_date='2026-01-01', end_date='2026-01-31', merge_scenes=True).model_dump()
        old = {key: value for key, value in request.items()
               if key not in ('scene_grouping', 'max_scene_span_days', 'mosaic_mask_clouds')}
        with tempfile.TemporaryDirectory() as root:
            store = SelectionStore(root)
            original = selection(store)
            legacy = replace(original, token='legacy', request=old)
            store.create(legacy)
            self.assertEqual(store.get('legacy', request=request).token, 'legacy')
            with self.assertRaises(SelectionMismatch):
                store.get('legacy', request={**request, 'scene_grouping': 'near_date'})

    def test_token_download_skips_search_and_rejects_changed_bands(self):
        from fastapi.testclient import TestClient
        from app import main
        from app.download_models import FrozenSelection, PIPELINE_VERSION
        from app.download_store import SelectionStore
        from app.geo import Bbox
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        body = dict(dataset='sentinel2', lon=107, lat=10, start_date='2026-01-01',
                    end_date='2026-01-31', merge_scenes=True, s2_auxiliary_bands=[])
        image = object()
        scene = imagery.SceneInfo('sentinel2', imagery.S2_COLLECTION, 'raw', '2026-01-01', 10, image)
        def frozen(scene, request, **kwargs):
            return FrozenSelection('token', 'selection', request, '{}', {},
                {k:v for k,v in scene.__dict__.items() if k not in {'ee_object','selection_recipe'}},
                'scope', PIPELINE_VERSION, 1., 9999999999.)
        def write(urls, path, **kwargs):
            with rasterio.open(path, 'w', driver='GTiff', width=2, height=2, count=1,
                               dtype='float32', crs='EPSG:4326', transform=from_origin(106,11,1,1)) as dst:
                dst.write(np.zeros((2,2), dtype='float32'), 1)
        with tempfile.TemporaryDirectory() as root, mock.patch.object(main, '_DOWNLOADS_DIR', Path(root)), \
             mock.patch.dict('os.environ', {'GEE_DOWNLOAD_SNAPSHOTS':'1'}), \
             mock.patch.object(main, '_ensure_ee'), \
             mock.patch.object(main, '_resolve_selection', return_value=('roi', Bbox(106,9,108,11),None)), \
             mock.patch.object(main, 'best_scene', return_value=scene) as search, \
             mock.patch.object(main, 'preview_rgb', return_value='preview'), \
             mock.patch('app.download_snapshots.provider_scope', return_value='scope'), \
             mock.patch('app.download_snapshots.freeze_selection', side_effect=frozen), \
             mock.patch('app.download_snapshots.restore_selection', return_value=image), \
             mock.patch.object(main, 'download_geotiff', return_value=[]), \
             mock.patch.object(main, 'merge_band_tiffs', side_effect=write), TestClient(main.app) as client:
            response = client.post('/api/scene', json=body)
            self.assertEqual(response.status_code,200,response.text)
            data = response.json()
            self.assertEqual(data['selection_token'], 'token')
            self.assertTrue(search.call_args.kwargs['freeze_assets'])
            search.side_effect = AssertionError('No reranking on token-bound download')
            response = client.get(data['download_url'])
            self.assertEqual(response.status_code,200)
            response = client.get(data['download_url'].replace('s2_auxiliary_bands=', 's2_auxiliary_bands=QA60'))
            self.assertEqual(response.status_code,409,response.text)
            response = client.get(data['download_url'].replace('selection_token=token','selection_token=missing'))
            self.assertEqual(response.status_code,410,response.text)
