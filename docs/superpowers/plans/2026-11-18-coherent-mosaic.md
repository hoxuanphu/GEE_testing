# Coherent optical mosaic implementation plan

> **For agentic workers:** Use superpowers:executing-plans inline. User has authorized implementation of the reviewed proposal and stable mosaic mask options. No additional approval handoff; preserve this checkout and do not commit/push.

**Goal:** Add near-date/same-pass source selection and stable mosaic cloud-mask choice with matching preview/TIFF behavior.

**Architecture:** A focused pure Python grouping module builds bounded ranked groups from metadata/footprints. A shared optical entrypoint validates shortlisted groups at pixel level and reuses RAW or one-scene-per-tile mosaic. API/UI and existing snapshots carry the new settings.

**Tech stack:** Existing EE, Shapely, Python/FastAPI, Rasterio and vanilla JS. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-11-18-coherent-mosaic-design.md`.

## Global constraints

- API defaults `scene_grouping='any'`, `max_scene_span_days=3`, `mosaic_mask_clouds=True`.
- Group-wide maximum 1/3/5/7 days; never relax. Candidate cap 400; shortlist 5; pixel checks 3.
- Stable mosaic always one scene per tile. Mask settings cannot change chosen scene IDs.
- Preserve native masks, zero values, color balance behavior and existing frozen download contracts.
- Preserve dirty branch `feature/eodag-catalog-mvp`; no dependency, branch or commit changes.

## Review focus

- Chained dates must not exceed total-span bounds; same orbit across cycles is not same pass.
- Missing metadata and mutually overlapping footprints must not produce fake coverage/compatibility.
- Cloud masking creates legitimate holes; masked/unmasked modes must pick identical sources.
- Snapshot/token keys must include all new settings; actual generated links work in all five ROI modes.
- Controls never leak into single-scene, time-series or nonoptical requests; stale results are invalidated.

## Tasks

### 1. Group ranking and stable masking

Files: create `app/scene_grouping.py`, `tests/test_scene_grouping.py`; modify `app/imagery.py` and RAW tests.

Interfaces: `rank_groups(rows, dataset, mode, max_days, roi_geometry) -> list[dict]`, `acquisition_key(row,dataset)`, group reports with indices, span, coverage estimate, compatibility. `_least_cloudy_per_tile(..., mosaic_mask_clouds=True)` retains legacy behavior by default; false skips masking without RAW fallback.

- [x] Write failing pure tests for time-span, same-pass, missing metadata, angle wrap, geometry unions and bounded groups; implement deterministic ranking.
- [x] Add failing graph tests for stable masked/unmasked selection invariance and valid-zero/NoData behavior; implement mask flag.
- [x] Add grouped optical builder with bounded metadata/pixel checks, report preservation and original-data coverage gating; test it offline.

### 2. API/snapshot/export

Files: `app/main.py`, `app/imagery.py`, `app/download_pipeline.py`, new API tests.

- [x] Add failing request validation, response/link propagation and actual TIFF/snapshot tests.
- [x] Extend point merges with raw/mosaic selection, grouping and mask options; propagate to admin/vector and planning.
- [x] Freeze new mode outputs using existing durable/in-memory snapshots. Include options in token comparisons and grouping/mask reports in TIFF tags. Existing defaults keep existing mocks compatible.
- [x] Run focused APIs, frozen downloads, color balance and rotated/vector suites.

### 3. UI/docs/review

Files: `static/index.html`, `static/workspace.js`, relevant UI fixtures, `README.md`; browser acceptance script.

- [x] Add failing UI tests for method/mask/group/day controls, request shape and invalidation; implement using existing styles/accessibility.
- [x] Update docs and test a real browser with fixture responses; use a small live EE synthetic check if quota allows.
- [x] Run full Python/Node suites, request independent scoped review and fix material findings with regression evidence.
- [x] Record exact verification and remaining live visual acceptance limitation.

## Ledger

- 2026-11-18: User requested implementation of the near-date option and stable mosaic with/without cloud mask. Current checkout has subsequent download-pipeline changes beyond prior color balance; inspected and preserve those contracts.
- Task 1 complete: pure grouping tests initially failed on missing module, then passed for whole-window span (no chaining), same-pass identity including 15-second tile timestamp differences, missing metadata, circular angles, overlapping footprint unions and explicit candidate/shortlist limits. Stable mask regression initially failed on missing keyword; now both modes select the same IDs with no RAW gap-fill fallback.
- Task 2 complete: API tests initially failed on missing fields; implemented grouping/mask options, point/rectangle method selection, responses, source metadata, planning and TIFF tags. Real tiny TIFF tests follow snapshot and direct links in all five ROI modes. Changed day/mask/merge settings are rejected; grouped color balance has only the full-policy snapshot, not a weaker alternative token. Nonoptical composite links remain unchanged.
- Task 2 compatibility: older durable snapshots lack the three new default keys. Regression failed before normalizing only absent default fields; legacy default links now work while changed policy still fails. Explicit unmasked stable graph regression caught Collection.first in the output; selected S2 assets are now bound directly and pass freeze_selection. Existing resumable download structure retained; its final write uses shared provenance tags before validation/publication.
- Task 3 complete: UI tests first failed on missing controls/request fields. Added raw/mosaic choice for point/rectangle merges, optional near-date/same-pass grouping, 1/3/5/7-day controls and independent stable cloud-mask selector. Existing sidebar styles retained; controls are together in advanced options. Time series and unrelated composites do not send new fields. Real Chrome/CDP fixture flow verifies both mask requests, result text, controls and invalidation; no runtime exceptions.
- Independent scoped review found two Important issues: Landsat native export loader dropped QA_PIXEL, and default stable process-local snapshot did not bind merge_scenes. Retained native Landsat QA for masking/calibration and added merge binding/validation; fixed with offline graph and failing-then-passing API regression. Active merge flags are normalized only for point/rectangle, avoiding false 409 errors for inactive admin flags.
- Live verification caught two further EE integration problems: nested FeatureCollection in Dictionary.getInfo returned schema without features; now metadata is an explicitly materialized list of dictionaries/geometries. mask().copyProperties returned Element; now explicitly cast to Image before toFloat. Float normalization happens after QA cloud-bit masking.
- Missing orbit/angle metadata is now explicitly listed per scene, in addition to missing acquisition identifiers. Group ranking penalizes it rather than assigning zero differences.
- Ruling: the 99% unmasked coverage gate applies when near-date/same-pass grouping is enabled. Grouping-disabled stable mosaic preserves legacy partial-coverage behavior and reports coverage; forcing a new gate there would break existing requests. Masked output can legitimately have lower coverage in either policy. Spec/README clarified this.
- Ruling: same-pass identity uses S2 spacecraft+datatake or Landsat spacecraft+path+UTC day; no exact timestamp equality or identical tile/row requirement. Orbit/angles are ranking preferences, not unvalidated hard angular thresholds. Effects on real color consistency remain unaccepted without a real ROI/date sample.
- Ruling: keep API grouping opt-in (`any`), near-date UI window 3 days, and stable cloud masking default on. No automatic expansion of user limits. Candidate cap400 / shortlist5 / pixel checks3 are reported bounded-search limits, not a claim of globally optimal selection.
- Python verification: `.venv/Scripts/python.exe -m unittest discover -s tests -v` -> 624 tests, OK with 3 optional EODAG skips, 88.449 s; `downloads/coherent-mosaic-python-suite.log`. A subsequent nonoptical compatibility guard was checked with all 14 mosaic API fixture tests, passing; `downloads/coherent-mosaic-api-final.log`.
- JS verification: `node --test tests/*.cjs` -> 155 passed, zero failures/skips; `downloads/coherent-mosaic-ui.log`.
- Browser verification: `node tests/browser/color-balance-acceptance.cjs` -> Chrome PASS for existing RAW balance and new coherent/stable mask flow. Evidence: `downloads/coherent-mosaic-browser.json`, `downloads/coherent-mosaic-browser.png`; fixture API, not real imagery.
- Completed live EE synthetic verification, including final repeat after fixes: S2 and Landsat, both mask modes; all unmasked data coverage 100%, masked coverage about75.1466%, source IDs unchanged, cloudy pixels masked and valid zero retained unmasked. Evidence: `downloads/coherent-mosaic-live.json`, `downloads/coherent-mosaic-live.log`. Final repeat completed successfully despite the provider warning about restricted noncommercial compute quota; no real-image visual acceptance claimed.
- No new dependencies, branch/worktree changes, commits or push. Keep existing unrelated changes. Native Chrome profile is disposable and separate from the user profile. No deferred reviewer Minor findings remain after metadata reporting clarification.
