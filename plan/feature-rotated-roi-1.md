---
goal: Xoay vùng tải quanh điểm bằng góc nhập và thanh trượt, cắt ảnh theo polygon
version: '1.1'
date_created: '2026-10-15'
last_updated: '2026-10-15'
owner: hoxuanphu
status: 'In progress'
tags: [feature, roi, geospatial, frontend, export]
---

# Introduction

![Status: In progress](https://img.shields.io/badge/status-In%20progress-yellow)

Cho phép xoay khung vuông quanh tọa độ đang chọn. Yêu cầu cập nhật: xoay cả lưới pixel đầu ra theo khung để không tạo các tam giác NoData do bbox bao ngoài; áp dụng cho GEE, Sentinel-2 CDSE/AWS, chuỗi thời gian và ESRI. Ảnh vẫn có thể thiếu dữ liệu do nguồn, mây hoặc ngoài footprint. Đã triển khai backend và giao diện. Kiểm chứng ngày 2026-10-15: toàn bộ 136 test trong thư mục tests đạt bằng unittest; compileall app đạt; JavaScript inline và esri.js kiểm tra cú pháp đạt; git diff --check sạch; HTTP 200 phục vụ rotation_slider. Hai lỗi phát hiện khi mở rộng test đã được sửa và kiểm tra lại. Chưa xác nhận thao tác trình duyệt cuối cùng vì công cụ trình duyệt không có client kết nối; chưa tải ảnh thật từ dịch vụ ngoài. Trạng thái giữ In progress cho đến khi hoàn tất phần kiểm thử trình duyệt của TASK-012.

## 1. Requirements & Constraints

- **REQ-001**: Trong chế độ `point`, thêm ô số `rotation_deg` và thanh trượt `rotation_slider`, cùng miền 0–360°, bước 1°, mặc định 0°. Hai điều khiển đồng bộ; 360° tương đương 0°. Ô số nhận số hữu hạn trong miền; bỏ trống hoặc ngoài miền thì báo lỗi, không gửi request. Có nút `rotation_reset` đặt về 0°.
- **REQ-002**: Góc dương xoay theo chiều kim đồng hồ quanh tâm chọn; 0° giữ khung Bắc–Nam hiện tại. Giữ nguyên tâm và kích thước trong hệ tọa độ mét cục bộ. `radius_m` tiếp tục là nửa cạnh khung vuông.
- **REQ-003**: Bản đồ hiển thị polygon xoay khi kéo thanh trượt hoặc nhập góc; không xoay bản đồ, không tự fitBounds trong mỗi sự kiện kéo. Ô số và slider có label tiếng Việt, đơn vị độ và hỗ trợ bàn phím.
- **REQ-004**: Backend là nguồn hình học chính thức. Mọi luồng cắt quanh điểm dùng cùng polygon; bbox chỉ là bao ngoài để truy vấn cảnh, chọn tile/window và phân lô. Trả `roi_geometry` GeoJSON WGS84 cùng bbox trong phản hồi có vùng chọn. UI không được thay polygon bằng bbox khi nhận kết quả tìm kiếm.
- **REQ-005**: Với point có góc khác 0°, GeoTIFF dùng lưới pixel xoay cùng khung, không xuất bbox Bắc–Nam có các góc trống nhân tạo. Dùng CRS mét cục bộ dạng PROJ `+proj=eqc +lat_ts={lat} +lat_0={lat} +lon_0={lon} +R={111320*180/pi} +units=m +no_defs` để khớp chính xác công thức ROI cục bộ hiện tại; rasterio/GDAL thực hiện đổi CRS. Đặt N=max(1,ceil(2*radius_m/scale_m)), p=2*radius_m/N; width=height=N, không nới footprint để làm tròn pixel. Với t=radians(rotation_deg), affine GDAL/rasterio là `Affine(p*cos(t), -p*sin(t), -r*cos(t)+r*sin(t), -p*sin(t), -p*cos(t), r*sin(t)+r*cos(t))`. Pixel (0,0) là góc trên trái trước xoay; bốn góc ngoài của raster khớp ROI. Lưu CRS và affine đầy đủ; không xoay mảng rồi gán georeference Bắc–Nam. Thiếu dữ liệu thật vẫn giữ NoData/mask; không coi 0 hợp lệ là NoData. Góc 0°/360° và API cũ giữ lưới xuất cũ để tương thích.
- **REQ-006**: Góc phải đi xuyên suốt request tìm kiếm, lập kế hoạch, preview, URL tải, ghép cảnh, snapshot chuỗi thời gian, job ESRI và resume. Thay đổi góc vô hiệu hóa kết quả và kế hoạch chờ chưa chạy; job đã tạo giữ snapshot cũ.
- **REQ-007**: Ẩn điều khiển xoay ở chế độ tỉnh/xã và ESRI bbox nhập riêng; không thay đổi hình học hành chính hoặc các luồng tải nguyên sản phẩm ZIP/SAFE. API cũ không truyền góc hoạt động như 0°. Từ chối góc khác 0° trong request hành chính/bbox riêng bằng lỗi validation rõ ràng.
- **SEC-001**: Từ chối NaN, Infinity, tọa độ/tâm không hợp lệ và polygon vượt [-180,180] kinh độ hoặc [-85.05112878,85.05112878] vĩ độ; không tự wrap qua đường đổi ngày. Không ghi thông tin xác thực vào tên file hoặc metadata.
- **CON-001**: Không thêm thư viện. Dùng math, Leaflet, Pydantic, rasterio và numpy hiện có. Giữ công thức mét cục bộ đang dùng để góc 0° tương thích khung hiện tại; không tuyên bố đây là hình vuông trắc địa chính xác ở mọi vĩ độ.
- **CON-002**: Có thay đổi chưa commit trong README.md, app/esri.py, static/esri.js, static/index.html và tests/test_esri.py tại thời điểm khảo sát. Không ghi đè hoặc hoàn nguyên chúng.
- **PAT-001**: Dùng `rotation_deg` cho góc, `roi_geometry` cho polygon GeoJSON và `bbox` cho bao ngoài. Không truyền polygon tùy ý từ client khi backend có thể dựng từ tâm/bán kính/góc.

## 2. Implementation Steps

### Implementation Phase 1

- **GOAL-001**: Xây dựng hình học dùng chung. Hoàn tất khi test hình học offline chứng minh góc 0° tương thích và các góc xoay có bbox đúng.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | Trong `app/geo.py`, thêm `point_roi(lon, lat, radius_m, rotation_deg=0)` trả `(geometry, bbox)`. Kiểm tra đầu vào hữu hạn, giới hạn bán kính theo API và miền tọa độ. Chuẩn hóa 360 thành 0 sau validation. Lấy bốn đỉnh mét cục bộ `(-r,-r),(r,-r),(r,r),(-r,r)`; với t=radians(góc), tính `east=x*cos(t)+y*sin(t)`, `north=-x*sin(t)+y*cos(t)`, rồi `lon_i=lon+east/(111320*max(cos(radians(lat)),1e-6))`, `lat_i=lat+north/111320`. Đóng vòng GeoJSON; bbox lấy min/max đỉnh. Giữ `point_bbox` và các caller cũ không đổi; nhánh góc 0 dùng bbox hiện tại để giữ tương thích số học. | | |
| TASK-002 | Thêm `tests/test_geo.py` kiểm tra hình học và các trường hợp trong TEST-001, TEST-002. Phụ thuộc TASK-001. Không khởi tạo Earth Engine hay truy cập mạng. | | |
| TASK-014 | Phụ thuộc TASK-001. Thêm `app/raster_grid.py` với `build_rotated_grid(lon, lat, radius_m, rotation_deg, scale_m)` theo REQ-005 và `warp_to_grid(source, destination, grid, categorical=False)` dùng rasterio.warp.reproject theo band/block. Serialize CRS WKT, 6 hệ số affine, width/height và actual_pixel_size_m vào output_grid. Liên tục/RGB dùng bilinear, SCL/Dynamic World dùng nearest từng band; mask nguồn nội suy nearest rồi kết hợp với validity, không biến 0 thành NoData. Tính cửa sổ đọc có halo tối thiểu một pixel nguồn cho bilinear, không clip polygon trước nội suy để tránh viền NoData nhân tạo. Test nguồn hằng phủ đủ cho output hợp lệ toàn bộ. | | |

### Implementation Phase 2

- **GOAL-002**: Cắt đúng polygon trên mọi backend. Phụ thuộc GOAL-001; hoàn tất khi test API và raster tổng hợp xác nhận vùng ngoài khung luôn bị mask.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-003 | Trong `app/main.py`, bổ sung `rotation_deg=0` có validation hữu hạn vào `SceneRequest`, `S1SceneRequest`, `TimeSeriesRequest` và endpoint tải quanh điểm. Mở rộng `_resolve_selection` với tham số tùy chọn cuối chữ ký; tạo EE Polygon từ `point_roi` với `geodesic=False`. Cập nhật toàn bộ caller; giữ nhánh tỉnh/xã hiện có. Thêm `roi_geometry` vào response có ROI và query góc vào URL tải. | | |
| TASK-004 | Phụ thuộc TASK-003 và TASK-014. Trong `app/main.py` và `app/imagery.py`, dùng polygon cho chọn cảnh/coverage/plan; cho `best_scene` và `best_s1_scene` nhận ROI tùy chọn, không chuyển point thành composite hành chính. Preview bản đồ vẫn georeference đúng polygon. Với download point xoay, lấy dữ liệu GEE trên bbox có halo, giữ mask nguồn nhưng không clip polygon trước nội suy, ghép band rồi gọi warp_to_grid một lần ra grid xoay. Không gửi CRS cục bộ tùy biến sang GEE; warp cục bộ bằng GDAL. Duy trì giới hạn chia tile, NoData Dynamic World và đường góc 0 cũ. | | |
| TASK-005 | Phụ thuộc TASK-003 và TASK-014. Trong `app/main.py::s2_bands` và `app/aws_stac.py::merge_all_bands`, thêm `output_grid=None` cuối chữ ký và truyền grid từ `s2_aws_download`/nhánh CDSE. Bbox chỉ dùng đọc nguồn; với góc khác 0 reproject từng band từ nguồn vào grid xoay bằng helper TASK-014, không ghép lên lưới Bắc–Nam rồi warp thêm lần nữa. Kết hợp mask nguồn, giữ 0 hợp lệ và chuyển reflectance đúng như hiện tại. Tạo file riêng theo UUID request, tên tải có tâm/bán kính/góc; không ghi đè request đồng thời. | | |
| TASK-006 | Phụ thuộc TASK-003 và TASK-014. Trong `app/main.py::search_timeseries`, lưu góc, polygon, bbox và output_grid vào snapshot. Trong `app/timeseries.py::TimeSeriesService`, tải nguồn bao ROI bằng `download_geotiff`, sau đó `align_timeseries_raster` warp trực tiếp sang output_grid cho góc khác 0. Không suy ra grid từ ảnh đầu tiên và không chạy thêm bước align Bắc–Nam sau warp. Tất cả thời điểm dùng cùng CRS, affine, width, height đã snapshot; lưu vào metadata ZIP. Snapshot cũ giữ hành vi cũ. | | |
| TASK-007 | Phụ thuộc TASK-001 và TASK-014. Trong `app/esri.py::EsriRequest`, thêm nhánh point lon/lat/radius_m/rotation_deg; validator yêu cầu đúng một nhánh point/bbox/commune, chỉ point nhận góc khác 0. `request_plan` dựng polygon và bbox để chọn tile. Nhánh xoay tải mosaic nguồn đủ bbox và halo, không mask polygon trước nội suy để tránh viền giả, rồi warp theo block sang output_grid bằng TASK-014. Lấy scale_m từ độ phân giải tile 3857 tại tâm, chuyển sang CRS cục bộ bằng tỷ số độ dài đoạn kiểm tra tại tâm; không coi mét Web Mercator là mét mặt đất. Job chỉ completed sau khi đóng file xoay hoàn chỉnh và atomic replace; snapshot/resume giữ grid và góc. Giữ đường tải góc 0 cũ, dọn file trung gian và tính cả dung lượng nguồn/đích trong kiểm tra disk. | | |

### Implementation Phase 3

- **GOAL-003**: Đồng bộ giao diện và request. Phụ thuộc GOAL-001; có thể làm song song với Phase 2 theo hợp đồng trường đã quy định. Hoàn tất khi mọi đường tải point gửi đúng góc và slider không gây request mạng liên tục.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-008 | Trong `static/index.html`, thêm điều khiển REQ-001 gần `radius_m`; thêm `positionRoi()` theo công thức TASK-001, giữ `positionBbox()` làm wrapper trả bbox bao polygon. Thay L.rectangle bằng L.polygon trong `updatePositionMap`. Đồng bộ input/slider qua một handler, throttle render bằng requestAnimationFrame, gọi `updatePositionMap(false)` khi xoay, chỉ fit khi người dùng chọn lại vị trí. Ẩn cả nhóm xoay khi không ở point. | | |
| TASK-009 | Phụ thuộc TASK-008. Trong `static/index.html`, gửi góc trong `opticalSceneBody`, search S1 GEE, `searchTimeSeries`, link S2 CDSE/AWS và mọi link tải point có `radius_m`. Dùng `roi_geometry` trả về để vẽ kết quả thay vì L.rectangle; bbox chỉ dùng fitBounds. Khi đổi góc gọi `invalidateTimeSeries`, xóa pendingCompositeBody và ẩn kết quả/link cũ; không hủy job đang chạy. Chặn thao tác gửi nếu góc đang không hợp lệ. | | |
| TASK-010 | Phụ thuộc TASK-008 và TASK-007. Trong `static/esri.js`, nhánh esriMode=point gửi lon/lat/radius_m/rotation_deg thay cho chỉ bbox. Giữ nhánh bbox riêng và commune không đổi. ESRI job đã chạy tiếp tục hiển thị trạng thái của snapshot cũ khi đổi góc; không gán khung mới là vùng của job cũ. | | |

### Implementation Phase 4

- **GOAL-004**: Kiểm chứng và tài liệu. Phụ thuộc GOAL-002 và GOAL-003; hoàn tất khi toàn bộ test mục tiêu đạt và kiểm thử trình duyệt đạt TEST-006.

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-011 | Bổ sung test mock vào `tests/test_api.py`, `tests/test_imagery.py`, `tests/test_timeseries.py`, `tests/test_esri.py`, `tests/test_esri_api.py`; thêm `tests/test_rotated_roi.py` cho raster tổng hợp và đường tải S2. Kiểm tra tất cả điều kiện TEST-003 đến TEST-005. Không tải ảnh thật hoặc tạo job từ xa trong test tự động. | | |
| TASK-012 | Chạy unittest hiện có cho `tests.test_geo tests.test_rotated_roi tests.test_api tests.test_imagery tests.test_timeseries tests.test_esri tests.test_esri_api` bằng interpreter workspace `.venv`. Không thêm pytest: baseline 78 test đã đạt bằng unittest. Kiểm tra diagnostics các file thay đổi. Dùng app/task VS Code để kiểm thử TEST-006 qua trình duyệt; tải thật chỉ khi có cấu hình/ủy quyền, không đưa credential vào báo cáo. | | |
| TASK-013 | Cập nhật `README.md`: ô góc/slider/reset, chiều xoay, lưới xuất xoay cùng khung, CRS cục bộ và affine, resampling, pixel size thực tế, giữ đường 0° cũ, bbox nguồn lớn hơn nhưng file đích không có tam giác NoData nhân tạo. Phân biệt thiếu dữ liệu thật với NoData do bbox. Nêu phần mềm GIS có thể hiển thị footprint nghiêng đúng địa lý; ZIP/SAFE nguyên sản phẩm không bị crop. | | |

### Execution record — 2026-10-15

- Đã hoàn tất triển khai và test tự động: TASK-001 đến TASK-011, TASK-013 và TASK-014. Mã và test đã lưu trong workspace; không tạo commit.
- TASK-012: unittest discovery đạt 136/136, compileall và kiểm tra cú pháp JS đạt, HTTP phục vụ UI mới đạt. Phần kiểm thử thao tác trình duyệt TEST-006 chưa thực hiện vì browser client ngắt kết nối; tải ảnh thật chưa chạy. Các ô Completed trong bảng để trống cho đến khi nghiệm thu tương tác cuối cùng.
- Luồng xuất xoay giữ mask dữ liệu thật, dùng lưới snapshot chung và có test nguồn phủ đủ không sinh tam giác NoData nhân tạo.

## 3. Alternatives

- **ALT-001**: Chỉ xoay khung bằng CSS/Leaflet nhưng giữ bbox tải: loại vì ảnh tải không khớp khung.
- **ALT-002**: Giữ lưới xuất Bắc–Nam và mask polygon: không chọn theo yêu cầu cập nhật vì sinh các tam giác NoData không cần thiết. Phương án được chọn là warp sang lưới xoay, chấp nhận một lần resampling.
- **ALT-003**: Thêm thư viện projection và định nghĩa khung trắc địa mới: chưa chọn để giữ hình học góc 0 tương thích và không tăng dependency; có thể nâng cấp độc lập nếu cần độ chính xác khảo sát.

## 4. Dependencies

- **DEP-001**: Leaflet hiện có cho polygon; math và Pydantic cho dựng hình/validation; rasterio/numpy cho transform_geom, rasterize và mask. Không thêm dependency runtime.
- **DEP-002**: Earth Engine, CDSE và AWS/ESRI vẫn dùng cấu hình sẵn có; kiểm thử tự động mock các dịch vụ này. Kiểm thử end-to-end thực tế phụ thuộc quyền truy cập và mạng, phải báo riêng nếu chưa chạy.

## 5. Files

- **FILE-001**: `app/geo.py`: dựng polygon và bbox dùng chung; `app/raster_grid.py`: CRS cục bộ, affine xoay và warp dùng chung.
- **FILE-002**: `app/main.py`: schema, giải vùng chọn, trả polygon và đường tải.
- **FILE-003**: `app/imagery.py`: chọn cảnh, coverage, preview và clip GEE.
- **FILE-004**: `app/aws_stac.py`: mask polygon sau ghép band trên lưới cuối.
- **FILE-005**: `app/timeseries.py`: snapshot ROI và mask trên lưới chung.
- **FILE-006**: `app/esri.py`: point request, plan polygon và persistence.
- **FILE-007**: `static/index.html`, `static/esri.js`: điều khiển, hiển thị và truyền góc.
- **FILE-008**: `tests/test_geo.py`, `tests/test_rotated_roi.py`: test mới; các test API/imagery/timeseries/ESRI đã nêu tại TASK-011 được mở rộng.
- **FILE-009**: `README.md`: hướng dẫn và giới hạn.

## 6. Testing

- **TEST-001**: Các góc 0/45/90/180/270/360: tâm không đổi, vòng đóng, 4 cạnh bằng nhau trong mét cục bộ, mọi đỉnh nằm trong bbox. 0/360 cho bbox cũ; 45 có nửa bề rộng/bề cao cục bộ bằng r*sqrt(2). Khung vuông tại 90° phủ cùng vùng với 0° dù thứ tự đỉnh khác.
- **TEST-002**: Từ chối góc âm, >360, NaN/Infinity, bán kính sai, vượt kinh/vĩ độ và qua đường đổi ngày. Không clamp polygon âm thầm.
- **TEST-003**: Mock EE chứng minh polygon xoay được dùng cho coverage, preview, download và composite plan; API cũ thiếu góc vẫn đạt test cũ. Request hành chính khác 0 bị từ chối; request hành chính mặc định không đổi.
- **TEST-004**: Raster nguồn hằng phủ đủ ở WGS84 và CRS projected: các góc 1/30/45/90/180/270/359 có width=height=N, mask hợp lệ toàn bộ kể cả bốn góc; không có tam giác NoData giả. Bốn góc ngoài chuyển sang WGS84 khớp polygon trong sai số 1e-7 độ; affine pixel-center round-trip sai số dưới 1e-6 pixel. Raster phân loại nearest không sinh nhãn mới, giá trị 0 hợp lệ còn nguyên; nguồn có lỗ/ngoài footprint vẫn giữ thiếu dữ liệu thật. Kiểm tra GEE/CDSE/AWS/ESRI, thời gian cùng grid và hai request không ghi đè file. Thêm test hồi quy 0/360 giữ đường cũ.
- **TEST-005**: Snapshot chuỗi thời gian và ESRI giữ nguyên góc/polygon khi đổi UI hoặc resume. Job cũ không có góc vẫn đọc được. Khi bbox tăng tại 45°, kiểm tra giới hạn tile/disk trước tạo job, không bỏ kiểm tra tài nguyên hiện có.
- **TEST-006**: Trình duyệt: nhập 30°, kéo 45°, nhập 360°, reset 0°, sửa tâm/bán kính, chuyển point/tỉnh/xã/ESRI bbox; kiểm tra khung, đồng bộ điều khiển, bàn phím, không giật zoom, không gọi API khi kéo. Inspect payload và URL cho GEE/S2 CDSE/S2 AWS/time-series/ESRI; tìm kiếm không vẽ lại khung Bắc–Nam; invalid input không tạo request. Một ảnh fixture 45° hiển thị mask đúng khi mở bằng rasterio hoặc phần mềm GIS.

## 7. Risks & Assumptions

- **RISK-001**: 45° làm bbox bao tăng diện tích lên khoảng 2 lần; tăng tile, dung lượng và thời gian tải. Giữ giới hạn hiện có, tính dự toán trên bbox bao và báo lỗi trước chạy nếu vượt.
- **RISK-002**: Những nhánh đang dùng điều kiện `province is not None` để clip có thể bỏ qua point xoay; test bắt buộc bao phủ cả preview và file tải, không chỉ schema/UI.
- **RISK-003**: Warp gây nội suy và có thể tạo viền trống nếu nguồn bị clip quá sớm. Đọc bbox nguồn có halo, warp một lần, giữ mask nguồn; categorical dùng nearest. Bbox chỉ là vùng đọc, không là footprint file cuối. Chặn grid quá lớn và warp theo block để giới hạn RAM.
- **RISK-004**: Phép xấp xỉ mét cục bộ sai lệch tăng ở vĩ độ cao/vùng lớn; giữ công thức cũ có chủ đích, giới hạn miền và mô tả rõ trong README.
- **ASSUMPTION-001**: Khung hiện tại là hình vuông, không bổ sung chiều rộng/chiều cao riêng hoặc tay nắm xoay trực tiếp trên bản đồ trong phiên bản này.
- **ASSUMPTION-002**: Người dùng muốn lưới ảnh xoay theo khung, loại bỏ NoData nhân tạo do bbox bao. Không cam kết loại bỏ NoData do nguồn. Phần mềm GIS có thể vẫn hiển thị ảnh nghiêng trên bản đồ Bắc–Nam vì tôn trọng georeference; trình xem theo hàng/cột raster hiển thị ảnh vuông kín khi nguồn phủ đủ.

## 8. Related Specifications / Further Reading

- [Geospatial helpers hiện tại](../app/geo.py)
- [Điều khiển chọn vùng hiện tại](../static/index.html)
- [Kế hoạch chuỗi thời gian GEE](./feature-timeseries-gee-1.md)
- [Rasterio masking](https://rasterio.readthedocs.io/en/stable/topics/masking-by-shapefile.html)
- [Leaflet Polygon](https://leafletjs.com/reference.html#polygon)
