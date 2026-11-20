"""Annual orchestration, persistent checkpoints and real raster/archive contracts."""
import csv
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import numpy as np
import rasterio
from rasterio.transform import from_origin

from app import timeseries as legacy
from app import timeseries_batches as batches


class AnnualBatches(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = batches.BatchStore(Path(self.temp.name))
        self.legacy = legacy.TimeSeriesService(self.store)
        self.addCleanup(self.legacy.shutdown)
        self.service = batches.BatchService(self.store, self.legacy)
        self.parameters = dict(dataset='dynamic_world', selection_mode='vector',
            start_date='2020-12-31', end_date='2021-01-01', bands=['label'], scale_m=10,
            nodata=255, bbox=dict(west=0,south=0,east=0.0004,north=0.0004),
            roi_geometry={'type':'Polygon','coordinates':[
                [[0,0],[0.0004,0],[0.0004,0.0004],[0,0.0004],[0,0]],
                [[0.0001,0.0001],[0.0001,0.0003],[0.0003,0.0003],[0.0003,0.0001],[0.0001,0.0001]]]})
        self.events = []
        self.failed_dates = set()

    def list_scenes(self, parameters, start, end, *, check):
        check()
        self.events.append(('search', start[:4]))
        return [dict(asset_id='GOOGLE/DYNAMICWORLD/V1/' + start, name='=unsafe', date=start,
                     timestamp=start+'T00:00:00Z', cloud_pct=100)]

    def export(self, parameters, scene, path, *, check):
        check()
        self.events.append(('export', scene['date'][:4]))
        if scene['date'] in self.failed_dates:
            raise ValueError('bad source')
        with rasterio.open(path,'w',driver='GTiff',height=4,width=4,count=1,dtype='uint8',
            crs='EPSG:4326',transform=from_origin(0,0.0004,0.0001,0.0001),nodata=255) as dst:
            dst.write(np.zeros((4,4),dtype='uint8'),1)

    def run_batch(self, batch_id):
        with patch.object(batches, 'list_year_scenes', side_effect=self.list_scenes), \
             patch.object(batches, 'export_batch_scene', side_effect=self.export):
            self.service.run_batch(batch_id)
        return self.store.get_batch(batch_id)

    def create(self):
        return self.store.create_batch(self.parameters)['batch_id']

    def test_partition_clipped_dates_leap_and_unknown_totals(self):
        batch = self.store.create_batch({**self.parameters, 'start_date':'2020-02-29'})
        self.assertEqual([(y['start_date'],y['end_date']) for y in batch['years']],
                         [('2020-02-29','2020-12-31'),('2021-01-01','2021-01-01')])
        self.assertTrue(all(y['total'] is None for y in batch['years']))
        self.assertEqual(batch['parameters']['roi_geometry'],self.parameters['roi_geometry'])

    def test_year_is_finished_before_next_search_and_annual_files_preserve_hole_and_zero(self):
        batch_id = self.create()
        result = self.run_batch(batch_id)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.events,[('search','2020'),('export','2020'),('search','2021'),('export','2021')])
        grids = []
        for year in result['years']:
            archive = self.store.artifact(batch_id,year['year'],'download')
            with zipfile.ZipFile(archive) as z:
                names = z.namelist()
                self.assertEqual(len([n for n in names if n.endswith('.tif')]),1)
                manifest = json.loads(z.read('manifest.json'))
                grids.append(manifest['grid'])
                scene = manifest['scenes'][0]
                with rasterio.open(archive.parent / scene['file']) as src:
                    data = src.read(1,masked=True)
                    self.assertTrue(data.mask[1,1])
                    self.assertFalse(data.mask[0,0])
                    self.assertEqual(data[0,0],0)
            with self.store.artifact(batch_id,year['year'],'csv').open(encoding='utf-8-sig') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]['name'],"'=unsafe")
        self.assertEqual(grids[0],grids[1])

    def test_pause_resume_retains_list_and_skips_successful_scene(self):
        batch_id = self.create()
        original = self.export
        def pause_after_export(*args,**kw):
            original(*args,**kw)
            self.store.pause(batch_id)
        with patch.object(batches,'list_year_scenes',side_effect=self.list_scenes), \
             patch.object(batches,'export_batch_scene',side_effect=pause_after_export):
            self.service.run_batch(batch_id)
        self.assertEqual(self.store.get_batch(batch_id)['status'],'paused')
        self.store.prepare_resume(batch_id)
        result = self.run_batch(batch_id)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(self.events.count(('search','2020')),1)
        self.assertEqual(self.events.count(('export','2020')),1)

    def test_graceful_shutdown_requests_pause_before_waiting_for_worker(self):
        batch_id=self.create()
        original=self.export
        def shutdown_during_scene(*args,**kw):
            original(*args,**kw)
            self.service.pause_active()
        with patch.object(batches,'list_year_scenes',side_effect=self.list_scenes), \
             patch.object(batches,'export_batch_scene',side_effect=shutdown_during_scene):
            self.service.run_batch(batch_id)
        self.assertEqual(self.store.get_batch(batch_id)['status'],'paused')
        self.assertNotIn(('search','2021'),self.events)

    def test_restart_requires_resume_and_missing_success_is_exported_again(self):
        batch_id = self.create()
        self.store.recover()
        self.assertEqual(self.store.get_batch(batch_id)['status'],'interrupted')
        self.store.prepare_resume(batch_id)
        result = self.run_batch(batch_id)
        first = self.store.artifact(batch_id,2020,'download').parent
        next(first.glob('*.tif')).unlink()
        self.store.prepare_resume(batch_id,retry=True)
        self.run_batch(batch_id)
        self.assertEqual(self.events.count(('search','2020')),1)
        self.assertEqual(self.events.count(('export','2020')),2)
        self.assertEqual(self.events.count(('export','2021')),1)

    def test_corrupt_checkpoint_file_is_not_skipped(self):
        batch_id = self.create(); self.run_batch(batch_id)
        directory = self.store.artifact(batch_id,2020,'download').parent
        next(directory.glob('*.tif')).write_bytes(b'corrupt')
        self.store.prepare_resume(batch_id,retry=True)
        self.run_batch(batch_id)
        self.assertEqual(self.events.count(('export','2020')),2)

    def test_failed_scene_retry_keeps_prior_successful_year(self):
        self.failed_dates = {'2021-01-01'}
        batch_id = self.create(); result = self.run_batch(batch_id)
        self.assertEqual(result['status'],'partial')
        self.assertIn('ValueError',result['years'][1]['error'])
        self.assertIsNotNone(self.store.artifact(batch_id,2020,'download'))
        self.assertIsNone(self.store.artifact(batch_id,2021,'download'))
        self.failed_dates.clear(); self.store.prepare_resume(batch_id,retry=True)
        self.assertEqual(self.run_batch(batch_id)['status'],'completed')
        self.assertEqual(self.events.count(('export','2020')),1)

    def test_empty_year_csv_has_header_no_zip(self):
        batch_id = self.create()
        with patch.object(batches,'list_year_scenes',return_value=[]), \
             patch.object(batches,'export_batch_scene') as export:
            self.service.run_batch(batch_id)
        result = self.store.get_batch(batch_id)
        self.assertEqual(result['status'],'completed')
        self.assertTrue(all(y['status']=='empty' and y['total']==0 for y in result['years']))
        self.assertIsNone(self.store.artifact(batch_id,2020,'download'))
        self.assertIsNotNone(self.store.artifact(batch_id,2020,'csv'))
        export.assert_not_called()

    def test_failed_listing_stays_unknown_and_does_not_export_incomplete_year(self):
        batch_id = self.create()
        def search(parameters,start,end,**kw):
            if start.startswith('2020'):
                raise ValueError('incomplete list')
            return self.list_scenes(parameters,start,end,**kw)
        with patch.object(batches,'list_year_scenes',side_effect=search), \
             patch.object(batches,'export_batch_scene',side_effect=self.export):
            self.service.run_batch(batch_id)
        result = self.store.get_batch(batch_id)
        self.assertEqual(result['status'],'partial')
        self.assertIsNone(result['years'][0]['total'])
        self.assertNotIn(('export','2020'),self.events)

    def test_shared_db_admission_blocks_both_directions(self):
        search = self.store.create_search(self.parameters,[{'asset_id':'x'}])['search_id']
        self.create()
        with self.assertRaises(legacy.BusyError): self.store.create_job(search)
        self.store.recover()
        self.store.create_job(search)
        with self.assertRaises(legacy.BusyError): self.store.create_batch(self.parameters)

    def test_resume_is_atomic_with_legacy_job_admission(self):
        batch_id = self.create(); self.store.recover()
        search = self.store.create_search(self.parameters,[{'asset_id':'x'}])['search_id']
        self.store.create_job(search)
        with self.assertRaises(legacy.BusyError): self.store.prepare_resume(batch_id)

    def test_resume_queues_new_run_while_previous_future_is_still_returning(self):
        published, release = threading.Event(), threading.Event()
        original_run, original_export = self.service.run_batch,self.export
        first = True
        def delayed_return(batch_id):
            nonlocal first
            original_run(batch_id)
            if first:
                first=False
                published.set()
                release.wait(10)
        def pause_once(parameters,scene,path,**kw):
            original_export(parameters,scene,path,**kw)
            if first:self.store.pause(batch_id)
        with patch.object(batches,'list_year_scenes',side_effect=self.list_scenes), \
             patch.object(batches,'export_batch_scene',side_effect=pause_once), \
             patch.object(self.service,'run_batch',side_effect=delayed_return):
            # Reserve before scheduling so the export callback has the ID.
            batch=self.store.create_batch(self.parameters);batch_id=batch['batch_id']
            self.service._schedule(batch)
            try:
                self.assertTrue(published.wait(10),'first worker must publish paused')
                self.assertEqual(self.store.get_batch(batch_id)['status'],'paused')
                self.service.resume(batch_id)
            finally:release.set()
            self.legacy._futures['batch:'+batch_id].result(timeout=10)
        self.assertEqual(self.store.get_batch(batch_id)['status'],'completed')

    def test_fatal_initialization_failure_stops_before_second_year(self):
        from app.timeseries_transport import BatchTransportError
        batch_id=self.create()
        with patch.object(batches,'list_year_scenes',side_effect=BatchTransportError('initialization_failed','GEE initialization failed.',True)) as query:
            self.service.run_batch(batch_id)
        result=self.store.get_batch(batch_id)
        self.assertEqual(query.call_count,1)
        self.assertEqual(result['status'],'failed')
        self.assertIn('initialization',result['error'])
        self.assertEqual(result['years'][1]['status'],'pending')

    def test_disk_failure_stops_before_later_year_and_keeps_good_archives(self):
        batch_id = self.create()
        original = self.export
        def disk(parameters,scene,path,**kw):
            if scene['date'].startswith('2021'): raise legacy.DiskSpaceError()
            return original(parameters,scene,path,**kw)
        with patch.object(batches,'list_year_scenes',side_effect=self.list_scenes), \
             patch.object(batches,'export_batch_scene',side_effect=disk):
            self.service.run_batch(batch_id)
        self.assertEqual(self.store.get_batch(batch_id)['status'],'failed')
        self.assertIsNotNone(self.store.artifact(batch_id,2020,'download'))

    def test_cleanup_pins_active_children_and_paused_retention(self):
        batch_id = self.create()
        old = time.time()-2*legacy.TTL_SECONDS
        with self.store._connect() as db:
            db.execute('UPDATE ts_batches SET activity=?',(old,))
        self.assertEqual(self.store.cleanup(),0)
        self.store.recover()
        with self.store._connect() as db: db.execute('UPDATE ts_batches SET activity=?',(old,))
        self.assertEqual(self.store.cleanup(),0)
        with self.store._connect() as db: db.execute('UPDATE ts_batches SET activity=?',(time.time()-8*legacy.TTL_SECONDS,))
        self.assertEqual(self.store.cleanup(),1)
        self.assertIsNone(self.store.get_batch(batch_id))


if __name__=='__main__': unittest.main()
