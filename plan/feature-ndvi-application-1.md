---
goal: Build professional NDVI and flood-scar analysis application
version: 1.0
date_created: 2026-10-16
last_updated: 2026-10-16
owner: GEE_testing
status: 'Planned'
tags: [feature, remote-sensing, ndvi, flood-analysis, sentinel-1, sentinel-2, landsat, uav, frontend]
---

# Introduction

![Status: Planned](https://img.shields.io/badge/status-Planned-blue)

Xây dựng ứng dụng phân tích viễn thám chuyên nghiệp trên nền FastAPI hiện có, mở rộng từ tải ảnh sang tính chỉ số phổ, so sánh trước–sau, chuỗi thời gian, phát hiện bất thường và khoanh vùng dấu vết lũ quét. MVP mở rộng phải hỗ trợ Sentinel-2 SR, Landsat 8/9 SR, Sentinel-1 SAR và ảnh UAV; frontend được tách thành ứng dụng riêng; hệ thống dùng luật/ngưỡng cấu hình được nhưng lưu schema dữ liệu và mẫu để sẵn sàng cho ML.

## 1. Requirements & Constraints

- **REQ-001**: Cho phép người dùng chọn ROI theo điểm, tỉnh/thành, xã/phường hoặc GeoJSON/Shapefile/UAV footprint; lưu geometry, CRS và diện tích ROI.
- **REQ-002**: Cho phép chọn nguồn Sentinel-2 SR, Landsat 8/9 SR, Sentinel-1 GRD và raster UAV; hiển thị ngày chụp, độ phân giải, độ phủ hợp lệ, mây và trạng thái xử lý.
- **REQ-003**: Tính NDVI, EVI, SAVI, NDMI, BSI và MNDWI; công thức, hệ số, tên band và scale phải được khai báo tập trung, không hard-code trong endpoint.
- **REQ-004**: Hỗ trợ cặp ảnh trước–sau và cửa sổ thời gian; tính delta index, phần trăm thay đổi và bản đồ phân loại bất thường.
- **REQ-005**: Hỗ trợ chuỗi thời gian theo ROI và theo vùng con, gồm trung bình, trung vị, phân vị, độ lệch chuẩn, số pixel hợp lệ và biểu đồ xu thế/mùa vụ.
- **REQ-006**: Hỗ trợ phát hiện dấu vết lũ quét dựa trên ΔNDVI, ΔBSI, ΔNDMI, MNDWI, thay đổi VV/VH, độ dốc, HAND và khoảng cách đến sông; các ngưỡng phải cấu hình được.
- **REQ-007**: Hỗ trợ tạo lớp kết quả dạng raster và vector, lọc nhiễu theo diện tích/kết nối, chỉnh sửa thủ công và xuất GeoTIFF, GeoJSON/GPKG, CSV và báo cáo.
- **REQ-008**: Cung cấp kiểm tra chất lượng, cảnh báo mây/NoData/độ phủ thấp, đối chiếu mẫu kiểm chứng và ma trận nhầm lẫn khi có nhãn.
- **REQ-009**: Lưu dataset mẫu/label/method metadata theo schema có thể dùng cho pipeline ML sau này, nhưng phiên bản đầu không huấn luyện mô hình ML.
- **REQ-010**: Tác vụ nặng phải chạy bất đồng bộ, có job status, tiến độ, hủy/recovery và dọn dẹp artifact hết hạn.
- **SEC-001**: Kiểm tra kích thước file UAV, loại MIME, extension, raster band/count/CRS và chống path traversal; không thực thi nội dung tải lên.
- **SEC-002**: Không đưa token Earth Engine/CDSE/S3 vào frontend hoặc log; giới hạn CORS theo cấu hình triển khai.
- **CON-001**: Giữ tương thích các endpoint tải ảnh, timeseries và test hiện có trong `app/main.py`.
- **CON-002**: Tận dụng Earth Engine cho Sentinel-2/Landsat/Sentinel-1 và Rasterio/GDAL cho UAV, phép chiếu, mask và xuất raster.
- **CON-003**: Frontend mới phải giao tiếp với API hiện có qua HTTP và không yêu cầu thay đổi backend ngoài các module/route phân tích cần thiết.
- **GUD-001**: Chuẩn hóa toàn bộ kết quả về CRS dự án/UTM phù hợp ROI trước thống kê diện tích.
- **GUD-002**: Mọi chỉ số phải kèm metadata nguồn, ngày ảnh, scale, mask, công thức, ngưỡng và quality flags.
- **PAT-001**: Tách lớp `models`, `services`, `repositories/jobs` và route; không đặt logic viễn thám trong route hoặc JavaScript UI.
- **PAT-002**: Dùng schema versioning cho analysis request/result và dataset labels để hỗ trợ migration/ML.

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: Establish analysis domain contracts and preserve the existing API baseline.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Kiểm kê route, service và test hiện có trong `app/main.py`, `app/imagery.py`, `app/cdse.py`, `app/timeseries.py`; chốt contract không phá vỡ cho các endpoint tải ảnh/timeseries. | | |
| TASK-002 | Tạo `app/analysis_models.py` với Pydantic models versioned cho ROI, source scene, index recipe, before/after request, time-series request, threshold rule, quality flags, label samples và analysis result. | | |
| TASK-003 | Tạo `app/index_registry.py` khai báo band map và công thức NDVI/EVI/SAVI/NDMI/BSI/MNDWI cho Sentinel-2, Landsat, Sentinel-1-derived features và UAV; thêm validate band/scale/CRS. | | |
| TASK-004 | Tạo migration/config schema cho thư mục artifact và job metadata dưới `downloads/analysis`; bổ sung cấu hình môi trường trong `.env.example`, không ghi secret vào repository. | | |

### Implementation Phase 2

- GOAL-002: Implement source ingestion, preprocessing and quality control.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-005 | Mở rộng `app/imagery.py` cho Sentinel-2/Landsat index-ready composites: cloud/shadow mask, scale reflectance, valid-pixel mask, scene metadata và before/after pairing. | | |
| TASK-006 | Mở rộng `app/imagery.py` hoặc module mới `app/sar_features.py` cho Sentinel-1 GRD VV/VH, log-ratio/difference trước–sau, orbit/polarization consistency và quality flags. | | |
| TASK-007 | Tạo `app/uav_ingestion.py` xử lý upload raster UAV qua Rasterio: validate an toàn, đọc band wavelengths, scale/NoData, reprojection và chuyển về analysis grid. | | |
| TASK-008 | Tạo `app/quality.py` tính cloud fraction, valid fraction, NoData, footprint coverage, temporal gap, resolution/CRS mismatch và cảnh báo hiển thị cho người dùng. | | |
| TASK-009 | Viết unit tests cho registry, band mapping, công thức, mask, pair selection, Sentinel-1 feature calculation và UAV validation trong `tests/test_analysis_models.py`, `tests/test_index_registry.py`, `tests/test_quality.py`, `tests/test_uav_ingestion.py`. | | |

### Implementation Phase 3

- GOAL-003: Implement asynchronous analysis execution and flood-scar detection.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-010 | Tạo `app/analysis_engine.py` nhận analysis request, tạo index rasters, delta layers, statistics và metadata; dùng cùng grid/transform để before-after pixel alignment. | | |
| TASK-011 | Tạo `app/flood_scar.py` áp dụng rule-based scoring từ ΔNDVI, ΔBSI, ΔNDMI, MNDWI, Sentinel-1 change, slope, HAND và distance-to-stream; cho phép ngưỡng, trọng số, min-area và confidence cấu hình. | | |
| TASK-012 | Tạo `app/analysis_jobs.py` dựa trên pattern `TimeSeriesService` để chạy background job, progress, cancellation, restart recovery, expiration cleanup và artifact manifest. | | |
| TASK-013 | Thêm route analysis vào `app/main.py`: create job, status, cancel, result metadata, statistics, raster/vector download, time-series query, label sample CRUD và report export; giữ các route cũ. | | |
| TASK-014 | Tạo `app/ml_schema.py` lưu feature stack, label polygons/points, split metadata, provenance và export dataset; không gọi training trong release đầu. | | |
| TASK-015 | Viết integration tests trong `tests/test_analysis_api.py` cho create/status/cancel/download, validation lỗi, quality warnings, before-after result và ML dataset export. | | |

### Implementation Phase 4

- GOAL-004: Replace the single-page UI with a maintainable professional frontend.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-016 | Tạo frontend độc lập dưới `frontend/` với TypeScript và framework component-based; cấu hình build để output vào thư mục static phục vụ bởi FastAPI hoặc deploy độc lập. | | |
| TASK-017 | Xây các màn hình `frontend/src/features/selection`, `sources`, `analysis`, `timeseries`, `quality`, `validation`, `exports`: chọn ROI, nguồn/ngày, chỉ số, threshold, chạy job và theo dõi tiến độ. | | |
| TASK-018 | Xây bản đồ lớp với Leaflet/MapLibre: base map, before/after swipe, layer opacity, legend, identify pixel/ROI, vector flood scar editing và overlay slope/HAND/stream. | | |
| TASK-019 | Xây biểu đồ chuỗi thời gian và bảng thống kê; hiển thị metadata, quality flags, confidence, cảnh báo và trạng thái thiếu dữ liệu. | | |
| TASK-020 | Xây workflow kiểm chứng: tạo/sửa/xóa mẫu label, gắn class, xem confusion matrix/precision-recall khi có nhãn và xuất ML dataset. | | |
| TASK-021 | Thêm frontend tests cho công thức hiển thị, form validation, job polling, map layer state, threshold editing và export links; cập nhật `static/index.html` chỉ làm shell/compatibility redirect nếu cần. | | |

### Implementation Phase 5

- GOAL-005: Validate scientific correctness, performance and release behavior.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-022 | Chuẩn bị fixture raster nhỏ và expected arrays trong `tests/fixtures/analysis`; kiểm tra công thức, NoData, CRS transform và diện tích vector bằng giá trị tham chiếu. | | |
| TASK-023 | Chạy regression suite hiện có cùng analysis tests; xác nhận các route tải ảnh/timeseries không đổi contract và không rò rỉ artifact. | | |
| TASK-024 | Benchmark ROI nhỏ/vừa/lớn cho thời gian, memory, số pixel và job concurrency; đặt giới hạn request, tileScale/maxPixels và thông báo lỗi rõ ràng. | | |
| TASK-025 | Thực hiện kiểm tra security cho upload UAV, download path, CORS/config/logging và quyền truy cập job/artifact trước khi phát hành. | | |
| TASK-026 | Cập nhật `README.md`, `.env.example`, `requirements.txt` và tài liệu API/schema; thêm hướng dẫn chạy backend/frontend, diễn giải chỉ số và giới hạn suy luận lũ quét. | | |

## 3. Alternatives

- **ALT-001**: Tiếp tục mở rộng một file `static/index.html`; không chọn vì frontend có nhiều workflow, bản đồ chỉnh sửa và biểu đồ cần component/state management.
- **ALT-002**: Chỉ dùng Google Earth Engine cho toàn bộ UAV; không chọn vì UAV upload, CRS, band wavelength và kiểm soát raster cần Rasterio/GDAL cục bộ.
- **ALT-003**: Huấn luyện ML ngay trong phiên bản đầu; không chọn vì cần dữ liệu nhãn và benchmark đủ lớn, trong khi rule-based scoring minh bạch hơn cho xác minh dấu vết lũ.
- **ALT-004**: Chạy phân tích đồng bộ trong HTTP request; không chọn vì composite, download và raster export có thể vượt timeout.

## 4. Dependencies

- **DEP-001**: FastAPI, Pydantic v2, Uvicorn và pattern job service hiện có.
- **DEP-002**: Earth Engine API và quyền truy cập collections Sentinel-2 SR, Landsat Collection 2 L2, Sentinel-1 GRD.
- **DEP-003**: Rasterio/GDAL, NumPy, Pillow và pyshp cho raster/vector/UAV processing.
- **DEP-004**: Frontend TypeScript framework, map library, chart library và test runner; phiên bản phải được pin trong manifest frontend.
- **DEP-005**: DEM/HAND và hydrography dataset có license/phạm vi sử dụng rõ ràng.
- **DEP-006**: Fixture/ground truth hoặc mẫu gán nhãn để đánh giá rule-based result và ML schema.

## 5. Files

- **FILE-001**: `app/analysis_models.py` — request/result/metadata schemas.
- **FILE-002**: `app/index_registry.py` — band maps, formulas and recipe validation.
- **FILE-003**: `app/quality.py` — data quality metrics and warnings.
- **FILE-004**: `app/sar_features.py` — Sentinel-1 feature preparation.
- **FILE-005**: `app/uav_ingestion.py` — secure UAV raster ingestion and grid normalization.
- **FILE-006**: `app/analysis_engine.py` — index, delta and statistics pipeline.
- **FILE-007**: `app/flood_scar.py` — configurable flood-scar scoring and vectorization.
- **FILE-008**: `app/analysis_jobs.py` — asynchronous job lifecycle and artifact manifests.
- **FILE-009**: `app/ml_schema.py` — feature/label provenance and ML-ready export.
- **FILE-010**: `app/main.py` — analysis API routes and compatibility wiring.
- **FILE-011**: `frontend/` — TypeScript UI, map, charts, state and API client.
- **FILE-012**: `tests/` and `tests/fixtures/analysis/` — unit, integration, regression and benchmark fixtures.
- **FILE-013**: `requirements.txt`, frontend manifest, `.env.example`, `README.md` — dependencies/config/docs.

## 6. Testing

- **TEST-001**: Unit-test every index formula against hand-calculated arrays, including zero denominator, negative values and NoData.
- **TEST-002**: Verify Sentinel-2/Landsat/Sentinel-1 band selection, scale factors, masks, alignment and quality flags.
- **TEST-003**: Verify UAV MIME/path/raster validation, CRS reprojection, wavelength mapping and rejection of malformed input.
- **TEST-004**: Verify rule-based flood-scar output on synthetic raster fixture and ensure slope/HAND/stream constraints reduce unrelated bare soil.
- **TEST-005**: Verify API job lifecycle, progress, cancellation, restart recovery, expiration and download manifest.
- **TEST-006**: Verify frontend form validation, map layers, before/after swipe, chart rendering, label editing and export actions.
- **TEST-007**: Run existing tests and targeted analysis tests; compare baseline failures before/after changes.
- **TEST-008**: Run benchmark and security checks with configured size, pixel, concurrency and CORS limits.

## 7. Risks & Assumptions

- **RISK-001**: ΔNDVI/BSI can be caused by harvesting, construction, landslide or seasonal change; output must remain a confidence-ranked candidate layer, not a definitive flood attribution.
- **RISK-002**: Cloud, haze, acquisition angle and temporal mismatch can produce false changes; quality flags and comparable-season pairing are mandatory.
- **RISK-003**: Sentinel-1 backscatter response depends on land cover, incidence angle and moisture; orbit/polarization consistency is required.
- **RISK-004**: Earth Engine quotas and large ROI exports may fail or be slow; asynchronous jobs, limits and retries are required.
- **RISK-005**: DEM/HAND/stream datasets may have coverage, resolution or licensing limitations.
- **ASSUMPTION-001**: Existing FastAPI app remains the backend entry point and current download/timeseries behavior must be preserved.
- **ASSUMPTION-002**: Frontend rebuild can introduce a package manifest/build tool and serve compiled assets through FastAPI without changing deployment ownership.
- **ASSUMPTION-003**: UAV files contain either documented band wavelengths or a user-provided band mapping.
- **ASSUMPTION-004**: Initial classification is deterministic rule-based; ML consumes exported feature/label datasets later.

## 8. Related Specifications / Further Reading

- Google Earth Engine Sentinel-2 SR Harmonized catalog documentation.
- Google Earth Engine Landsat Collection 2 Level-2 catalog documentation.
- Google Earth Engine Sentinel-1 GRD catalog documentation.
- Rasterio dataset, transform and reprojection documentation.
- Existing project modules: `app/main.py`, `app/imagery.py`, `app/timeseries.py`, `static/index.html`.
