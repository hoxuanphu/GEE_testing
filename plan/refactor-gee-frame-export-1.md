---
goal: Khắc phục xuất GeoTIFF GEE với CRS tùy chỉnh mà vẫn giữ chính xác lưới ảnh mẫu
version: '1.0'
date_created: '2026-10-23'
last_updated: '2026-10-23'
owner: GEE_testing
status: 'Implemented; original-scene acceptance blocked'
tags: [refactor, bug, gee, raster, projection]
---

# Introduction

> Theo dõi bản sửa/kiểm tra tiếp theo tại
> [kế hoạch 2026-10-25](../docs/superpowers/plans/2026-10-25-matched-frame-repair.md)
> và [báo cáo thực hiện](../docs/superpowers/plans/2026-10-25-matched-frame-implementation.md).
> Trạng thái lịch sử dưới đây được giữ để đối chiếu; không dùng làm kết quả nghiệm thu mới.

![Status: Implemented](https://img.shields.io/badge/status-Implemented-orange)

Job b632c8a3-eb7f-472a-a583-ea56278bc3b0 thất bại khi lấy thumbnail GeoTIFF: GEE trả HTTP 400 INVALID_ARGUMENT, thông báo `Unable to write GeoTIFFs in projection`. Tính toán GEE và đọc VV/VH cùng cảnh vẫn thành công; xuất thử vùng nhỏ trong EPSG:4326 thành công. CRS đích là Equirectangular cầu tùy chỉnh do ứng dụng tạo. Lỗi không chứng minh tài khoản thiếu quyền. Đã triển khai mã nguồn và nghiệm thu CLI; kết quả thực tế và giới hạn được ghi tại mục 9.

## 1. Requirements & Constraints

- **REQ-001**: Giữ nguyên CRS tương đương ngữ nghĩa, sáu hệ số affine, width, height, thứ tự band và chế độ match_grid/match_extent đã xác nhận. Không thay nhãn CRS để giả chuyển chiếu.
- **REQ-002**: Giữ đường xuất trực tiếp cho CRS được GEE chấp nhận; khi GEE từ chối ghi projection, tải qua CRS chuẩn rồi warp cục bộ về lưới đích.
- **REQ-003**: Sentinel-1 giữ Float32 dB, nearest, không log/clamp; Sentinel-2 scale 0.0001 đúng một lần. Không thực hiện bilinear ở cả hai bước trung gian và đích.
- **REQ-004**: Mask hợp lệ từng band phải tách khỏi giá trị, warp nearest và lấy giao sau khi về lưới đích; zero hợp lệ không phải NoData. Giá trị NoData cuối là NaN.
- **REQ-005**: Phân biệt lỗi projection, tham số, xác thực, quyền, quota, timeout; UI không coi mọi HTTP 4xx là lỗi quyền.
- **REQ-006**: Chỉ công bố TIFF sau validate_frame_export; báo coverage thực, all_nodata và require_full_coverage theo hợp đồng hiện hành.
- **SEC-001**: Không đưa token, credentials, signed URL, key, đường dẫn nhạy cảm hoặc nguyên body upstream vào UI/log. Chỉ lưu chẩn đoán đã allowlist và giới hạn độ dài.
- **CON-001**: Tái sử dụng rasterio, numpy, affine, ee, requests hiện có; không thêm thư viện chỉ để triển khai warp.
- **CON-002**: Giữ giới hạn tile 2048 pixel/cạnh và 16 MiB cho mỗi phản hồi giá trị; tính thêm mask, halo, bộ nhớ warp và đĩa tạm khi lập kế hoạch. Không đọc toàn cảnh vào RAM.
- **CON-003**: Không thay đổi global EE transport/deadline, không sửa dữ liệu job/snapshot cũ và không ghi đè thay đổi không liên quan đang có trong working tree.
- **PAT-001**: Một kế hoạch lưới trung gian cố định cho toàn cảnh; các cửa sổ đích đọc dữ liệu từ cùng mosaic trung gian để tránh đường nối tile.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Phân loại đúng lỗi, giữ đường trực tiếp và bật fallback chỉ với lỗi projection có bằng chứng. Hoàn tất khi test phân loại tất cả mã HTTP bên dưới đạt.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Trong app/frame_search.py, bổ sung HTTP status và reason nội bộ vào FrameUpstreamError, giữ tương thích constructor hiện tại. Trong _RPC.request_once, đọc error JSON có giới hạn, phân loại 400 với thông báo Unable to write GeoTIFFs in projection thành unsupported_export_projection; 400 khác thành invalid_upstream_request; 401 thành upstream_unauthenticated; 403 thành upstream_forbidden. Giữ retry giới hạn cho 408/429/5xx và lỗi mạng hiện tại; không retry 400/401/403. Không công bố nguyên thông báo Google. | Yes (section 9 adjustments) | 2026-10-23 |
| TASK-002 | Trong app/frame_export.py, tách primitive tải tile trực tiếp khỏi điều phối fallback; giữ fetch_grid_tile tương thích. Trong export_frame_scene, thử trực tiếp một lần với lưới đích, chuyển toàn cảnh sang staging trung gian khi bắt đúng unsupported_export_projection; xóa/reset staging đích trước khi ghi lại. Không fallback cho lỗi IAM, quota, asset, tham số chung hoặc dữ liệu rỗng. Cache quyết định chỉ trong một lần export. Phụ thuộc TASK-001. | Yes (section 9 adjustments) | 2026-10-23 |

### Implementation Phase 2

- **GOAL-002**: Tải và warp cục bộ có giới hạn tài nguyên, giữ hình học và mask. Phụ thuộc Phase 1; hoàn tất khi fixture CRS cầu tùy chỉnh và fixture lưới xoay đều qua kiểm tra lưới, giá trị và mask.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-003 | Tạo app/frame_projection.py với plan_transport_grid(grid, dataset, bands). Densify bốn cạnh footprint bằng 64 đoạn/cạnh rồi chuyển về WGS84; chọn EPSG UTM từ tâm nếu footprint nằm trong một vùng UTM và vĩ độ [-80,84], ngược lại EPSG:4326. Xử lý kinh tuyến 180 bằng tách footprint; không dùng bbox gần toàn cầu. Dùng rasterio.warp.calculate_default_transform để suy ra scale từ lưới đích, không chọn pixel thô hơn scale đích tại tâm. Snap bounds ra ngoài theo lưới chung, thêm halo 2 pixel; kiểm tra số hữu hạn, kích thước, số tile, bộ nhớ và đĩa. Nếu không xác định phép đổi datum đáng tin cậy, trả unsupported_reference_crs trước tải, không tự gán datum WGS84. | Yes (section 9 adjustments) | 2026-10-23 |
| TASK-004 | Trong app/frame_projection.py, thêm export_via_transport_grid và gọi từ fallback của export_frame_scene. Tải giá trị Float32 cùng mask từng band trên lưới trung gian cố định, dùng nearest ở GEE để tránh bilinear hai lần. Ghi TIFF tạm theo cửa sổ và sentinel wire hiện tại; không suy mask từ giá trị zero. S1 warp giá trị nearest; S2 warp giá trị bilinear với src_nodata/dst_nodata rõ ràng. Warp mask từng band nearest rồi giao tại đích; dùng WarpedVRT hoặc rasterio.warp.reproject theo cửa sổ, warp_mem_limit=64 MiB, num_threads=1. Mọi cửa sổ đích lấy nguồn từ cùng mosaic có halo, không warp từng tile nguồn độc lập. Phụ thuộc TASK-003. | Yes (section 9 adjustments) | 2026-10-23 |
| TASK-005 | Trong app/frame_export.py, mở lại TIFF fallback và chạy validate_frame_export trước atomic replace hiện hành; bảo toàn band descriptions, NaN, metadata, coverage. Ghi export_strategy, transport_crs, transport_resolution, value_resampling_steps và projection_fallback_reason trong tags/report/manifest bằng trường có version; giữ PROCESSING hiện hành tương thích hoặc nâng schema cùng validator. Dọn TIFF trung gian, mask, staging và sidecar của riêng lần export trong finally khi thành công hoặc lỗi. Phụ thuộc TASK-004. | Yes (section 9 adjustments) | 2026-10-23 |

### Implementation Phase 3

- **GOAL-003**: Kiểm tra trước tải, công bố đúng trạng thái và phục hồi vận hành. Phụ thuộc Phase 2; hoàn tất khi preflight không cho chạy yêu cầu vượt tài nguyên và job lỗi vẫn có thể tạo lại mà không sửa lịch sử.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | Trong FrameService.preflight tại app/frame_jobs.py, bổ sung ước lượng worst-case staging trung gian gồm mask, halo, output và ZIP; kiểm tra giới hạn đĩa bằng _space. Không thực hiện tải cả cảnh trong preflight. Với CRS không có EPSG tương đương chính xác, cảnh báo có thể dùng warp cục bộ và nội suy không đồng nghĩa đồng đăng ký. Lưu estimator_version trong snapshot; với preflight cũ tính lại reserve tại run_job trước tải mà không đổi snapshot. | Yes (section 9 adjustments) | 2026-10-23 |
| TASK-007 | Trong static/matched-frame.js, giữ render text an toàn và hiển thị thông báo theo mã lỗi mới nếu cần; không suy luận permission từ HTTP 502 của backend. Trong README.md, mô tả đường trực tiếp/fallback, ý nghĩa mask và cách tạo preflight/job mới cho snapshot cũ. Không tự restart tiến trình chưa xác định chủ sở hữu. | Yes (section 9 adjustments) | 2026-10-23 |

### Implementation Phase 4

- **GOAL-004**: Xác nhận bằng test offline và job thực tế trước khi đánh dấu hoàn tất. Phụ thuộc Phase 3.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | Bổ sung các test trong mục 6 vào tests/test_frame_search.py, tests/test_frame_export.py, tests/test_frame_jobs.py và tests/test_frame_projection.py. Chạy một lệnh .venv\\Scripts\\python.exe -m unittest tests.test_frame_search tests.test_frame_export tests.test_frame_jobs tests.test_frame_projection; nếu tests không là package dùng unittest discover với pattern test_frame*.py. Chạy test giao diện hiện hữu tests/test_matched_frame_ui.cjs bằng runner hiện có khi sửa JS. | Yes (section 9 adjustments) | 2026-10-23 |
| TASK-009 | Xác minh bản server đang chạy nạp mã mới bằng tiến trình/cấu hình hiện có; chỉ restart đúng tiến trình do phiên làm việc quản lý hoặc được người dùng cho phép. Tạo preflight/job mới từ snapshot lỗi còn hiệu lực, không sửa job cũ. Tải cảnh Sentinel-1 thực tế với CRS tùy chỉnh của job b632c8a3-eb7f-472a-a583-ea56278bc3b0; kiểm tra TIFF và manifest persisted, coverage, CRS/affine/kích thước đúng. Nếu snapshot hết hạn, tìm lại đúng asset và dùng cùng metadata tham chiếu. Kiểm tra thêm đường CRS EPSG trực tiếp. Không đánh dấu Completed khi chỉ có mock test hoặc khi không có quyền gọi GEE. | Partial / blocked (section 9) | 2026-10-23 |

## 3. Alternatives

- **ALT-001**: Chỉ sửa thông báo lỗi: không giải quyết việc tải ảnh nên không đủ.
- **ALT-002**: Thay CRS của mẫu bằng EPSG gần nhất hoặc gán EPSG không warp: làm sai hình học nên loại bỏ.
- **ALT-003**: Luôn tải EPSG:4326 rồi warp: tăng chi phí, thay đổi đường đang hoạt động và có thể tăng sai số nội suy; chỉ dùng trong fallback ngoài vùng UTM phù hợp.
- **ALT-004**: Dùng computePixels định dạng NPY để tránh writer GeoTIFF: có thể giữ trực tiếp lưới đích và giảm nội suy, nhưng chưa được kiểm chứng với chính CRS này, cần đánh giá giới hạn response/mask riêng; không chọn làm đường production trong bản sửa đầu.

## 4. Dependencies

- **DEP-001**: Các dependency hiện có rasterio/GDAL/PROJ, numpy, affine, earthengine-api, requests phải thực hiện được phép đổi từ CRS cầu tùy chỉnh sang CRS trung gian và ngược lại.
- **DEP-002**: Tài khoản GEE hiện tại truy cập được scene và endpoint thumbnails; đã xác nhận ở chẩn đoán nhưng phải kiểm tra lại khi chạy nghiệm thu.
- **DEP-003**: Dung lượng và snapshot tham chiếu còn khả dụng; job cũ giữ nguyên trạng thái failed như lịch sử.

## 5. Files

- **FILE-001**: app/frame_search.py — lỗi upstream có cấu trúc và phân loại projection.
- **FILE-002**: app/frame_export.py — chọn chiến lược, validate, metadata và atomic publication.
- **FILE-003**: app/frame_projection.py — module mới lập lưới trung gian và warp mask-aware.
- **FILE-004**: app/frame_jobs.py — preflight, reserve, report và tương thích snapshot cũ.
- **FILE-005**: static/matched-frame.js — thông báo lỗi và cảnh báo fallback nếu cần.
- **FILE-006**: tests/test_frame_search.py, tests/test_frame_export.py, tests/test_frame_jobs.py, tests/test_frame_projection.py, tests/test_matched_frame_ui.cjs — test hồi quy.
- **FILE-007**: README.md — quy trình xuất, giới hạn và chạy lại job.

## 6. Testing

- **TEST-001**: Mock 400 projection, 400 tham số khác, 401, 403, 408, 429, 500 và body không JSON; assert code, thông báo an toàn, số retry và fallback chỉ đúng lỗi projection. Test body vượt giới hạn không bị đọc vô hạn.
- **TEST-002**: Fixture CRS Equirectangular cầu R=6378166.17539632, tâm lon=104.035720835673 lat=21.7423783969593; lưới affine [9.993915937710756,0,-8065.090161732581,0,-9.99956743608598,8094.649839511601], width=1614 height=1619. Assert output giữ đúng lưới và CRS tương đương bằng rasterio.CRS.
- **TEST-003**: Raster tổng hợp gồm pixel zero hợp lệ, số dB ngoài dải preview, mask khác nhau từng band và lỗ NoData. Assert không log/clamp, mask đích là giao các mask nearest, Float32/NaN đúng và S2 scale một lần.
- **TEST-004**: So sánh kết quả warp chia cửa sổ với warp toàn mosaic nhỏ làm oracle; S1 values bằng nhau, S2 allclose rtol=1e-5 atol=1e-6 trên pixel hợp lệ, mask bằng nhau. Kiểm tra seam tại biên tile, lưới xoay/shear và halo.
- **TEST-005**: Assert direct EPSG path không đổi; fallback không chạy cho 403/429/asset error; full coverage/all_nodata/partial giữ hành vi hiện tại. Validate metadata và manifest ghi đúng chiến lược thực tế.
- **TEST-006**: Giả lập lỗi mạng, hết đĩa, lỗi warp và validation fail; không có output công bố, không còn staging riêng của lần export, job ghi lỗi an toàn. Giới hạn tài nguyên thất bại trước cấp phát quá mức.
- **TEST-007**: Lưới geographic, khác múi UTM, gần cực, cắt kinh tuyến 180 và CRS không đổi datum được: hoặc xử lý đúng với bounded grid hoặc trả lỗi cụ thể trước tải; không bbox gần toàn cầu hoặc âm thầm dùng ballpark không kiểm soát.
- **TEST-008**: Nghiệm thu GEE live với scene gốc: job mới completed, TIFF đọc được, validate_frame_export verified=true, output lưới đúng và coverage thực đáp ứng require_full_coverage; artifact tồn tại sau khi kết thúc request. Ghi nhận riêng kiểm tra trực quan không thay thế kiểm tra số.

## 7. Risks & Assumptions

- **RISK-001**: CRS mẫu dùng sphere không phải ellipsoid WGS84; không thể mặc định mọi phép chuyển datum là chính xác. Cần kiểm tra phép biến đổi và round-trip điểm góc/tâm; round-trip không tự chứng minh đồng đăng ký tuyệt đối.
- **RISK-002**: Fallback có thêm lần lấy mẫu nearest trung gian; không hứa bảo toàn giá trị từng pixel so với xuất trực tiếp. Phải công bố chuỗi resampling và không bilinear hai lần.
- **RISK-003**: Mask nearest và bilinear giá trị có thể khác ở rìa; kiểm thử lỗ mask, biên ảnh và coverage thực bắt buộc.
- **RISK-004**: Cảnh lớn và ROI cắt kinh tuyến có thể vượt reserve; fail sớm, yêu cầu giảm vùng/độ phân giải thay vì cấp phát vô hạn.
- **ASSUMPTION-001**: User explicitly approved implementation on 2026-10-23 and authorized live GEE validation; restarting an unowned server is not authorized.

## 8. Related Specifications / Further Reading

- [Đặc tả tải ảnh đồng khung](feature-matched-frame-download-1.md)
- [Transport GEE hiện tại](../app/frame_search.py)
- [Exporter hiện tại](../app/frame_export.py)
- [Lưới tham chiếu hiện tại](../app/raster_grid.py)
- [Rasterio reprojection](https://rasterio.readthedocs.io/en/stable/topics/reproject.html)

## 9. Execution record — 2026-10-23

Implementation persisted in shared tree without commits; unrelated existing changes preserved.
TASK-001–008 implemented; TASK-009 remains partial because original-scene full-coverage
acceptance is impossible and the existing server is not owned by this session.

### Narrow technical adjustments and evidence

- Dateline-crossing footprints fail early with `unsupported_transport_extent` rather
  than splitting into mosaics. This avoids near-global allocation and ambiguous wrapping.
- Datum scope is deliberately conservative: authority WGS84 or explicit spherical EQC
  angular-coordinate inversion only. Other datums fail before download. Densified
  footprint round-trip is checked, but is not claimed to prove absolute registration.
- Intermediate band masks are downloaded separately (one band/request) using the exact
  representable wire sentinel, then written as byte masks. Common validity is computed
  only after nearest destination warp; values never infer validity from zero.
- Live testing exposed another tightly coupled bug: Float32 minimum used as an integral
  EE unmask constant overflowed to `-9223372036854775808`, which the old decoder accepted
  as finite valid data. Replaced with exact fractional Float32 `-999999.25`; decoder also
  recognizes legacy overflow and validator rejects unconverted sentinel. Initial false
  positive validation TIFF/ZIP files in this session's separate store were removed and
  their jobs invalidated. Historic original store was not modified.
- Verified original asset footprint: longitude 102.2447–104.7774, latitude
  -1.3853–0.6006; reference is centered at 104.0357, 21.7424. Original scene is **not**
  over the reference. GEE sampled VV/VH at that reference returns null. Replaying the
  original retained snapshot correctly fails `all_nodata`, not permission or success.
- Covering 2026-10-18 scene has ~95.4% actual coverage; strict coverage correctly rejects
  it. A newly searched 2026-10-11 scene covers the whole retained original grid.

### Validation and durable proof

- `.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_frame*.py'`:
  **56 tests passed** (HTTP classification/retry/body bounds, projection-only dispatch,
  windowed/full-mosaic warp oracle with different per-band masks, rotated/sheared grids,
  zero/dB/S2 values, cleanup, resource limits, old-snapshot reserve and existing behavior).
- `node --test tests\test_matched_frame_ui.cjs`: **26 tests passed**. Existing safe-text
  rendering and backend error messages already satisfy TASK-007; no JS change required.
- Changed Python files: no IDE errors; projection syntax check passes. `git diff --check`
  passes (only unrelated existing CRLF warning).
- Live runner: `.venv\Scripts\python.exe validate_frame_projection_live.py`.
- Original-snapshot replay: `bd728e9f-21ab-476c-8c09-d46ace183f24`, failed `all_nodata`.
- Positive fresh job: `8c5a689d-7a66-475e-8d5e-5f248ea30c9c`, completed, verified=true;
  actual VV/VH coverage **100%**, Float32/NaN, exact original 1614×1619 grid/CRS/affine.
  Asset: `COPERNICUS/S1_GRD/S1D_IW_GRDH_1SDV_20261011T111348_20261011T111413_004967_0095F5_9485`.
  Values vary naturally: VV -38.6329..10.6161, VH -52.3222..2.39294 dB.
- Durable directory: `downloads\projection_validation\jobs\8c5a689d-7a66-475e-8d5e-5f248ea30c9c`:
  `77eac622-5811-4f1f-a972-98a10594f662.tif` (16,385,819 bytes),
  `manifest.json`, `result.zip`. Summary: `downloads\projection_validation\proof.json`.
- Same covering asset standard EPSG direct probe: `downloads\projection_validation\direct_epsg.tif`,
  verified direct strategy. Original job JSON checked byte-for-byte unchanged through read-only SQLite.
- Existing uvicorn Python processes 3268/20356 predate these edits (13:34:15); not restarted.
  CLI loaded new code, but deployment/server reload remains for the server owner. No
  claim is made that the existing running API process has loaded the new implementation.

