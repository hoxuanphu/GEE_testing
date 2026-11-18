# Kiểm tra lại tính năng đồng khung GeoTIFF — 2026-10-25

**Kết luận:** Lỗi gốc tìm nhầm vùng do diễn giải CRS EQC và lỗi tải trực tiếp CRS tùy chỉnh đã được xử lý trong mã hiện tại. Tìm mới và tải một cảnh thành công với Sentinel-1 VV/VH và Sentinel-2 RGB trên chính lưới mẫu EQC. Tuy nhiên, luồng ghép vẫn có trường hợp kiểm tra trước tải chấp nhận nhưng tải thất bại vì thiếu độ phủ; snapshot chưa có cơ chế nhận diện phiên bản ROI cũ.

Đây là báo cáo kiểm tra, không phải thực hiện phương án sửa. Không sửa mã ứng dụng hoặc test hiện có; chỉ tạo script chẩn đoán và artifact trong kho kiểm tra riêng.

## 1. Phạm vi và phương pháp

- Đọc lại mã hiện tại; không dùng kết luận ngày 2026-10-23 làm trạng thái hiện hành.
- Chạy 65 test `test_frame*.py`, 5 test `test_reference_frame.py`, 37 test giao diện: tất cả qua.
- Gọi GEE thật; gọi các route FastAPI bằng TestClient nạp trực tiếp mã hiện tại, với SQLite và thư mục output riêng.
- Dùng metadata CRS/affine/kích thước mẫu lưu trong lịch sử; upload một TIFF tổng hợp có cùng georeference để đi qua route upload. Không dùng pixel của ảnh mẫu làm dữ liệu đầu ra.
- Không thấy tiến trình Python server khi kiểm tra; không kết nối được 127.0.0.1:8000. Kết quả dưới đây xác nhận mã nạp trong tiến trình kiểm tra, chưa xác nhận một server/UI đang phục vụ ngoài tiến trình đó.
- Snapshot lịch sử nguồn được mở SQLite read-only, hash trước/sau không đổi.

## 2. Bằng chứng luồng tìm và tải một cảnh

GEE nhận bounds ROI WGS84:

```text
longitude: 103.95772232469126 .. 104.11371934665476
latitude:   21.669663252874575 .. 21.815093541044025
```

ROI ở đúng vùng khoảng 21,7 độ Bắc, không còn quanh vĩ độ 0 như lỗi gốc. `_frame_roi()` hiện chuyển qua `reference_footprint()` rồi truyền EPSG:4326 cho GEE.

| Kiểm tra | Sentinel-1 | Sentinel-2 |
|---|---|---|
| Khoảng ngày | 2026-10-01 đến 2026-10-25 | 2026-10-01 đến 2026-10-25 |
| Ngày cảnh được chọn | 2026-10-23 | 2026-10-12 |
| Band đã tải | VV, VH | B4, B3, B2 |
| Job | `3ad16cb7-de98-405d-80ec-fd5efcd14698` | `e5b44146-db48-49ea-a3cf-33c0263a2c4a` |
| Trạng thái | completed | completed |
| Preview/TIFF/ZIP HTTP | 200 / 200 / 200 | 200 / 200 / 200 |
| Kiểm tra TIFF | verified=true | verified=true |
| Độ phủ thực từng band | 100% / 100% | 100% / 100% / 100% |
| Lưới | EQC mẫu, 1614×1619, affine mẫu | EQC mẫu, 1614×1619, affine mẫu |
| Chiến lược | EPSG:32648 trung gian → warp về mẫu | EPSG:32648 trung gian → warp về mẫu |

Đã mở lại TIFF, kiểm CRS/affine/kích thước/band/Float32 qua `validate_frame_export()`. Các band có giá trị biến thiên, không phải ảnh hằng hoặc toàn NoData. ZIP đọc được, kiểm CRC không lỗi và có manifest cùng TIFF.

Nguồn thực:

```text
COPERNICUS/S1_GRD/S1D_IW_GRDH_1SDV_20261023T111348_20261023T111413_005142_009BF4_9FF7
COPERNICUS/S2_SR_HARMONIZED/20261012T033659_20261012T034959_T48QUK
```

## 3. Vấn đề còn lại đã kiểm tra

### A. Nhóm ghép thiếu độ phủ chỉ bị phát hiện sau khi tải

Tái hiện bằng API hiện tại, không sửa eligibility trên kết quả tìm:

1. Search S1, khoảng ngày như trên, cùng hướng ASCENDING, relative orbit 128.
2. Bật `merge_scenes=true`, đặt `min_coverage_pct=99.9`, `require_full_coverage=false`.
3. Chọn hai nguồn ngày 2026-10-18 và 2026-10-06, phủ ước lượng lần lượt 95.7617% và 95.4995%.
4. Preflight trả HTTP 200, chưa có phép đo `selection_quality` trên hợp dữ liệu.
5. Job `68b3d7ae-9afb-4209-9fbe-b8bec7324e6d` tải nguồn, ghép và đo phủ thực.
6. Ảnh cuối chỉ phủ **95.402106%** trên VV/VH; job failed với `insufficient_coverage`, không có download URL.

Exporter kiểm tra đúng ngưỡng; hai nguồn trùng phần thiếu không tự cộng thành đủ khung. Hạn chế hiện tại là preflight chưa đánh giá hợp mask nên người dùng vẫn có thể chọn/xác nhận rồi chờ tải mới biết nhóm không đạt. Ưu tiên bổ sung phép đo nhóm trên ROI trước tải như tác vụ 3 của phương án.

### B. Chưa phân biệt snapshot tạo bằng phiên bản ROI cũ

Trong kho kiểm tra, sao chép nguyên snapshot cũ từng tìm sai vùng vào một record mới, không sửa parameters/scenes: preflight vẫn trả HTTP 200 dù không có `roi_version`/`roi_fingerprint`.

Đây là kiểm tra logic của snapshot còn hiệu lực, không phải khẳng định snapshot lịch sử gốc vẫn còn TTL sau hai ngày. TTL 24 giờ có thể chặn record gốc vì hết hạn; nó không thay thế việc kiểm phiên bản xử lý đối với snapshot còn hạn khi nâng cấp server. Cần thêm chặn ở preflight/start/worker, giữ history nguyên trạng.

### C. Ngưỡng phủ 0 cho phép hàng có độ phủ không xác định

Gọi `_scene_row()` với `require_full_coverage=false`, `min_coverage_pct=0`, coverage=None trả `eligible=true`. Đây là ca kiểm tra logic, chưa phải một cảnh GEE thực tế gặp trong lượt này. Exporter vẫn từ chối TIFF toàn NoData; nên loại hàng không xác định hoặc không có dữ liệu từ bước chọn để tránh lỗi muộn.

### D. Preview nhóm chưa có

API hiện có preview theo từng scene token; `make_frame_preview()` chỉ dựng một asset. Preflight chưa có endpoint/đồ thị preview của toàn bộ nguồn ghép theo thứ tự ưu tiên. Preview từng cảnh hoạt động; chưa thể coi nó là bằng chứng ảnh ghép cuối trông như thế nào.

## 4. Giới hạn kết luận

- Live đã kiểm một mẫu EQC, một cảnh S1 VV/VH, một cảnh S2 RGB và một nhóm SAR thiếu phủ. Chưa nghiệm thu live preset S2 đủ 12 band, mọi CRS, mọi ngày hay mọi vùng.
- Không chạy thử tương tác trong browser. Giao diện được kiểm bằng 37 test Node hiện có.
- Đường EPSG trực tiếp có test offline; ca live của lượt này dùng lưới mẫu EQC và warp trung gian.
- Không restart server và không sửa lỗi trong lượt kiểm tra này.

## 5. Artifact và lệnh tái hiện

- [Bằng chứng JSON](../../../downloads/matched_frame_audit/run-831314d4b2a8/proof.json)
- [TIFF Sentinel-1](../../../downloads/matched_frame_audit/run-831314d4b2a8/s1gee-download.tif)
- [TIFF Sentinel-2 RGB](../../../downloads/matched_frame_audit/run-831314d4b2a8/sentinel2-download.tif)
- [Runner tìm–tải](../../../downloads/matched_frame_audit/check_current.py)
- [Runner tái hiện ghép thiếu phủ](../../../downloads/matched_frame_audit/check_mosaic.py)
- [Phương án triển khai](2026-10-25-matched-frame-repair.md)

```powershell
.venv/Scripts/python.exe -W ignore -m unittest discover -s tests -p 'test_frame*.py'
.venv/Scripts/python.exe -W ignore -m unittest discover -s tests -p 'test_reference_frame.py'
node --test tests/test_matched_frame_ui.cjs
.venv/Scripts/python.exe -W ignore downloads/matched_frame_audit/check_current.py
.venv/Scripts/python.exe -W ignore downloads/matched_frame_audit/check_mosaic.py
```

Runner đầu tạo thư mục run mới. Runner nhóm dùng run có proof mới nhất; phải chạy tuần tự. Mỗi lần chạy live sẽ tạo search/job/output mới trong kho audit, không dùng kho matched_frame đang phục vụ người dùng.
