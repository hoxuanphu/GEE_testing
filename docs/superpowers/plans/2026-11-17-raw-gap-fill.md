# RAW original-data gap filling

User authorized implementation of the previously proposed repair: keep original
clouds and valid pixels; fill only missing footprints/NoData with other dates,
including dates in the same MGRS/WRS tile. Retain the 99% data coverage gate.

The one-scene-per-tile limit loses available coverage. Cloud masking violates
the requested output. Start with the best haze score per tile; rank overlap
within that primary set. Additional dates fill only gaps in the whole primary
set; they cannot replace a valid primary pixel, even when their haze score is
better than a primary from another tile. Stop at99%coverage or exhaustion.
Keep the existing 1 km blue-band haze ranking (a heuristic, not exact cloud %).

## Implementation and validation

- [x] Add a failing regression for two complementary scenes in the same tile.
- [x] In `app/imagery.py`, route only `keep_clouds=True` through the gap filler.
  Materialize ranking metadata once, without per-scene Python RPCs. Build a
  source-index mosaic from original joint spectral masks, then apply ownership
  to every source band so auxiliary masks cannot mix dates. A source histogram
  over the ROI identifies actual contributing scenes/dates for metadata.
- [x] Preserve explicit masked mosaic, alternate methods, ROI halo, valid zero,
  missing optional-band masks, and large-raster download changes.
- [x] Update RAW UI descriptions and README; run focused, complete Python, and
  relevant Node checks. Validate complementary masks and retained cloudy pixels
  using synthetic images evaluated by GEE, then smoke-test the live API.
- [x] Obtain an independent review; record results here. Do not commit/push.

Scope: `app/imagery.py`, `tests/test_raw_roi_mosaic.py`, `static/index.html`,
`README.md`. No dependencies, branch changes, or broad rollback.

## Evidence

- RED: same-tile fallback absent from result (`downloads/raw-gap-fill-red.log`).
- GREEN: 543 Python tests, 3 optional skips, no failures, 44.261 s
  (`downloads/raw-gap-fill-suite.log`); 36 Node tests passed.
- Live GEE synthetic: 94.6805% preferred coverage -> 100% result. Bright cloudy
  values retained, valid B8=0 retained, missing preferred QA60 remains null,
  fallback QA60=7 only in original data gaps; unused date excluded from metadata
  (`downloads/raw-gap-fill-live-validation.json`).
- Independent review found one Important issue: native UInt16 auxiliaries and
  Float placeholders reject mosaicking depending on stack order. Reproduced
  live RED, added offline graph regression, normalized original source bands to
  Float32 before adding Int32 source index. Both live fixtures now evaluate.
  Scripts retained as `.py.txt` in downloads to avoid triggering Uvicorn reload.
- Live API first attempt interrupted by auto-reload waiting for active request,
  client timed out after180s; worker restarted for fresh validation.
- Live API 11-month interval (2026-01-01 to2026-11-17 inclusive), 2x2km ROI:
  HTTP200,100%coverage,10.47s total (5.81s scene search), artifact
  `downloads/raw-gap-fill-live-api.json`.
- Large83.33x76.95km smoke of all-date stack exceeded180s. Added RED/GREEN
  regression proving full primary coverage does not build an80-date pixel
  stack; start1rankedsceneper tile, expand2/4/... only when coverage<99%, keep
  prior valid pixels ahead of all additional batches. Source histogram now
  includes original missing area using sentinel-1 to evaluate the stop gate.
  Synthetic GEE regression retained94.68->100%coverage and pixel/QA invariants.
- Final independent review found no Important/Critical issues under explicit
  primary-set-preservation and99%stop semantics; focused6tests passed.
- Final liveAPI:83.33x76.95km rectangle,2026-01-01..2026-11-17inclusive,
  HTTP200,4contributing scenes,99.9967289794%coverage,61.94s total
  (60.87s scene search). Artifact `downloads/raw-gap-fill-live-large-api.json`.
  This validates search/selection, not a new multi-GiB image download.
