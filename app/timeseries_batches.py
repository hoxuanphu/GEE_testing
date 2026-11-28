"""Year-at-a-time exports with immutable listings and durable scene checkpoints."""
from __future__ import annotations

import csv
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
import uuid
import zipfile

import rasterio

from .timeseries import (TimeSeriesStore, BusyError, DiskSpaceError, TTL_SECONDS,
                         _safe, _scene_filename, align_timeseries_raster)
from .vector_mask import mask_raster_to_polygon

ACTIVE = ('queued', 'running', 'pausing')


class BatchPaused(Exception):
    pass


def list_year_scenes(*args, **kwargs):
    from .timeseries_transport import list_year_scenes as operation
    return operation(*args, **kwargs)


def export_batch_scene(*args, **kwargs):
    from .timeseries_transport import export_batch_scene as operation
    return operation(*args, **kwargs)


def _digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def _fatal(exc):
    return (getattr(exc, 'fatal', False) or isinstance(exc, (DiskSpaceError, PermissionError))
            or getattr(exc, 'errno', None) == 28
            or getattr(exc, 'code', None) in ('upstream_unauthenticated', 'upstream_forbidden'))


def _error(exc):
    if isinstance(exc, DiskSpaceError) or getattr(exc, 'errno', None) == 28:
        return 'Không đủ dung lượng đĩa; đã dừng an toàn.'
    if getattr(exc, 'code', None) and isinstance(getattr(exc, 'message', None), str):
        return exc.message
    return f'Không thể hoàn tất thao tác ({type(exc).__name__}).'


class BatchStore(TimeSeriesStore):
    def __init__(self, root):
        super().__init__(root)
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ts_batches (
                  id TEXT PRIMARY KEY, parameters TEXT NOT NULL, status TEXT NOT NULL,
                  grid TEXT, current_year INTEGER, error TEXT, pause_requested INTEGER NOT NULL DEFAULT 0,
                  created REAL NOT NULL, activity REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS ts_years (
                  batch_id TEXT NOT NULL, year INTEGER NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', total INTEGER, error TEXT,
                  listed INTEGER NOT NULL DEFAULT 0, zip_ready INTEGER NOT NULL DEFAULT 0,
                  csv_ready INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(batch_id,year));
                CREATE TABLE IF NOT EXISTS ts_scenes (
                  batch_id TEXT NOT NULL, year INTEGER NOT NULL, asset_id TEXT NOT NULL, metadata TEXT NOT NULL,
                  status TEXT NOT NULL DEFAULT 'pending', file TEXT, checksum TEXT, error TEXT,
                  PRIMARY KEY(batch_id,year,asset_id));
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(ts_batches)')}
            if 'enqueued_at' not in columns:
                db.execute('ALTER TABLE ts_batches ADD COLUMN enqueued_at REAL')
                db.execute('UPDATE ts_batches SET enqueued_at=created WHERE enqueued_at IS NULL')

    def queued_tasks(self):
        with self._connect() as db:
            return db.execute("""SELECT id,created AS enqueued_at,'timeseries' AS kind
                    FROM jobs WHERE status='queued'
                    UNION ALL
                    SELECT id,COALESCE(enqueued_at,created),'annual' AS kind
                    FROM ts_batches WHERE status='queued'
                    ORDER BY enqueued_at,id""").fetchall()

    def directory(self, batch_id, year=None):
        if not isinstance(batch_id, str) or str(uuid.UUID(batch_id)) != batch_id:
            raise ValueError('Invalid batch ID')
        path = self.root / 'batches' / batch_id
        if year is not None:
            if isinstance(year, bool) or not isinstance(year, int) or not 1 <= year <= 9999:
                raise ValueError('Invalid year')
            path /= str(year)
        path = path.resolve()
        path.relative_to(self.root)
        return path

    def create_batch(self, parameters):
        parameters = _safe(parameters)
        start, end = date.fromisoformat(parameters['start_date']), date.fromisoformat(parameters['end_date'])
        if start > end:
            raise ValueError('Invalid date interval')
        batch_id, now = str(uuid.uuid4()), time.time()
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO ts_batches(id,parameters,status,grid,created,activity,enqueued_at) VALUES(?,?,?,?,?,?,?)',
                (batch_id,json.dumps(parameters),'queued',json.dumps(parameters.get('output_grid')),now,now,now))
            for year in range(start.year, end.year+1):
                first, last = max(start,date(year,1,1)), min(end,date(year,12,31))
                db.execute('INSERT INTO ts_years(batch_id,year,start_date,end_date) VALUES(?,?,?,?)',
                           (batch_id,year,first.isoformat(),last.isoformat()))
        return self.get_batch(batch_id)

    def snapshot(self, batch_id):
        self.directory(batch_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM ts_batches WHERE id=?',(batch_id,)).fetchone()
        if row is None:
            raise ValueError('Batch not found or expired')
        return {**dict(row), 'parameters':json.loads(row['parameters']), 'grid':json.loads(row['grid'] or 'null')}

    def get_batch(self, batch_id, *, touch=True):
        self.directory(batch_id)
        with self._connect() as db:
            row = db.execute('SELECT * FROM ts_batches WHERE id=?',(batch_id,)).fetchone()
            if row is None:
                return None
            if touch:
                db.execute('UPDATE ts_batches SET activity=? WHERE id=?',(time.time(),batch_id))
            years = []
            for year in db.execute('SELECT * FROM ts_years WHERE batch_id=? ORDER BY year',(batch_id,)):
                counts = dict(db.execute('SELECT status,COUNT(*) FROM ts_scenes WHERE batch_id=? AND year=? GROUP BY status',
                                        (batch_id,year['year'])).fetchall())
                item = {key:year[key] for key in ('year','start_date','end_date','status','total','error')}
                item.update(succeeded=counts.get('succeeded',0), failed=counts.get('failed',0),
                            processed=counts.get('succeeded',0)+counts.get('failed',0))
                prefix = f'/api/timeseries/batches/{batch_id}/years/{year["year"]}'
                directory = self.directory(batch_id, year['year'])
                item['zip_url'] = prefix+'/download' if year['zip_ready'] and (directory / 'timeseries.zip').is_file() else None
                item['csv_url'] = prefix+'/csv' if year['csv_ready'] and (directory / 'manifest.csv').is_file() else None
                years.append(item)
        return dict(batch_id=batch_id,status=row['status'],parameters=json.loads(row['parameters']),
                    current_year=row['current_year'],error=row['error'],years=years)

    def list_batches(self):
        with self._connect() as db:
            rows = db.execute("""SELECT id,created FROM ts_batches b
                WHERE NOT EXISTS (SELECT 1 FROM hidden_tasks h
                                  WHERE h.kind='annual' AND h.id=b.id)
                ORDER BY created DESC""").fetchall()
        return [{**batch, 'created': row['created']} for row in rows
                if (batch := self.get_batch(row['id'], touch=False)) is not None]

    def hide_batch(self, batch_id, *, delete_files=False):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM ts_batches WHERE id=?', (batch_id,)).fetchone()
            if row is None:
                return False
            if row['status'] in ACTIVE:
                raise BusyError('Hãy tạm dừng và chờ tác vụ dừng trước khi xóa.')
            if delete_files:
                from .task_files import remove_task_directory
                try:
                    remove_task_directory(self.root / 'batches' / batch_id, self.root / 'batches')
                except RuntimeError as exc:
                    raise BusyError(str(exc)) from None
            db.execute("INSERT OR IGNORE INTO hidden_tasks VALUES ('annual', ?)", (batch_id,))
        return True

    def update(self, batch_id, **values):
        if not set(values) <= {'status','grid','current_year','error','pause_requested'}:
            raise ValueError('Invalid batch update')
        if 'grid' in values:
            values['grid'] = json.dumps(values['grid'])
        values['activity'] = time.time()
        with self._connect() as db:
            db.execute(f'UPDATE ts_batches SET {",".join(k+"=?" for k in values)} WHERE id=?',(*values.values(),batch_id))

    def finish_batch(self, batch_id, status):
        with self._connect() as db:
            db.execute("""UPDATE ts_batches SET
                status=CASE WHEN pause_requested=1 THEN 'paused' ELSE ? END,
                pause_requested=0,current_year=NULL,activity=? WHERE id=?""",
                (status, time.time(), batch_id))

    def update_year(self, batch_id, year, **values):
        if not set(values) <= {'status','total','listed','error','zip_ready','csv_ready'}:
            raise ValueError('Invalid year update')
        with self._connect() as db:
            db.execute(f'UPDATE ts_years SET {",".join(k+"=?" for k in values)} WHERE batch_id=? AND year=?',
                       (*values.values(),batch_id,year))
            db.execute('UPDATE ts_batches SET activity=? WHERE id=?',(time.time(),batch_id))

    def year_rows(self, batch_id):
        with self._connect() as db:
            return [dict(row) for row in db.execute('SELECT * FROM ts_years WHERE batch_id=? ORDER BY year',(batch_id,))]

    def scene_rows(self, batch_id, year):
        with self._connect() as db:
            rows = db.execute('SELECT * FROM ts_scenes WHERE batch_id=? AND year=? ORDER BY asset_id',(batch_id,year)).fetchall()
        return [{**dict(row),'metadata':json.loads(row['metadata'])} for row in rows]

    def save_listing(self, batch_id, year, scenes):
        # A complete list and its authoritative total become visible in one transaction.
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for scene in scenes:
                db.execute('INSERT INTO ts_scenes(batch_id,year,asset_id,metadata) VALUES(?,?,?,?)',
                           (batch_id,year,scene['asset_id'],json.dumps(_safe(scene))))
            db.execute("UPDATE ts_years SET listed=1,total=?,status='downloading',error=NULL WHERE batch_id=? AND year=?",
                       (len(scenes),batch_id,year))

    def checkpoint(self, batch_id, year, asset_id, status, *, file=None, checksum=None, error=None, grid=None):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE ts_scenes SET status=?,file=?,checksum=?,error=? WHERE batch_id=? AND year=? AND asset_id=?',
                       (status,file,checksum,error,batch_id,year,asset_id))
            if grid is not None:
                db.execute('UPDATE ts_batches SET grid=? WHERE id=?',(json.dumps(grid),batch_id))
            db.execute('UPDATE ts_batches SET activity=? WHERE id=?',(time.time(),batch_id))

    def pause(self, batch_id):
        self.snapshot(batch_id)
        with self._connect() as db:
            db.execute("""UPDATE ts_batches SET pause_requested=1,
                       status=CASE WHEN status='queued' THEN 'paused' ELSE 'pausing' END,
                       activity=? WHERE id=? AND status IN ('queued','running','pausing')""",
                       (time.time(),batch_id))
        return self.get_batch(batch_id)

    def prepare_resume(self, batch_id, retry=False):
        self.snapshot(batch_id)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM ts_batches WHERE id=?',(batch_id,)).fetchone()
            allowed = ('paused','interrupted','failed','partial','completed') if retry else ('paused','interrupted')
            if row['status'] not in allowed:
                raise ValueError('Batch cannot resume in its current state')
            db.execute("DELETE FROM hidden_tasks WHERE kind='annual' AND id=?", (batch_id,))
            now = time.time()
            db.execute("UPDATE ts_batches SET status='queued',pause_requested=0,error=NULL,activity=?,enqueued_at=? WHERE id=?",(now,now,batch_id))
        return self.get_batch(batch_id)

    def recover(self):
        with self._connect() as db:
            return db.execute("UPDATE ts_batches SET status='interrupted',pause_requested=0,error=?,activity=? WHERE status IN ('running','pausing')",
                              ('Ứng dụng đã khởi động lại; bấm Tiếp tục.',time.time())).rowcount

    def artifact(self, batch_id, year, kind):
        directory = self.directory(batch_id,year)
        with self._connect() as db:
            row = db.execute('SELECT * FROM ts_years WHERE batch_id=? AND year=?',(batch_id,year)).fetchone()
        if row is None or kind not in ('download','csv') or not row['zip_ready' if kind=='download' else 'csv_ready']:
            return None
        path = directory / ('timeseries.zip' if kind=='download' else 'manifest.csv')
        return path if path.is_file() else None

    def cleanup(self, now=None):
        now = time.time() if now is None else now
        removed = 0
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT id,status,activity FROM ts_batches WHERE status NOT IN ('queued','running','pausing')").fetchall()
            for row in rows:
                ttl = 7*TTL_SECONDS if row['status'] in ('paused','interrupted') else TTL_SECONDS
                if row['activity'] >= now-ttl:
                    continue
                from .task_files import remove_task_directory
                directory = self.root / 'batches' / row['id']
                try:
                    remove_task_directory(directory, self.root / 'batches')
                except (RuntimeError, ValueError, OSError):
                    continue
                for table in ('ts_scenes','ts_years'):
                    db.execute(f'DELETE FROM {table} WHERE batch_id=?',(row['id'],))
                db.execute('DELETE FROM ts_batches WHERE id=?',(row['id'],))
                removed += 1
        return removed


class BatchService:
    def __init__(self, store, legacy_service):
        self.store, self.legacy = store, legacy_service

    def _schedule(self, batch):
        try:
            self.legacy.submit_task('batch:'+batch['batch_id'],self.run_batch,batch['batch_id'])
        except Exception:
            self.store.update(batch['batch_id'],status='failed',error='Không khởi chạy được worker.')
            raise
        return batch

    def start(self, parameters):
        with self.legacy._lock:
            return self._schedule(self.store.create_batch(parameters))

    def resume(self, batch_id, retry=False):
        with self.legacy._lock:
            return self._schedule(self.store.prepare_resume(batch_id,retry=retry))

    def pause(self, batch_id):
        with self.legacy._lock:
            batch = self.store.pause(batch_id)
            if batch['status'] == 'paused':
                future = self.legacy._futures.get('batch:' + batch_id)
                if future is not None and not future.cancel():
                    future.result()
            return batch

    def pause_active(self):
        """Let graceful app shutdown stop at a checkpoint before executor join."""
        with self.store._connect() as db:
            db.execute("UPDATE ts_batches SET pause_requested=1,status='pausing',activity=? WHERE status IN ('running','pausing')",
                       (time.time(),))

    def _check(self, batch_id):
        if self.store.snapshot(batch_id)['pause_requested']:
            raise BatchPaused()

    @staticmethod
    def _valid_file(path, row, parameters, grid):
        try:
            if not grid or not path.is_file() or _digest(path) != row['checksum']:
                return False
            with rasterio.open(path) as src:
                return (src.count==len(parameters['bands']) and src.width==grid['width'] and src.height==grid['height']
                        and src.crs==rasterio.crs.CRS.from_user_input(grid['crs'])
                        and all(abs(a-b)<1e-8 for a,b in zip(list(src.transform)[:6],grid['transform'])))
        except (OSError,ValueError,rasterio.errors.RasterioError):
            return False

    def _package(self, batch_id, year, parameters, grid):
        from .scene_inventory import _csv_cell
        directory = self.store.directory(batch_id,year)
        rows = self.store.scene_rows(batch_id,year)
        records = [{**row['metadata'],'status':row['status'],'file':row['file'],'error':row['error']} for row in rows]
        manifest = _safe(dict(batch_id=batch_id,year=year,parameters=parameters,grid=grid,scenes=records,
                              total=len(rows),succeeded=sum(r['status']=='succeeded' for r in rows),
                              failed=sum(r['status']=='failed' for r in rows)))
        for name in ('manifest.json','manifest.csv'):
            temporary = directory/(name+'.part')
            if name.endswith('json'):
                temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
            else:
                with temporary.open('w',encoding='utf-8-sig',newline='') as output:
                    columns = ['asset_id','name','date','timestamp','cloud_pct','status','file','error','source_metadata']
                    writer = csv.DictWriter(output,fieldnames=columns)
                    writer.writeheader()
                    for record in records:
                        writer.writerow({key:_csv_cell(json.dumps(record.get(key),ensure_ascii=False)
                            if isinstance(record.get(key),(dict,list)) else record.get(key,'')) for key in columns})
            os.replace(temporary,directory/name)
        self.store.update_year(batch_id,year,csv_ready=1,
                               error=next((row['error'] for row in records if row['error']),None))
        succeeded = [r for r in records if r['status']=='succeeded']
        if succeeded:
            self.legacy._check_disk(parameters,directory,archive=True)
            temporary = directory/'timeseries.zip.part'
            try:
                with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,allowZip64=True) as archive:
                    for record in succeeded:
                        self._check(batch_id)
                        archive.write(directory/record['file'],record['file'])
                    for name in ('manifest.json','manifest.csv'):
                        archive.write(directory/name,name)
                os.replace(temporary,directory/'timeseries.zip')
                self.store.update_year(batch_id,year,zip_ready=1)
            finally:
                temporary.unlink(missing_ok=True)
        return ('empty' if not records else 'completed' if len(succeeded)==len(records)
                else 'partial' if succeeded else 'failed')

    def run_batch(self, batch_id):
        with self.store._connect() as db:
            claimed = db.execute("UPDATE ts_batches SET status='running' WHERE id=? AND status IN ('queued','pausing')",(batch_id,))
            if claimed.rowcount != 1:
                return
        snapshot = self.store.snapshot(batch_id)
        parameters, grid = snapshot['parameters'],snapshot['grid']
        check = lambda:self._check(batch_id)
        try:
            for year_row in self.store.year_rows(batch_id):
                check()
                year = year_row['year']
                self.store.update(batch_id,current_year=year)
                directory = self.store.directory(batch_id,year)
                directory.mkdir(parents=True,exist_ok=True)
                if not year_row['listed']:
                    self.store.update_year(batch_id,year,status='searching',error=None)
                    try:
                        scenes = list_year_scenes(parameters,year_row['start_date'],year_row['end_date'],check=check)
                        self.store.save_listing(batch_id,year,scenes)
                    except BatchPaused:
                        raise
                    except Exception as exc:
                        self.store.update_year(batch_id,year,status='failed',error=_error(exc))
                        if _fatal(exc): raise
                        continue
                self.store.update_year(batch_id,year,status='downloading',error=None)
                for row in self.store.scene_rows(batch_id,year):
                    check()
                    scene = row['metadata']
                    filename = _scene_filename(parameters['dataset'],scene)
                    destination = directory/filename
                    if row['status']=='succeeded' and self._valid_file(destination,row,parameters,grid):
                        continue
                    raw, aligned = directory/'scene.raw.tif',directory/'scene.aligned.tif'
                    try:
                        self.legacy._check_disk(parameters,directory)
                        export_batch_scene(parameters,scene,raw,check=check)
                        next_grid = align_timeseries_raster(raw,aligned,grid,
                            categorical=parameters['dataset']=='dynamic_world',bands=parameters['bands'],nodata=parameters.get('nodata'))
                        if parameters.get('selection_mode')=='vector':
                            mask_raster_to_polygon(aligned,parameters['roi_geometry'])
                        checksum = _digest(aligned)
                        os.replace(aligned,destination)
                        # Geometry/grid and scene completion are committed together after rename.
                        self.store.checkpoint(batch_id,year,scene['asset_id'],'succeeded',file=filename,checksum=checksum,grid=next_grid)
                        grid = next_grid
                    except BatchPaused:
                        raise
                    except Exception as exc:
                        self.store.checkpoint(batch_id,year,scene['asset_id'],'failed',error=_error(exc))
                        if _fatal(exc): raise
                    finally:
                        raw.unlink(missing_ok=True)
                        aligned.unlink(missing_ok=True)
                check()
                self.store.update_year(batch_id,year,status='packaging')
                status = self._package(batch_id,year,parameters,grid)
                self.store.update_year(batch_id,year,status=status)
            years = self.store.get_batch(batch_id)['years']
            good = any(y['status'] in ('completed','empty','partial') for y in years)
            bad = any(y['status'] in ('failed','partial') for y in years)
            self.store.finish_batch(batch_id, 'partial' if good and bad else 'failed' if bad else 'completed')
        except BatchPaused:
            self.store.update(batch_id,status='paused',pause_requested=0,error=None)
        except Exception as exc:
            if snapshot is not None:
                current = self.store.snapshot(batch_id)['current_year']
                if current is not None:
                    self.store.update_year(batch_id,current,status='failed',error=_error(exc))
            self.store.update(batch_id,status='failed',error=_error(exc))
