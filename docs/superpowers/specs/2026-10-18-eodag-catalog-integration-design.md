# EODAG Catalogue Integration Design

**Date:** 2026-10-18

**Status:** Architecture approved; detailed specification awaiting user review

**Scope:** Optional EODAG catalogue, metadata, collection discovery, and quicklook support

## Problem

The repository already has a stable catalogue flow in `app/scene_catalog.py`, a CDSE OData client in `app/cdse.py`, an Earth Search STAC client in `app/aws_stac.py`, and specialized download and raster-processing paths. These paths preserve important application behavior: exact scene snapshots, inclusive date handling, cloud-range filtering, pagination warnings, COG window reads, CDSE band downloads, HTTP Range requests, `.SAFE` ZIP streaming, rotated output grids, and GEE processing.

EODAG can add provider configuration, common product metadata, collection discovery, and quicklook support, but it must not replace those specialized paths prematurely. The first integration therefore adds EODAG as an optional catalogue backend behind an explicit adapter. Existing providers and downloads remain the default and retain their current API behavior.

## Goals

1. Add an optional EODAG backend for catalogue search and normalized metadata.
2. Support explicit Earth Search and Planetary Computer provider selection for Sentinel-2 L2A, with provider identity preserved in every result.
3. Expose configured EODAG collections and provider capabilities without exposing credentials or transport URLs.
4. Proxy provider quicklooks through the backend when a quicklook is available.
5. Preserve current search semantics: inclusive dates, cloud bounds, 50-result pages, provider limits, `truncated` warnings, immutable snapshots, and missing metadata represented as `null`.
6. Keep all existing GEE, CDSE, AWS, raster, mosaic, ZIP, and time-series download paths unchanged unless a later compatibility-tested feature explicitly opts into them.
7. Allow EODAG to be disabled or absent with no import or startup failure in the existing installation.

## Non-goals

- Replacing `app/cdse.py` or `app/aws_stac.py`.
- Routing EODAG products into the current CDSE/AWS download endpoints by guessing that two IDs or collections are equivalent. Reusing the existing Earth Search downloader is allowed only after verifying the same provider-native item, as specified below.
- Adding EODAG downloading, `.SAFE` construction, HTTP Range handling, COG crop, mosaic, or GEE export in this version.
- Replacing GEE imagery selection, cloud masking, composites, time-series jobs, raster grids, or administrative boundary logic.
- Automatic fallback from a user-selected EODAG provider to another provider. A search snapshot must remain reproducible and provider-specific.
- Treating a missing cloud value, footprint, orbit, processing level, or quicklook as a value that can be inferred safely.

## Architecture

```text
Existing scene-catalog API
          |
          +-- source=gee   -> existing GEE/time-series code
          +-- source=cdse  -> existing CDSE OData code
          +-- source=aws   -> existing Earth Search STAC code
          +-- source=eodag -> app/eodag_adapter.py
                                  |
                                  +-- explicit Earth Search provider
                                  +-- explicit Planetary Computer provider
                                  +-- EODAG collection/provider metadata
                                  +-- quicklook fetch
```

`app/eodag_adapter.py` is the only application module allowed to import EODAG. It owns EODAG initialization, provider allowlisting, collection mapping, search pagination, metadata conversion, product re-resolution, and quicklook retrieval. The adapter exposes application-shaped dictionaries and a small protocol so `scene_catalog.py` does not depend on EODAG product internals.

EODAG is optional. The base `requirements.txt` remains sufficient for the existing application. A separate `requirements-eodag.txt` includes the base requirements and pins `eodag==4.9.2`, the release whose provider configuration and API were checked for this design. Installation is verified in a separate virtual environment before adopting that pin; a dependency or provider incompatibility blocks feature enablement. Installation must not alter the user's current virtual environment during implementation. The adapter must import EODAG lazily and report a clear disabled/unavailable status instead of failing `import app.main`.

The adapter uses the v4 `search(..., provider=..., raise_errors=True)` result and its `next_page()` interface; it does not depend on deprecated `search_iter_page()` or `available_providers()`. Instances with mutable pagination/plugin state are isolated per operation; concurrent requests must not change another request's selected provider, configuration, or results. Existing extra-CA/proxy handling is reused and TLS verification remains enabled.

## Configuration and provider policy

The following environment settings are added to `.env.example` and documented in `README.md`:

```dotenv
EODAG_ENABLED=false
EODAG_PROVIDERS=earth_search,planetary_computer
EODAG_CONFIG_FILE=
EODAG_MAX_RESULTS=4000
EODAG_QUICKLOOK_MAX_BYTES=10485760
```

`EODAG_ENABLED=false` is the compatibility default. `EODAG_PROVIDERS` is an administrator-controlled allowlist, restricted in this release to `earth_search` and `planetary_computer`. A request can select one allowlisted provider but cannot extend that allowlist. Provider-specific authentication and configuration remain in the explicitly configured EODAG file or environment variables; they are never accepted from the browser and never returned in API responses. EODAG configuration/cache files belong in an application-owned directory under `downloads/eodag`; the feature must not rewrite a user's unrelated global EODAG configuration. `EODAG_MAX_RESULTS` is validated in the range 1–4000 and applies only to explicit EODAG requests.

The application's logical product is `sentinel2_l2a`. EODAG 4.9.2 maps Earth Search to `S2_MSI_L2A_COG` and Planetary Computer to `S2_MSI_L2A`; both use native collection `sentinel-2-l2a`. These identifiers are separate fields, not interchangeable collection IDs. The adapter records the actual provider, native collection, EODAG collection ID, and native item ID in each result. It does not assume that an Earth Search COG and a Planetary Computer asset have identical processing baseline, band layout, SCL values, or scaling.

| Application product | Provider | EODAG collection | Native collection | Existing downloader |
|---|---|---|---|---|
| `sentinel2_l2a` | `earth_search` | `S2_MSI_L2A_COG` | `sentinel-2-l2a` | Same-item verification required |
| `sentinel2_l2a` | `planetary_computer` | `S2_MSI_L2A` | `sentinel-2-l2a` | Unsupported in this release |

## Canonical scene schema

EODAG results are converted to the existing scene-catalog row shape and these additional non-secret fields:

```json
{
  "id": "provider-native-product-id",
  "name": "provider-native-title",
  "date": "2026-01-31",
  "timestamp": "2026-01-31T04:12:03Z",
  "cloud_pct": 23.4,
  "tile": "48PWS",
  "platform": "Sentinel-2A",
  "instrument": "MSI",
  "processing_level": "L2A",
  "product_type": "S2_MSI_L2A",
  "provider": "planetary_computer",
  "collection": "sentinel-2-l2a",
  "eodag_collection": "S2_MSI_L2A",
  "bbox": [106.0, 10.0, 106.2, 10.2],
  "footprint": null,
  "asset_keys": ["B02", "B03", "B04", "B08", "SCL"],
  "quicklook_available": true,
  "download_capabilities": {
    "status": "unsupported",
    "reason": "provider_download_not_integrated"
  }
}
```

`timestamp` is the source sensing time normalized to UTC when available. `date` is its ISO date; a source date without time does not become an invented midnight timestamp. `cloud_pct`, `footprint`, `tile`, orbit, processing, and asset fields remain `null` or empty when the provider omits them. EODAG missing-value sentinels and non-finite numbers become `null`. Malformed or out-of-range cloud metadata is counted as missing, not a fatal error for every scene. Products without a stable native item ID are skipped with an explicit warning/count.

`asset_keys` contains provider-native names only. Optional `asset_descriptors` can include a canonical band ID, media type, resolution, units, scale, and offset only when sourced from the provider or a tested version-specific mapping. Keep a separate `source_metadata` allowlist for processing baseline, product URI, relative orbit, orbit direction, polarization, and provenance of normalized values. Do not invent harmonization or imply that EODAG-normalized reflectance equals GEE harmonized reflectance. Signed URLs, bearer tokens, SAS query strings, credentials, and local paths are never stored. The row `id` is the native item ID; any different EODAG product ID is retained separately as `eodag_product_id`.

The existing snapshot sanitizer remains a second line of defense. Before writing a snapshot, the catalogue removes transport URLs and credentials from all provider metadata. The snapshot stores `source=eodag`, `provider`, `collection`, product ID, canonical metadata, query parameters, and a capability record, not an EODAG object or serialized URL.

## Search behavior

`CatalogRequest` gains `source=eodag`, an optional `provider`, and an optional `eodag_collection`. EODAG requests use `dataset=s2`, require an allowlisted `provider`, and default `eodag_collection` to that provider's supported entry in the mapping table. A supplied collection must match that mapping. Requests combining `source=eodag` with GEE datasets or `dataset=s2aws` are rejected with HTTP 422 rather than silently entering the existing branch where `s2aws` overrides the requested source. Provider/collection fields on non-EODAG requests are rejected when non-null. Existing payloads without these new fields preserve behavior and do not acquire new defaults in their persisted parameter dictionaries.

The adapter receives the same normalized selection bbox already computed by `scene_catalog.py`, for point, rotated point, province, or commune modes. It uses that bbox as the search envelope, preserving the existing external-catalogue semantics rather than promising exact polygon clipping. It passes the date interval as `[start_date 00:00:00 UTC, day-after-end_date 00:00:00 UTC)` and enforces that interval locally for timestamp-bearing results. Provider-specific inclusive-end behavior is handled by the adapter, including next-day exclusion. Date conversion is tested at month and leap-day boundaries. A provider's footprint intersection is not treated as proof that every pixel in the requested region is covered.

Cloud bounds are pushed down only through provider queryables verified to express the same minimum/maximum comparison. In particular, a scalar EODAG cloud-cover parameter must not be mistaken for a minimum. When pushdown is incomplete, the adapter filters fetched pages before selecting the result cap and continues fetching until exhaustion or the work budget is reached. No first-page-only cloud filtering is allowed.

Explicitly request descending sensing-time order from the selected provider before applying any result cap; Earth Search's checked EODAG configuration otherwise defaults to ascending order. Sort locally again for deterministic ties, but do not assume sorting a capped ascending subset recovers the newest acquisitions. If provider-side ordering cannot be established, report that limitation and mark capped/budget-limited results partial.

The adapter uses 100 items per provider page and performs pagination until 4000 qualifying unique results (or a configured lower cap), confirmed exhaustion, a repeated-page condition, 200 pages, or a 120-second operation budget. Configure each provider's request timeout to at most the remaining operation budget and 30 seconds; this must constrain total elapsed time as well as socket inactivity, and no retry may outlive the operation budget. Provider page limits (500 Earth Search, 1000 Planetary Computer in the checked release) are distinct from the application result cap. De-duplicate by `(provider, native collection, native item ID)` only; do not merge acquisitions across providers or processing versions. The response records:

```json
{
  "parameters": {
    "source": "eodag",
    "provider": "earth_search",
    "provider_limit": 4000,
    "truncated": true,
    "completion": "partial",
    "truncation_reason": "result_cap"
  }
}
```

For EODAG responses, `total` is the number of qualifying rows in the stored snapshot, not the remote catalogue's total count. `completion=complete` is used only after confirmed provider exhaustion. `truncated=true` and `completion=partial` cover result caps, page/time budgets, repeated-page loops, and a provider error after usable pages. Set the precise `truncation_reason` and a sanitized warning, even when local cloud filtering leaves fewer than 4000 rows. A failure before any usable page is HTTP 502; a successful exhausted search with no qualifying scenes is HTTP 200 with `total=0`. Missing SDK/configuration is HTTP 503. Provider limits are never silently raised above the configured hard cap. EODAG's own provider-priority fallback is disabled by always passing the explicitly selected provider, and returned product provider identities must match it.

Results are filtered again in the adapter/catalogue boundary. A scene with an unknown cloud value is excluded from a cloud-range result, matching the current catalogue behavior; a valid `0` value remains valid. Results are sorted newest-first using timestamp, date, and stable product ID. The stored result order is immutable and page URLs continue to reference snapshot indices, so a later provider query cannot change what a user selected.

## Collection and provider discovery API

Add these read-only endpoints:

- `GET /api/eodag/status` returns `{enabled, installed, ready, configured_providers, supported_sources}` and a sanitized error code/message when unavailable. It is a local status check, performs no remote discovery, and uses package metadata to determine installation without constructing the SDK when disabled.
- `GET /api/eodag/providers` returns only allowlisted provider names, descriptions, availability state, and supported logical collections.
- `GET /api/eodag/collections?provider=<name>` returns collection ID, title/description, provider, product type, and supported capabilities (`search`, `quicklook`).

Collection discovery lists only the two allowlisted providers. It may show other provider collections as `search=false` with `reason=not_enabled_for_search`, but it never turns a discovered collection into an enabled dataset automatically. Sentinel-2 L2A is the only enabled product in this release. Provider availability means local configuration readiness, not a promise of current network health; discovery never attempts every provider at application startup.

Discovery is cached per provider for 15 minutes, with one refresh at a time per provider. On refresh failure, a previous successful entry is returned with `stale=true`, its fetch time, and a sanitized warning. Without a previous entry, the explicit provider operation returns HTTP 502. Error states are cached for 30 seconds to prevent repeated failed requests. Disabled discovery endpoints return HTTP 503; status remains available. A failed provider must not make other providers or the existing CDSE/AWS endpoints unavailable. Errors contain provider and operation names but no request headers, tokens, signed URLs, or credential values.

## Quicklook API

Add `GET /api/scene-catalog/{search_id}/quicklook/{index}`. It resolves the scene from the immutable snapshot, checks that `source=eodag` and `quicklook_available=true`, then asks the adapter to resolve the exact product by provider, EODAG collection, native collection, and native product ID. It never trusts a URL supplied by the client or a URL saved in the snapshot. Provider, collection, and item identity must match again after restart; a replacement/reprocessed item must not be substituted.

Fetch the provider's quicklook/thumbnail reference through an adapter-owned bounded HTTP reader; do not call an SDK download method that writes unbounded data before the application can check its size. Use EODAG authentication plugins only when that reference requires signing; signed references live only for the current operation. If no supported image reference can be fetched, report quicklook unavailable without falling back to a full-resolution band download.

The reader allows only configured HTTPS provider/asset hosts, rejects local/private destinations, and validates each redirect before following it (maximum three redirects). These restrictions apply to the new quicklook proxy only and do not alter the existing downloaders. Enforce the byte limit while reading even without `Content-Length`, a 30-second total fetch deadline, JPEG/PNG/WebP signatures, and a Pillow image dimension limit of 16 million pixels. Return HTTP 413 for oversized content, HTTP 415 for unsupported content, HTTP 404 for missing scene/quicklook, HTTP 503 when the feature is disabled, and a sanitized HTTP 502 for upstream failure. Images are read into a bounded buffer and responses set `X-Content-Type-Options: nosniff`. Do not proxy arbitrary HTML or SVG. No persistent quicklook file is necessary; temporary data is removed on success and failure. Never initialize GEE for this operation.

The UI shows an optional `Xem nhanh toàn cảnh` action for rows with `quicklook_available=true`; this does not change the existing preview button. Fetch only after user interaction, never eagerly for all 4000 rows. A quicklook error remains local to the row and does not clear results or trigger another search.

## Existing download and processing guarantees

When EODAG is disabled, existing endpoints, response schemas, provider defaults, limits, and tests remain unchanged. When EODAG is enabled, only requests explicitly selecting `source=eodag` use it. A user selecting CDSE or AWS continues to use the current downloader, including CDSE Nodes band downloads, CDSE S3 object listing, HTTP Range chunking, `.SAFE` ZIP streaming, AWS COG window reads, province mosaics, and rotated grids.

Planetary Computer scenes have `download_capabilities.status=unsupported`, no download URL, and a visible explanation that this source currently supports catalogue/quicklook only. Calling their download route directly returns HTTP 409 with code `download_not_supported`, without contacting CDSE or Earth Search.

Earth Search scenes can reuse `/api/s2/aws/download` through a catalogue-only verification step. Expose `status=requires_verification` only when the pinned SDK's fixture-tested native item ID mapping and native collection are available. Clicking the action uses the adapter to retrieve the raw item at the fixed Earth Search native collection/item endpoint and compares it with the saved snapshot: provider/collection/native ID, sensing timestamp, tile, processing level, and processing baseline. Then check the existing `aws_get_item` projection has that same native ID and the required download assets before redirecting with the unchanged snapshot selection. Required comparison fields must be present and equal; missing metadata or a mismatch returns HTTP 409 `product_identity_unverified` without choosing an alternative item. The existing `aws_get_item` normalization discards timestamp/baseline, so it cannot be the only identity evidence. Native item retrieval is not a broad spatial search. No new raster or EODAG downloader is introduced. The additional provider reads occur on demand rather than once per result during search.

For legacy snapshots without capabilities, preserve the current download URL and dispatch behavior. For new snapshots, `page()` must add URLs according to row capabilities rather than generate a download URL unconditionally. Direct requests must enforce the same capability rules server-side. EODAG-disabled rollback must preserve old CDSE/AWS/GEE snapshots; existing EODAG snapshots remain readable with cached metadata, but SDK-dependent quicklook/verification actions return HTTP 503 until re-enabled.

The current GEE `imagery.py`, `raster_grid.py`, `timeseries.py`, `admin_boundaries.py`, `commune_boundaries.py`, and raster export code are outside this change. A future Planetary Computer or EODAG download experiment requires a separate provider-specific design and compatibility checks.

## Minimal UI integration

Keep the existing map, form layout, dataset choices, automatic best-scene mode, time-series mode, and legacy preview/download controls. Add optional controls next to the current Sentinel-2 catalogue source choice only when status is ready and manual scene listing is active: an EODAG provider selector and a supported collection selector populated by discovery. The product name identifies the provider and clearly marks Planetary Computer as catalogue/quicklook only. Do not replace the existing `s2_source` options used by `/api/s2/scenes` or province mosaic flows.

Put additive behavior in `static/eodag.js`, with small integration hooks in `static/index.html`. On status error, timeout, or disabled feature, omit the new controls and preserve existing rendering. Result rows show provider, processing level if known, capability-appropriate actions, missing metadata as `Không có dữ liệu`, and a warning for partial results. Insert provider text using `textContent`, not interpolated HTML. Preserve selection snapshots when users change source or ROI, and prevent a stale search/discovery/quicklook response from replacing a newer selection. Returning to an old result uses the saved provider and ROI rather than the current controls. No changes belong in the untracked `frontend/` analysis work.

## Error handling and security

- Missing EODAG installation with `EODAG_ENABLED=false` is normal; status reports disabled and the app starts.
- Missing installation with an explicit EODAG search returns HTTP 503 with installation/configuration guidance.
- Provider timeout/authentication failure before useful results is a sanitized HTTP 502; failures after useful results preserve a partial snapshot. Malformed individual rows are skipped with an explicit count. Quicklook and capability errors use the specific status codes defined above without leaking transport details.
- No automatic provider fallback occurs within a search.
- Provider and collection names use fixed allowlists. Product IDs must match the saved snapshot, have a bounded length, and are encoded as identifiers rather than interpreted as URL/path fragments.
- Quicklook and product re-resolution use server-side adapter calls only.
- Snapshot and logs exclude signed URLs, SAS tokens, authorization headers, cookies, passwords, API keys, and local paths.
- Existing UUID snapshot URLs are not treated as authentication; the deployment remains local/trusted unless separate ownership controls are added.

## Testing strategy and acceptance criteria

All tests use mocks or local fixtures; CI does not require provider credentials or network access.

1. Adapter tests verify lazy import, disabled behavior, concurrent provider isolation, version-specific collection/native-ID mapping, inclusive dates, bbox geometry, cloud minimum/maximum, zero cloud, malformed/missing cloud/footprint, UTC normalization, duplicate IDs across pages, repeated pages, a matching scene appearing only on a later page, cap/page/deadline/partial-failure warnings, and no automatic fallback. Include a result set exceeding 1000 items to disprove an accidental per-page total cap.
2. Contract tests verify that EODAG rows contain the canonical schema, no signed URL survives snapshot creation, and provider/collection/product identity is preserved.
3. Discovery tests verify provider isolation, sanitized failures, collection capability output, and discovery caching.
4. Quicklook tests verify snapshot-index lookup, restart re-resolution, identity mismatch, chunked bodies without content length, oversize images, private destinations/redirects, image format/dimension limits, missing quicklook behavior, cleanup, and rejection of client-supplied URLs. Assert no full raster download or GEE initialization occurs.
5. Existing `tests/test_scene_catalog.py`, CDSE/AWS tests, API tests, imagery tests, raster tests, and time-series tests must remain green without EODAG installed.
6. Capability tests verify Earth Search same-item handoff with original ROI/rotation, missing/mismatched baseline rejection, native-ID mismatch, Planetary Computer returning no download link and HTTP 409 on direct download, and legacy snapshots remaining usable after restart and with EODAG disabled.
7. UI tests using local API fixtures cover optional installation visibility, provider/collection selection, stale responses, missing fields, quicklook failure, partial-result warnings, and capability-aware actions. Existing sources remain usable if status/discovery requests fail.
8. An opt-in smoke test, documented separately from unit tests, checks one Earth Search and one Planetary Computer search/quicklook with the real pinned SDK in an isolated configured environment. Record provider, EODAG/native collection, native item ID, normalized metadata, pagination behavior, and whether a quicklook is available; do not download full scenes. Enabling Earth Search handoff also requires a smoke check of native identity verification. A failed live check is reported explicitly and blocks promotion of the affected capability, not the old application.

Acceptance requires a fresh baseline of the current working tree (the earlier review observed 153 passing tests; this count is historical and must be re-measured at implementation time). Run the full old/new suite in two isolated environments: base dependencies with EODAG absent, and optional dependencies with EODAG installed. Verify both feature-disabled and feature-enabled paths, SDK-specific tests against the pinned SDK, `pip check`, and application startup on Windows/Python 3.12. No existing source may change behavior merely because `requirements-eodag.txt` is installed. Mock tests alone cannot establish that the pinned SDK calls and pagination work.

For fixed local fixtures, legacy default searches must make zero EODAG calls, perform no EODAG startup discovery, preserve response semantics and stored snapshot paging, and keep raster band order/units/masks/nodata/grid output identical. Catalogue pages continue to load from SQLite without provider re-query. The new source adds provider-specific metadata, discovery, and quicklook; it does not replace a legacy function with a reduced-capability path. No global latency or data-quality improvement is claimed without measurements.

## Rollout

1. Ship the adapter, optional dependency file, configuration, endpoints, tests, and documentation with EODAG disabled.
2. Enable Earth Search in a local environment and compare result counts, dates, cloud values, tile IDs, footprints, and quicklooks against the existing AWS path without changing the default UI source.
3. Enable Planetary Computer separately and record provider-specific metadata differences; do not merge results across providers in one snapshot.
4. Only after the comparison is stable may the UI expose an optional EODAG source selector. If the smoke evidence is absent, keep the feature disabled by default and document the remaining enablement check. Existing source choices remain the defaults.
5. EODAG download support is a separate design and release decision.

## Implementation boundary

Expected implementation files are `app/eodag_adapter.py` (SDK boundary), `app/catalog_models.py` (application contracts), `app/eodag_api.py` (status/discovery routes), `app/catalog_quicklook.py` (bounded image reader), `app/scene_catalog.py` (optional dispatch/snapshot actions), `app/main.py` (route registration before the static mount), `static/eodag.js`, minimal hooks in `static/index.html`, `.env.example`, `README.md`, `requirements-eodag.txt`, and focused adapter/catalogue/UI tests. Split the adapter further only if needed to keep SDK query conversion and metadata normalization understandable. Do not refactor the current catalogue's nested registration pattern or move the shared time-series store merely for this feature.

The implementation must not modify the current imagery, raster, time-series, CDSE download, AWS COG, or boundary algorithms. Work from the user's actual working tree, which currently has uncommitted changes; a checkout of HEAD alone omits relevant CDSE/S3 credential behavior. Capture a fresh baseline, preserve those changes, and isolate implementation using the worktree workflow without discarding or silently committing unrelated edits.

## Delivery slices for the implementation plan

1. Capture compatibility contracts and add optional dependency/configuration handling with no default-path changes.
2. Implement the tested SDK adapter, metadata mapping, bounded search pagination, and safe snapshot integration.
3. Add cached provider/collection discovery and optional API integration.
4. Add bounded quicklook retrieval and provider-specific capability enforcement, including verified same-item Earth Search handoff.
5. Add minimal optional catalogue controls and test graceful UI failure behavior.
6. Verify both installation modes, run opt-in SDK/provider smoke checks, document evidence, and retain default-off rollback.

The implementation plan must give each slice exact files, contract signatures, meaningful failing tests, validation commands, and promotion criteria. Writing application code is not authorized by approval of this specification alone; the current request is planning.

## Source evidence and review notes

Repo anchors: `app/scene_catalog.py` currently forces `s2aws` to `aws`, adds a download URL to every row, removes assets before calling `TimeSeriesStore`, and dispatches any non-GEE/non-AWS snapshot into CDSE. Each of those integration points needs an explicit EODAG branch; adding only `source=eodag` to the request regex is unsafe. `TimeSeriesStore._safe` removes URL-like keys/values, so quicklook links must be generated on read and stable identifiers stored instead of transport URLs.

Checked against upstream tag `v4.9.2` on 2026-10-18:

- https://github.com/CS-SI/eodag/blob/v4.9.2/eodag/api/core.py — explicit provider search, collection listing, v4 pagination, default provider fallback behavior.
- https://github.com/CS-SI/eodag/blob/v4.9.2/eodag/resources/providers/earth_search.yml — `S2_MSI_L2A_COG`, native `sentinel-2-l2a`, per-page `max_limit=500`, default ascending sort, asset URL conversion.
- https://github.com/CS-SI/eodag/blob/v4.9.2/eodag/resources/providers/planetary_computer.yml — `S2_MSI_L2A`, native `sentinel-2-l2a`, per-page `max_limit=1000`, SAS authentication.
- https://github.com/CS-SI/eodag/blob/v4.9.2/eodag/api/product/_product.py — quicklook API downloads to a local path; an application bound must be enforced before accepting image bytes.

These checks are source/configuration inspection, not proof that both remote providers currently return complete results or provide every quicklook. The earlier statement that EODAG imposed total result limits of 500/1000 is corrected here: those values are provider page limits.
