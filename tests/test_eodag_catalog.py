"""Catalogue dispatch and immutable, non-downloadable EODAG snapshots."""
import importlib.util
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main, eodag_adapter
from app.catalog_models import CatalogFailure, CatalogResult, CatalogUnavailable
from app.scene_catalog import register_scene_catalog


class TestEodagCatalog(unittest.TestCase):
    def setUp(self):
        self.directory = Path(__file__).resolve().parent.parent / 'downloads' / ('eodag-test-' + uuid.uuid4().hex)
        self.addCleanup(lambda: shutil.rmtree(self.directory, ignore_errors=True))
        self.env = patch.dict(os.environ, EODAG_ENABLED='true', EODAG_PROVIDERS='earth_search,planetary_computer')
        self.env.start()
        self.addCleanup(self.env.stop)
        app = FastAPI()
        with patch.object(main, '_DOWNLOADS_DIR', self.directory):
            self.store = register_scene_catalog(app)
        self.client = TestClient(app)
        self.body = dict(dataset='s2', source='eodag', provider='earth_search', lon=106, lat=10,
                         start_date='2026-01-01', end_date='2026-01-31')

    def test_validation_precedes_all_network_paths(self):
        with patch.object(eodag_adapter, 'search') as search, patch.object(main, '_ensure_ee') as ee:
            for changes in ({'provider': None}, {'provider': 'not-allowed'}, {'eodag_collection': 'S2_MSI_L2A'},
                            {'dataset': 's2aws'}, {'dataset': 'sentinel2'}, {'dataset': 'landsat'},
                            {'source': 'aws'}, {'source': 'cdse'}):
                with self.subTest(changes=changes):
                    self.assertEqual(self.client.post('/api/scene-catalog', json={**self.body, **changes}).status_code, 422)
            search.assert_not_called()
            ee.assert_not_called()

    def test_snapshot_pages_capabilities_and_no_download_dispatch(self):
        product = SimpleNamespace(provider='earth_search', properties={'id': 'exact', 'eo:cloud_cover': 0},
                                  geometry=None, assets={'B02': {'href': 'https://private/?token=secret'}})
        row = eodag_adapter.normalize(product, 'earth_search')
        result = CatalogResult([{**row, 'id': str(i)} for i in range(61)], dict(truncated=False, completion='complete',
                              eodag_collection='S2_MSI_L2A_COG', provider_limit=4000))
        with patch.object(eodag_adapter, 'search', return_value=result) as search, \
             patch.object(main, 'aws_search_s2_l2a_bbox') as aws, patch.object(main, 'search_s2_area') as cdse:
            response = self.client.post('/api/scene-catalog', json={**self.body, 'rotation_deg': 30})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            self.assertEqual(data['total'], 61)
            self.assertEqual(len(data['scenes']), 50)
            search.assert_called_once()
            self.assertEqual(search.call_args.kwargs['provider'], 'earth_search')
            self.assertEqual(search.call_args.args[0], tuple(vars(main.point_roi(106, 10, 2000, 30)[1]).values()))
            second = self.client.get(f'/api/scene-catalog/{data["search_id"]}?offset=50').json()
            self.assertEqual(len(second['scenes']), 11)
            self.assertEqual(search.call_count, 1)
            for scene in data['scenes'] + second['scenes']:
                self.assertNotIn('download_url', scene)
                self.assertEqual(scene['asset_keys'], ['B02'])
                self.assertIsNone(scene['platform'])
                self.assertEqual(scene['cloud_pct'], 0)
                self.assertFalse(scene['quicklook_available'])
                self.assertEqual(scene['download_capabilities']['status'], 'unsupported')
            snapshot = self.store._snapshot(data['search_id'])
            self.assertNotIn('secret', repr(snapshot))
            response = self.client.get(f'/api/scene-catalog/{data["search_id"]}/download/0', follow_redirects=False)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('location', response.headers)
            aws.assert_not_called()
            cdse.assert_not_called()

    def test_status_errors_and_disabled_feature(self):
        for error, code in ((CatalogUnavailable('EODAG disabled'), 503), (CatalogFailure('Provider failed'), 502),
                            (RuntimeError('https://private/?token=secret'), 502)):
            with patch.object(eodag_adapter, 'search', side_effect=error):
                response = self.client.post('/api/scene-catalog', json=self.body)
                self.assertEqual(response.status_code, code)
                self.assertNotIn('secret', response.text)
        with patch.dict(os.environ, EODAG_ENABLED='false'), patch.object(eodag_adapter, '_gateway') as gateway:
            self.assertEqual(self.client.post('/api/scene-catalog', json=self.body).status_code, 503)
            gateway.assert_not_called()

    def test_administrative_selection_uses_existing_bbox(self):
        result = CatalogResult([], dict(truncated=False, completion='complete'))
        for mode in ('province', 'commune'):
            with patch.object(main, 'get_' + mode, return_value=SimpleNamespace(bbox=main.Bbox(105, 9, 107, 11))), \
                 patch.object(eodag_adapter, 'search', return_value=result) as search:
                response = self.client.post('/api/scene-catalog', json={**self.body, 'selection_mode': mode, mode + '_id': 'fixture'})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(search.call_args.args[0], (105, 9, 107, 11))

    def test_legacy_parameter_shape_and_zero_eodag_calls(self):
        with patch.object(eodag_adapter, 'search') as search, patch.object(main, 'aws_search_s2_l2a_bbox', return_value=[]):
            body = {k: v for k, v in self.body.items() if k not in ('provider', 'source')}
            response = self.client.post('/api/scene-catalog', json={**body, 'dataset': 's2aws'})
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('provider', response.json()['parameters'])
            self.assertNotIn('eodag_collection', response.json()['parameters'])
            search.assert_not_called()

    def test_module_import_does_not_import_sdk(self):
        module_path = Path(eodag_adapter.__file__)
        spec = importlib.util.spec_from_file_location('app._lazy_eodag_test', module_path)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'eodag': None, 'eodag.config': None}):
            spec.loader.exec_module(module)
            with self.assertRaises(CatalogUnavailable):
                module._gateway(self.directory / 'eodag', 'earth_search', float('inf'))


if __name__ == '__main__':
    unittest.main()
