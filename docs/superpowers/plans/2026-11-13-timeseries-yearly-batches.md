# Yearly Time Series Batches Implementation Plan

> **For agentic workers:** User approved immediate implementation; continue without another approval gate. Use superpowers:dispatching-parallel-agents for independently owned transport/controller modules and root for shared service/API integration. Follow test-driven-development and verification-before-completion. Work in this existing dirty checkout; no commits, pushes or new worktree.

**Goal:** Search/export one year at a time, checkpoint each scene and publish independent yearly ZIP/CSV with resumable background progress.

**Architecture:** Add batch/year/scene tables beside legacy time-series tables, sharing the same database and worker slot. A batch worker searches one year through bounded metadata windows, exports one scene at a time, and persists a common grid. Standalone frontend controller polls/restores batch IDs independently of current selection.

**Tech Stack:** Existing FastAPI, SQLite, Rasterio, GEE REST `_RPC`, vanilla JS, unittest/node:test. No dependencies added.

**Spec:** `docs/superpowers/specs/2026-11-13-timeseries-yearly-batches-design.md`

## Global Constraints

- One export worker shared with old jobs; one current year/scene; at most two tile connections (sequential transport is permitted).
- Metadata: native collections, monthly windows, recursive split to one day, 120 s monotonic deadline/window, 3 attempts, 20.000 scenes/year. Never publish incomplete lists.
- Raster transport: isolated `_RPC`, 180 s per tile operation within 900 s/scene; <=16 MiB tile payload, <=100 million pixels/scene, <=30.000 pixels/edge, <=20.000 tiles. No global EE deadline changes.
- Immutable ROI/platform/bands/dates snapshot; same grid across years; exact final vector mask. Preserve old APIs/default behavior.
- Atomic TIFF/ZIP publication; successful checkpoints verified before skip; empty/partial/failed are distinct.
- Completed/partial/failed retention 24 h inactive; paused/interrupted 7 days inactive; active batch never cleaned. Children retained as a unit.
- No code changes to unrelated dirty files; do not commit/push.

## Review Focus

- Restart between TIFF rename and checkpoint, and checkpoint referring to missing/corrupt/wrong-grid TIFF.
- Resume/legacy-job race: database-wide admission prevents concurrent workers, not just thread-local locks.
- A paused batch keeps successful previous years, and cleanup/download reads never renew unrelated jobs silently.
- A failed year has no authoritative truncated count; retry does not silently rewrite complete scene lists.
- Browser restored batch polling must remain independent of subsequent ROI changes and stale replies.

### Task 1: Bounded metadata and raster transport

**Files:** Create `app/timeseries_transport.py`, `tests/test_timeseries_transport.py`.

**Interfaces:** `list_year_scenes(parameters: dict, start: str, end: str, *, check=lambda:None) -> list[dict]` (inclusive dates, raises `BatchPaused` from check unchanged); `export_batch_scene(parameters: dict, scene: dict, destination: Path, *, check=lambda:None) -> None` exports a raw TIFF. `BatchTransportError(code,message,fatal=False)` has safe fields; all RPC errors use safe messages and auth failures are fatal.

- [x] Write and observe failing tests: monthly/leap boundaries, no prepared image graph, full asset IDs, point center rule, vector geometry, cap/incomplete windows, recursive split, deadlines/retries, pause passthrough; actual TIFF bounded tile assembly with zero/NoData and connection count.
- [x] Implement scoped metadata graph construction and raster transport reusing exact scene loading and existing grid/tile helpers where safe; no edits to imagery/frame modules without root coordination.
- [x] Run `.venv/Scripts/python.exe -m unittest discover -s tests -p test_timeseries_transport.py`; expected all pass; provide evidence.

### Task 2: Persistent annual batch worker and shared admission

**Files:** Create `app/timeseries_batches.py`, `tests/test_timeseries_batches.py`; modify narrow blocks of `app/timeseries.py`.

**Interfaces:** `BatchStore(TimeSeriesStore)` uses same db/root with additive schema. `create_batch(parameters)`, `get_batch(id)`, `pause(id)`, `prepare_resume(id,retry=False)`, `recover()`, `cleanup()` return/read plain dictionaries. Batch response `{batch_id,status,parameters,current_year,error,years:[{year,start_date,end_date,status,total,processed,succeeded,failed,error,zip_url,csv_url}]}`; total null before successful listing. `BatchService(store, legacy_service)` schedules `start(parameters)`, `resume(id,retry=False)`, `pause(id)` and `run_batch(id)` through legacy executor. Use injectable `list_year_scenes`/`export_batch_scene` module functions for meaningful tests.

- [x] Write failing tests for calendar partitions, year ordering, partial/empty/failed, pause/resume/restart, complete scene-list reuse, corrupt-file re-export, grid persistence, CSV/ZIP contents, fatal disk/auth and shared legacy admission.
- [x] Add tables `ts_batches`, `ts_years`, `ts_scenes` beside legacy schema. DB transaction checks both active jobs/batches; old `create_job` checks batch reservation too. `TimeSeriesService.submit_task(key,operation,*args)` shares executor and shutdown.
- [x] Implement worker checkpoints, per-year packaging, disk checks and safe outputs. Publish prior ZIP while retry rebuilds. Pause at safe boundaries; restart becomes interrupted. Validate UUID/year before paths. Cleanup verifies root containment on Windows before recursive removal.
- [x] Run focused worker + old timeseries tests; expected all pass.

### Task 3: Frontend controller

**Files:** Create `static/timeseries-batches.js`, `tests/test_timeseries_batches_ui.cjs`; root owns HTML/CSS/workspace integration.

**Interfaces:** Root supplies `#timeSeriesBatchControls`, `#timeSeriesBatchPanel`. Controller builds controls (checkbox `#time_series_yearly`, independent start via existing Search dispatch) and jobs table. Exports `window.TimeSeriesBatches.start(body)`, `.setAvailable(available)`, `.enabled()`. `enabled` true only when available and checkbox selected; `start` requires explicit user action. API `POST /api/timeseries/batches` same body as TimeSeriesRequest; GET `/{id}`; POST `/{id}/pause|resume|retry`; GET `/{id}/years/{year}/download|csv`. localStorage `gee.timeseries.batch_id`, best-effort only. Poll active/pausing states every2s; restored pause/terminal displayed without restarting. Safe text throughout.

- [x] Observe failing node tests for explicit start, year table unknown counts, downloads, pause/resume/retry, restore, poll error, immutable selection/stale replies, localStorage unavailable and text safety.
- [x] Implement controller only, preserve legacy UI, no global request monkeypatching. Provide IDs/hooks to root.
- [x] Run `node --test tests/test_timeseries_batches_ui.cjs`; expected all pass.

### Task 4: API/UI integration and verification

**Files:** Create `app/timeseries_batch_api.py`, `tests/test_timeseries_batch_api.py`; modify `app/main.py`, `static/index.html`, `static/workspace.js`, `static/workspace.css`, README and this ledger.

- [x] Add failing API tests for instant job submission (provider not called on request thread), strict selection validation/snapshots, shared busy404/409 handling, yearly download/CSV, recovery/cleanup.
- [x] Extract/reuse parameter snapshot building from existing search without changing search contracts. Register endpoints; lifecycle recovers batch and cleans it, pin legacy cleanup safely. Integrate controller into all `searchTimeSeries` callers (including vector dispatcher) through single early branch; explicit yearly action label and jobs navigation.
- [x] Focused tests then full Python/Node; real browser with isolated real API/store and deterministic provider; bounded live GEE one/few scenes only. Record evidence/limitations under ignored `downloads/yearly-validation`.
- [x] Independent review, resolve material findings with regression checks, `git diff --check`; update README/ledger and report no commit/push.

## Execution record

- 2026-11-13: user approved spec and immediate implementation. No additional plan approval requested; permission already supplied.
- Preflight: transport accepts immutable parameter snapshot + year range, returns validated native metadata/raw TIFF; root owns alignment/masking/checkpoints. Frontend consumes fixed batch API independent of root's legacy UI. Tasks1/3 have disjoint files and can run concurrently.
- Ruling: use existing checkout, preserve current source selection rules and shared single-worker policy. No new infrastructure/dependencies. Paused/interrupted retain7days; no automatic restart of interrupted work.
- Tasks1–3 implemented. Transport18 focused tests; worker14 focused tests; API4 focused tests; controller12 focused tests plus2 dispatch tests. All initial feature tests observed RED before implementation. Independent review and targeted recheck found no remaining material findings.
- Review fixes verified RED→GREEN: (1) every newly DB-admitted continuation queues on shared executor even if prior same-key future is returning; controlled Event regression reproduced stuck queued state; (2) initial restored-ID network/5xx fetch retries without a current response, 404 clears stored ID and stops retry; (3) initialization EEException is fatal safe initialization_failed, graph evaluation failures remain distinct. Root also tested fatal stop before second year.
- Live-discovered wire bug: Float32 before unmask was promoted to Float64 by fill constant. Captured actual tile schema in `downloads/yearly-validation/diagnose-tile.json`; final Float32 cast after unmask fixes the graph. Serialized wire-graph regression observed RED→GREEN; strict dtype and byte checks retained.
- Added safe native scene name for annual CSV; first failed-scene message exposed in year status; both observed RED→GREEN. No tests/limits weakened.
- Real isolated Chrome with actual UI/API/store/worker and deterministic provider passed17 checks: explicit opt-in, unknown future-year total, first-year pause, selection-independent immutable snapshot, checkpoint skip on resume, annual artifact downloads, no runtime errors and restore without re-export. Evidence: `downloads/yearly-validation/browser.py`, `browser-results.json`. DevTools MCP is not configured; this used installed Chrome headless with a separate fresh profile.
- Live GEE final run passed6 checks in64.38s, submission0.156s: native scenes2023-12-29 and2024-01-03 over2023-12-28..2024-01-03, one per year, separate annual ZIP/CSV. Two12-band Float32 TIFFs, each11×12 pixels; both share identical grid,90 valid pixels/band,42 outside pixels including20 hole pixels all NoData. Source import removed after submission to verify saved ROI geometry. Results: `downloads/yearly-validation/live-results.json`, `live-2023.zip`, `live-2024.zip`, matching CSVs/TIFFs. Only2 successful real scenes downloaded, not bulk benchmark evidence.
- Earlier upstream/proxy403 and wire-dtype failures retained in `live-upstream-failure.json`/`live-float64-failure.json`. A later full Python run hit a pre-existing offline browser fixture that called live GEE despite mocking tile data; repaired fixture isolation (mock RPC and image loader, preserve real TIFF/ZIP and all assertions), adding a guard regression observed RED→GREEN. Focused browser-acceptance9tests passed. No product frame-export changes.
- Final lifecycle check added graceful shutdown pause before executor join: avoids processing all remaining years while the app is closing. Unexpected restart still recovers interrupted; graceful shutdown reaches paused at a safe checkpoint. Regression observed RED→GREEN; no next-year search after shutdown request. Worker suite now15tests. Full508-test run after fixture isolation passed (3optional skips); repeating after final lifecycle change.
- Node verification after review fixes: all136tests passed,0failed. Log `downloads/yearly-ui-tests.log`.
- Final complete Python run after graceful-shutdown change: `.venv/Scripts/python.exe -m unittest discover -s tests` ran509tests in53.892s, OK(skipped=3 optional EODAG checks). Log `downloads/yearly-python-tests.log`. Final diff check clean apart from existing LF/CRLF conversion notices. README and this execution record updated. No branch/worktree creation, dependency changes, commit or push.
- Remaining acceptance limits: only tiny Sentinel-2 live smoke, not large-volume/large-ROI benchmark or live acceptance of Landsat/SAR/NICFI/Dynamic World. External proxy/quota/provider availability can still fail bounded operations; annual isolation/retry/checkpoints do not guarantee GEE uptime. Existing one-process/local deployment policy preserved.
