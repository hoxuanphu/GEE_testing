# Đếm ảnh theo ROI và khoảng ngày

## Mục đích

Thêm nút **Đếm ảnh** riêng để biết có bao nhiêu cảnh trong nguồn ảnh đang chọn giao ROI và được chụp trong khoảng ngày đã chọn (bao gồm ngày cuối). Người dùng đã chọn cách bấm nút riêng thay vì tự truy vấn mỗi lần đổi ROI/ngày.

## Hành vi

- Chỉ đọc metadata/số lượng, không tạo preview, tải raster hoặc khởi chạy job.
- Không áp dụng bộ lọc mây hay tự chọn cảnh ít mây: tính cả cảnh 100% mây và cảnh thiếu metadata mây. Giữ bộ lọc vệ tinh, sản phẩm/phân cực khi có.
- Dùng đúng ROI hiện hành, gồm khung xoay và footprint mẫu; không âm thầm đổi sang tâm hoặc bao ngoài. Ghi rõ cảnh giao ROI có thể chỉ phủ một phần.
- Hiển thị tổng số cảnh, nguồn và khoảng ngày. NICFI được ghi là composite theo kỳ. Nguồn không hỗ trợ đếm phải thông báo rõ; không dùng nguồn khác thay thế.
- Kết quả cũ bị xóa khi đổi ROI, ngày, nguồn hoặc bộ lọc có ảnh hưởng. Hủy/bỏ qua phản hồi cũ khi lựa chọn đã thay đổi.
- Dùng truy vấn đếm của nguồn nếu có. Không lấy số dòng của một trang/giới hạn catalogue làm tổng đầy đủ. Nếu phải phân trang, chỉ công bố tổng chính xác sau khi duyệt hết; lỗi/giới hạn được báo rõ.
- Tổng là số cảnh trong catalogue, không phải cam kết mọi lần tải đều thành công hay mọi cảnh phủ kín ROI. Không thay đổi điều kiện chọn/tải hiện có.

## Các cách đã cân nhắc

1. **Nút Đếm ảnh riêng — người dùng chọn:** tránh truy vấn liên tục khi vẽ/chỉnh ROI; không ảnh hưởng nút tìm/tải hiện có.
2. **Tự động đếm khi đổi lựa chọn:** giảm một thao tác nhưng phát sinh nhiều truy vấn và cần debounce khi chỉnh ROI; không chọn.

## Phạm vi triển khai đề xuất

API đếm độc lập, các adapter theo nguồn ảnh hiện có, điều khiển giao diện riêng, kiểm tra hồi quy cho bộ lọc/ROI/ngày và phản hồi cũ. Tận dụng cách dựng ROI, chọn collection và vệ tinh hiện có. DEM/bản đồ nền tĩnh không có số cảnh theo ngày. Chưa thêm biểu đồ, ước lượng dung lượng hoặc tải hàng loạt.

## Bổ sung đã được người dùng yêu cầu

Cho tải danh sách CSV (UTF-8 BOM để mở trong Excel) gồm tên ảnh, ngày chụp, timestamp UTC, mã cảnh, nguồn và % mây nếu có. Nút đếm lấy metadata đầy đủ và lưu snapshot bất biến; CSV lấy từ chính snapshot này để số dòng khớp số đếm, không truy vấn lại theo form hiện tại. Cảnh thiếu metadata mây vẫn có dòng trong CSV. Không tải pixel ảnh khi đếm hay xuất danh sách.

## Kiểm chứng

Kiểm tra offline: ảnh mây/thiếu metadata không bị loại; ngày cuối được tính; hình chữ nhật xoay/footprint mẫu được giữ; tổng không bị giới hạn theo số dòng của trang; lựa chọn đổi trong khi truy vấn không hiện số cũ; nút đếm không gọi chức năng xuất raster. Kiểm tra nguồn trực tiếp nếu môi trường và thông tin truy cập cho phép, ghi rõ phần chưa kiểm chứng.
