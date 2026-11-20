# Thực hiện tiếp kế hoạch matched-frame — 2026-10-25

Đã đối chiếu cả checkout đang mở và hai worktree cũ. Checkout đang mở vẫn ở
baseline 65 test frame/37 test UI; phần triển khai rộng hơn nằm trong
`GEE_frame_repair_worktree`. Phiên này tiếp tục phần đó, không lấy trạng thái
checkbox hoặc báo cáo cũ làm bằng chứng đã hoàn tất.

## Thay đổi đã kiểm tra và hoàn thiện

| Tác vụ | Kết quả |
|---|---|
| 1 — ROI identity | Identity phiên bản 2 từ polygon WGS84 server; snapshot thiếu/sai identity bị chặn trước RPC/export/cache; không nâng cấp lịch sử tự động. Bổ sung chặn tọa độ boolean trong geometry. |
| 2 — Chất lượng/chọn ảnh | Chung ngưỡng phủ search/preflight/export; None/0/NaN và giá trị sai không được chọn ở ngưỡng 0. Giữ chọn ảnh tốt nhất và ngưỡng tương thích 99,9%. |
| 3 — Mask/preflight | Đo AND mask các band của từng nguồn, rồi OR các nguồn trên đúng lưới xuất, dùng cùng nearest transport/warp với exporter. Preflight v2 có quality/can_start/blocking_reason; nhóm thiếu phủ không tạo job. |
| 4 — Preview nhóm | PNG theo đúng nguồn/thứ tự trong preflight, kể cả nhóm bị chặn; mask chọn nguồn dùng mọi band xuất. Cache/TTL/digest/identity được kiểm trước đọc. |
| 5 — TIFF/nguồn/lỗi | Worker ràng buộc digest và danh sách nguồn chính xác; TIFF kiểm phủ hữu hạn chung các band, giữ NaN/zero/Float32/scale. Báo cáo lỗi và manifest giữ nguồn, platform, thời gian và chiến lược export. |
| 6 — ROI từ mẫu | Giữ mẫu/polygon/lưới độc lập với bản nháp ROI thủ công; chuyển ROI dùng lại reference_id; không suy rectangle thay polygon. |
| 7 — Vệ tinh | S1 A/B/C/D, S2 A/B/C; lọc trước tìm/xếp hạng và nguồn bù. Sửa thêm nhánh danh sách S2 ROI thường từng bỏ platform; kiểm lại nguồn khi tải. |
| 8 — Nghiệm thu | HTTP/GEE, artifact và luồng browser thật đạt; bằng chứng browser bổ sung nằm ở phần dưới. |

Lượt live khởi động lạnh phát hiện search dựng EE geometry trước khi vào `_RPC`;
đã chuyển geometry vào scope khởi tạo EE và thêm test hồi quy cho cả S1/S2.
Runner dùng rasterio đã có trong dependencies để tạo fixture EPSG.

## Kiểm tra offline

Lượt chốt sau nghiệm thu browser chạy lại tại checkout chính với `GEE_DOWNLOADS_DIR`
trỏ vào thư mục tạm riêng. Python dùng cùng `.venv`; 8 test browser mới được tính
trong tổng 390 test (lượt trước khi thêm bộ nghiệm thu browser là 382):

```text
python -W ignore -m unittest discover -s tests -p 'test*.py'
Ran 390 tests — OK (skipped=3)

node --test tests/test_matched_frame_ui.cjs tests/test_selection_state.cjs tests/test_rectangle_ui.cjs tests/test_theme.cjs tests/test_admin_map.cjs
91 tests — 91 pass, 0 fail
```

Ba test skip là các test tích hợp tùy môi trường đã có. Bốn ca SAR trong
`test_rectangle_e2e` từng lỗi do fixture giả không có `select`; fixture được sửa
đúng biên `prepare_s1` và thêm kiểm platform qua search→download, không sửa
production code để chiều mock.

## Nghiệm thu live

Lượt `run-6ea0ed7e080c` đã chứng minh S1/S2 EQC phủ 100%, platform đúng ở ROI
từ mẫu và ROI thủ công, nhóm SAR orbit 128 phủ 95,402106% bị chặn ở ngưỡng 99,9%
và tải thành công khi tạo request mới yêu cầu 95%. Lượt đó dừng ở bước fixture
EPSG do runner import `pyproj` chưa cài; không đánh dấu proof này là hoàn tất.

Lượt `run-4d565e50473c` đã hoàn tất với `proof.complete=true` và lưu tại
[`downloads/matched_frame_acceptance/run-4d565e50473c/proof.json`](../../../downloads/matched_frame_acceptance/run-4d565e50473c/proof.json).
Lượt này được tiếp tục sau khi thu gọn fixture S2: vùng 50×30 km vượt ngân sách
`maxPixels=10000000` của phép đo SCL 7 lớp; đổi sang vùng 50×10 km trên ranh hai
tile và ngày 12/10, không nới ngân sách hay hạ ngưỡng. Runner chỉ tiếp tục khi
hash toàn bộ mã app/static không đổi và mọi checksum/độ phủ TIFF đã lưu được
kiểm lại. Proof giữ metadata cả hai tiến trình server và lượt xác minh artifact;
không chạy lại hay sửa các job đã hoàn tất để giả kết quả mới.

| Ca live | Kết quả |
|---|---|
| ROI EQC | GEE bounds 103,957722–104,113719°E; 21,669663–21,815094°N. |
| S1 VV/VH, EQC 1614×1619 | completed; preview/TIFF/ZIP 200; phủ hữu hạn chung 100%. |
| S2 RGB, cùng EQC | completed; preview/TIFF/ZIP 200; phủ hữu hạn chung 100%. |
| S1 với lưới EPSG:32648 | completed; TIFF/ZIP 200; phủ 100%; kiểm CRS/affine/band/Float32. |
| Nhóm S1 18/10 + 06/10, orbit 128, ngưỡng 99,9% | Mask 95,40210618484187%; can_start=false; POST jobs 422 insufficient_selection_coverage. |
| Cùng nhóm, request mới ngưỡng 95% | completed; TIFF/ZIP 200; phủ thực đúng 95,40210618484187%. |
| Hai tile S2 thực sự bù vùng, ngưỡng 99,9% | Mỗi nguồn 60,082866% và 59,597220%; một TIFF cuối phủ 100%; preview nhóm đạt; manifest giữ đúng thứ tự/ngày/platform. |
| Vệ tinh ở ROI từ mẫu/ROI thủ công | S1D và S2B đúng metadata GEE; ROI thủ công tải TIFF 200; ROI từ mẫu tải S1D phủ 100%. |
| S1B không có dữ liệu | Search mới trả total=0; không fallback all. |
| Snapshot cũ còn TTL, geometry/fingerprint bị sửa | 409 stale_search. |
| Preflight can_start bị sửa | 409 stale_preflight. |
| Chỉ có cảnh thiếu phủ | 2 cảnh trong search, không cảnh eligible; preflight 422 invalid_selection. |
| SAR khác quỹ đạo | Preflight 422 incompatible_orbits. |

Hai nguồn bù vùng là
`COPERNICUS/S2_SR_HARMONIZED/20261012T033659_20261012T034959_T48QVK`
và `COPERNICUS/S2_SR_HARMONIZED/20261012T033659_20261012T034959_T48QUK`,
cùng Sentinel-2B. Fixture EPSG:32648 có affine `[100,0,380000,0,-100,2416000]`,
500×100 pixel. Đây là fixture bổ sung; ca âm SAR vẫn giữ nguyên mẫu EQC/ngày/orbit/ngưỡng.

`server.json` ghi PID,
command, port, thời điểm khởi động, revision và hash mã nạp. `proof.json` giữ
search/preflight/job IDs, ordered assets, HTTP status, kiểm grid/coverage, SHA-256
và đường dẫn artifact bền vững. Không replay snapshot cũ cho ca dương. Các TIFF
đều có giá trị hữu hạn biến thiên; ZIP được kiểm CRC và manifest. Review độc lập
không phát hiện thêm lỗi ứng dụng nghiêm trọng sau các bản sửa; các yêu cầu kiểm
thêm phủ chung, provenance và ca âm live của reviewer đã được thực hiện.

## Nghiệm thu browser bổ sung

Mục cuối của checklist đã được thực hiện qua
[`browser-proof.json`](../../../downloads/matched_frame_browser/run-a3f270f8d2ad/browser-proof.json)
với `complete=true`. Người dùng mở trang localhost và bấm **Bắt đầu nghiệm thu**;
harness dùng DOM, Leaflet, File/DataTransfer, fetch và controller thật trong
Chromium 150/Electron 43 của browser tích hợp VS Code. Đây là lượt nghiệm thu
trình duyệt thật được điều khiển bằng script; không phải xác nhận mọi bước đều
được người dùng click thủ công hoặc có kiểm tra screenshot.

Các bước đã đạt:

- Upload mẫu EQC đúng một lần; giữ `reference_id=9ebf28fe-3838-4ef9-a4f7-b2d4223cadb6`.
- So sánh toàn bộ các đỉnh polygon WGS84 trên layer Leaflet thật khi chuyển ROI.
- Tạo ROI thủ công khác rồi dùng lại mẫu; hai bản nháp độc lập, không đổi metadata/lưới.
- Giữ ngày và Sentinel-1D khi đổi mode; search dùng `selection_context=reference_roi`
  và reference_id cũ, kể cả khi trường tọa độ điểm ẩn bị để trống.
- Preflight/xác nhận/job hoàn tất; browser tải TIFF/ZIP 200; quay về mẫu vẫn giữ polygon/lưới.
- Server kiểm lại checksum từ browser với artifact thực, CRC ZIP, CRS/affine/band
  và độ phủ hữu hạn chung 100% trên TIFF.

Search `d94c7aa8-c467-4b2d-982f-e44403f273f6`, preflight
`6ce73490-ce3a-4371-ae3f-222bc259a813`, job
`a8d6fa2c-5fa0-45ce-bcd1-c2a28d9cf6c6`. Hash app/static/runner/harness khớp mã
đã nạp vào tiến trình nghiệm thu. Mọi store nằm trong thư mục run riêng.

Runner mới [validate_matched_frame_browser.py](../../../validate_matched_frame_browser.py)
phục vụ một trang localhost để người dùng mở bằng browser của họ; không khởi
chạy hoặc attach Chrome. Tám test offline kiểm tra các endpoint, thiếu bằng
chứng, kiểm origin/kích thước, lỗi browser, TIFF/ZIP thật và checksum tải sai.
Lần từ chối mở Chrome trước đó (`blocked by policy`) được giữ là lịch sử;
không lặp lại lệnh đã bị chặn. Không gửi request thử đến modelapi và không dùng
subagent trong lượt hoàn thiện này.

## Phạm vi kết luận

Đã xác minh UI do tiến trình nghiệm thu riêng phục vụ, cùng hash với checkout
hiện tại. Không khẳng định một server khác có sẵn đã được reload; chưa nghiệm
thu responsive, mọi trình duyệt, mọi tổ hợp dataset/ngày/band hoặc screenshot.

Mask preflight vẫn là ước lượng. Nội suy S2 hoặc giá trị không hữu hạn có thể
làm TIFF cuối ít phủ hơn mask; final validator tiếp tục chặn và giữ báo cáo lỗi.
Không hạ ngưỡng, đổi nguồn hoặc nới điều kiện SAR tự động.

Mọi test/lượt live của phiên này dùng store riêng. Ledger của worktree cũ
`matched-frame-repair-20261025` ghi một sự cố ở phiên trước: full-suite trên
checkout chính từng gọi lifespan cleanup và xóa record đã hết TTL. Phiên này
không chạy lại cleanup trên production, không sửa lịch sử và không kiểm lại
trạng thái production để biến ghi chú đó thành một kết luận mới.
