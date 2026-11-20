# Unmasked ROI scene mosaic

User requirement: for overlapping Sentinel-2 ROI scenes, prefer the least-cloudy
scene across its available ROI footprint, then fill remaining gaps from the next
scenes. Keep cloud pixels; avoid cloud-mask cut-and-paste composites.

Scope: the existing optical point/rectangle `merge_scenes` path, shared by
Sentinel-2 and Landsat preview/download. Keep date/platform/cloud filters, ROI
ranking, original data masks, >=99% data coverage and component metadata. Other
explicit province composite choices keep their existing algorithms.

- [x] Add a pixel-value regression with a cloudy preferred-scene pixel, valid zero,
  original NoData, and a fallback scene with better whole-scene cloud metadata but
  a worse ROI clarity score. Run it against both optical datasets before editing.
- [x] Replace masked/unmasked blending with the existing ordered GEE mosaic.
- [x] Update existing graph regressions and user-facing labels/documentation.
- [x] Run focused tests, full Python suite and existing rectangle UI tests.

Evidence: `downloads/unmasked-mosaic-red.log` shows the original algorithm replacing
preferred scene values 20/200 with fallback values 2/22 at its cloudy pixel. This
is the unwanted behavior; original NoData still needs fallback filling.

Validation: 42 focused tests passed; full Python suite ran 538 tests in 48.822s,
OK with 3 existing optional EODAG skips. 29 rectangle UI tests passed. Logs:
`downloads/unmasked-mosaic-focused.log`, `downloads/unmasked-mosaic-suite.log`,
`downloads/unmasked-mosaic-ui.log`. Independent reviewer found no Important or
Critical issues and reran the 15 scoring/coverage tests successfully. Source diff
whitespace check passed; no live GEE rerun or visual acceptance was claimed.
Existing dirty changes retained; no commit, branch change, or push.
