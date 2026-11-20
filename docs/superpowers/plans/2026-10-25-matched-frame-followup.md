# Kiểm tra và hoàn thiện phần còn thiếu — 2026-10-25

Đã đối chiếu kế hoạch sửa với checkout hiện tại, chạy lại test và review độc lập.
Các tác vụ 1–8 đã có triển khai và bằng chứng nghiệm thu. Phát hiện một thiếu sót
ở tác vụ 7: tải từng cảnh với `platform=null` giữ đúng bộ lọc “Tất cả”, nhưng
manifest chưa lưu vệ tinh thực của từng cảnh.

## Phần đã bổ sung

- `app/frame_export.py`: report của ảnh đơn thành công hoặc lỗi kiểm tra TIFF
  giữ `platform` từ scene snapshot; manifest cho phép trường này.
- `app/frame_jobs.py`: manifest lấy platform từ từng nguồn bất biến, kể cả nguồn
  tải lỗi chưa có report. Platform ở cấp sản phẩm vẫn là bộ lọc đã yêu cầu;
  `reports[i].platform` là vệ tinh thực của cảnh tương ứng.
- Thêm ba test hồi quy: exporter thành công, exporter không đạt dữ liệu/độ phủ,
  và worker tải các cảnh S2A/S2C thành công cùng một cảnh S2B lỗi. Test worker
  dùng exporter thật với biên GEE giả, kiểm SQLite mở lại, manifest và ZIP.

Các test mới đã thất bại do thiếu platform trước khi sửa. Sau sửa, 26 test
exporter và 17 test worker đạt. Review độc lập chấp thuận cả yêu cầu và chất lượng
bản sửa, không phát hiện thêm vấn đề cụ thể trong phạm vi đã kiểm.

## Kiểm tra mới trong lượt này

```text
python -W ignore -m unittest discover -s tests -p 'test*.py'
Ran 393 tests — OK (skipped=3)

node --test tests/test_matched_frame_ui.cjs tests/test_selection_state.cjs tests/test_rectangle_ui.cjs tests/test_theme.cjs tests/test_admin_map.cjs
91 tests — 91 pass, 0 fail

git diff --check
exit 0; có thông báo CRLF hiện hữu
```

Trước khi sửa, bộ Python có 390 test và cũng đạt (3 skip). Mỗi tiến trình chạy
full-suite đặt `GEE_DOWNLOADS_DIR` vào thư mục tạm riêng trước khi import app.
Ba skip là các test tích hợp phụ thuộc môi trường. Bộ JS chạy trước bản sửa
Python; lượt này không sửa mã giao diện. Các tệp frame đang untracked từ trước,
nên `git diff --check` không thay thế review nội dung; bản sửa được review bằng
diff so với bản sao trước chỉnh sửa và đọc các test mới.

## Bằng chứng live đã được kiểm lại

Đã đọc lại proof HTTP/GEE `run-4d565e50473c` và browser `run-a3f270f8d2ad`.
Trước bản sửa nhỏ này, 36 hash mã trong mỗi `server.json` khớp checkout.
Đã kiểm lại SHA-256 của tám TIFF live, grid/độ phủ của các TIFF đồng khung,
CRC/manifest/nội dung TIFF trong ZIP của sáu ca đồng khung, và checksum/grid/
độ phủ/CRC/manifest của artifact browser trong kho nghiệm thu riêng.

S1/S2 đơn và nhóm S2 bù vùng vẫn có độ phủ chung 100% trong artifact đã lưu.
Nhóm SAR giữ nguyên ngưỡng 99,9% có proof `can_start=false`, POST job 422 và
mask 95,40210618484187%; request riêng ngưỡng 95% có TIFF phủ đúng giá trị đó.
Proof cũ cũng xác nhận lỗi provenance: nguồn S2 ghi `Sentinel-2B`, trong khi
manifest của cảnh đơn chưa có platform thực.

Đây là xác minh lại bằng chứng lịch sử bằng thao tác đọc; lượt này không gọi
GEE hoặc chạy browser mới. Hai tệp app vừa sửa làm hash khác proof cũ, vì vậy
proof cũ không được coi là nghiệm thu live của trường platform mới. Trường mới
đã được kiểm qua exporter/worker/SQLite/ZIP trong test offline nêu trên.

Giữ nguyên proof, job và artifact lịch sử. Không reload server có sẵn, không
commit/merge/push và không chạy cleanup trên kho production. Các giới hạn
nghiệm thu browser/ước lượng mask trong báo cáo thực hiện trước vẫn áp dụng.
