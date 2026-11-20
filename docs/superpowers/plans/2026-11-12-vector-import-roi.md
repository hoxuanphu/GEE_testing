# Vector Import and Polygon ROI Implementation Plan

> **For agentic workers:** Implement in the current dirty feature checkout. User approved immediate execution. Parser/store and standalone Leaflet controller have fixed interfaces and separate owned files, so superpowers:dispatching-parallel-agents may execute them concurrently. Root owns API/legacy integration. Do not create another branch, commit or push.

**Goal:** Import vector files, control individual map features and apply selected polygons as exact ROI for scene counts and supported GEE searches/downloads.

**Architecture:** Normalize imports to bounded WGS84 GeoJSON, store immutable imports/ROI in SQLite, and send opaque ROI IDs through existing selection contracts. Leaflet controller manages visibility/selection separately from applied ROI. Search/job snapshots keep geometry; final raster masking preserves holes/disconnected regions.

**Tech Stack:** FastAPI/Pydantic, pyshp/rasterio, Shapely 2.x, defusedxml 0.7.x, Leaflet/vanilla JS, unittest/node:test.

**Spec:** `docs/superpowers/specs/2026-11-12-vector-import-roi-design.md`

## Global Constraints

- Exact WGS84 Polygon/MultiPolygon; preserve holes and disconnected parts. No bbox/center fallback for vector ROI.
- Import maximum 20 MiB, ZIP/KMZ 100 members/80 MiB expanded, normalized output 80 MiB, 10.000 features/500.000 positions per file and applied ROI.
- No remote NetworkLink/icon fetching, archive extraction, HTML property execution or automatic topology repair.
- Changes apply only to the current workspace; preserve existing dirty changes and existing selection modes.
- GEE search/download/time series enabled only with exact clipping/final masking. External vector counts supported; unsupported external search/download blocked explicitly.
- Apply creates an immutable ROI; layers, drafts and existing jobs remain independent. UUIDs/geometry copied into request snapshots.

## Review Focus

- XML/ZIP bounded reads, traversal/encryption/duplicate members and geometry nesting abuse.
- Import atomicity and property/CRS/topology correctness, including GeometryCollection children and polygon holes.
- Visibility/selection versus active ROI; stale import/apply/search replies and removing an active layer.
- Existing point/rectangle/reference/admin requests and download URLs must remain compatible.
- Actual TIFF outside polygon/inside holes is NoData on all enabled vector export paths.

### Task 1: Import normalization and immutable store

**Files:** Create `app/vector_import.py`, `app/vector_store.py`, `tests/test_vector_import.py`, `tests/test_vector_store.py`; root modifies `requirements.txt` only to add Shapely/defusedxml.

**Interfaces:** `read_vector(data: bytes, filename: str) -> dict` returns `{filename,feature_collection:{type:'FeatureCollection',features},warnings,counts}`; each feature has unique string `id`, `geometry`, `properties`, `name`, `layer`. `VectorImportError(code,message,status=422)` has public safe fields. `VectorStore(root)` exposes `save_import(payload)`, `get_import(id)`, `delete_import(id)`, `create_roi(selections)`, `get_roi(id)`. Selections are `[{import_id,feature_ids:[str]}]`; ROI returns `{roi_id,name,geometry,bbox,feature_refs}`. Import/ROI IDs are opaque canonical UUIDs.

- [x] Write and run failing parser/store tests before implementation.
- [x] Implement GeoJSON/KML/KMZ/GPX/Shapefile ZIP, CRS/geometry validation and bounded archive/XML handling.
- [x] Implement SQLite immutable IDs, union with combined bounds and 24-hour activity lifetime.
- [x] Run focused parser/store tests and report exact evidence.

### Task 2: Standalone Leaflet manager

**Files:** Create `static/vector-layers.js`, `tests/test_vector_layers_ui.cjs`. Root owns HTML/CSS/workspace wiring.

**Interfaces:** `POST /api/vectors/import` multipart `file` returns stored import; `POST /api/vectors/rois` JSON `{selections:[...]}` returns ROI; `DELETE /api/vectors/imports/{id}` removes import while ROI snapshots persist. Controller builds its UI under `#vectorLayersPanel`, uses existing `map`/`L`, and exports `window.VectorLayers` with `request()` returning `{selection_mode:'vector',vector_roi_id:<id>}`, `getRoi()`, `clearRoi()`. Apply sets `#selection_mode` to `vector`, dispatches change and calls existing `invalidateSearchResults()`; root extends that mode. Imported geometry is safely rendered, no property HTML. Progressive/paged rows and labeled keyboard controls. One active ROI outline persists independently of overlay visibility.

- [x] Write/run failing node tests, then implement fixed API contract and per-feature controls.
- [x] Cover multiple files, GeometryCollection leaf features, visibility/selection, polygon-only Apply, stale responses, deletion and XSS-safe attributes.
- [x] Run focused node tests; report HTML/CSS hooks required from root.

### Task 3: Vector APIs and exact polygon operation paths

**Files:** Create `app/vector_api.py`, `app/vector_mask.py`, `tests/test_vector_api.py`, `tests/test_vector_roi.py`; modify selected blocks in `app/main.py`, `app/scene_inventory.py`, `app/scene_catalog.py`, `app/timeseries.py`, `app/s5p.py`, `static/index.html`, `static/workspace.js`, `static/scene-inventory.js`, `static/selection-state.js`, `static/workspace.css`.

**Interfaces:** `selection_mode='vector'`, `vector_roi_id` resolved only through trusted store; resolver returns exact EE geometry/bbox plus area metadata for existing polygon handling. Snapshot geometry is authoritative for later downloads/jobs. Final `mask_raster_to_polygon(path, geometry)` preserves NoData/metadata with bounded raster blocks.

- [x] Write and observe failing endpoint/selection/download tests, including holes and disjoint parts.
- [x] Register bounded multipart import/ROI APIs before static mount; reject unsupported external vector search/download.
- [x] Extend request/response models, resolver, links and scene-count geometry. Extend GEE scene/catalog/SAR/DEM/S5P/time-series paths without changing other modes.
- [x] Apply polygon mask after final raster alignment/merge on enabled vector paths and verify real tiny TIFF fixtures.
- [x] Integrate layer panel and mode; bypass hidden point/rectangle validation when vector ROI active; invalidate only on actual ROI change.

### Task 4: Verification and handoff

**Files:** Update `README.md`, this execution record, local evidence under `downloads/vector-validation/`.

- [x] Run full Python unittest suite and all node tests, recording any failures/skips.
- [x] Real browser: import mixed features, toggle/select, apply polygon and count with exact ROI; verify no stale result after ROI replacement.
- [x] Live GEE count and one small TIFF download with actual polygon-mask checks if provider access permits.
- [x] Fresh review, resolve material findings, re-run relevant verification and report source/test/live limits truthfully.

## Execution record

- User confirmed polygon-as-ROI and approved design/immediate execution.
- Fresh builder check supplied defaults now recorded in spec; no further user clarification required.
- Tasks 1–3 complete. Parser/store observed RED before implementation; final focused parser/store run: 27 tests. Frontend observed RED before implementation, final manager run: 11 tests. Root integration/export regressions cover request validation, exact resolver geometry, scene inventory, catalog/S5P/time-series snapshots, optical/SAR/DEM final masks, holes/disconnected regions and retained NoData.
- Ruling: keep `static/selection-state.js` unchanged. Active vector ROI lives in its own immutable-ID controller, while existing manual/reference drafts remain isolated. Existing selection-state regression suite remains green.
- Final independent review found two material issues: inherited properties could amplify normalized output and removed files retained data/listeners. Both fixed with observed RED→GREEN regressions. Added cumulative 80 MiB normalization budget before copying; removal now releases registry/DOM/Leaflet references and aborts pending import, preserving failed-delete retry. Targeted independent recheck: no remaining findings.
- Final integration check found structured API errors lost their message in the manager. Changed a regression to use the actual `{detail:{code,message}}` response shape, observed failure, then restored safe text messages (GREEN).
- 2026-11-13, after the live-discovered asset-ID fix: `.venv/Scripts/python.exe -m unittest discover -s tests` ran 471 tests in 58.759 s: OK, 3 optional EODAG tests skipped. Log: `downloads/vector-python-tests.log`. All `tests/test_*.cjs` through `node --test`: 122 passed, 0 failed. Log: `downloads/vector-ui-tests.log`.
- Real isolated Chrome using current UI/parser/store: all 14 checks passed for five formats/eight leaf features, actual Leaflet visibility, safe properties, cross-file exact union with a hole, excluded point, count/CSV snapshot geometry and invalidation on active source removal. Metadata provider was deterministic for this browser run; this is not live-provider evidence. Harness/results: `downloads/vector-validation/browser.py`, `browser-results.json`.
- README now documents formats, controls, explicit Apply, source capabilities, storage lifetime, limits and unsupported editing.
- Live acceptance found a pre-existing blocker on the enabled catalog download path: mapped `Image.id()` returned only a relative scene index, rejected by the exact-asset loader. Regression now supplies relative native IDs for Sentinel-2, Landsat 8/9 (same index across collections), Dynamic World, NICFI and Sentinel-1; observed five failing subtests before fixing. Full collection/index IDs are tagged before merging, without weakening loader validation. Focused imagery suite: 14 tests GREEN.
- First live attempt against the long-running app: GEE count/CSV succeeded (7 Sentinel-2 scenes, 2024-12-01 through 2024-12-31); catalog request exceeded the 180 s client timeout. A fresh isolated app with a one-day catalog query succeeded and exposed the relative-ID blocker above. Failed-attempt evidence is retained as `downloads/vector-validation/live-existing-server-results.json` and `live-relative-id-results.json`; these are not successful TIFF evidence.
- Final live run after the fix, fresh isolated app using real service-account GEE access: all six checks passed in 96.83 s. Count/CSV: 7 Sentinel-2 scenes for December 2024. One-day catalog: one scene on 2024-12-03, full native asset ID and exact saved ROI. Download: 14.380-byte TIFF, 12×12 pixels, 26 bands, EPSG:4326; all 48 outside pixels (including 25 hole pixels) masked in every band, with 96 valid pixels per band. Harness: `downloads/vector-validation/live.py --fresh`; results: `live-results.json`, `live-catalog.json`, `live-inventory.json`, `live-inventory.csv`, `live-hole.tif`. This validates Sentinel-2 against live GEE; other enabled exports have offline contract/TIFF coverage, not live-provider acceptance.
- Final `git diff --check`: clean (existing LF/CRLF conversion notices only). No branch/worktree creation, commit or push. External-provider vector search/download remains explicitly unsupported; metadata counts remain available. Large date ranges can still time out on legacy GEE search paths; annual background export jobs are outside this vector feature.
