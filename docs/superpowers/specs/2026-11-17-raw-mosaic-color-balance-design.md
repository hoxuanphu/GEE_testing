# Cân bằng màu cho ảnh ghép RAW

Ngày: 2026-11-17. Trạng thái: người dùng đã duyệt; đã triển khai và kiểm tra offline/GEE giả lập/Chrome. Nghiệm thu trực quan trên ROI/ngày thực tế còn mở.

## Mục tiêu và lựa chọn của người dùng

Người dùng thấy các mảng lệch màu trong cả preview và TIFF của chế độ RAW và đã chọn ưu tiên ảnh ghép đều màu, chấp nhận hiệu chỉnh giá trị pixel. Chưa có ảnh mẫu, ROI hoặc khoảng ngày cụ thể để xác nhận hiệu quả thực tế.

## Hiện trạng

`app/imagery.py:_raw_gap_fill` chọn cảnh theo thứ hạng độ trong, ban đầu một cảnh mỗi tile, thêm cảnh khác để bù dữ liệu thiếu. Một bản đồ ownership quyết định cảnh nguồn cho mọi băng tại mỗi pixel. Không cân bằng màu; mây được giữ lại. Luồng point/rectangle merge và province RAW đều dùng bộ ghép này. Preview và download cần dùng cùng chính sách hiệu chỉnh.

## Các phương án

1. Chỉ ưu tiên cảnh gần ngày/cùng lượt chụp: giảm một phần khác biệt, nhưng có thể giảm độ phủ và không xử lý chênh lệch còn lại.
2. Cân bằng histogram toàn cảnh: đơn giản nhưng có thể đổi màu sai khi tỷ lệ đất/nước/thực vật khác nhau giữa các footprint.
3. Cân bằng phổ từ vùng chồng lấp: so sánh cùng vị trí trên mặt đất; phù hợp với lựa chọn ưu tiên đều màu. Đây là phương án đề xuất, các chi tiết thuật toán dưới đây là quyết định đề xuất của người triển khai, chưa phải yêu cầu do người dùng chỉ định.

## Thiết kế đề xuất

- Thêm tùy chọn “Cân bằng màu giữa các cảnh” cho optical RAW; bật sẵn trên giao diện khi ghép RAW. API dùng tham số riêng với mặc định tắt để giữ tương thích cho client cũ. Khi bật, kết quả mang nhãn “RAW + cân bằng màu” và ghi rõ giá trị phổ đã hiệu chỉnh.
- Giữ cách chọn cảnh, độ phủ, ownership, footprint, NoData và pixel mây của RAW. Chỉ dùng mask mây/bóng mây để loại mẫu khỏi bước ước lượng màu, không xóa mây khỏi đầu ra.
- Giữ cảnh ưu tiên làm tham chiếu. Với các cảnh đóng góp còn lại, ước lượng gain/offset riêng cho từng băng phổ từ cùng các vị trí hợp lệ và sạch mây trong vùng chồng lấp với tham chiếu đã hiệu chỉnh. Cho phép nối qua cảnh đã hiệu chỉnh khi không chồng trực tiếp với cảnh đầu; thứ tự tham chiếu phải xác định được và được ghi lại.
- Dùng thống kê robust và giới hạn hệ số để tránh mây sót, vùng đồng nhất hoặc thay đổi bề mặt gây hiệu chỉnh cực đoan. Thống nhất hệ số trong toàn footprint của một cảnh, không tính lại từng block export.
- Chỉ sửa băng phổ Sentinel-2/Landsat; không sửa SCL, QA, xác suất mây và các băng phụ trợ khác. Không feather/trộn nguồn tại đường nối trong lần đầu này để giữ ownership nhất quán.
- Khi vùng chồng lấp thiếu mẫu hoặc hệ số không đáng tin, giữ cảnh đó nguyên và trả trạng thái bỏ qua kèm lý do; không tự cân bằng histogram toàn cảnh. Mức mẫu tối thiểu, thang lấy mẫu, giới hạn gain/offset phải là hằng số có tài liệu và kiểm tra, dùng đơn vị phản xạ hiện tại của collection.
- Preview, thumbnail và TIFF áp dụng cùng bộ hệ số. Truyền tùy chọn qua request, response và download URL; lưu danh sách cảnh, cảnh tham chiếu, hệ số từng băng, trạng thái và phiên bản phương pháp trong metadata kết quả/TIFF.
- Giới hạn xử lý vào các cảnh thực sự đóng góp và vùng chồng lấp; dùng ngân sách aggregation hiện có. Không chạy cân bằng trên toàn bộ các ngày không được sử dụng.

## Phạm vi mã dự kiến

- `app/imagery.py`: helper ước lượng/áp dụng hệ số, tích hợp sau bước xác định ownership; mở rộng SceneInfo và các entrypoint optical RAW.
- `app/main.py`: tham số request/download, response, truyền tùy chọn, nhãn và metadata TIFF.
- `static/index.html`: điều khiển cân bằng màu, truyền tham số và hiển thị trạng thái kết quả; sửa module UI liên quan nếu đường đi thực tế dùng module tách riêng.
- `README.md`: phân biệt RAW nguyên giá trị và RAW đã cân bằng màu.
- Tests optical RAW/API/UI hiện có và regression dành cho cân bằng màu.

## Kiểm chứng và giới hạn

- Regression số học: hai cảnh cùng nền có gain/offset biết trước phải giảm sai khác trong overlap; cảnh tham chiếu giữ nguyên, QA và NoData giữ nguyên, mây không bị cắt bỏ.
- Trường hợp thiếu overlap/mẫu, vùng phổ phẳng, outlier và graph tham chiếu rời rạc phải trả trạng thái rõ ràng, không phát sinh hệ số NaN/Inf hoặc mất dữ liệu.
- Kiểm tra request -> preview -> download giữ nguyên tùy chọn; metadata ghi đúng hiệu chỉnh, các luồng RAW tắt tùy chọn giữ hợp đồng cũ.
- Chạy kiểm tra Python/UI phù hợp. Kiểm tra offline/graph không chứng minh ảnh thật đã đều màu. Cần chạy cùng ROI/ngày thực tế và so sánh preview/TIFF trước/sau để chấp nhận trực quan.
- Không hứa xóa hết đường nối: khác mùa, thay đổi bề mặt, mây/sương và các nhóm cảnh không có overlap có thể vẫn lệch. TIFF cân bằng màu không còn là phổ nguồn nguyên trạng; không coi là sản phẩm đã được kiểm định cho chỉ số hoặc huấn luyện định lượng.

## Thao tác kho mã

Giữ các thay đổi đang có; không tạo branch, commit hoặc push trong bước thiết kế này. Sau khi duyệt sẽ lập kế hoạch triển khai và xác định các ngưỡng số cụ thể trước khi viết mã.
