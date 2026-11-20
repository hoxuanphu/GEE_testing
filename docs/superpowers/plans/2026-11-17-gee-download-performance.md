# GEE download performance implementation plan

> **For agentic workers:** Approved for implementation; use superpowers:executing-plans. Steps use checkbox syntax. Keep runtime/performance gates open until measured.

**Goal:** Tải nhanh hơn bằng cách bỏ xử lý lặp và TIFF trung gian có nén, có checkpoint cho các lượt tải lớn, giữ đúng dữ liệu/ROI/RAW.

**Architecture:** Snapshot bất biến từ search → tile plan không RPC → tải stream giới hạn tài nguyên → tile checkpoints → VRT và warp theo block vào BigTIFF đích. Job nền được thêm sau khi đường tải tối ưu ổn định.

**Tech Stack:** Python/FastAPI, Earth Engine SDK, Requests, Rasterio/GDAL, NumPy, SQLite, JavaScript, unittest/Node hiện có; không thêm dependency bắt buộc.

**Spec:** [Thiết kế và gate](../specs/2026-11-17-gee-download-performance-design.md).

**Status:** Đã được người dùng duyệt triển khai. Đang thực hiện và kiểm chứng; cờ tối ưu chưa bật mặc định. Bằng chứng và gate còn mở: [progress ledger](../../../.superpowers/sdd/2026-11-17-gee-download-performance/progress.md).

**Kết quả 18/11:** [Source đã giao, bằng chứng và gate còn mở](../reports/2026-11-18-gee-download-performance.md). Checkbox gộp nhiều điều kiện vẫn để mở nếu chưa kiểm đủ, nhất là full live ROI và rollout.

## Global constraints

- Giữ nguyên dirty checkout/branch; không reset, tạo branch, commit hoặc push trong công việc này.
- Tuân thủ toàn bộ hợp đồng dữ liệu ở spec §3, bao gồm categorical ownership fix, valid0/QA NoData, halo và BigTIFF.
- Thay đổi từng bước sau baseline; không đo nặng hoặc restart server khi người dùng đang tải.
- Không đổi dependency, giảm băng/resolution, ép full ROI10m hay nới gate để qua test.
- Ban đầu chỉ bật fast path Sentinel-2 GEE điểm/chữ nhật. Giữ API/link và các workflow khác hoạt động.

## Review focus

1. Snapshot chỉ lấy8contributing scenes làm mất16 selected sources/halo → Task1 kiểm trace đầy đủ và thứ tự.
2. VRT đổi first-valid thành last-valid hoặc sai fractional origins → Task3 kiểm parity toàn block.
3. Retry/auth/resume tải lại nhầm snapshot hoặc publish output cũ → Task2/4 kiểm fingerprint và attempt fencing.
4. Worker/GDAL tăng theo số job làm đầy RAM trên máy15,5 GiB → Task2/5 kiểm limiter và RSS.
5. Mất dữ liệu sau4 GiB, crash giữa rename và DB, hoặc báo xong khi chỉ tải input → Task4/6 kiểm publication/progress.

## Task 0 — Có baseline download trước khi tối ưu

**Files:** tạo `app/download_metrics.py`, `tools/benchmark_gee_download.py`, `tests/test_download_metrics.py`;
sửa `app/main.py`, `app/imagery.py`, `app/raster_grid.py` chỉ để đo; giữ SearchTimingMiddleware hiện có.

**Interfaces:** `DownloadMetrics(job_id)`; `phase(name)` context manager;
`record_tile(tile_id, *, event, bytes_count=0, elapsed_s=0, attempt=0)`;
`write_report(path)` tạo JSON/CSV không URL/token.
Benchmark CLI: `--case user-roi-12|user-roi-26|small|rotated|local-big`,
`--mode legacy|optimized`, `--repeat N`, `--cache cold|warm`, `--output PATH`.
`--live` phải chọn rõ; chế độ local dùng cùng fixture tile bytes.

- [x] Viết test nested timing/exception và redaction; chạy RED với unittest.
- [ ] Đo các pha và tài nguyên theo spec §5; Windows dùng process counters/ctypes hoặc công cụ đang có, không thêm package chỉ để đo.
- [x] Kiểm timing không thay dữ liệu hoặc biến lỗi thành thành công; chạy GREEN và suite.
- [ ] Chạy baseline local5lượt và live3lượt nếu có điều kiện, server đo không dùng `--reload`.
  Lưu request/snapshot hash, versions, CPU/RAM/đĩa, thời gian từng pha và cold/warm.
- [ ] Handoff: `downloads/download-performance/baseline/`; còn thiếu live phải ghi rõ, không suy diễn từ test nhỏ.

## Task 1 — Lưu và tải đúng lựa chọn đã preview

**Files:** tạo `app/download_models.py`, `app/download_store.py`, `app/download_snapshots.py`,
`tests/test_download_snapshots.py`; sửa `app/imagery.py`, `app/main.py`,
`tests/test_raw_roi_mosaic.py`, `tests/test_rectangle_downloads.py`.

**Interfaces:**

- `SelectionTrace`: full ordered selected assets, primary/fallback layers, actual contributors; chỉ state cục bộ mỗi request.
- Thêm keyword-only optional trace vào private RAW helpers, giữ return tuple2; thêm optional `selection_recipe` cuối `SceneInfo`, không phá positional callers.
- `FrozenSelection`: token, canonical request/grid/bands/halo, image_graph/hash, recipe/version, provider scope, expires_at.
- `SelectionStore.create(selection)`, `get(token)`, `cleanup(now)`; DB `downloads/gee-downloads/state.sqlite3`.
- `freeze_selection(scene, export_parameters) -> FrozenSelection`:
  rebind full S2 asset IDs và recipe bằng helper compose RAW dùng chung;
  `restore_selection(snapshot) -> ee.Image` từ graph server, không rerank.
- `/api/scene`: thêm optional selection_token. `/api/download/{dataset}`:
  thêm optional token, kiểm request canonical khớp snapshot; token hợp lệ bỏ best_scene,
  token không hợp lệ trả409/410 rõ ràng; URL không token giữ hành vi hiện tại.

- [ ] RED:16 selected / 8 contributors được giữ đủ; round-trip priority; None/[] bands;
  đổi form/vector sau search không đổi snapshot; hết hạn/version/account mismatch;
  token-bound download không gọi best_scene; graph/pixel label fix vẫn được giữ.
- [ ] RED: thêm/reorder catalog sau search không đổi single scene; frozen graph chỉ
  có direct asset loads, không còn collection positional selector; preview dùng
  cùng graph bound với download; RAW compose helper không đổi mask/pixel/source order.
- [x] Implement trace và persist; serialize graph không gọi thêm provider metadata.
  Graph cùng recipe ghi nguyên tử; DB không trỏ graph chưa tồn tại.
- [x] Cache fingerprint đầy đủ theo spec; không dùng tên file/scene ID riêng làm khóa.
  Dùng selection_hash/tile_key/artifact_key đúng §4.5. Pin snapshot theo attempt/job,
  không xóa graph đang được dùng chỉ vì search TTL đã hết.
- [ ] GREEN + toàn suite; live cùng snapshot preview/download, đủ nguồn halo và đúng bands.

## Task 2 — Lấy URL lazy, tải stream và checkpoint từng ô

**Files:** tạo `app/download_transport.py`, `app/export_resources.py`,
`tests/test_download_transport.py`, `tests/test_export_resources.py`;
sửa `app/imagery.py`, `app/main.py`, `app/download_store.py`, `app/frame_export.py`,
`app/timeseries.py`, `app/timeseries_transport.py` ở outer export/admission cần thiết.
Không đổi `_RPC` shared state hoặc chặn preview bằng lock bulk export.

**Interfaces:**

- `TileSpec`: tile_key, selection_hash, band names/order, requested region,
  CRS/scale, layout version và validation constraints; không chứa URL lâu dài.
- `plan_tiles(snapshot, settings) -> list[TileSpec]`: không RPC; dùng legacy tile geometry/layout ở bản đầu.
- `stream_tile_to_file(image, tile, destination, *, deadline, check, metrics) -> TileReceipt`.
- `DownloadRPC(credentials_factory, project, ...)`: constructor không mạng;
  `prepare_tile(image, params, *, deadline, check) -> resource_name` và
  `stream_pixels(resource_name, destination, *, deadline, check)` dùng REST cô lập,
  có bounded OAuth, không dùng SDK getDownloadURL/hidden retries.
- `TileReceipt`: filename, SHA256, size, actual CRS/transform/shape/bands/dtypes/nodata.
- Store: `get_verified_tile(tile_key)`, `save_tile(receipt)`, `invalidate_tile(tile_key)`.
- `claim_attempt(artifact_key)`, `commit_tile(attempt, receipt)`, `release_attempt(...)`
  trong SQLite; khóa fingerprint/CAS có từ direct GET, không chờ Task4.
- `ExportResources.raster_admission(check, wait_deadline=None)` FIFO một bulk raster;
  deadline xử lý bắt đầu sau admission; tối đa6transfer và một writer trong raster.
  Không acquire lồng permit đã sở hữu; preview/metadata không lấy admission này.

- [ ] RED: kế hoạch ô không gọi URL; URL chỉ tạo khi worker bắt đầu; chunked GET không
  dùng resp.content; giả lập ngắt/socket timeout/429/403 thật/HTML200/TIFF hỏng;
  nhận cả TIFF/BigTIFF; wrong dtype/grid bị từ chối; valid zero/mask giữ nguyên.
- [ ] RED: resume bỏ qua ô đúng hash, tải lại ô hỏng; đổi bands/grid/version không reuse;
  delayed future không làm dừng các worker khác; tổng in-flight/future backlog đúng cap.
- [ ] RED: hai direct GET cùng fingerprint chỉ một attempt ghi; stale worker không
  overwrite receipt mới; URL creation/OAuth bị treo vẫn hết ngân sách; SDK network
  methods bị cấm trong worker; không có retry ẩn vượt controller.
- [ ] RED: đổi compression tạo artifact_key mới nhưng giữ tile_key; đổi source/grid/
  bands/wire layout không được dùng receipt cũ; cleanup không xóa snapshot đang pin.
- [ ] RED: annual/frame chờ admission không mất ngân sách processing trước khi chạy;
  caller có deadline sẵn không được kéo dài; cancel lúc queue; preview không chờ bulk.
- [x] Implement Session theo worker, bounded producer, `.part`→verify→rename→DB;
  xác minh decode block TIFF theo bộ nhớ giới hạn và không để đường dẫn client điều khiển file.
- [x] Giữ `.part` theo attempt, receipt content-addressed bất biến và commit CAS;
  request trùng đang chạy trả409 hoặc artifact đã kiểm tra như spec.
- [x] Tách local writer thành `merge_local_band_tiffs(receipts, out, ...)` từ phần
  sau download của helper cũ, giữ hành vi. Task2 dùng writer này với receipt để
  chưa cần VRT/job UI. Wrapper cũ giữ contract; fast path được chọn riêng qua cờ.
- [ ] Một retry/deadline theo spec; đóng response trước khi retry; không xếp chồng
  retry của `_fetch_with_retry` và `_download_to` cũ; giữ wrapper tương thích nơi cần.
- [ ] POST thumbnail resource/GET pixel/OAuth dùng DownloadRPC và remaining deadline,
  provider readiness là stage riêng. Tận dụng serializer/request shape của `_RPC`
  nhưng không dùng bản trả bytes nguyên tile, không monkeypatch SDK global Session.
- [ ] Tránh eager probe/discard mọi ô. Nếu lỗi layout, kết thúc attempt rõ ràng;
  retile tạo layout/fingerprint mới và không trộn ô hai lưới trong cùng output.
- [ ] Bọc outer legacy/direct/annual/frame export trước timer xử lý bằng cùng
  admission, nhả giữa các scene; không đổi admission DB/sequence của annual jobs
  hoặc deadline hiện có. Với caller đã có deadline, queue wait dùng remaining.
- [ ] GREEN + suite; benchmark A/B URL calls/time-to-first-tile/bytes/RSS.
  Checkpoint bị xóa chỉ theo retention/cancel, không `rmtree` mọi ô đã kiểm tra khi một ô lỗi.

## Task 3 — Ghi/nén TIFF đích một lần

**Files:** tạo `app/raster_mosaic.py`, `tests/test_download_mosaic.py`;
sửa `app/imagery.py`, `app/raster_grid.py`, `app/main.py`, `tests/test_large_rasters.py`.

**Interfaces:** chuyển helper placements/read_window sang module chung và giữ alias cũ
để không phá caller; không đổi thuật toán khi di chuyển.
`build_transport_vrt(receipts, directory) -> Path`;
`assemble_to_grid(receipts, output, grid, *, categorical_flags, check, metrics)`.

- [ ] RED: parity với legacy trên cùng bytes, gồm fractional origins/resolutions,
  overlap ưu tiên nguồn đầu, nan/-9999/valid0, QA missing, affine xoay, halo và band tags.
  So mask tuyệt đối, Float32 valid bitwise, không chỉ nhìn RGB.
- [x] Implement VRT với lưới placements chuẩn, thứ tự nguồn đảo đúng cho GDAL last-wins,
  mapping NoData và per-band masks chính xác; dùng thư viện hiện có.
- [ ] Warp từng band/block bằng helper hiện tại, giữ cả bilinear values và nearest
  validity. Writer1, output BigTIFF/interleave band/atomic; chưa đổi compression ở task này.
- [ ] Dùng đường VRT khi output_grid tồn tại và parity pass; các path khác giữ writer cũ.
  Không tạo full-size `.source.tif` trong fast path; không có full-array allocation.
- [ ] GREEN + suite; local5lượt cùng fixture đo CPU/IO/RSS và số lần nén full raster.
  Không đạt parity thì tắt fast path, report gate chưa đạt; không nới tolerance.

## Task 4 — Job nền, progress, resume và artifact ổn định

**Files:** tạo `app/download_jobs.py`, `app/download_api.py`, `static/download-jobs.js`,
`tests/test_download_jobs.py`, `tests/test_download_api.py`, `tests/test_download_jobs_ui.cjs`;
sửa `app/download_store.py`, `app/main.py`, `static/index.html`, `static/workspace.js`,
`static/workspace.css` và tests wiring tương ứng.

**Interfaces:**

- `DownloadService.start(selection_token) -> job`, `pause(job_id)`, `resume(job_id)`,
  `cancel(job_id)`, `recover()`, `shutdown()`; SQLite CAS attempt counter.
- `POST /api/download-jobs` body `{selection_token}`; `GET /api/download-jobs/{id}`;
  POST pause/resume/cancel; `GET /api/download-jobs/{id}/file`.
- Status: state/stage, tile counts, received/verified bytes, blocks, error code,
  expires_at, artifact metadata/file URL khi completed; không trả graph/provider URL.
- Khi cờ mới bật, UI tạo job từ kết quả search; link GET cũ vẫn hoạt động.
  Không tự fallback lỗi job sang legacy và tạo thêm lượt tải nặng.

- [ ] RED: job POST trả nhanh không search/provider; dedupe fingerprint; đổi form không
  đổi job; pause-resume nhanh/restart không tải lại tile hợp lệ; worker cũ không publish.
- [ ] RED: crash trước/sau rename nhưng trước DB completed; disk full; partial không
  được tải; GET file/Range206 không gọi GEE; progress không complete sớm.
- [ ] RED: cleanup TTL/quota khi đang GET/Range giữ file lease đến hết response;
  HEAD không tải pixel; Range offset trên4 GiB đọc đúng bytes mà không gom full file.
- [x] Implement worker một job mới đang xử lý, queue bounded/durable và check cancellation;
  resume assembly từ verified inputs, không reopen TIFF dang dở để nối mù.
- [ ] Tích hợp lifespan recover/cleanup/pause tại ranh giới. Không đổi thứ tự các job
  annual/matched-frame hiện có; kiểm limiter chung bằng test cross-workflow.
- [x] UI poll stage, restore job ID sau reload, xử lý expiry/transient polling;
  che chi tiết kỹ thuật theo spec. Browser runtime test trước khi nghiệm thu UI.
- [ ] GREEN Python/Node + real browser; xác minh Range qua test HTTP thực, không suy từ tên FileResponse.

## Task 5 — Tuning có đo, resource và disk budget

**Files:** sửa `app/download_models.py`, `app/export_resources.py`, `app/raster_grid.py`,
`tools/benchmark_gee_download.py`; tạo/sửa `tests/test_download_profiles.py`.

**Interfaces:** `DownloadSettings`: workers6, backlog12, writer1, compression_threads2,
gdal_cache_bytes256 MiB, warp_mem_limit_mb64, retry/retention/disk settings theo spec.
Không đổi default dùng chung của mọi raster trước khi profile đã kiểm chứng.

- [ ] RED: cap tổng worker khi nhiều job; writer1; cancel lúc chờ permit;
  sai đơn vị GDAL cache; thiếu disk reserve; cleanup không xóa active/ngoài owned root.
- [ ] Benchmark một biến mỗi lượt: workers4/6/8; compression2/4threads;
  DEFLATE level1/3/6 và predictor3; ZSTD chỉ khi driver hỗ trợ và parity pass.
- [ ] Giữ layout8M hiện tại mặc định. Thử4/6/8M hoặc multi-band thành profile khác,
  cap32 MiB tổng bands×pixels×bytes; không dùng kích thước giảm riêng mỗi band để bỏ cap tổng.
- [ ] Chọn profile từ timing/RSS/CPU/disk/output-size thực; giảm worker khi429 hoặc
  memory pressure nếu thử nghiệm adaptive được gate. Không tự tăng vượt8.
- [ ] GREEN + suite và kiểm cache key bao gồm writer/layout profile.
  Không đạt mục tiêu benchmark thì giữ profile cũ; giải thích pha nào còn chi phối.

## Task 6 — Nghiệm thu tích hợp và bật theo đợt

**Files:** `tools/benchmark_gee_download.py`, `tests/live_raw_seam.py`, test modules ở trên,
`README.md`; ghi evidence vào `downloads/download-performance/`.

- [ ] Chạy `.venv\Scripts\python.exe -m unittest discover tests` và các Node tests liên quan;
  kiểm `tests.test_large_rasters`, `tests.test_raw_roi_mosaic`, `tests.test_rotated_exports`,
  `tests.test_s2_auxiliary`, time-series/frame regression. Không báo GREEN nếu chỉ chạy test mới.
- [ ] Chạy live synthetic fractional-mask/valid0/QA; PNG đúng ROI trước304→sau0 phải giữ0.
- [ ] Full live ROI người dùng12 bands,26 bands và rotated; kiểm toàn grid/masks,
  sample spectral/cloud/QA và band order, lưu selected recipe/checksums/timing.
- [x] Local output vật lý>4 GiB, last block mọi band, bounded RSS và crash recovery;
  dùng fixture có entropy đủ để file nén thực sự vượt4 GiB.
- [ ] Report median cold/warm, queue/selection/network/local/publish/delivery riêng;
  ít nhất3 live và5 local như spec. Nếu chi phí/env không cho đủ thì đánh dấu gate mở.
- [ ] Review độc lập thay đổi; chỉ bật default khi correctness/resources/performance
  gates đạt. Cờ riêng cho snapshot transport, VRT writer và job UI để rollback từng phần.
- [ ] Rollback về writer/transport cũ không xóa job/cache/checkpoint đang chạy.
  Recipe/profile version cũ không dùng chung với version mới; không rerank âm thầm.
- [x] README mô tả đã kiểm gì, giới hạn provider và mức tiếp tục theo ô; không hứa
  tốc độ cố định/không giới hạn file và không gọi một local synthetic là tải GEE thật.

## Handoff và thứ tự thực hiện

0 →1 →2 →3 →4 →5 →6. Sau Task2 có thể cung cấp cải thiện bước đầu qua GET cũ;
Task3 là gate tối ưu CPU/đĩa; Task4 hoàn thiện khả năng tải dài/resume cho người dùng.
Checkbox chỉ được đánh dấu khi toàn bộ điều kiện của bước đã được kiểm chứng.
Source đã có một phần; các gate live/performance/large-file còn mở được ghi trong ledger.
