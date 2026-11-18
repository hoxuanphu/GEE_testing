"""One durable worker for large exports, with generation-fenced state updates."""
from __future__ import annotations

from dataclasses import asdict
import threading
import time

from .download_metrics import DownloadMetrics
from .download_models import DownloadSettings
from .download_pipeline import execute_selection,export_plan
from .download_store import StaleAttempt,JobNotFound


class JobStopped(InterruptedError):
    pass


class WriterProfileChanged(ValueError):
    pass


class DownloadService:
    def __init__(self,store,*,runner=execute_selection,settings=None,autostart=True):
        self.store,self.runner=store,runner
        self.settings=settings or DownloadSettings()
        self._stop=threading.Event()
        self._wake=threading.Event()
        self._thread=None
        self._active_job=None
        self._active_abort=None
        self.store.recover_jobs()
        self.cleanup()
        if autostart:
            self._thread=threading.Thread(target=self._loop,name='gee-download-worker',daemon=True)
            self._thread.start()

    def start(self,selection_token):
        if self._stop.is_set():
            raise RuntimeError('download_service_closed')
        snapshot=self.store.get(selection_token)
        plan=export_plan(snapshot,self.settings)
        job=self.store.create_job(snapshot,plan['key'],asdict(self.settings))
        self._wake.set()
        return self.status(job['id'])

    def status(self,job_id):
        job=self.store.get_job(job_id)
        public={key:job[key] for key in ('id','state','stage','created_at','expires_at','tiles_total',
            'tiles_verified','received_bytes','verified_bytes','blocks_written','error_code')}
        if job['state']=='completed':
            receipt=self.store.get_artifact(job['artifact_key'])
            if receipt:
                public.update(file_url=f'/api/download-jobs/{job_id}/file',artifact_bytes=receipt.size,
                              sha256=receipt.sha256)
            else:
                public.update(state='expired',stage='expired',error_code='artifact_expired')
        return public

    def pause(self,job_id):
        return self._transition(job_id,'paused',('queued','running'))

    def cancel(self,job_id):
        return self._transition(job_id,'cancelled',('queued','running','paused','interrupted','failed'))

    def _transition(self,job_id,state,allowed):
        job=self.store.get_job(job_id)
        if job['state']==state:
            return self.status(job_id)
        self.store.update_job(job_id,generation=job['generation'],allowed_states=allowed,
                              state=state,stage=state,increment_generation=True)
        if self._active_job==job_id and self._active_abort is not None:
            self._active_abort.set()
        # State changes alone invalidate all subsequent running-worker writes;
        # generation increments on resume distinguish rapid pause/resume.
        self._wake.set()
        return self.status(job_id)

    def resume(self,job_id):
        job=self.store.get_job(job_id)
        if job['state'] in ('queued','running','completed'):
            return self.status(job_id)
        if job['state']=='cancelled':
            raise ValueError('download_job_cancelled')
        self.store.get(job['selection_token'],pinned_by='job-'+job_id)
        if job['expires_at']<=time.time():
            raise LookupError('download_job_expired')
        self.store.update_job(job_id,generation=job['generation'],allowed_states=('paused','interrupted','failed'),
                              state='queued',stage='queued',error_code=None)
        self._wake.set()
        return self.status(job_id)

    def _loop(self):
        while not self._stop.is_set():
            jobs=self.store.queued_jobs()
            if not jobs:
                self._wake.wait(1)
                self._wake.clear()
                continue
            self._run(jobs[0])

    def _run(self,job):
        job_id=job['id']
        # Start increments generation within CAS. An older worker cannot report
        # progress/completion after pause/resume/recovery changed this generation.
        try:
            job=self.store.update_job(job_id,generation=job['generation'],allowed_states=('queued',),
                                      state='running',stage='preparing',increment_generation=True)
        except (StaleAttempt,JobNotFound):
            return
        generation=job['generation']
        abort=threading.Event()
        self._active_job,self._active_abort=job_id,abort
        last_check=[0.]
        check_lock=threading.Lock()
        def check():
            if self._stop.is_set() or abort.is_set():
                raise JobStopped()
            with check_lock:
                if time.monotonic()-last_check[0]>=.2:
                    current=self.store.get_job(job_id)
                    if current['generation']!=generation or current['state']!='running':
                        raise JobStopped()
                    last_check[0]=time.monotonic()
        last_progress=[0.]
        def progress(**changes):
            check()
            self.store.update_job(job_id,generation=generation,allowed_states=('running',),**changes)
        class Metrics(DownloadMetrics):
            def record_tile(self,tile_id,**kwargs):
                super().record_tile(tile_id,**kwargs)
                # Aggregate progress at most once per second, plus tile completion.
                if time.monotonic()-last_progress[0]<1 and kwargs['event'] not in ('verified','cache_hit'):
                    return
                report=self.report()
                verified={event['tile_id']:event['bytes_count'] for event in report['tiles']
                          if event['event'] in ('verified','cache_hit')}
                progress(received_bytes=sum(event['bytes_count'] for event in report['tiles'] if event['event']=='received'),
                         verified_bytes=sum(verified.values()),tiles_verified=len(verified))
                last_progress[0]=time.monotonic()
        metrics=Metrics(job_id)
        try:
            snapshot=self.store.get(job['selection_token'],pinned_by='job-'+job_id)
            if job['settings'].get('writer_version',1)!=2:
                raise WriterProfileChanged()
            settings=DownloadSettings(**job['settings'])
            if export_plan(snapshot,settings)['key']!=job['artifact_key']:
                raise WriterProfileChanged()
            receipt=self.runner(snapshot,self.store,settings=settings,
                                check=check,progress=progress,metrics=metrics,job_context=(job_id,generation))
            check()
            self.store.update_job(job_id,generation=generation,allowed_states=('running',),
                state='completed',stage='completed',expires_at=time.time()+72*3600,
                artifact_key=receipt.tile_key,error_code=None)
        except (JobStopped,StaleAttempt,JobNotFound):
            pass
        except Exception as exc:
            code=('writer_profile_changed' if isinstance(exc,WriterProfileChanged) else
                  'tile_timeout' if isinstance(exc,TimeoutError) else
                  'disk_or_file_error' if isinstance(exc,OSError) else 'download_failed')
            try:
                self.store.update_job(job_id,generation=generation,allowed_states=('running',),
                                      state='failed',stage='failed',error_code=code)
            except (StaleAttempt,JobNotFound):
                pass

    def cleanup(self):
        self.store.expire_jobs()
        self.store.cleanup_artifacts()
        self.store.cleanup_tiles()
        self.store.cleanup_orphans()
        self.store.cleanup()

    def shutdown(self):
        if self._stop.is_set():
            return
        self._stop.set()
        self._wake.set()
        self.store.recover_jobs()
        if self._thread:
            self._thread.join(timeout=2)
