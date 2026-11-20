# Restore recent optical merge edit and use existing RAW

User instruction: restore the code referred to as 14:40, then switch to RAW.
No timestamp-specific VS Code snapshot exists for that time. Restore the exact
unmasked point-mosaic edit and its tests made in this conversation, then route
public optical `merge_scenes` requests through the repo's existing per-tile RAW
selector. Preserve BigTIFF and all other existing dirty changes.

Decision: RAW means the existing `_least_cloudy_per_tile(..., keep_clouds=True)`:
one scene per MGRS/WRS tile, blue-band mean at 1 km as the selection heuristic,
newest selected tile priority in overlap, original cloud pixels retained. This is
different from exact per-scene SCL cloud percentages over the ROI. Keep the >=99%
data coverage gate and original rectangular warp halo. Single-scene selection is
unchanged; explicit administrative composite choices remain available. Set RAW as
the default in the UI and composite endpoints.

- [x] Back up seven affected files and record SHA-256 before edits in
  `downloads/raw-mosaic-rollback/manifest.json`.
- [x] Verify new route/metadata/halo tests fail against the old sequential path;
  `downloads/raw-mosaic-red.log`. Verify RAW UI request regression fails;
  `downloads/raw-mosaic-ui-red.log`.
- [x] Restore the preceding point-mosaic implementation/test changes, then add
  the RAW public dispatch, defaults, response metadata and explicit download URL.
- [x] Focused tests: 37 passed; rectangle UI tests: 30 passed.
- [x] Run full Python suite, independent review and final source checks.

No exact timestamp restore or live performance improvement is claimed. The
backup records the actual before-state; this plan supersedes the public merge
behaviour documented in `2026-11-17-unmasked-roi-mosaic.md`.

Final validation: full suite ran 540 tests in 106.954s, OK with 3 optional EODAG
skips; 30 rectangle UI tests passed. Independent reviewer found no Important or
Critical issues and independently reran 8 RAW/point coverage tests. Source diff
check passed. Running server GET /openapi.json confirmed both SceneRequest and
/api/download/{dataset} composite_method defaults are raw. No live GEE scene
search or speed benchmark was run. No branch changes, commits or pushes.
