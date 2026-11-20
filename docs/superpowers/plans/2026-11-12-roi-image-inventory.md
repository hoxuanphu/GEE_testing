# ROI Image Inventory Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task in the current checkout. User approved immediate implementation; preserve all existing dirty files and do not create a branch, commit or push as part of this task.

**Goal:** Count all scenes intersecting the selected ROI/date interval and download the same scene names/acquisition dates as CSV.

**Architecture:** A separate inventory endpoint enumerates metadata without cloud thresholds and persists an immutable TimeSeriesStore snapshot. A snapshot CSV endpoint exports every stored row. Source adapters keep the exact WGS84 ROI and fail clearly if pagination is incomplete. A small JS controller manages the button, CSV link and stale responses.

**Tech Stack:** FastAPI, Pydantic, Earth Engine, CDSE OData, Earth Search STAC, SQLite, vanilla JS; unittest and node:test.

**Spec:** `docs/superpowers/specs/2026-11-12-roi-image-count-design.md`

## Global Constraints

- Include the entire end date and all cloud values, including missing metadata.
- Keep exact point-square/rotated rectangle/administrative/reference geometry and selected source/platform/product/polarization.
- Count metadata only; never export raster or start jobs.
- Never report a capped page as an exact total; failures produce no snapshot.
- CSV names and dates come from the counted snapshot, with UTF-8 BOM and ordinary CSV quoting.
- Invalidate stale results on selection changes; preserve existing search/download behavior.

## Review Focus

- Missing cloud metadata and 100% clouds must remain in the count and CSV.
- Incomplete/repeated provider pages must fail without presenting an exact count.
- ROI rotation and reference-source switching must not reuse an old count.
- Export must include more than 50 rows, Unicode and quoted names from the snapshot.
- Network/provider failures and unsupported sources must recover the button without publishing a result.

### Task 1: Metadata inventory and immutable CSV API

**Files:** Create `app/scene_inventory.py`, `app/inventory_sources.py`, `tests/test_scene_inventory.py`; modify registration in `app/main.py`.

**Interfaces:** `POST /api/scene-inventory` accepts current ROI/date/source selection, returns `{search_id,total,parameters,scenes,offset,limit,csv_url}`. `GET /api/scene-inventory/{search_id}/csv` returns the full immutable CSV. `inventory_sources.list_scenes(dataset, source, geometry, start, end, platform, polarization, product, product_type)` returns normalized metadata rows.

- [x] Write failing API/adapter tests for exact geometry, inclusive dates, null/100% clouds, platform, provider pagination, missing references, CSV over 50 rows, quoting and query failure.
- [x] Run `.venv/Scripts/python.exe -m unittest discover -s tests -p test_scene_inventory.py -v`; confirm failure is missing feature.
- [x] Implement request validation, trusted geometry resolution, metadata adapters, isolated snapshot store, snapshot CSV export and registration before static mount.
- [x] Run focused tests and existing selection/catalog/platform API regressions.

### Task 2: Count/CSV controls and state invalidation

**Files:** Create `static/scene-inventory.js`, `tests/test_scene_inventory_ui.cjs`; modify `static/index.html` and `static/workspace.js` for controls; add a small invalidation hook to `static/matched-frame.js`/rectangle callback only where required.

**Interfaces:** `window.SceneInventory.invalidate()` aborts/clears the current count; `count()` builds a fresh whitelisted inventory request from the existing selected ROI and updates a live status and CSV link.

- [x] Write failing node tests for request geometry/source/platform, successful/zero counts, CSV link, validation/error recovery and stale response rejection.
- [x] Run `node --test tests/test_scene_inventory_ui.cjs`; confirm missing feature.
- [x] Implement independent controller, visible count button and CSV download link near the existing search action, keeping consistent UI and live status.
- [x] Run node tests and existing UI tests; check real browser using available local tooling.

### Task 3: Documentation, regression and live verification

**Files:** Update `README.md` with the action, metadata-only semantics, CSV columns and limitations.

- [x] Run `.venv/Scripts/python.exe -m unittest discover -s tests` and `node --test` for all `tests/test_*.cjs` files; record any existing/environment failures by name.
- [x] Run live metadata checks on available sources, without downloading raster data; verify count equals CSV rows. Initial GEE attempt did not complete; the follow-up fix and successful real GEE checks are recorded below.
- [x] Review the task diff against the spec, update this plan with evidence and report any unverified runtime conditions.

## Execution record

- Design approved and immediate implementation authorized; CSV names/dates added by explicit user request.
- Ruling: enumerate metadata once rather than count then re-query CSV. This preserves the same row set and avoids exposing a count that the CSV cannot reproduce.
- Ruling: execute in the current dirty feature checkout and keep changes separate by file/function; do not include unrelated edits in commits.
- Ruling: no new geospatial dependency. Serialize trusted Polygon/MultiPolygon rings directly to OData WKT, preserving holes/rotation; the current environment has no Shapely.
- RED: initial 12 API/source cases failed because the new modules did not exist; initial 5 Node cases failed because the controller did not exist. Added SAR orbit preservation test failed before implementation.
- Review: fresh reviewer found duplicate response contents could be deduplicated into a false complete result. Added terminal repeated-page regression for both providers, observed both failures, then rejected any nonempty page adding no IDs. Reviewer rechecked and marked the issue resolved.
- Final Python suite: **421 tests, OK (3 skipped)**; Node suite: **103 passed, 0 failed**. Focused new suite: **14 Python + 5 Node passed**. Existing FastAPI/rasterio deprecation notices remain; dependencies unchanged.
- Real Chrome, isolated profile and isolated metadata fixture: **9 checks passed**, including actual workspace placement, minimum button size, rendered count, CSV link/BOM/all 123 rows/names/dates, ROI invalidation and static-source visibility. This is real browser integration with fixture metadata, not live GEE acceptance.
- Live AWS/CDSE: each returned **2 real scenes**, dated **2025-01-01 and 2025-01-06**, with cloud metadata >0; CSV contained exactly 2 rows. ROI: rotated 37-degree 4 km by 2 km rectangle at 107 E / 10 N; interval 2025-01-01 through 2025-01-07. Repeated with real provider page size forced to 1: **3 actual metadata pages per source**, still exact count/CSV equality.
- Live GEE attempt remained pending and was explicitly stopped; no successful GEE runtime claim. GEE collection/geometry/platform/cloud/date behavior has offline coverage.
- Evidence artifacts (ignored, local): `downloads/roi-inventory-validation/python-tests.log`, `node-tests.log`, `browser-results.json`, `browser-dom.html`, `live-s2aws.json`, `live-s2.json`, `live-s2aws-paginated.json`, `live-s2-paginated.json`; validation runners and CSVs are in the same directory.

## Follow-up: GEE inventory failure on the user's actual ROI

- User supplied manual rectangle centered at 105.58731973171236 E / 20.036475833269925 N, width 40.39367743961984 m, height 55.18933644607458 m, rotation 0; Sentinel-2 SR, all platforms; 2018-01-13 through 2024-12-31.
- Diagnosis: native collection size was **446** (1.3 seconds), raw metadata returned **446** (3.2 seconds). The previous inventory used `imagery.timeseries_collection`, including raster preparation; its metadata request exceeded 20 seconds in the comparison and the original unbounded `getInfo` was still waiting on an SSL response at the 75-second diagnostic cutoff. This was a defect in the inventory path; no ROI enlargement or date reduction was required.
- Fix: build metadata-only native collections with the same ROI/date/platform/product/SAR filters; tag canonical collection/index asset IDs before merging; evaluate through the existing isolated `_RPC` transport with an operation deadline. Leave all existing raster download/time-series processing unchanged. API now distinguishes timeout, connection and GEE transport errors; unknown errors log only source/dataset/exception type, never raw provider bodies or credentials.
- RED/GREEN: added a regression prohibiting the old raster preparation path plus real EE expression checks for ROI/date/platform, Landsat merge and SAR filters, and a structured timeout API test. Observed failures before implementation, all **18 focused Python tests passed** after the fix. Fresh review found no actionable regressions.
- Current-source live adapter: exact user query returned **446** in **7.05 seconds** including initialization.
- Actual running localhost:8000 API (auto-reload enabled): `POST /api/scene-inventory` returned **HTTP 200, 446 scenes in 15.08 seconds**; CSV returned **HTTP 200, exactly 446 rows**, dates **2018-01-19 through 2024-12-28**. All 446 had cloud metadata >0 and remained included. Source selection and ROI unchanged; no raster downloaded.
- Final regression after this fix: **425 Python tests, OK (3 skipped)** and **103 Node tests passed**. New GEE-specific tests: `tests/test_inventory_gee_metadata.py`.
- Additional local evidence: `downloads/roi-inventory-validation/user-roi-validation.json`, `user-roi-446.csv`, `python-tests-gee-fix.log`, `node-tests-gee-fix.log`. This successful real GEE evidence supersedes the initial unverified GEE status above.
