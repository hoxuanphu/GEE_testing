# RAW mosaic color balance implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement task by task. User approved the design; execute in this session. Preserve the current dirty branch; no commits or push.

**Goal:** Reduce optical RAW scene color differences in preview and TIFF using reproducible overlap calibration.

**Architecture:** A focused `app/color_balance.py` estimates robust per-band affine coefficients from paired clear overlap samples and applies them to contributing scenes before the existing ownership mosaic. API defaults remain off; UI defaults on only for RAW optical merges. Metadata records coefficients and skipped scenes.

**Tech stack:** Existing Python, NumPy, Earth Engine, FastAPI, Rasterio and plain JavaScript; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-17-raw-mosaic-color-balance-design.md` (approved).

## Global constraints

- Same ownership, masks, cloud pixels and auxiliary band values as RAW; only spectral values may change.
- Work on `feature/eodag-catalog-mvp`; preserve all existing changes. No branch/worktree/commit/push.
- API `color_balance: bool = False`; UI checkbox checked by default, active only for optical RAW composite/merge.
- Reflectance units: gains [0.5, 2.0], offsets [-0.1, 0.1]; reject, never silently clamp invalid fits.
- Sample at 60 m, at most 4096 paired clear pixels, deterministic seed 42; minimum 64 valid pairs per band.
- Fit gain from paired sample 10–90 percentile spans, offset from median residual; reject span <0.01, median absolute residual >0.025, or no improvement over uncorrected median error (identity if already aligned within 0.002).
- Maximum 32 sample evaluations, at most 3 passes to connect scenes to already calibrated references; explicit skip reason when disconnected or budget exhausted. Shared optical aggregation lock guards evaluation.
- No global histogram fallback. SCL/QA affects calibration samples only. No feathering or cloud removal.

## Review focus

- Mixed landcover/outliers/changed surfaces must not produce unsafe coefficients (task 1 numerical tests).
- Missing QA, no overlap, flat spectra and disconnected scenes must preserve data and report skip reasons (task 1 + live synthetic check).
- Very small ROI, excessive contributor counts and chained references must obey sampling budgets (task 1 orchestration tests).
- Province/commune/vector and rectangle/point must carry the setting to download, with API defaults unchanged (task 2 API tests).
- Browser changes must invalidate stale results and exclude time series/SAR/non-RAW requests (task 3 UI tests).

## Task 1: Calibration and RAW integration

Files: create `app/color_balance.py`, `tests/test_color_balance.py`; modify `app/imagery.py`; add explicit live synthetic script under `tests/`.

Interfaces: `fit_band(source, target) -> dict` with status/reason/gain/offset/errors; `balance_sources(sources, dataset, roi, evaluate, scene_ids) -> (images, report)`; `apply_coefficients(image, coefficients) -> ee.Image`. `evaluate` evaluates an EE FeatureCollection under the existing lock. `SceneInfo.color_balance` holds JSON-serializable report or None.

- [x] Write numerical tests for known affine shifts/outliers, bounded/rejected fits, flat/insufficient/nonfinite pairs and held-out error reduction; run and observe feature-missing failure.
- [x] Implement robust fits and bounded reference orchestration; test chained/disconnected/budget-limited cases using mocked network sampling and real numerical fitting.
- [x] Add graph tests verifying spectral-only overwrite and untouched masks/QA; integrate after contributor ownership selection, without changing the disabled path.
- [x] Run focused RAW, optical, numerical and graph tests. Run explicit synthetic GEE evaluation if available, recording real values/coverage/QA.

## Task 2: Request, download and TIFF provenance

Files: modify `app/main.py`, `app/imagery.py`; create `app/scene_snapshots.py`, `tests/test_color_balance_api.py`.

Interfaces: `color_balance=False` keyword through `best_scene`, `_raw_optical_scene`, `_least_cloudy_per_tile`, `best_scene_for_roi`; `SceneResponse.color_balance` report; query `color_balance=true`.

- [x] Add failing request/response/download tests for point/rectangle/admin/vector and default false; reject unsupported enabled requests before GEE calls.
- [x] Pass optional keyword only when enabled to preserve existing mocked interfaces; ensure preview receives calibrated ee_object. Include report in RAW contributor metadata without discarding calibration details.
- [x] Record report and method in TIFF tags after final warp/mask. Suffix calibrated filenames `_balanced`; preserve NoData on calibrated exports.
- [x] Run API and existing rotated/vector/RAW tests.

## Task 3: UI, docs and final verification

Files: modify `static/index.html`, `static/workspace.js`, `README.md`, `tests/test_rectangle_ui.cjs`, `tests/test_s2_layers_ui.cjs`; add `tests/browser/color-balance-acceptance.cjs`; update this plan with evidence.

- [x] Add a checked checkbox “Cân bằng màu giữa các cảnh” and explanation that spectral values change; visible for optical RAW merge only. Add failing JS tests for request inclusion, invalidation and exclusion from non-RAW/SAR/time series.
- [x] Implement request propagation and output status (“RAW + cân bằng màu”, skipped count/reason); avoid changing existing color or layout conventions.
- [x] Document thresholds, skip behavior, metadata, API compatibility and quantitative-use limitation.
- [x] Run Python discovery and existing Node UI suites, inspect UI with a browser when available; obtain a fresh review of this scoped change and resolve material findings.
- [x] Record truthful runtime/visual evidence separately from offline checks; do not claim real-image visual acceptance without a supplied ROI/date example.

## Execution ledger

- 2026-11-17: Read approved spec, current branch, RAW/API/UI paths and tests. Existing checkout has extensive unrelated changes; preserve them. User approval authorizes implementation; continue inline without another approval handoff.
- Task 1: numerical tests first failed because the calibration module did not exist; RAW integration regression then failed because `_raw_gap_fill` did not accept the setting. Implemented robust fitting, contributor-only calibration, shared-lock sampling, per-band skip decisions and reports. Focused tests passed; disabled RAW regressions unchanged.
- Task 2: new API tests first failed because the field/propagation/snapshots were missing. Implemented optional keyword propagation, report retention, real TIFF metadata and NoData preservation. Added actual link-following tests for all five selection modes, caught and fixed irrelevant-coordinate snapshot key mismatches before final verification. Added cache expiry/eviction/tamper checks.
- Task 3: UI tests first failed on the missing control/helper/invalidation. Reused existing rectangle UI fixture and S2 extraction fixture instead of creating a duplicate fixture file. New control lives beside RAW in advanced options via `workspace.js`. All UI suites pass.
- Ruling: preserve the calibrated EE graph in a bounded process-local cache (32 entries, one hour) so download cannot silently choose new sources or coefficients. This targets the existing single-worker desktop app; multi-worker deployments require a shared snapshot store. Expired/evicted/restarted snapshots return 410 and must be searched again.
- Ruling: fully calibrated/identity scenes can become references; partly calibrated scenes cannot anchor later scenes, avoiding propagation of unreliable per-band fits. Disconnected scenes may retain visible color differences and are reported.
- Ruling: Chrome DevTools MCP was unavailable; verified the actual UI using native Chrome CDP and an isolated disposable profile, with a local fixture API. This verifies browser behavior, not real-image visual quality.
- Final review: independent reviewer found two Important runtime issues in reference sampling: incompatible constant-label types with multiple references, and mixed spectral ownership when one reference band was missing. No Critical findings or deferred Minor findings.
- Final: fixed multi-reference label types using Int32; explicit three-source GEE regression failed before the fix (`color-balance-live-three-source-red.log`) and passed afterward.
- Final: fixed mixed reference spectra/fractional blending using shared binary joint sample validity, retaining original footprints for geometric overlap. Live missing-band regression failed before the fix (`color-balance-live-reference-red.log`) and passed afterward. Output ownership/masks are unchanged.
- Final verification: `.venv/Scripts/python.exe -m unittest discover -s tests -v` -> 558 tests, OK, 3 existing optional EODAG skips, 45.417 s. Log: `downloads/color-balance-python-suite.log`.
- Final verification: `node --test tests/*.cjs` -> 149 passed, zero failures/skips. Log: `downloads/color-balance-ui-suite.log`.
- Live GEE: three contributors for Sentinel-2 and Landsat, both coverage 1.0, max sampled reflectance error about 3.8e-8; cloudy QA retained. Additional missing-band, fractional-reference-mask, no-QA and disjoint-overlap checks are in `tests/live_color_balance.py`. Evidence: `downloads/color-balance-live-synthetic.json`, `downloads/color-balance-live-green.log`.
- Browser: `node tests/browser/color-balance-acceptance.cjs` -> PASS; default checked control, RAW request flag, result/skip text and invalidation on toggle, no runtime JS exceptions. Evidence: `downloads/color-balance-browser.json`, `downloads/color-balance-browser.png` (fixture response; no live imagery).
- `git diff --check` passed. No new dependencies, branch changes, commits or push. Original unrelated changes retained. Real-image visual acceptance remains open because no affected ROI/date/file was supplied.
