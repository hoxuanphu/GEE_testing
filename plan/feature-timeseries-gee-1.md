---
goal: 'Tải chuỗi ảnh GEE theo vị trí, khoảng ngày và bán kính'
version: '1.0'
date_created: '2026-10-13'
last_updated: '2026-10-13'
owner: 'GEE_testing'
status: 'In progress'
tags: ['feature', 'timeseries', 'gee']
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

Phương án đã được duyệt và triển khai; kiểm chứng live GEE còn chờ tài khoản/quota được phép. Bổ sung công tắc time series vào giao diện hiện tại, tìm các cảnh GEE trong khoảng ngày, hiển thị danh sách rồi tải một ZIP chứa GeoTIFF theo từng thời điểm và metadata. Các yêu cầu người dùng đã chọn được ghi riêng với các đề xuất kỹ thuật chưa được duyệt.

## 1. Requirements & Constraints

- **REQ-001**: Chỉ cho phép time series ở `selection_mode=point`; công tắc mặc định tắt. Khi tắt, giữ nguyên luồng tìm/tải ảnh hiện tại.
- **REQ-002**: Hỗ trợ `sentinel2`, `landsat`, `nicfi`, `dynamic_world`, `s1gee`; không hỗ trợ CDSE/AWS trong phiên bản đầu.
- **REQ-003**: Dùng kinh độ, vĩ độ, ngày bắt đầu, ngày kết thúc và `radius_m` từ các ô hiện có. Tính cả ngày bắt đầu và ngày kết thúc; chuyển ngày kết thúc thành ngày kế tiếp khi gọi `filterDate`.
- **REQ-004**: Tìm xong hiển thị số lượng và danh sách; chỉ bắt đầu tải khi người dùng nhấn `Tải toàn bộ ZIP`.
- **REQ-005**: Lấy mọi cảnh trong khoảng ngày đáp ứng nguồn dữ liệu và vùng chọn; không áp dụng ngưỡng mây 60% hoặc ngưỡng Sentinel-2 90%. Ghi % mây metadata nếu có; không coi metadata mây là % mây trong ROI.
- **REQ-006**: Không chọn một cảnh tốt nhất cho toàn bộ khoảng thời gian; không composite theo tháng hoặc ghép các ngày khác nhau. Giữ các scene khác ID dù cùng ngày, sắp xếp theo timestamp UTC rồi asset ID.
- **REQ-007**: Giữ xử lý và băng dữ liệu tương ứng của từng nguồn: Sentinel-2/Landsat reflectance giữ mây như luồng điểm hiện tại; NICFI composite theo kỳ của nguồn; Dynamic World label với nodata 255; Sentinel-1 GEE theo tiền xử lý dB hiện có, không log10 thêm.
- **SEC-001**: Chỉ chấp nhận asset ID từ kết quả tìm đã lưu phía server; không cho client cung cấp URL tải, đường dẫn file hoặc asset tùy ý. Tên file ZIP/GeoTIFF được chuẩn hóa, không chứa đường dẫn tương đối thoát thư mục.
- **CON-001**: Phương án đề xuất giữ ROI hình chữ nhật từ `point_bbox`, giống chế độ điểm hiện tại, không chuyển thành hình tròn. Cảnh phải có footprint chứa điểm trung tâm; tải vùng bbox, phần thiếu coverage giữ nodata. Đây không phải bảo đảm mỗi cảnh phủ đủ bbox.
- **CON-002**: NICFI là composite theo kỳ, không phải ảnh chụp từng ngày; quyền truy cập và khoảng thời gian có dữ liệu phụ thuộc collection. Sentinel-1 chỉ lấy IW và các băng polarization phù hợp, không ưu tiên duy nhất DESCENDING/latest như chọn ảnh đơn.
- **CON-003**: Đề xuất phân trang danh sách 50 cảnh/trang, tải tuần tự từng cảnh, tối đa một job tải đang chạy trong một tiến trình ứng dụng. Không âm thầm cắt bớt kết quả hoặc giới hạn khoảng ngày.
- **CON-004**: Đề xuất job nền và SQLite dùng thư viện chuẩn, không thêm Celery/Redis. Phiên bản đầu chạy một worker ứng dụng; restart đánh dấu job đang chạy là interrupted, không tự tiếp tục. Dọn job/file sau 24 giờ không hoạt động, không xóa job đang chạy.
- **GUD-001**: Đề xuất mọi GeoTIFF trong cùng job dùng chung CRS, transform, kích thước raster và bbox để so sánh theo thời gian. Mặc định grid EPSG:4326, độ phân giải danh nghĩa theo nguồn; lấy grid chuẩn từ kết quả cảnh đầu và warp các cảnh sau lên đúng grid đó. Dynamic World dùng nearest-neighbour.
- **PAT-001**: Tái sử dụng `download_geotiff`, `merge_band_tiffs`, helpers scale nguồn và `_resolve_selection`; không gọi `best_scene` hoặc `best_s1_scene` để tải một scene trong chuỗi.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Liệt kê đầy đủ metadata các cảnh theo vị trí mà không thay đổi tìm ảnh đơn. Chỉ thực hiện sau khi phương án được duyệt.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Trong `app/imagery.py`, thêm `timeseries_collection(dataset, start_date, end_exclusive, polarization)` và `list_timeseries_scenes(...)`. Dựng collection time series riêng để bỏ ngưỡng mây của `_s2_collection`; tái sử dụng `_scale_s2`, `_landsat_scale`, `_nicfi_collection`, `_dynamic_world_collection`. Với S1 lọc đủ các băng yêu cầu trước khi select và giữ metadata timestamp/orbit/track. Lưu full asset ID trước khi scale/select; Landsat 8/9 phải phân biệt collection. Lọc footprint chứa tâm, sắp xếp UTC timestamp + full asset ID, khử trùng chỉ theo full asset ID. Không tính clear-fraction từng cảnh và không tạo preview hàng loạt. | Yes | 2026-10-13 |
| TASK-002 | Trong `app/main.py`, thêm `TimeSeriesRequest`, `TimeSeriesSceneResponse`, `POST /api/timeseries/search` và `GET /api/timeseries/search/{search_id}?offset=0&limit=50`. Kiểm tra mode point, dataset allowlist, tọa độ, radius, ngày và polarization. Trả search_id, total, trang scenes, thông số ROI/bands/scale; scene gồm asset ID, timestamp UTC, date, cloud_pct nullable và metadata nguồn. Không có ảnh trả total=0, không tạo job. Phụ thuộc TASK-001 và TASK-003. | Yes | 2026-10-13 |
| TASK-003 | Thêm `app/timeseries.py`: `TimeSeriesStore` lưu search snapshot bất biến bằng SQLite tại `downloads/timeseries/jobs.sqlite3`, gồm tham số, danh sách asset ID và thời điểm tạo. Tạo UUID riêng cho mỗi search/job; chỉ ghi bên trong `downloads/timeseries`. Truy vấn lại các trang không gọi lại GEE. | Yes | 2026-10-13 |

### Implementation Phase 2

- **GOAL-002**: Tải đúng cảnh đã liệt kê, đóng gói ZIP và theo dõi tiến độ. Phụ thuộc GOAL-001.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | Trong `app/imagery.py`, thêm `load_timeseries_scene(asset_id, dataset, polarization)` dựng đúng asset thuộc snapshot, áp dụng scale/bands tương ứng rồi gọi `download_geotiff` và `merge_band_tiffs`. Trong `app/timeseries.py`, thêm `align_timeseries_raster(...)` dùng rasterio hiện có để chuẩn hóa raster về grid cảnh đầu theo GUD-001, bảo toàn nodata và band descriptions. Giữ nguyên hàm chọn/tải ảnh đơn. | Yes | 2026-10-13 |
| TASK-005 | Trong `app/timeseries.py`, thêm `run_timeseries_job(job_id)` tải tuần tự, dùng thư mục UUID và tên `dataset_YYYYMMDDTHHMMSSZ_scene-token.tif`; retry lỗi tạm thời có giới hạn, tận dụng `_fetch_with_retry`. Lỗi một cảnh không dừng các cảnh sau. ZIP gồm GeoTIFF thành công, `manifest.json` và `manifest.csv` ghi tham số, grid, asset ID, timestamp, băng, unit/scale, cloud metadata, orbit/track nếu có và lỗi từng cảnh. Không chứa signed URL hoặc credentials. Phụ thuộc TASK-004. | Yes | 2026-10-13 |
| TASK-006 | Trong `app/main.py`, thêm `POST /api/timeseries/jobs` nhận search_id; `GET /api/timeseries/jobs/{job_id}` trả status và tiến độ; `GET /api/timeseries/jobs/{job_id}/download` trả FileResponse ZIP khi sẵn sàng. Trạng thái queued/running/completed/partial/failed/interrupted; partial nếu có cả thành công và thất bại, failed nếu tất cả thất bại. Chống double-click tạo job trùng và ghi trạng thái SQLite. Giới hạn một job active với thông báo rõ; không chiếm request HTTP trong suốt quá trình tải. Phụ thuộc TASK-003 và TASK-005. | Yes | 2026-10-13 |
| TASK-007 | Trong `app/timeseries.py`, thêm `cleanup_expired_jobs(now)` và `recover_interrupted_jobs()`. Ghi ZIP qua file tạm rồi rename khi hoàn tất; giữ TIFF thành công và thông tin lỗi đến khi hết hạn. Kiểm tra dung lượng đĩa trước mỗi cảnh, dừng an toàn nếu không đủ; không xuất ZIP thiếu mà báo completed. Gọi recovery/cleanup từ lifecycle hiện có của `app/main.py`, không thay đổi EE initialization. Phụ thuộc TASK-006. | Yes | 2026-10-13 |

### Implementation Phase 3

- **GOAL-003**: Bổ sung công tắc và hành vi tìm/tải chuỗi trên UI. Phụ thuộc GOAL-001; tích hợp tải phụ thuộc GOAL-002.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | Trong `static/index.html`, thêm công tắc `time_series_enabled` gần khoảng ngày/bán kính và `updateTimeSeriesVisibility()`. Chỉ khả dụng khi mode point + nguồn GEE trong allowlist. Chuyển sang tỉnh/xã hoặc CDSE/AWS phải tắt công tắc, ẩn kết quả time series và giữ luồng cũ. Đổi nhãn btnSearch thành `Tìm ảnh time series` khi bật; không thêm ô chọn thời gian mới. Gắn kiểm tra trước các nhánh dataset hiện tại trong click handler btnSearch. | Yes | 2026-10-13 |
| TASK-009 | Trong `static/index.html`, thêm `searchTimeSeries(body)`, `renderTimeSeriesResults(data)` và `startTimeSeriesDownload(searchId)`. Hiển thị tổng số, ngày/giờ, scene ID, % mây nếu có, trang kết quả và nút Tải toàn bộ ZIP. Poll job mỗi 2 giây khi queued/running, hiển thị đã xử lý/total + thành công/lỗi, dừng poll khi terminal state. Completed/partial hiển thị link ZIP; partial hiện cảnh báo. Không tự tải từng file qua nhiều tab. Khi sửa đầu vào, vô hiệu hóa kết quả cũ; response tìm cũ không được ghi đè kết quả mới. Phụ thuộc TASK-002, TASK-006 và TASK-008. | Yes | 2026-10-13 |

### Implementation Phase 4

- **GOAL-004**: Kiểm chứng hồi quy và ghi hướng dẫn. Phụ thuộc các phase trên.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Bổ sung tests trong `tests/test_api.py`, `tests/test_imagery.py`, thêm `tests/test_timeseries.py` để kiểm chứng các mục TEST bên dưới bằng mock GEE/download. Dùng unittest hiện có, không gọi dịch vụ thật trong unit tests. Chạy ba test module liên quan rồi chạy regression API/imagery nếu cần. | Yes | 2026-10-13 |
| TASK-011 | Cập nhật `README.md`: phạm vi GEE-only/point-only, luồng công tắc → tìm → ZIP, mọi cảnh và mây, NICFI theo kỳ, footprint/nodata, lưới chung, endpoint mới, tiến độ/lỗi một phần, worker limit, TTL và hành vi restart. Kiểm tra thủ công UI trên server local và tải ZIP nhỏ với tài khoản GEE được phép trước khi kết luận hoàn tất. Phụ thuộc TASK-010. | Partial: docs/local UI done; live GEE pending | 2026-10-13 |

## 3. Alternatives

- **ALT-001**: Gọi endpoint tải ảnh đơn lặp theo từng ngày: không chọn vì có thể bỏ sót nhiều cảnh cùng ngày và endpoint hiện tại chọn lại ảnh tốt nhất.
- **ALT-002**: Tải từng GeoTIFF bằng nhiều link tự động: không chọn vì trình duyệt có thể chặn nhiều lượt tải và khó quản lý chuỗi.
- **ALT-003**: Một request đồng bộ tạo ZIP: đơn giản hơn nhưng không chọn vì chuỗi dài dễ timeout và không có tiến độ tin cậy.
- **ALT-004**: Composite theo ngày/tháng: không chọn vì thay đổi giá trị cảnh gốc và người dùng yêu cầu lấy mọi cảnh; NICFI vẫn giữ composite có sẵn của nguồn.

## 4. Dependencies

- **DEP-001**: GEE credentials/quyền collection hiện có; NICFI có thể không khả dụng với tài khoản hiện tại. Không đọc hoặc đưa credentials vào plan/manifest.
- **DEP-002**: FastAPI, Pydantic, earthengine-api, requests, rasterio hiện có; SQLite/zipfile/concurrent.futures dùng thư viện chuẩn. Không yêu cầu thêm package ở phương án này.
- **DEP-003**: Dung lượng đĩa đủ cho các GeoTIFF và ZIP, hạn mức GEE và cấu hình chạy một worker theo CON-004.

## 5. Files

- **FILE-001**: `static/index.html` — công tắc, phân nhánh tìm chuỗi, bảng kết quả và job progress.
- **FILE-002**: `app/main.py` — model/request validation, routes và lifecycle job.
- **FILE-003**: `app/imagery.py` — collection chuỗi, metadata và tải asset cụ thể.
- **FILE-004**: `app/timeseries.py` — file mới cho SQLite snapshots/jobs, executor, grid alignment, ZIP và cleanup.
- **FILE-005**: `tests/test_api.py`, `tests/test_imagery.py`, `tests/test_timeseries.py` — validation, ảnh và đóng gói.
- **FILE-006**: `README.md` — hướng dẫn chức năng và API.

## 6. Testing

- **TEST-001**: Tắt công tắc thì request/endpoint/luồng ảnh đơn không đổi; bật chỉ hoạt động point + nguồn GEE; API từ chối province/commune/nguồn chưa hỗ trợ.
- **TEST-002**: Cùng ngày bắt đầu/kết thúc vẫn lấy mọi timestamp trong ngày; gồm cảnh cuối ngày kết thúc, loại cảnh ngày sau; từ chối ngày đảo, sai format, tọa độ/radius không hợp lệ.
- **TEST-003**: Không bỏ ảnh cloud 60/90/100%; không composite; giữ nhiều asset cùng ngày, phân biệt Landsat 8 và 9, sắp xếp ổn định, không thiếu cảnh khi chuyển trang.
- **TEST-004**: Tải đúng full asset ID trong snapshot dù form thay đổi hoặc collection có cảnh mới; chặn asset/URL/path tùy ý; double-click không sinh hai job.
- **TEST-005**: Băng và scale đúng theo nguồn; Dynamic World water=0 không thành nodata, label uint8/nodata=255; S1 không log10 và không select polarization không có.
- **TEST-006**: ZIP/manifest/tên file đúng, cùng ngày không ghi đè; từng TIFF chung CRS/transform/shape, coverage thiếu giữ nodata, categorical chỉ nearest.
- **TEST-007**: Không có ảnh, hết quyền NICFI, EE transient error, lỗi một cảnh, tất cả cảnh lỗi, hết đĩa, restart và TTL cho trạng thái/thông báo đúng; không xóa job active.
- **TEST-008**: Kiểm tra browser: chuyển mode/source tự tắt, dữ liệu đầu vào thay đổi vô hiệu kết quả, stale response không ghi đè, poll dừng, ZIP tải được. Kiểm chứng ZIP nhỏ thực tế nếu có quyền GEE; ghi rõ nếu chưa thể kiểm chứng dịch vụ thật.

## 7. Risks & Assumptions

- **RISK-001**: Khoảng thời gian dài/bán kính lớn tạo nhiều dữ liệu, quota/time/disk cao. Danh sách xác nhận và tải tuần tự giảm rủi ro nhưng không loại bỏ; không cam kết thời gian tải cố định.
- **RISK-002**: S1 khác orbit/track và optical có mây ảnh hưởng so sánh thời gian; metadata phải thể hiện, không tự lọc hoặc giả định chuỗi đồng nhất.
- **RISK-003**: Grid alignment cần resampling với raster không cùng projection; phải ghi grid trong manifest và không tuyên bố bảo toàn nguyên pixel sau warp.
- **RISK-004**: Job UUID không thay thế authentication; deployment public/multi-user cần cơ chế quyền sở hữu job. Phương án hiện tại cho app local/trusted, không tự mở rộng public hosting.
- **ASSUMPTION-001**: Yêu cầu nguồn GEE, xác nhận tải ZIP và mọi cảnh/không ngưỡng mây đã được người dùng chọn; ROI bbox, grid chung, job nền, một job active và TTL 24 giờ đã được người dùng duyệt.
- **ASSUMPTION-002**: Chưa triển khai/tạo dữ liệu GEE trong giai đoạn lên phương án; không xác nhận availability từng collection bằng mạng ở giai đoạn này.

## 8. Related Specifications / Further Reading

- [README hiện tại](../README.md)
- [Luồng API chọn và tải ảnh](../app/main.py)
- [Xử lý collection và GeoTIFF](../app/imagery.py)
- [ROI hình chữ nhật chế độ điểm](../app/geo.py)
- [Giao diện hiện tại](../static/index.html)

## 9. Implementation verification (2026-10-13)

- TASK-001–010 implemented; 62 targeted API/imagery/job tests passed and full tests/ regression suite: 85 passed (mock services only).
- SQLite snapshots, retries, partial/all-failed exports, disk stop, restart, TTL, real raster grid/nodata and ZIP manifests verified by automated tests.
- Local browser verified default-off toggle, search/results including cloud 100%, explicit job start, partial progress and ZIP link, input invalidation, stale-response rejection, mode/source auto-off using mocked API. Health endpoint responsive, EE not initialized.
- TASK-011 documentation complete; live GEE small ZIP/permissions/availability NOT verified. No credentials were read, no live GEE service was invoked. This remaining verification is not claimed complete.


