import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import main
from app.timeseries import TimeSeriesService
from app.timeseries_batches import BatchStore, BatchService
from app.timeseries_batch_api import register_batch_routes


class BatchAPI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = BatchStore(Path(self.temp.name))
        self.legacy = TimeSeriesService(self.store); self.addCleanup(self.legacy.shutdown)
        self.service = BatchService(self.store,self.legacy)
        self.api = FastAPI()
        register_batch_routes(self.api,self.store,lambda:self.service)
        self.client = TestClient(self.api)
        self.body = dict(dataset='sentinel2',selection_mode='rectangle',center_lon=105.587,
                         center_lat=20.036,width_m=40,height_m=55,rotation_deg=15,
                         start_date='2020-12-31',end_date='2021-01-01',platform='A',s2_auxiliary_bands=[])

    def test_create_snapshots_without_provider_call_and_submits_same_executor(self):
        with patch.object(main,'_ensure_ee',side_effect=AssertionError('must not initialize provider')), \
             patch.object(self.legacy,'submit_task') as submit:
            response = self.client.post('/api/timeseries/batches',json=self.body)
        self.assertEqual(response.status_code,200,response.text)
        batch = response.json()
        self.assertEqual(batch['status'],'queued')
        self.assertEqual([r['total'] for r in batch['years']],[None,None])
        self.assertEqual(batch['parameters']['platform'],'A')
        self.assertIn('output_grid',batch['parameters'])
        self.assertEqual(batch['parameters']['roi_geometry']['type'],'Polygon')
        submit.assert_called_once()
        self.body['width_m']=200
        saved = self.client.get('/api/timeseries/batches/'+batch['batch_id']).json()
        self.assertEqual(saved['parameters']['width_m'],40)
        with patch.object(self.legacy, 'submit_task'):
            next_batch = self.client.post('/api/timeseries/batches',json=self.body)
        self.assertEqual(next_batch.status_code, 200)
        self.assertEqual(next_batch.json()['status'], 'queued')

    def test_rgbnir_batch_freezes_four_bands_for_all_years(self):
        with patch.object(self.legacy, 'submit_task'):
            response = self.client.post('/api/timeseries/batches',
                                        json={**self.body, 's2_band_preset': 'rgbnir'})
        self.assertEqual(response.status_code, 200, response.text)
        batch = response.json()
        saved = self.client.get('/api/timeseries/batches/' + batch['batch_id']).json()
        self.assertEqual(saved['parameters']['bands'], ['B2', 'B3', 'B4', 'B8'])

    def test_invalid_and_missing_ids_have_safe_status(self):
        for path in ('bad','00000000-0000-0000-0000-000000000000'):
            self.assertEqual(self.client.get('/api/timeseries/batches/'+path).status_code,404)
            self.assertEqual(self.client.post('/api/timeseries/batches/'+path+'/resume').status_code,404)
        self.assertEqual(self.client.post('/api/timeseries/batches',json={**self.body,'lon':105}).status_code,422)
        self.assertEqual(self.client.post('/api/timeseries/batches',json={**self.body,'dataset':'s2aws'}).status_code,422)

    def test_pause_and_resume_use_existing_snapshot(self):
        with patch.object(self.legacy,'submit_task'):
            batch = self.client.post('/api/timeseries/batches',json=self.body).json()
            prefix='/api/timeseries/batches/'+batch['batch_id']
            self.assertEqual(self.client.post(prefix+'/pause').json()['status'],'paused')
            self.assertEqual(self.client.post(prefix+'/resume').json()['status'],'queued')
            self.assertEqual(self.client.post(prefix+'/resume').status_code,409)
            self.assertEqual(self.client.get(prefix+'/years/2020/download').status_code,409)
            self.assertEqual(self.client.get(prefix+'/years/2019/csv').status_code,404)

    def test_vector_snapshot_survives_source_removal(self):
        from app.vector_import import read_vector
        geometry={'type':'Polygon','coordinates':[[[105,20],[105.01,20],[105.01,20.01],[105,20]]]}
        import json
        imported=main._vector_store.save_import(read_vector(json.dumps(geometry).encode(),'roi.json'))
        roi=main._vector_store.create_roi([dict(import_id=imported['import_id'],feature_ids=['f1'])])
        body=dict(dataset='dynamic_world',selection_mode='vector',vector_roi_id=roi['roi_id'],
                  start_date='2020-12-31',end_date='2021-01-01')
        with patch.object(self.legacy,'submit_task'):
            response=self.client.post('/api/timeseries/batches',json=body)
        self.assertEqual(response.status_code,200,response.text)
        main._vector_store.delete_import(imported['import_id'])
        self.assertEqual(response.json()['parameters']['roi_geometry'],roi['geometry'])


if __name__=='__main__':unittest.main()
