# Time series: tìm và tải tuần tự từng năm

Trạng thái: người dùng đã duyệt và yêu cầu triển khai ngay ngày 2026-11-13. Builder check độc lập không còn câu hỏi quan trọng; các mặc định được giao cho builder được chốt trong implementation plan.

## Yêu cầu và quyết định đã xác nhận

Người dùng muốn tải lượng lớn ảnh qua time series, chia theo năm và không tìm/tải toàn bộ khoảng nhiều năm cùng lúc để giảm timeout/quá tải. Người dùng chọn **tự động chạy lần lượt từng năm**, thay vì chọn chạy từng năm bằng tay.

Tính năng tải raster thật, khác yêu cầu trước đó chỉ xuất CSV danh sách theo năm. Mỗi cảnh giữ một GeoTIFF riêng; không biến các cảnh thành composite năm.

## Hiện trạng đã kiểm tra

- `app/main.py:search_timeseries` tìm đồng bộ toàn khoảng ngày trước khi tạo snapshot.
- `app/imagery.py:list_timeseries_scenes` lấy danh sách bằng một lần `getInfo`; phân trang UI chỉ phân trang snapshot đã tìm xong.
- `app/timeseries.py` đã có SQLite, một worker và xuất từng cảnh tuần tự, nhưng tạo một ZIP cho toàn bộ kết quả.
- Bộ ghép band/tile trong `imagery.py` có tối đa 6 luồng tải bên trong một cảnh. Một worker chưa có nghĩa chỉ một kết nối mạng.
- Restart đánh dấu job gián đoạn, chưa tiếp tục từ checkpoint từng cảnh. Kết quả không hoạt động bị dọn sau 24 giờ.
- Đã có ROI điểm/chữ nhật/vector, grid chung, kiểm tra đĩa, retry lỗi mạng và manifest. Giữ các hợp đồng này.

## Các cách đã cân nhắc

1. **Hàng đợi nền tự chạy từng năm — người dùng đã chọn.** Không phụ thuộc tab; có tiến độ, kết quả và khôi phục theo năm/cảnh. Cần thêm trạng thái batch và checkpoint.
2. **Người dùng chạy từng năm bằng tay.** Ít thay đổi hơn nhưng phải thao tác lặp và tự theo dõi năm đã tải.
3. Chỉ chia ZIP sau khi tìm mọi năm hoặc dùng vòng lặp trong trình duyệt không đáp ứng mục tiêu: bước tìm vẫn lớn hoặc việc điều phối phụ thuộc tab. Không chọn.

## Luồng xử lý

1. Bật time series, chọn nguồn/ROI/khoảng ngày và chế độ **Tải lần lượt theo năm**.
2. Hiển thị các khoảng năm từ tham số ngày tại chỗ, chưa gọi catalogue để đếm tất cả năm. Nút **Bắt đầu tải theo năm** tạo batch và trả mã ngay.
3. Backend chụp cố định tham số, hình học ROI và grid phù hợp. ROI ID dùng khi tạo batch; các bước sau dùng geometry snapshot để không phụ thuộc thời hạn import.
4. Xử lý năm tăng dần: tìm metadata của năm hiện tại → cố định danh sách → tải lần lượt từng cảnh → ghi manifest/CSV → công bố ZIP năm. Sau đó mới tìm năm tiếp theo.
5. Giao diện hiển thị bảng theo năm và tiến độ năm đang chạy. Năm chưa tìm hiển thị số ảnh **chưa biết**, không dùng 0 hoặc tổng giả.
6. Kết quả năm hoàn tất tải được ngay, không đợi hết batch. Đóng tab không dừng worker; mở lại trang có thể khôi phục theo batch ID được lưu cục bộ và kiểm tra trên server.

Ví dụ `2018-01-13` đến `2024-12-31`: năm đầu chỉ `2018-01-13..2018-12-31`, các năm giữa trọn năm, năm cuối đến ngày đã chọn. Khoảng trong một năm tạo một mục. Bao gồm ngày cuối; không trùng cảnh do ranh giới ngày/năm hoặc thử lại.

## Giới hạn truy vấn và tải

- Áp dụng các nguồn time series GEE hiện có: Sentinel-2, Landsat, Sentinel-1, NICFI, Dynamic World. Không mở rộng sang CDSE/AWS hoặc S5P trong thay đổi này.
- Tái sử dụng quy tắc chọn cảnh của time series: điểm cần cảnh chứa tâm; chữ nhật/vector dùng geometry đã áp dụng. Không tự thêm lọc mây, chọn best scene hoặc composite. Giữ vệ tinh, phân cực và band đã chọn.
- Tìm danh sách metadata trên collection gốc, không chuẩn bị band ảnh chỉ để liệt kê. Lưu full native asset ID, thời gian và source metadata cần cho manifest.
- Mỗi năm tìm metadata theo cửa sổ tháng, tuần tự, vẫn chỉ lưu/hiển thị kết quả thuộc năm đó. Nếu một cửa sổ timeout/vượt trần, chia đôi đến ngày; một ngày vẫn lỗi thì báo năm chưa lấy đủ, không coi danh sách thiếu là hoàn tất và không bắt đầu tải từ danh sách đó.
- Đề xuất mặc định của agent: tối đa 20.000 cảnh/năm; deadline đơn điệu 120 giây mỗi cửa sổ, tối đa 3 lần thử cho lỗi tạm thời trong cùng ngân sách. Transport có timeout riêng theo phép gọi, không đổi deadline GEE toàn ứng dụng. HTTP tạo batch không chờ truy vấn này.
- Toàn ứng dụng chỉ có một lượt xuất time series đang xử lý, dùng chung quyền chạy cho luồng cũ và batch mới. Không tạo một executor riêng chạy song song ngoài giới hạn cũ. Batch sau có thể trả busy rõ ràng thay vì âm thầm chạy đồng thời.
- Trong batch: một năm, một cảnh tại một thời điểm; tối đa 2 kết nối band/tile trong cảnh. Tham số giới hạn tải chỉ áp dụng batch mới, giữ mặc định tương thích cho luồng khác.
- Có ngân sách tổng cho từng thao tác mạng, retry có backoff cho 429/5xx/lỗi kết nối; lỗi dữ liệu/quyền không thử vô hạn. Giới hạn chính xác của tải raster do builder xác định theo transport hiện có và ghi trong plan; không được chỉ thêm timeout cho vòng poll UI.
- Kiểm tra đĩa trước cảnh và trước đóng ZIP; thiếu đĩa dừng batch an toàn, giữ kết quả đã hoàn tất. Giữ nguyên độ phân giải/bands; không tự giảm chất lượng để tải tiếp.

Chia năm giảm phạm vi thao tác nhưng không bảo đảm mọi ROI lớn hoặc nguồn lỗi sẽ tải thành công. Tiến độ phải phân biệt chờ, đang tìm, đang tải, đóng gói, thành công, thành công một phần, không có cảnh và lỗi.

## Lưu trữ và đầu ra

- Tái sử dụng SQLite/service của time series, mở rộng có migration tương thích để lưu batch, năm và checkpoint từng cảnh. Builder quyết định bố cục module; tránh sao chép toàn bộ bộ xuất cũ.
- Mỗi batch có UUID; mỗi năm có thư mục riêng trong `downloads/timeseries`, GeoTIFF có tên ổn định từ dataset/thời gian/asset ID.
- Mỗi năm công bố `timeseries_<dataset>_<year>.zip`, gồm TIFF thành công và `manifest.json`/`manifest.csv`; CSV metadata năm cũng có link riêng. Các file CSV xuất dùng quy tắc an toàn bảng tính hiện có.
- Không có ZIP gộp mọi năm. Tải GEE xuống ổ đĩa máy chạy backend diễn ra tự động; lưu ZIP về máy trình duyệt là link tải riêng từng năm, không tự mở hàng loạt hộp thoại download.
- Không có cảnh: đánh dấu năm empty, cung cấp CSV có header, không ZIP ảnh rỗng. Năm partial công bố ZIP chỉ chứa TIFF thành công, số lỗi/manifest phải rõ.
- Công bố TIFF/ZIP bằng rename atomic từ file tạm. Không đưa TIFF chưa hoàn tất vào ZIP.
- Giữ grid chung của batch xuyên các năm: dùng grid xác định từ ROI nếu có; nếu phải lấy từ cảnh thành công đầu tiên thì lưu bền trước khi sang cảnh/năm sau. Giữ mask polygon/lỗ, giá trị 0 hợp lệ và NoData qua căn chỉnh cuối.

## Gián đoạn, dừng và thử lại

- Nút **Tạm dừng / Tiếp tục** và **Thử lại phần lỗi**. Tạm dừng có hiệu lực tại điểm an toàn giữa phép gọi/cảnh; không hứa hủy ngay một RPC hoặc GDAL đang chạy.
- Checkpoint ghi sau khi TIFF hoàn tất, lưu ID cảnh, file và grid. Tiếp tục bỏ qua file thành công sau khi kiểm tra tồn tại/cấu trúc raster; file thiếu/hỏng phải tải lại, không tính thành công chỉ vì có checkpoint.
- Nếu app restart, batch active thành interrupted và chờ người dùng Tiếp tục; không tự khởi chạy tải ngoài ý muốn khi mở app. Không tìm lại năm đã có danh sách đầy đủ.
- Lỗi một cảnh: ghi nhận rồi tiếp tục cảnh sau; hoàn tất năm partial và sang năm tiếp theo. Lỗi tìm danh sách cả năm: ghi nhận năm failed, chuyển năm sau, để thử lại năm lỗi sau. Lỗi toàn cục như hết đĩa/quyền xác thực dừng batch, tránh lặp cùng lỗi trên mọi cảnh/năm.
- Khi thử lại, chỉ chạy năm/cảnh còn thiếu/lỗi. Các ZIP đã công bố giữ dùng được; ZIP năm đang cập nhật chỉ thay thế sau khi bản mới hoàn tất.
- Dọn sau 24 giờ không hoạt động chỉ khi batch terminal; không dọn năm đã xong trong lúc các năm sau còn chạy hoặc batch đang paused/interrupted chờ tiếp tục. Chính sách dọn batch paused/interrupted lâu dài cần hành động xóa rõ ràng hoặc hạn riêng được ghi cụ thể trong plan, không giữ vô hạn ngầm.

## Tương thích và phạm vi

- Giữ API/UI time series tìm một lần hiện có; thêm chế độ riêng và API batch riêng, không âm thầm đổi nút Tìm thành tải raster.
- Đổi ROI/nguồn/ngày sau khi tạo batch không đổi snapshot của batch đó. Không tự tải gì khi bật checkbox hoặc nhập vector.
- Triển khai local với một process ứng dụng; không tuyên bố hỗ trợ nhiều replica/người dùng công khai.
- Không thay đổi workflow CSV đếm ảnh hiện có, không tải kho nhiều ROI, không xuất nền lên Drive/Cloud Storage, không triển khai hệ thống quota phân tán.
- Không commit/push, không tạo branch/worktree mới, giữ nguyên các thay đổi đang có.

## Kiểm chứng bắt buộc

- Chia năm/tháng/ngày đúng ngày nhuận, đầu/cuối khoảng, UTC và khoảng một ngày; không bỏ/trùng cảnh.
- Chứng minh chưa gọi tìm năm sau khi năm trước chưa kết thúc; không liệt kê mọi năm trước khi tạo batch.
- Worker dùng chung giới hạn với job cũ; giới hạn kết nối thực tế, deadline/retry/backoff và danh sách không đầy đủ.
- Khôi phục checkpoint sau pause/restart, bỏ qua TIFF thành công, tải lại file thiếu/hỏng; không mất grid/snapshot ROI.
- Empty/partial/failed, hết đĩa, cleanup bảo vệ năm đã hoàn tất, publish ZIP atomic và CSV an toàn.
- TIFF thật nhỏ có lỗ/phần rời, NoData và pixel 0; nhiều năm cùng grid.
- Node/browser: điều khiển batch, tiến độ chưa biết, khôi phục theo ID, link mỗi năm và thay selection không sửa batch đang chạy.
- Chạy toàn bộ Python/Node phù hợp; live GEE chỉ smoke test ít cảnh/nhiều năm ngắn hoặc fixture chia ranh giới, không tự tải hàng trăm ảnh để kiểm thử.

## Các mặc định cần builder ghi rõ trong implementation plan

- Ngân sách tải raster, số byte/pixel/tile/cảnh và thời hạn giữ batch paused/interrupted (đề xuất 7 ngày không hoạt động).
- API routes/models, migration và checkpoint transactions; cách dùng chung slot worker với luồng cũ.
- Handoff giữa metadata chỉ đọc và bộ xuất có hạn mạng; không thay transport toàn cục hay làm yếu test hiện có.
