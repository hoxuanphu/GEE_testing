# Kết quả triển khai luồng tải GEE — 18/11/2026

Đã triển khai mã cho snapshot, transport có checkpoint, ghép VRT theo khối và job nền.
Các cờ mới **chưa bật mặc định**: nghiệm thu download đầy đủ ROI lớn và A/B tổng thời gian
vẫn còn mở. Project GEE hiện phát cảnh báo vượt quota tính toán phi thương mại/chế độ hạn chế.
Không đổi dependency, số băng, độ phân giải, lựa chọn RAW hoặc mask mây.

## Phần đã có

- Search có thể trả `selection_token`, pin nguồn native trực tiếp và toàn bộ thứ tự/lớp RAW;
  preview/download cùng graph. SQLite lưu nguyên tử graph, recipe và request.
- Download bỏ probe/discard URL trên đường mới, dùng REST/OAuth riêng, stream 1 MiB,
  tối đa 6 worker/12 future; Session riêng được tái dùng trong từng worker và đóng khi kết thúc.
  Tối đa 3 lần thử/ô; kiểm deadline, response đóng trước retry. Không sửa Session/deadline EE toàn cục.
- Ô hoàn chỉnh được kiểm raster, hash và checkpoint; ô hỏng tải lại. Attempt/generation CAS
  chặn worker cũ, tránh hai request cùng công bố một artifact. Đổi writer giữ lại ô mạng hợp lệ.
- VRT dùng cùng sampling/ưu tiên first-valid như legacy. Hai warp dùng chung giữ nguyên schema;
  đọc từng băng trong từng khối 256 × 256 để giữ đúng đường đọc GDAL cũ, rồi ghi BigTIFF đích một lần.
- Job có queued/running/paused/interrupted/failed/cancelled/completed; pause/resume giữ ô hợp lệ.
  Resume khi đang ghép sẽ ghép lại, không nối mù TIFF dở dang. Restart yêu cầu resume rõ ràng.
- Tệp hoàn tất có URL ổn định, HEAD/ETag/Range; lease giữ tệp suốt response/disconnect.
  Có khôi phục khi crash sau rename nhờ receipt dự kiến được persist trước publication.
- Admission FIFO dùng chung cho bulk GEE, reentrant, vào trước deadline scene annual/frame;
  preview/metadata không chờ bulk. Cleanup bảo vệ attempt/job/file đang dùng, thu hồi lease của
  process đã chết và dọn file tạm mồ côi theo tên/phạm vi quản lý.
- Giao diện theo dõi stage, bytes/ô đã kiểm tra, tạm dừng/tiếp tục/hủy và phục hồi ID sau reload.
  Không báo hoàn tất chỉ vì input đã tải đủ. Tắt tạo job mới vẫn cho tiếp tục job cũ đã lưu.

## Bằng chứng đang dùng

| Kiểm tra | Kết quả | Giới hạn |
| --- | --- | --- |
| Local A/B 5 lượt/mode, cùng SHA input, 12 băng, 3072 × 1536 | Median **41,826 → 13,985 s**, giảm **66,56%** | Ô tổng hợp cục bộ; không phải toàn ROI người dùng hoặc tốc độ GEE end-to-end |
| RSS đỉnh của local A/B | Legacy 760,08 MiB; mới 825,54 MiB | Cả hai dưới 2 GiB; không tuyên bố giảm RAM |
| BigTIFF vật lý, writer hiện tại | **4.804.261.380 byte = 4,474 GiB**, 20 băng, 8192 × 8192 | Tổng hợp local, không phải ảnh vệ tinh tải về |
| Đọc/warp sau mốc 4 GiB | Đúng khối cuối của cả 20 băng, warp đúng phần cuối; RSS 413,75 MiB | Rasters thử lớn đã được dọn; giữ JSON evidence |
| Sentinel-2 thật, ROI nhỏ, 12 băng | 3 lượt cold application cache hoàn tất; cùng snapshot; median 6,673 s | Smoke test; chưa có A/B legacy/live trên ROI lớn, không dùng để nghiệm thu mục tiêu 15% |
| Transport tái dùng Session sau cùng | Một lượt cold ROI nhỏ hoàn tất 5,081 s, 429.300 byte | Smoke test phiên bản transport cuối; không dùng một lượt để kết luận tốc độ |
| Tiếp tục từ dữ liệu Sentinel-2 đã tải | 12 ô dùng lại, **0 RPC pixel**, pixel/mask khớp bit-for-bit | ROI nhỏ |
| Sentinel-2 thật 26 băng, xoay 15° | Toàn bộ mask và Float32 hợp lệ khớp tuyệt đối legacy trên cùng ô; 0 RPC khi ghép lại | ROI nhỏ, không phải khung 83 × 77 km |
| Live RAW regression | Mây gốc/0 hợp lệ/QA thiếu/fallback/coverage giữ đúng | Cảnh tổng hợp chạy trên GEE |
| Live wire NoData | 276 pixel 0 hợp lệ, 253 pixel thiếu; NoData chuẩn hóa thành -9999 | Phát hiện/sửa việc GEE gắn -Infinity dù unmask(-9999) |
| HTTP thực | HEAD, Range/206, disconnect giải phóng lease; offset >4 GiB được kiểm riêng | Fixture HTTP, không gọi GEE |
| Chrome profile tạm | Tạo job, processing chưa complete, pause/resume, reload và link artifact; không lỗi JS | API fixture, không phải full browser download GEE lớn |

Bản VRT từng băng thử trước đó **bị loại** dù benchmark nhanh: live 26 băng xoay phát hiện
sai khác. Thử nghiệm cô lập chứng minh GDAL `read(1)` và `read(all)[0]` trên cùng warp cho
kết quả khác. Writer profile 2 giữ kiểu đọc từng băng trong các warp dùng chung; các ca pixel/mask
khớp tuyệt đối mới được dùng ở bảng trên. Artifact của bản thử cũ không được dùng chung profile mới.

Rà soát độc lập đã tìm và sửa lỗi cache hỏng không được thay thế, dedupe job đã hủy/tệp đã bị
thu hồi, việc chứng nhận lại artifact sai checksum và race xóa job làm chết worker.
Các lỗi đều có ca RED tái hiện và GREEN sau sửa. Không hạ ngưỡng so sánh hay bỏ kiểm tra mask.

## Gate còn mở và triển khai

- Full download chính ROI `106.4437867045,10.076415139`, `83328,84 × 76948,82 m`,
  `2023-01-18..2023-05-17`, với 12/26 băng và xoay; kiểm provenance/halo/thumbnail theo khung này.
- A/B cùng snapshot trên ROI lớn: ít nhất 3 lượt live cold/warm, browser delivery và mục tiêu
  giảm median end-to-end ≥15%; case nhỏ không chậm quá 10% so legacy. Smoke test nhỏ không thay gate này.
- Tuning workers/compression/layout theo ma trận kế hoạch chưa chạy; giữ mặc định an toàn,
  không tăng worker hoặc đổi compression dựa trên suy đoán.
- Full resource acceptance của ROI lớn 12/26 băng còn mở; BigTIFF tổng hợp chỉ chứng minh
  khả năng ghi/đọc lớn và bộ nhớ trong bài thử đó.

Cách bật từng cờ nằm trong [README](../../../README.md#luồng-tải-sentinel-2-có-checkpoint-đang-kiểm-chứng).
Không ép khởi động lại server; chưa bật rollout mặc định. Không tạo branch, commit hoặc push.

Evidence: [so sánh local](../../../downloads/download-performance/comparison.json),
[BigTIFF hiện tại](../../../downloads/download-performance/physical-big-v2/result.json),
[native 26 băng xoay](../../../downloads/download-performance/live-26-rotated/report.json),
[live 12 băng](../../../downloads/download-performance/live-small-ca/summary.json),
[resume](../../../downloads/download-performance/live-small-ca/resume-report.json),
[Chrome](../../../downloads/download-performance/browser/report.json).
Lịch sử quyết định/RED/GREEN và lệnh kiểm tra nằm trong [ledger](../../../.superpowers/sdd/2026-11-17-gee-download-performance/progress.md).

Kiểm thử sau cùng: **596 bài Python được chạy, 3 bài tùy chọn bỏ qua, không lỗi**;
**152 bài Node đạt**; Chrome fixture đạt. `git diff --check` không báo lỗi whitespace.
