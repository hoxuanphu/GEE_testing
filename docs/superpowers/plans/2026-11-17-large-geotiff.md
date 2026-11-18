# Large GeoTIFF implementation plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Fix Sentinel-2 GEE exports failing near 4 GiB and keep local raster memory bounded as output dimensions grow.

**Architecture:** Retain the existing GEE download and raster contracts. Use tiled, band-interleaved BigTIFF for local outputs, bounded mosaic/stack windows, and atomic publication after successful close. Keep network downloads bounded by existing per-band tiles and six workers.

**Tech stack:** Existing Python, Rasterio/GDAL, NumPy, FastAPI, unittest; no dependency changes.

**Spec:** User-approved direction in this conversation: fix large-file writing, reduce RAM, clean partial results, preserve geospatial/data contracts. Earlier read-only restriction was superseded by the explicit implementation request.

## Global constraints

- Preserve the dirty checkout and stay on the current branch; no commits or pushes.
- Preserve band order, Float32 values, valid zero, categorical nearest-neighbour, NoData, CRS, transform and metadata.
- Support the existing Rasterio >=1.3 floor; do not depend on newer merge `mem_limit` parameters.
- A failed write must not replace an existing valid result or expose an incomplete new result.
- This does not remove GEE quotas or imply unlimited disk, concurrency, or provider availability.

## Review focus

- Compressed multi-band outputs can exceed 4 GiB while being rewritten: force BigTIFF and band interleave.
- Tile overlaps, seams, explicit masks, sentinel NoData, and water zero must preserve first-valid merge semantics.
- Failure during write or close must preserve an existing destination and remove only this attempt's temporary files.
- Differing band grids must fail explicitly instead of silently stacking shifted data.
- Large images must not be allocated as full-band mosaics or a full multi-band stack.

### Task 1: Safe bounded local raster output

**Files:** `app/raster_grid.py`, `app/imagery.py`, `app/vector_mask.py`, `tests/test_large_rasters.py`.

**Interfaces:** Preserve `grid_profile`, `write_grid_band`, `warp_to_grid`, and `merge_band_tiffs` public signatures. Add a shared atomic-path context manager. Mosaic each band in bounded destination windows with globally anchored source sampling and existing first-valid merge semantics.

- [x] Add regressions for actual BigTIFF output, bounded tile reads/merges, masks/zeros/overlaps, mismatched grids, and failed writes preserving destinations.
- [x] Run `python -m unittest tests.test_large_rasters -v` and record expected failures.
- [x] Implement shared output profile, atomic publication, and windowed mosaic/stack writing.
- [x] Run the new regressions and existing rotated, rectangle, imagery and auxiliary-band tests.

### Task 2: Report local write failures and document limits

**Files:** `app/main.py`, `tests/test_large_rasters.py`, `README.md`.

**Interfaces:** Preserve download request/response contracts. Return a descriptive local-write error with server traceback for Rasterio/OSError failures; remove unpublished output on error.

- [x] Add an endpoint regression injecting a local disk/write error, run it RED, then implement cleanup and handling.
- [x] Document BigTIFF compatibility, disk/RAM behaviour, and remaining upstream limits.
- [x] Run `python -m unittest discover -s tests` with full output captured.
- [x] Run a local BigTIFF check with a logical payload >4 GiB, reading last-band/last-block values; separately report whether a full live GEE retry occurred.
- [x] Review the final task diff and record validation results below.

## Evidence

- Pre-flight: Task 2 consumes Task 1's atomic output and preserves the existing error/status boundary. No interface conflict.
- Initial evidence: 8333 x 7695 x 12 Float32 partial TIFF, 4,294,881,614 bytes, classic TIFF header, first five band descriptions only. Traceback fails inside `write_grid_band`.
- Small diagnosis: pixel-interleaved per-band writes produced 74.03 MiB versus 10.79 MiB for band interleave with the same data; current defaults created classic TIFF at incident dimensions.

- Task 1 RED: new tests exposed classic TIFF output, full-band reads, unbounded mosaics, shifted grid acceptance, and partial destination replacement. Logs: `downloads/large-raster-red.log` and `downloads/large-raster-errors-red.log`.
- Task 1/2 GREEN: 12 new raster regressions passed, including fractional origins/resolution, valid zero/NaN, atomic write failure, polygon BigTIFF preservation, local API error cleanup, and >8x8 pretiling.
- Ruling: create BigTIFF for all outputs on these shared paths rather than use a compressed-size heuristic. Cost: older TIFF-only readers need BigTIFF support; GDAL/QGIS support it. This avoids uncertain compressed-size prediction.
- Ruling: preserve current GEE network scheduling, quotas and ROI limits; this change bounds raster array memory and removes the 4 GiB file ceiling, not upstream execution limits or HTTP resume. No live GEE retry was performed.
- Ruling: copy the BigTIFF options when polygon masking because Rasterio dataset profiles do not retain the BigTIFF creation option; otherwise vector outputs could regress to classic TIFF.
- Ruling: derive download tile counts from an eight-million-pixel default and remove the fixed eight-tile-axis ceiling; more provider calls for large regions are exchanged for bounded per-request payloads.
- Independent review: `review_large_geotiff` found one Important issue (fractional origins produced window seams). Fixed using globally anchored source/destination windows. Regression `test_fractional_tile_origins_match_original_full_merge_without_seams` failed before the correction and passed after it. No Critical findings or deferred minors.
- Full suite final: `.venv\Scripts\python.exe -m unittest discover -s tests` ran 537 tests in 50.977s: OK, 3 skipped (two optional EODAG SDK tests and the opt-in live EODAG smoke test). Log: `downloads/large-raster-suite.log`. Dependency deprecation warnings remain; dependencies were not changed.
- Physical large-file validation: 8192x8192x20 Float32, 5,368,709,120 logical bytes; BigTIFF physical size 4,804,256,511 bytes (4.474 GiB). Last blocks of all 20 bands and a warp from data beyond 4 GiB matched exactly. Rasterio 1.5.1 / GDAL 3.12.4; 244.8s. Large generated files removed after verification; proof: `downloads/large-raster-validation/result.json` and `downloads/large-raster-validation-run.log`.
- Final source check: `git diff --check` passed. Existing unrelated dirty changes retained; no commits, branch changes, or pushes.
