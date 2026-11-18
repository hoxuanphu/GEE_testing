# RAW thumbnail categorical-source seam fix

User authorized fixing the diagnosed seam. Evidence and exact ROI are in
`downloads/thumbnail-seam-user-2023/diagnosis.md`.

## Scope and approach

Change only source-ID validity in `app/imagery.py:_raw_gap_fill`: expand missing
validity to zero, threshold `>0`, then mask a constant Int32 source ID. This
prevents GEE mosaic alpha blending from creating intermediate source labels.
Keep original spectral values/masks, clouds, valid zeros, auxiliary NoData,
source ownership, progressive gap filling,99%gate, halo and BigTIFF behavior.
Do not force whole-ROI reprojection or add cloud masking/blending.

## Evidence checklist

- [x] Explicit live GEE regression `tests/live_raw_seam.py` failed on old code:
  fractionally masked preferred source lost valid data; metadata falsely
  included unused source2. Log `downloads/raw-seam-fix-live-red.log`.
- [x] Offline graph guard failed on old code (`raw-seam-fix-red.log`).
- [x] Implement binary categorical source validity; leave original masks intact.
- [x] Live regression passes: preferred0.8, B8zero, QA60null; fallback0.2,
  QA60seven;100%coverage and only actual sources0/3 in provenance.
- [x]31focused unit tests passed, including large-raster checks.
- [x] Full offline suite:543tests,3optional skips,no failures,42.433s
  (`downloads/raw-seam-fix-suite.log`). Independent review found no
  Important/Critical issues and reran6focused tests successfully.
- [x] Fresh runningAPI thumbnail at exact user ROI/date:304transparent pixels
  before ->0after; verify native spectral/cloud/NoData behavior separately.

Backups: `downloads/raw-seam-fix-backup/*.py.txt` (avoid server autoreload).
No branch changes, commits or pushes.

First runtime validation waited behind Uvicorn graceful reload and timed out;
restarted the application worker, confirmed OpenAPI HTTP200, and requested a
fresh exact-ROI thumbnail without further Python file changes.

Final live acceptance:HTTP200,8contributors,100%coverage. Thumbnail512x466:
zero transparent pixels and zero partial alpha; visually inspected with no
white seam. Search129.23s, total including PNG retrieval133.69s. Artifacts:
`downloads/thumbnail-seam-fixed-2023/current.png` and `report.json`.
Live synthetic checks evaluated native10m spectral/cloud/zero/QA behavior;
no new multi-GiB satellite download was performed.
