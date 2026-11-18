---
goal: Tải Sentinel-2 SR và Sentinel-1 SAR đồng khung từ GeoTIFF mẫu trong Satellite Workspace
version: '1.0'
date_created: '2026-10-22'
last_updated: '2026-10-22'
owner: hoxuanphu
status: 'Completed'
tags: [feature, geotiff, matched-frame, gee, sentinel2, sentinel1, export]
---

# Introduction

> Trạng thái sửa lỗi và nghiệm thu tiếp theo được theo dõi tại
> [kế hoạch 2026-10-25](../docs/superpowers/plans/2026-10-25-matched-frame-repair.md)
> và [báo cáo thực hiện](../docs/superpowers/plans/2026-10-25-matched-frame-implementation.md).
> Các kết quả live cũ dưới đây không thay thế nghiệm thu của bản sửa mới.

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

Đã triển khai quy trình tải ảnh đồng khung: chọn GeoTIFF mẫu → xem footprint và metadata → chọn nguồn/khoảng ngày → so sánh chất lượng trong ROI → chọn tối đa 10 cảnh → kiểm tra lưới/dung lượng → tải nền và nhận GeoTIFF/ZIP kèm manifest. Các task trong kế hoạch đã hoàn tất; live smoke đã chạy với một cảnh Sentinel-2 SR và một cảnh Sentinel-1 SAR.

Phạm vi được người dùng xác nhận ngày 2026-10-22: Sentinel-2 SR GEE và Sentinel-1 SAR GEE; chọn GeoTIFF qua trình duyệt, backend đọc metadata, không nhận đường dẫn máy chủ tùy ý; mặc định đồng biên với pixel xấp xỉ 10 m; tùy chọn đồng lưới mẫu; tối đa 10 cảnh mỗi job và cảnh báo dung lượng trước tải.

Điểm tích hợp backend đã khảo sát: [grid_profile](../app/raster_grid.py#L65), [warp_to_grid](../app/raster_grid.py#L92), [_scale_s2](../app/imagery.py#L164), [download_geotiff](../app/imagery.py#L1313), [TimeSeriesStore](../app/timeseries.py#L82), [TimeSeriesService](../app/timeseries.py#L290), [lifespan](../app/main.py#L94) và [static mount](../app/main.py#L2692). UI đang chạy từ [static/index.html](../static/index.html), không từ frontend prototype; [setSelectionMode](../static/index.html#L503), [opticalSceneBody](../static/index.html#L1017), [btnSearch handler](../static/index.html#L1178) là các hook cần mở rộng. [static/workspace.js](../static/workspace.js) di chuyển DOM node vào region/results và theo dõi danh sách result IDs cố định, nên bắt buộc thêm matched-frame panel vào danh sách này. [tests/test_rectangle_ui.cjs](../tests/test_rectangle_ui.cjs) chạy Node test + vm + DOM/fetch fake. Số dòng là mốc khảo sát, tên symbol mới là điểm định vị khi triển khai.

## 1. Requirements & Constraints

- **REQ-001**: Chế độ vùng chọn mới `reference` nhận duy nhất một GeoTIFF `.tif`/`.tiff` qua upload. Backend đọc CRS, sáu hệ số affine theo thứ tự rasterio `(a,b,c,d,e,f)`, width, height, band descriptions, dtype và các tag nguồn trong allowlist. Lưu basename đã làm sạch, không lưu đường dẫn máy người dùng. Pixel ảnh mẫu không được dùng làm dữ liệu nguồn xuất hoặc gửi sang GEE.
- **REQ-002**: Footprint là bốn góc ngoài `T*(0,0)`, `T*(W,0)`, `T*(W,H)`, `T*(0,H)`, không phải bbox Bắc–Nam khi raster xoay/shear. Tạo polygon WGS84 bằng densify mỗi cạnh thành 32 đoạn rồi đổi CRS, dùng bbox chỉ để tìm ứng viên; hình học phân tích trong GEE giữ polygon và CRS nguồn. Phạm vi là toàn khung raster, không phải footprint alpha/NoData của ảnh mẫu.
- **REQ-003**: `grid_mode=match_extent` mặc định, `target_resolution_m=10` mặc định. Với CRS projected có đơn vị mét: `Wout=ceil(W*hypot(a,d)/r)`, `Hout=ceil(H*hypot(b,e)/r)`; `Tout=T*Affine.scale(W/Wout,H/Hout)`. Bảo toàn footprint, CRS và hướng affine, không nới mép để ép pixel vuông. Trả hai độ dài vector pixel thực, không gọi chúng là đúng 10 m. `r` thuộc {10,20,30,60}; nguồn SAR mặc định 10. Với CRS khác mét, từ chối chế độ này bằng lỗi có hướng dẫn chọn match_grid; không tự đổi CRS làm sai nghĩa đồng khung.
- **REQ-004**: `grid_mode=match_grid` sao chép CRS, affine, width/height của ảnh mẫu; không làm tròn tọa độ. Chấp nhận CRS georeferenced hợp lệ kể cả CRS địa lý; khi không có đơn vị mét chỉ hiển thị pixel theo đơn vị CRS. Cảnh báo nếu nội suy lên pixel nhỏ hơn độ phân giải nguồn. Không tuyên bố đồng khung là đồng đăng ký đối tượng hoặc tăng chi tiết ảnh.
- **REQ-005**: Chỉ tìm trong `COPERNICUS/S2_SR_HARMONIZED` và `COPERNICUS/S1_GRD`, khoảng ngày giao diện bao gồm cả ngày kết thúc; backend đổi thành end-exclusive đúng một lần. Giới hạn khoảng tìm 366 ngày. Trả danh sách cảnh, không tự thay cảnh khi tải; timestamp UTC và tên vệ tinh/tile/orbit luôn có trong snapshot nếu nguồn có trường đó. Không suy đoán ngày/orbit từ tên tệp mẫu; tag acquisition chỉ là gợi ý người dùng có thể xác nhận.
- **REQ-006**: S2 mặc định đủ 12 band theo thứ tự `B1,B2,B3,B4,B5,B6,B7,B8,B8A,B9,B11,B12`; hỗ trợ preset `rgb` = B4,B3,B2 và `rgbnir` = B2,B3,B4,B8. Xuất Float32, phản xạ DN × 0.0001, không clamp, không mask DN=0 theo giá trị; chỉ dùng validity mask gốc. Nội suy bilinear mặc định; metadata ghi độ phân giải gốc 10/20/60 m từng band. Kênh SCL không nằm trong 12 band phản xạ.
- **REQ-007**: S2 trả riêng `roi_cloud_pct` (SCL 8/9/10), `roi_shadow_pct` (3), `roi_cloud_shadow_pct`, `roi_snow_pct` (11), `scl_valid_pct`, `band_coverage_pct` và `tile_cloud_pct`. Denominator mây/bóng/tuyết là diện tích có SCL hợp lệ, loại 0/1 và mask; scl_valid_pct dùng toàn ROI kể cả phần ngoài footprint. band_coverage_pct dùng giao mask các band được chọn trên toàn ROI, giữ zero hợp lệ. Tính trên lưới nguồn SCL ở 20 m, maxPixels=10000000, bestEffort=false; coverage band ở 20 m và ghi rõ đây là ước lượng. Không đủ dữ liệu thì metric=null, không đổi thành 0. Không gọi 0% SCL là bảo đảm không sương/mây mỏng.
- **REQ-008**: Mặc định `require_full_coverage=true` chỉ cho chọn cảnh có hình học phủ ROI và band_coverage_pct ≥99.9%; nhãn là "phủ gần đủ theo ước lượng", không hứa 100% pixel. Có tùy chọn cho phép cảnh thiếu dữ liệu, mặc định tắt. S2 sort `least_cloud` theo tổng mây+bóng tăng dần, coverage giảm dần, khoảng cách tới reference_date tăng dần nếu có, timestamp tăng dần, asset_id tăng dần; `nearest_date` cần reference_date và ưu tiên khoảng cách ngày trước mây/coverage. Cho chọn rõ ID khi hai vệ tinh cùng ngày.
- **REQ-009**: S1 hỗ trợ `vvvh`, `vv`, `hhhv`, `hh`, chỉ IW. Cho lọc hướng bay, relative orbit 1–175 và platform; mặc định không áp ràng buộc orbit nếu không biết. `prefer_reference_orbit` chỉ hoạt động khi có pass+relative orbit được người dùng xác nhận hoặc đọc từ tag allowlist và xác nhận; gắn cờ match và xếp matching trước ngày gần nhất, không loại cảnh khác một cách im lặng. S1 xuất native dB Float32, nearest mặc định, không log10 và không clamp. Cloud metric=null. Coverage mask chung trên toàn ROI ở 100 m, ghi scale và tính chất ước lượng. Không tự mosaic/ghép khác ngày hoặc khác track trong v1.
- **REQ-010**: Search snapshot bất biến chứa metadata mẫu, output_grid, nguồn, band order, xử lý, quality policy, asset IDs và kết quả xếp hạng. Job chọn 1–10 scene IDs từ snapshot; không nhận asset_id/affine/CRS tùy ý ở endpoint tạo job. `preflight` trả số pixel, byte chưa nén, byte disk reserve, cảnh báo nội suy/coverage/mây và effective grid; tạo job yêu cầu xác nhận đúng digest preflight.
- **REQ-011**: Mỗi scene thành một GeoTIFF; job có trạng thái queued/running/completed/partial/failed/interrupted, đếm processed/succeeded/failed và giai đoạn hiện tại. Tiếp tục cảnh khác khi một scene lỗi; disk full dừng an toàn. Cung cấp tải từng TIFF đã hoàn thành, ZIP chứa các TIFF thành công và manifest.json kể cả job partial; không cung cấp kết quả chưa kiểm tra. Tên hiển thị gồm dataset, ngày UTC, short scene hash và grid_mode; đường dẫn lưu thực dùng UUID ngắn để tránh MAX_PATH Windows.
- **REQ-012**: Trước công bố TIFF, mở lại kiểm tra CRS tương đương, affine, width/height, thứ tự band, dtype, metadata, NaN/mask và coverage thực từng band. Affine sai số tuyệt đối ≤1e-8 theo đơn vị CRS; match_grid còn giữ nguyên các hệ số yêu cầu khi ghi. Manifest ghi cả coverage ước lượng trước tải và coverage thực sau tải. Cảnh thiếu thật phải giữ NoData, không nội suy lấp lỗ; scene toàn NoData thất bại. Nếu require_full_coverage và coverage thực band bất kỳ <99.9%, đánh dấu scene thất bại với lý do coverage và giữ báo cáo, không công bố TIFF thành công.
- **REQ-013**: Preview S2 là RGB B4/B3/B2, stretch cố định phản xạ 0–0.3, tối đa 512 px cạnh dài; preview S1 là VV hoặc HH với dải hiển thị -25 đến 0 dB. Ghi rõ preview chỉ đổi hiển thị, không sửa TIFF. Cảnh báo pixel gốc 20–60 m, SCL không đảm bảo chất lượng nhìn và đồng lưới không phải đồng đăng ký.
- **SEC-001**: Upload tối đa 256 MiB/file; middleware ASGI áp raw body limit 257 MiB (1 MiB dành multipart overhead) trước multipart parser, đếm từng chunk kể cả không có Content-Length. Parser chỉ nhận một file, giới hạn số field; copy file theo block 1 MiB và kiểm lại file bytes ≤256 MiB, không đọc toàn tệp vào RAM. Không chỉ kiểm sau khi UploadFile đã spool xong. Dùng tên tạm UUID và rasterio driver GTiff, tắt PAM/đọc sidecar ngoài tệp, không mở VRT, URL, UNC hay GDAL virtual paths do client cung cấp. Xóa cả spool và ảnh mẫu upload sau trích metadata trong finally; không giữ pixel lâu dài. Tên file/tag là dữ liệu không tin cậy, không dùng làm đường dẫn hoặc innerHTML. Trả 413 cho vượt byte, 415 cho không phải GTiff, 422 cho CRS/affine không hợp lệ.
- **SEC-002**: Chỉ giữ tag allowlist `ACQUISITION_DATE,ACQUISITION_TIME_UTC,SOURCE_COLLECTION,SOURCE_SCENE,ORBIT_PASS,RELATIVE_ORBIT,PLATFORM`; mỗi giá trị tối đa 256 ký tự, validate kiểu/range trước dùng; không hiển thị tag tùy ý. Từ chối affine singular/non-finite, raster chỉ có GCP/RPC thiếu affine tin cậy, footprint qua đường đổi ngày hoặc tọa độ không đổi được sang miền WGS84. Không mở tệp mà người dùng chưa upload.
- **SEC-003**: Chỉ UUID hợp lệ và scene ID thuộc snapshot được truy xuất. Không trả/persist signed GEE URL, credentials, nội dung exception hoặc đường dẫn tuyệt đối máy chủ; lỗi có code và thông báo tiếng Việt. Preview đi qua endpoint server, không trả URL ký cho client. Xác thực Origin cho POST theo cùng origin ứng dụng, không dùng CORS wildcard của route cũ làm quyền cho các route upload/job mới. Giữ deployment localhost, không tuyên bố đây là hệ thống đa người dùng đã có auth.
- **CON-001**: Không thay hành vi API cũ. `download_geotiff` hiện xuất S2 sáu band qua EPSG:4326; `_s1_collection` và `_process_timeseries_image` clamp -30..10 dB. Không đi qua các nhánh đó cho matched-frame mới; không sửa mặc định legacy để phục vụ tính năng này. Tái sử dụng retry/URL refresh và rasterio/numpy, nhưng xuất thẳng grid đích, không resample qua một raster WGS84 trung gian.
- **CON-002**: Hằng số v1: max_dimension=30000; max_output_pixels=100000000 mỗi cảnh; max_scene_count=10; max_uncompressed_job_bytes=4 GiB; min_free_disk=512 MiB; disk_reserve=max(512 MiB, 4× tổng byte chưa nén); tile_bytes_limit=16 MiB; tile_dimension_limit=2048. Kiểm tra trước tạo job và trước mỗi scene/ZIP; từ chối vượt giới hạn, không tự giảm độ phân giải. Tối đa 500 ứng viên tìm; lấy 501 để phát hiện overflow, trả lỗi hẹp khoảng ngày, không xếp hạng trên kết quả bị cắt âm thầm.
- **CON-003**: Một matched-frame worker cho export, queued tối đa một job; job khác đang active trả 409; search riêng dùng semaphore một request, request khác trả 409. GEE RPC/network timeout mỗi lần gọi là 120 s cho search và 180 s mỗi tile; toàn search có deadline 300 s. Tối đa ba lần thử tổng cộng với backoff 1 s rồi 2 s cho lỗi tạm thời, không retry lỗi validation/quyền vĩnh viễn. Cấu hình transport dành riêng module, không đổi timeout GEE global của luồng cũ; hết deadline chặn xử lý kết quả đến muộn và chỉ nhả semaphore khi worker request đã kết thúc, không coi asyncio timeout là hủy thread. TTL metadata/search/job hoàn thành là 24 giờ không hoạt động, cleanup mỗi giờ; active jobs được bảo vệ. Restart đánh dấu active jobs interrupted, không tự chạy lại hoặc đổi scene.
- **CON-004**: Chỉ thêm `python-multipart>=0.0.20` vào requirements.txt để dùng UploadFile/Form; các thư viện địa lý đã có. Chỉ cài sau thay manifest, không cài React/geotiff.js hoặc broker mới. Dùng unittest + TestClient và Node test runner đang có. Không hard-code các tệp riêng của người dùng vào code hay test.
- **PAT-001**: Thêm các module matched-frame riêng, đăng ký router trước StaticFiles mount; giữ shared grid dictionary theo `crs,transform,width,height`, thêm metadata chứ không đổi các hàm build grid cũ. Store mới dùng SQLite/atomic file theo mẫu TimeSeriesStore; không kế thừa schema time-series vì còn search subsets/preflight/per-scene paths khác hợp đồng.
- **GUD-001**: UI tiếng Việt, cùng theme hiện tại; label rõ, errors cạnh trường và error summary focus được, status aria-live=polite, không chỉ dùng màu. Chọn file bằng bàn phím, không ép drag-drop; không tự fit map mỗi khi chỉnh control; stale upload/search responses không được ghi đè state mới. Đổi grid/source/bands/ROI vô hiệu hóa selection và preflight chưa chạy, không đổi job đang chạy.

## 2. Implementation Steps

Các TASK đã được thực thi theo quan hệ phụ thuộc; các task độc lập được triển khai song song. Mỗi GOAL đã đạt điều kiện nghiệm thu tương ứng. Các file source/test/docs đã được thay đổi trong worktree hiện tại; chưa commit để bảo toàn các thay đổi chưa commit có sẵn.

### Implementation Phase 1

- **GOAL-001**: Hợp đồng metadata/lưới/upload được kiểm chứng offline; footprint giữ đúng cả raster xoay. Không phụ thuộc phase khác.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Thêm `app/frame_models.py`: Pydantic frozen models `ReferenceFrame`, `FrameGrid`, `FrameSearchRequest`, `FrameScene`, `FramePreflightRequest`, `FrameJobRequest`; enum nguồn/grid/sort/band preset; hằng số và validator theo REQ-001–REQ-010, SEC-002, CON-002. Cấu trúc response có metric nullable, unit/denominator/scale của quality; affine 6 phần tử, row/column integer dương; forbid extra fields, dùng date thật. Không dùng lại SceneRequest vì reference không cần lon/lat/radius. | ✅ | 2026-10-22 |
| TASK-002 | Phụ thuộc TASK-001. Thêm `app/reference_frame.py` với `read_reference_metadata(path, display_name)`, `reference_footprint(frame)`; đọc header không đọc pixel, lấy bốn góc và densify 32 đoạn/cạnh; validate GTiff/CRS/affine/footprint, sanitize allowlist tag. Thêm `build_reference_grid(frame, mode, resolution_m)` trong `app/raster_grid.py`, theo REQ-003/REQ-004, không gọi `_build_rotated_grid` để tránh tái dựng sai CRS. | ✅ | 2026-10-22 |
| TASK-003 | Phụ thuộc TASK-001. Thêm `app/frame_store.py`: `FrameStore` SQLite tables references(id,metadata,created,activity), searches(id,reference_id,parameters,scenes,created,activity), preflights(id,search_id,scene_ids,digest,estimate,created,activity), jobs(id,preflight_id,status,counts,stage,created,activity), job_scenes(job_id,scene_id,status,report,file_token). JSON serialization không chứa URL/secret/nonfinite. Methods `save_reference`, `get_reference`, `save_search`, `get_search`, `save_preflight`, `create_job`, `get_job`, `update_scene`, `result_path`, `recover`, `cleanup`; job claim bằng BEGIN IMMEDIATE; snapshot nhúng đủ frame/grid để không phụ thuộc tệp upload đã xóa. Trạng thái và TTL theo CON-003. | ✅ | 2026-10-22 |
| TASK-004 | Phụ thuộc TASK-002/TASK-003. Thêm `app/frame_api.py` với `register_frame_routes(app, service)` và `FrameUploadLimitMiddleware` chỉ áp đường POST reference, enforce raw-body 257 MiB trước multipart parser và một file 256 MiB theo SEC-001. POST `/api/matched-frame/references` nhận multipart file; stream vào tempfile trong downloads/matched_frame/upload, kiểm Origin, gọi read_reference_metadata trong thread; lưu metadata qua FrameStore, xóa tempfile và đóng spool trong finally cả khi hủy request. Thêm GET `/api/matched-frame/references/{reference_id}`. Cập nhật `requirements.txt` theo CON-004; lỗi theo SEC-001. Các route còn lại bổ sung ở TASK-012. | ✅ | 2026-10-22 |
| TASK-005 | Phụ thuộc TASK-002/TASK-004. Thêm `tests/test_reference_frame.py`, `tests/test_frame_grid.py` và nhóm upload trong `tests/test_frame_api.py`; dùng GeoTIFF synthetic tại TemporaryDirectory, không ảnh thật/mạng; cover TEST-001–TEST-003, TEST-009. Hoàn tất phase khi các test này pass. | ✅ | 2026-10-22 |

### Implementation Phase 2

- **GOAL-002**: Tìm và xếp hạng cảnh theo đúng ROI, chất lượng và orbit, lưu snapshot bất biến. Phụ thuộc GOAL-001.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | Phụ thuộc TASK-001/TASK-002/TASK-003. Thêm `app/frame_search.py` với `search_frame_scenes(request, frame)`, `s2_roi_metrics(image, roi, bands)`, `s1_roi_metrics(image, roi, bands)`, `rank_frame_scenes(scenes, request)`. Dùng collection raw allowlist, chuyển end-exclusive một lần, không gọi processed legacy. ROI từ affine nguồn, không bounding rectangle thay footprint; SCL denominator tường minh, cloud/coverage tách riêng, source valid zero giữ nguyên. Xử lý cap 501/500, empty list, timeout, nullable quality và filter/sort theo REQ-005–REQ-009. S1 chỉ prefer orbit khi đủ dữ liệu đã xác nhận. | ✅ | 2026-10-22 |
| TASK-007 | Phụ thuộc TASK-006. Thêm `make_frame_preview(image, dataset, roi)` và scene preview cache trong `app/frame_search.py`: S2 RGB stretch 0–0.3, S1 dB -25..0, max 512 px; thao tác trên image dùng riêng preview, không chạm graph xuất. Lưu thumbnail cache UUID trong workspace download; phục vụ bằng server, không serialize signed URL. Preview lỗi không làm search/job thất bại. | ✅ | 2026-10-22 |
| TASK-008 | Phụ thuộc TASK-006/TASK-007. Thêm `tests/test_frame_search.py` cho TEST-004/TEST-005/TEST-010 với EE fake hoặc mock tương tự tests/test_point_coverage.py; kiểm metric denominator, sort tie, timestamp/asset snapshot, no-scene, cap overflow và preview/data separation. Không cần credentials để chạy. | ✅ | 2026-10-22 |

### Implementation Phase 3

- **GOAL-003**: Export 1–10 cảnh trực tiếp vào target grid, có preflight và dữ liệu kiểm chứng sau tải. Phụ thuộc GOAL-001; các unit export có thể làm song song GOAL-002 sau TASK-002/TASK-003.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-009 | Phụ thuộc TASK-001/TASK-002. Thêm `app/frame_export.py`: `load_frame_image(asset_id, dataset, bands)`, `iter_grid_tiles(grid, band_count, byte_limit=16777216)`, `fetch_grid_tile`, `export_frame_scene`. Load exact allowlisted asset, S2 multiply 0.0001 một lần, S1 native dB; dùng common source-mask validity nearest và continuous resampling riêng để không lấp lỗ. Mỗi tile target là integer window, affine=T*Affine.translation(col_off,row_off), dimensions<=2048 và width*height*bands*4<=16 MiB; gọi GEE với CRS+crs_transform+dimensions, không truyền scale/region ghi đè grid. Bilinear có halo nguồn trước clip; không clip ROI trước resampling. Reuse imagery._DownloadURL/_fetch_with_retry với timeout/retry rõ của CON-003, refresh URL khi rỗng/corrupt/expired; validate tile header/count trước ghi window vào TIFF tạm. Không tạo raster WGS84 trung gian; band SCL nếu dùng trong kiểm tra chỉ nearest. | ✅ | 2026-10-22 |
| TASK-010 | Phụ thuộc TASK-009. Trong `app/frame_export.py` thêm `validate_frame_export(path, grid, bands)` và `build_frame_manifest(snapshot, reports)`: mask/NaN thực từng band, coverage actual, source ID/date/native resolution/resampling/units, bounds+four corners, fingerprint metadata frame (không chứa pixel hay local path), quality denominator/scale, grid mode và warning; không log/clamp dB hoặc bỏ zero. Verify TIFF trước os.replace; đóng handles trước replace trên Windows; tên trong ZIP an toàn. Từ chối công bố scene thiếu coverage theo REQ-012; giữ lý do lỗi và tỷ lệ thực trong manifest. | ✅ | 2026-10-22 |
| TASK-011 | Phụ thuộc TASK-003/TASK-009/TASK-010. Thêm `app/frame_jobs.py`: `FrameService.preflight`, `FrameService.start_job`, `FrameService.run_job`, `recover`, `cleanup`, `shutdown`. Preflight tính estimate W*H*bands*4*scene_count, disk reserve, validity selection và SHA-256 canonical snapshot+ordered scene IDs+grid+processing; trước start recompute digest, giới hạn 10 và quota, không đổi search. Một worker export; idempotent start theo preflight_id; active job khác trả BusyError. Scene lỗi thường tiếp tục; fatal disk full dừng; nguyên tử TIFF/ZIP/manifest; cung cấp progress/counters và finished file tokens. Không dựa vào grid của scene thành công đầu tiên. | ✅ | 2026-10-22 |
| TASK-012 | Phụ thuộc TASK-004/TASK-006/TASK-007/TASK-011. Trong `app/frame_api.py` đăng ký hợp đồng API bên dưới; main.py tạo store/service trong downloads/matched_frame và lifecycle recover/cleanup/shutdown theo mẫu hiện tại, đăng ký trước app.mount. Search là POST bounded sync trong worker thread, semaphore=1; status 404 missing, 409 busy/not-ready/stale preflight, 422 invalid/quota/too-many-scenes, 502 sanitized upstream. Không đổi scene-catalog/timeseries API cũ. | ✅ | 2026-10-22 |
| TASK-013 | Phụ thuộc TASK-010/TASK-011/TASK-012. Thêm `tests/test_frame_export.py`, `tests/test_frame_jobs.py`; mở rộng `tests/test_frame_api.py` cho TEST-006–TEST-009/TEST-011. Kiểm tile seam, valid-zero, native source holes, dtype/bands, dB ngoài -30..10, exact snapshot, partial/restart/cleanup/disk/corrupt response và không double-scale. Hoàn tất phase khi targeted offline suite pass. | ✅ | 2026-10-22 |

Hợp đồng API mới (không thay route cũ):

| Method | Path | Input → Output |
|--------|------|----------------|
| POST | `/api/matched-frame/references` | multipart file → reference_id, metadata, footprint_geojson, compatible_grid_modes, warnings; upload pixel chỉ đến server cục bộ và xóa sau đọc metadata |
| GET | `/api/matched-frame/references/{reference_id}` | UUID → metadata an toàn hoặc 404 |
| POST | `/api/matched-frame/searches` | reference_id, dataset, start_date/end_date, grid_mode/resolution, band_preset/polarization, sort, optional reference_date/orbit, require_full_coverage, max_cloud_shadow_pct nullable → search_id và trang đầu tối đa 50 scene |
| GET | `/api/matched-frame/searches/{search_id}` | offset>=0, limit 1–50 → snapshot parameters/effective_grid/total/scenes |
| GET | `/api/matched-frame/searches/{search_id}/scenes/{scene_token}/preview` | scene_token thuộc snapshot → image/png hoặc sanitized lỗi; token không phải asset/path |
| POST | `/api/matched-frame/preflight` | search_id, unique scene_tokens 1–10 → preflight_id, digest, output_grid, estimates, warnings, expiry |
| POST | `/api/matched-frame/jobs` | preflight_id, digest, acknowledged=true → job_id và trạng thái snapshot |
| GET | `/api/matched-frame/jobs/{job_id}` | UUID → progress, scene reports, error codes và relative result links |
| GET | `/api/matched-frame/jobs/{job_id}/files/{file_token}` | file_token thành công thuộc job → image/tiff; không nhận tên/đường dẫn tùy ý |
| GET | `/api/matched-frame/jobs/{job_id}/download` | completed/partial job có ít nhất 1 TIFF thành công → application/zip; chưa sẵn sàng trả 409 |

### Implementation Phase 4

- **GOAL-004**: Người dùng thực hiện toàn bộ luồng trên Satellite Workspace, không cần script/đường dẫn cục bộ; bảo toàn các chế độ vùng chọn đang có. Phụ thuộc GOAL-002/GOAL-003 cho tích hợp thật; UI mock có thể làm sau TASK-001 khi hợp đồng đã cố định.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-014 | Phụ thuộc TASK-001/TASK-004. Trong static/index.html thêm option `reference` của selection_mode, container `referenceSelection` có input file/label, summary CRS/footprint/pixel/native bands và nhóm grid mode/resolution; thêm `matchedFrameResults` trong kết quả. Mở rộng setSelectionMode để reference không hiện point/radius/rotation và tắt rectangle editor; dispatch reference ngay đầu btnSearch handler trước parse lon/lat/rotation, không gọi opticalSceneBody/searchTimeSeries/searchSceneCatalog legacy. Thêm static/matched-frame.js quản lý upload/search generation token bằng AbortController, footprint layer riêng, preset S2 và S1 orbit filters, validation và invalidation theo GUD-001. File picker không nhận path text, không tự suy đoán cloud/orbit từ tên. | ✅ | 2026-10-22 |
| TASK-015 | Phụ thuộc TASK-012/TASK-014. Trong static/matched-frame.js thêm bảng cảnh có checkbox tối đa 10, platform/UTC/date/tile-orbit/coverage/cloud+shadow, nullable=N/A, sort rõ và preview; tách tile_cloud_pct khỏi ROI metrics. Nút tải mở preflight confirmation có exact/approx pixel, band/native resolution, dung lượng và quality warnings; chỉ tạo job khi xác nhận đúng digest. Hiển thị tiến độ trong Kết quả, giữ job_id qua reload bằng localStorage có fallback khi bị chặn; polling 2 s chỉ khi tab visible, backoff tối đa 15 s và dừng terminal; không lấy thay đổi form sửa snapshot job. Dùng helper workspace hiện có để mở panel, không đổi điều hướng Esri. | ✅ | 2026-10-22 |
| TASK-016 | Phụ thuộc TASK-014/TASK-015. Thêm style scoped theo vùng matched-frame vào static/workspace.css, tái dùng theme tokens; sửa static/workspace.js: move referenceSelection vào region và matchedFrameResults vào workspaceResults; thêm label reference, thêm matchedFrameResults vào cả hasResults và MutationObserver; xuất hook showView qua object SatelliteWorkspace cho module mới thay vì giả click DOM. Đặt script order để controller khởi tạo sau controls/workspace sẵn sàng. Thêm tests/test_matched_frame_ui.cjs theo Node test pattern hiện có: mode dispatch trước lon/lat validation, errors, stale responses, max10, digest confirmation, partial/reload/poll stop và accessible labels. Kiểm bàn phím, 375px/desktop, sáng/tối trong browser; không đưa tính năng sang frontend prototype khi app đang phục vụ static. | ✅ | 2026-10-22 |

### Implementation Phase 5

- **GOAL-005**: Tài liệu, hồi quy và smoke download chứng minh frame/grid/giá trị được bảo toàn; ghi rõ phần cần credentials và không tuyên bố chất lượng thị giác chỉ từ SCL. Phụ thuộc các GOAL trước.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-017 | Phụ thuộc TASK-012/TASK-016. Cập nhật README.md thêm mục tải đồng khung: workflow upload, hai mode, footprint vs alpha mask, CRS projected-metre constraint, 12 band/native resolution, SR scaling/native dB, valid-zero, ranking SCL vs haze/coverage, preview vs raw TIFF, max10/quota/TTL/local-only và ZIP manifest. Cập nhật phần dependency install cho python-multipart; không thay mô tả hành vi legacy. | ✅ | 2026-10-22 |
| TASK-018 | Phụ thuộc TASK-005/TASK-008/TASK-013/TASK-016/TASK-017. Chạy targeted commands trong phần Testing, git diff --check; nếu có lỗi hồi quy mới do thay đổi shared code, thêm selector test liên quan trước khi chạy rộng. Khởi động bằng VS Code task Run GEE web app nếu chưa có server, verify /api/health rồi browser smoke. Live GEE smoke opt-in với quyền hiện có: một S2 và một S1 nhỏ từ fixture upload tự tạo, xác minh TIFF trên đĩa+manifest+CRS/affine/bands/masks, báo riêng unavailable credentials; không tải toàn mẫu hoặc gửi pixel mẫu sang GEE. Đánh dấu plan In progress cho đến khi live/browse checks hoàn thành hoặc ghi rõ phần blocked; chỉ Completed khi tất cả điều kiện GOAL đạt. | ✅ | 2026-10-22 |

## 3. Alternatives

- **ALT-001**: Nhập path cục bộ như script thủ công: không chọn vì browser không có quyền đọc path máy người dùng và backend có thể thành endpoint đọc tệp tùy ý; người dùng đã chọn upload.
- **ALT-002**: Dùng lon/lat center+radius hoặc bbox Bắc–Nam để tái tạo khung: không chọn vì mất CRS/affine, footprint xoay và pixel alignment.
- **ALT-003**: Dùng download_geotiff + warp_to_grid mặc định: không chọn cho matched-frame vì legacy S2 chỉ sáu band và EPSG:4326 intermediate; S1 legacy clamp. Không refactor toàn pipeline cũ chỉ để thêm tính năng.
- **ALT-004**: Buộc toàn bộ nguồn lên 3 m hoặc dùng AI sharpen: không chọn vì không tăng chi tiết đo được và làm dung lượng tăng; người dùng chọn default khoảng 10 m.
- **ALT-005**: Auto mosaic/composite, cùng ngày nhiều tile hoặc ghép track: hoãn; v1 một scene rõ nguồn cho mỗi TIFF, cảnh thiếu dữ liệu chỉ được chọn khi bật cho phép và giữ NoData.
- **ALT-006**: Dùng Celery/Redis hoặc frontend React prototype: không chọn vì ứng dụng localhost đã có mô hình SQLite/single worker và live UI static; không thêm hạ tầng/build migration ngoài phạm vi.

## 4. Dependencies

- **DEP-001**: FastAPI/Pydantic/rasterio/numpy/earthengine-api/requests đã khai báo trong requirements.txt; thêm python-multipart>=0.0.20 cho multipart parsing, sau đó mới restore môi trường.
- **DEP-002**: app/ee_session.py::init_ee và quyền đọc hai collection GEE; tests offline phải mock credential/network, live smoke tách riêng.
- **DEP-003**: app/raster_grid.py::grid_profile/warp_to_grid và app/imagery.py::_DownloadURL/_fetch_with_retry; giữ caller legacy không đổi. Các file đang sửa chưa commit phải được bảo toàn.
- **DEP-004**: Thư mục downloads/matched_frame được ignore theo downloads/; cần quyền ghi và disk reserve. SQLite không thay schema jobs.sqlite3 của time-series/scene-catalog.
- **DEP-005**: VS Code task Run GEE web app trong .vscode/tasks.json; unittest và Node built-in tests; browser smoke chỉ khi server responsive. Nếu TestClient thiếu httpx, ghi missing dependency trước, thêm dev dependency phù hợp trước cài, không biến httpx thành dependency runtime nếu không cần.

## 5. Files

- **FILE-001**: Tạo app/frame_models.py, app/reference_frame.py: request/response, constants, metadata và footprint validation.
- **FILE-002**: Sửa app/raster_grid.py bằng hàm bổ sung build_reference_grid, không đổi grid legacy.
- **FILE-003**: Tạo app/frame_search.py: exact collection search, ROI quality, orbit ranking, preview.
- **FILE-004**: Tạo app/frame_export.py: direct target-grid tiling, band processing, mask-aware export, TIFF validation và manifest.
- **FILE-005**: Tạo app/frame_store.py và app/frame_jobs.py: SQLite snapshot/preflight/result records và single worker lifecycle.
- **FILE-006**: Tạo app/frame_api.py; sửa app/main.py đăng ký route/service/lifespan trước static mount.
- **FILE-007**: Sửa requirements.txt thêm multipart dependency; không thay phiên bản dependency khác trong tác vụ này.
- **FILE-008**: Sửa static/index.html, static/workspace.css, static/workspace.js cho reference mode và panel results; tạo static/matched-frame.js.
- **FILE-009**: Tạo tests/test_reference_frame.py, tests/test_frame_grid.py, tests/test_frame_search.py, tests/test_frame_export.py, tests/test_frame_jobs.py, tests/test_frame_api.py, tests/test_matched_frame_ui.cjs.
- **FILE-010**: Cập nhật README.md và trạng thái plan này; không sửa các plan NDVI/time-series/rotation không liên quan.

## 6. Testing

- **TEST-001**: Metadata fixture projected north-up, CRS địa lý, affine xoay/shear; thiếu CRS, singular/NaN affine, GCP-only, non-GTiff, uint8/RGBA/multiband; đọc header không gọi read pixel; allowlist tags sanitized.
- **TEST-002**: Fixture W=1508,H=1157,T=(3,0,398712,0,-3,2406162), CRS EPSG:32648: match_extent r=10 → W=453,H=348, pixel vector length 9.986754966887418/9.974137931034482, bounds [398712,2402691,403236,2406162]; match_grid → giữ đúng W/H/T. Test xoay/shear giữ cả bốn góc; không tái dựng từ bbox; geographic match_grid accepted, geographic match_extent rejects.
- **TEST-003**: Pixel source hợp lệ bằng 0 không thành NoData; NoData source không được lấp bởi bilinear; nguồn hằng với halo không sinh mép trống; footprint geometry/bbox khác nhau khi xoay; không xử lý alpha mẫu như mask xuất.
- **TEST-004**: SCL synthetic có lớp 0/1/3/4/8/9/10/11, thiếu footprint và mask; tính đúng cloud/shadow/snow với denominator valid SCL, coverage toàn ROI, no valid → null. Metadata whole tile clouds không ảnh hưởng ROI ranking; báo rõ scale20/100 và estimated/full vs actual.
- **TEST-005**: S1 native [-45,-10,17] còn nguyên sau export, không log/clamp; mismatch orbit/platform không bị gọi là same orbit; missing tag không suy đoán; S2 full12/presets band order/scale một lần. Ties ngày khác vệ tinh vẫn là hai scene riêng, end-date inclusive, snapshot asset không đổi theo form.
- **TEST-006**: Target-grid tiles sát mép, số hàng/cột không chia hết tile; direct-grid export khớp untiled synthetic, seam không lặp/thiếu pixel, window transform đúng; per-tile byte/dimension constraints; không gọi legacy six-band/WGS84 exporter. TIFF persisted reopen kiểm W/H/CRS/affine/bands/dtypes/masks/tags.
- **TEST-007**: Partial job 1 success+1 failure, all failed, duplicate preflight start, >10 scenes, arbitrary scene token, quota/space, stale digest, UUID/path traversal, signed URL redaction, concurrent job/search; atomic promotion sau validation, cleanup handles Windows, restart interrupted không reselect, TTL không xóa active/đang stream file.
- **TEST-008**: TestClient upload chunked over-limit/incorrect Content-Length, multipart lỗi, fake TIFF/VRT, nonfinite metadata, rejected origin, 404 expired ID, unsupported dataset, no-scene, cap>500 error; download route chỉ trả TIFF verified hoặc ZIP ready; manifest không chứa pixel/đường dẫn/token ký.
- **TEST-009**: Node UI tests mock fetch, AbortController, storage denied, stale responses, đổi mode/source/grid, checkbox max10, preview accessible, error summary focus, explicit preflight confirm, reload polling và terminal stop; các luồng point/rectangle/province/commune/Esri cũ không thay dispatch.
- **TEST-010**: Preview dùng đúng RGB B4/B3/B2 hoặc single-pol SAR, stretch không đưa vào export graph; SCL 0% vẫn hiển thị cảnh báo chất lượng, cloud retained; error preview không hủy search hoặc job.
- **TEST-011**: Browser/live smoke opt-in chỉ dùng khung nhỏ synthetic metadata: chọn file → map footprint → tìm → preview → preflight → tải 1–2 cảnh → reopen TIFF và đối chiếu manifest. Không coi thiếu credentials là offline suite pass cho live, ghi rõ blocked.

Lệnh triển khai kiểm chứng đã chạy:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest tests.test_reference_frame tests.test_frame_grid tests.test_frame_search tests.test_frame_export tests.test_frame_jobs tests.test_frame_api
node --test tests\test_matched_frame_ui.cjs tests\test_rectangle_ui.cjs tests\test_theme.cjs
& '.\.venv\Scripts\python.exe' -m unittest tests.test_rotated_exports tests.test_rectangle_downloads tests.test_timeseries tests.test_timeseries_image_type tests.test_scene_catalog tests.test_s1_mosaic tests.test_point_coverage
 git --no-pager diff --check
```

Chạy suite mới sau mỗi phase với selector tương ứng; chạy nhóm hồi quy một lần sau tích hợp shared lifecycle/grid/UI. Không chạy toàn suite nếu targeted không cho thấy cần thiết. Khởi động app qua task đã có, không tạo server trùng cổng và không dừng process không thuộc tác vụ.

## 7. Risks & Assumptions

- **RISK-001**: Upload toàn ảnh mẫu có thể chậm/dung lượng lớn dù chỉ đọc metadata. V1 giới hạn 256 MiB, progress rõ; ảnh mẫu không ra khỏi server cục bộ và bị xóa ngay sau parse. Đọc metadata hoàn toàn tại browser hoặc sidecar là mở rộng sau, không lén thêm scope.
- **RISK-002**: GEE giới hạn byte/dimension/compute và signed URL hết hạn; fixed-grid tile <=16 MiB, refresh có giới hạn, job partial và timeout. Không nhầm 2^25 bytes là 48 MB; byte limit cấu hình thấp hơn ngưỡng GEE.
- **RISK-003**: Footprint hình học phủ đủ chưa chứng minh band mask phủ đủ; coverage estimated có sai số. Xác minh actual sau export và ghi manifest theo từng band; lỗi nguồn có thể làm scene fail dù search pass.
- **RISK-004**: SCL 0% không chứng minh ảnh đẹp; mây mỏng, haze, bóng địa hình và khác điều kiện chiếu sáng vẫn tồn tại. Preview/RGB stretch chỉ giúp quan sát, không tăng độ phân giải hay sửa số đo; không chọn mặc định lọc tất cả bóng địa hình như mây.
- **RISK-005**: GEE có thể xử lý affine xoay/shear khác grid rasterio. Kiểm direct target-grid với fixture/live smoke; nếu upstream từ chối CRS/affine, trả lỗi unsupported_grid có hướng dẫn, không xuất sai grid hoặc tự fallback sang EPSG:4326 hai lần nội suy.
- **RISK-006**: TIFF đã scale là dữ liệu phân tích, không phải visual 8-bit; người dùng có thể nhầm band index do 12 kênh. UI/README liệt kê band order, native resolution và preview đúng RGB. Compression Deflate lossless, không phải nguyên nhân giảm chất lượng.
- **RISK-007**: Có nhiều thay đổi chưa commit, gồm README.md, app/main.py, static/index.html và các file mới. Không reset/overwrite chúng, không đưa secret/GeoTIFF vào git, không triển khai migration frontend hoặc sửa SAR legacy trong feature này.
- **ASSUMPTION-001**: Phiên bản đầu là ứng dụng localhost một người dùng, không có yêu cầu multi-user/auth/RBAC hoặc server đọc file tùy ý. Nếu chuyển deployment dùng chung, cần kế hoạch auth/ownership riêng trước mở route upload/download ra mạng.
- **ASSUMPTION-002**: Giữ footprint toàn raster là nghĩa mặc định đồng khung; không clipping theo alpha/road corridor và không tự đăng ký ảnh theo đối tượng. Các tag mẫu có thể thiếu hoặc sai và chỉ được dùng như gợi ý đã xác nhận.
- **ASSUMPTION-003**: Chưa triển khai hủy/resume giữa tile trong v1; khi restart cảnh đã xong giữ báo cáo nhưng job interrupted, người dùng tạo lại từ snapshot để tải lại. Không tuyên bố hỗ trợ resume chính xác byte hoặc multi-scene composite.
- **ASSUMPTION-004**: Scope mặc định S2/S1 GEE và max10 đã được người dùng xác nhận; các quota khác và projected-metre constraint là đề xuất kỹ thuật v1 được ghi rõ, không phải năng lực đã có của repo.

## 8. Related Specifications / Further Reading

- [Shared raster grid](../app/raster_grid.py)
- [GEE imagery processing and downloads](../app/imagery.py)
- [Persistent time-series job pattern](../app/timeseries.py)
- [Scene catalog snapshots](../app/scene_catalog.py)
- [Current application documentation](../README.md)
- [Rotated ROI plan](./feature-rotated-roi-1.md)
- [Time-series plan](./feature-timeseries-gee-1.md)
- [Earth Engine Image.getDownloadURL](https://developers.google.com/earth-engine/apidocs/ee-image-getdownloadurl)
- [Sentinel-2 SR Harmonized collection](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_SR_HARMONIZED)
- [Sentinel-1 GRD collection](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S1_GRD)
- [Rasterio georeferencing](https://rasterio.readthedocs.io/en/stable/topics/georeferencing.html)
- [FastAPI upload files](https://fastapi.tiangolo.com/tutorial/request-files/)



