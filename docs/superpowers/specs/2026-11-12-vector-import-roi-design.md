# Nhập lớp vector và dùng polygon làm ROI

## Yêu cầu đã xác nhận

Người dùng yêu cầu nhập KML, KMZ, GeoJSON và các định dạng vector tương tự; sau nhập có thể bật/tắt và chọn riêng polygon, point, line. Người dùng xác nhận polygon được chọn phải dùng được làm ROI tìm kiếm, đếm và tải ảnh.

## Phương án đề xuất

Backend đọc/kiểm tra định dạng và trả GeoJSON chuẩn WGS84; Leaflet hiển thị và quản lý đối tượng. ROI được lưu thành hình học bất biến riêng với mã tham chiếu, để cùng một polygon được dùng xuyên suốt truy vấn, danh sách và cắt ảnh.

Đã cân nhắc đọc hoàn toàn trong trình duyệt: nhẹ cho GeoJSON/KML nhưng cần thêm thư viện KMZ/Shapefile và vẫn phải kiểm tra hình học phía backend khi dùng làm ROI. Chọn backend để tận dụng Python, pyshp và rasterio hiện có, giữ một quy tắc xử lý định dạng/CRS và một hình học ROI thống nhất. Đây là đề xuất triển khai của agent, chưa phải lựa chọn kỹ thuật do người dùng chỉ định.

## Định dạng

- GeoJSON/JSON: FeatureCollection, Feature hoặc Geometry; hỗ trợ Point/MultiPoint, LineString/MultiLineString, Polygon/MultiPolygon và GeometryCollection.
- KML/KMZ: các Placemark, thư mục và hình học tương ứng; KMZ có thể chứa nhiều KML. Không truy cập NetworkLink, URL icon hay tài nguyên ngoài file. GroundOverlay raster không thuộc tính năng này.
- GPX: waypoint, route, track.
- Shapefile ZIP: .shp/.shx/.dbf cùng bộ, dùng .prj để nhận diện CRS; hỗ trợ nhiều bộ trong ZIP. Thiếu CRS phải báo rõ, không tự đoán tọa độ phẳng là kinh/vĩ độ.
- Đưa hình học về EPSG:4326, giữ tên và thuộc tính dùng để xem. Bảo toàn lỗ polygon và các phần rời nhau.

## Thao tác giao diện

- Thêm mục **Lớp vector** có nhập nhiều file; mỗi file chứa danh sách đối tượng có tên, loại hình học và trạng thái hiển thị/chọn.
- Bật/tắt toàn file hoặc từng đối tượng. Chọn riêng, chọn tất cả, bỏ chọn; lọc theo tên/loại; xem toàn lớp và xóa lớp.
- Chọn trên bản đồ hoặc danh sách sẽ tô nổi và hiện thuộc tính dưới dạng văn bản. Phân trang danh sách dài; không đưa hàng nghìn dòng vào DOM một lần.
- Thêm nút **Dùng polygon đã chọn làm ROI**. Chỉ Polygon/MultiPolygon được dùng trực tiếp; point và line vẫn có thể hiển thị/chọn nhưng không tự mở rộng thành vùng.
- Có thể chọn nhiều polygon: dùng hợp hình học chính xác, giữ vùng rời và lỗ, không thay bằng hình chữ nhật bao ngoài.
- Bật/tắt hiển thị không âm thầm thay đổi ROI đã áp dụng; ROI hoạt động có đường biên và tên riêng. Đổi tập polygon để áp dụng hoặc xóa lớp chứa ROI phải làm mất hiệu lực kết quả tìm/đếm cũ.
- Dự thảo ROI vẽ tay và khung GeoTIFF mẫu được giữ riêng khi đổi cách chọn vùng. Chọn một đối tượng để xem không tự khởi chạy tìm kiếm/tải ảnh.

## Tích hợp tìm, đếm và tải

- Thêm chế độ chọn vùng từ vector; yêu cầu API mang mã ROI, không nhét toàn bộ polygon vào URL tải.
- Đếm ảnh/CSV dùng đúng Polygon/MultiPolygon tại nguồn GEE, CDSE và AWS như luồng metadata hiện có; giữ nguồn, ngày, vệ tinh và quy tắc không lọc mây của bước đếm.
- Tìm và tải ảnh GEE theo đúng vùng polygon, gồm lỗ và phần rời; preview và GeoTIFF sử dụng cùng ROI. Bao ngoài chỉ dùng để xác định lưới vận chuyển; pixel ngoài polygon/lỗ phải là NoData ở kết quả cuối.
- Giữ giao diện/liên kết/tùy chọn xuất hiện có. Snapshot kết quả và job giữ bản sao hình học, nên thay lựa chọn hay xóa lớp sau đó không sửa một job đã tạo.
- Các đường tải ngoài GEE chỉ được kích hoạt với ROI vector khi đã có xử lý cắt/mask polygon tương ứng. Nếu chưa hỗ trợ, báo rõ tại giao diện/API; không âm thầm tìm bằng tâm hay trả ảnh chỉ cắt theo bbox.
- Tính năng nhập và đếm không tự tải raster. Việc tải ảnh do người dùng kích hoạt trong luồng hiện có.

## Kiểm tra dữ liệu và giới hạn

- Dùng Shapely 2.x để kiểm tra hình học và hợp polygon; defusedxml 0.7.x để đọc KML/GPX. Không đổi phiên bản các dependency hiện có để né lỗi.
- Giới hạn đề xuất: mỗi file tải lên 20 MiB; ZIP/KMZ tối đa 100 entry và tổng dữ liệu giải nén 80 MiB; tối đa 10.000 feature và 500.000 vị trí tọa độ mỗi file. Đọc archive có giới hạn, không giải nén ra đường dẫn người dùng cung cấp.
- Báo lỗi rõ cho file hỏng, CRS không xác định, tọa độ không hữu hạn/sai miền, hình học không hợp lệ hoặc vượt giới hạn; không âm thầm sửa topology hay bỏ đối tượng lỗi. Feature không có geometry được thông báo và không hiển thị.
- Tên/thuộc tính không được chèn thành HTML thực thi. Không tải tài nguyên ngoài được tham chiếu trong file.
- ROI quá lớn cho một thao tác tải phải qua giới hạn tài nguyên hiện có hoặc báo lỗi rõ, không tự đổi tỷ lệ/độ phân giải.

## Kiểm chứng và phạm vi

Kiểm tra parser cho từng định dạng, CRS, polygon có lỗ/phần rời, file hỏng và archive/XML bất thường; kiểm tra thao tác bật/tắt/chọn nhiều file; kiểm tra ROI, nguồn và snapshot nhất quán từ đếm/tìm đến TIFF. Kiểm tra browser thật cho luồng nhập → chọn → áp dụng ROI → đếm, và kiểm tra raster nhỏ để chứng minh pixel ngoài polygon/lỗ là NoData.

Đây là tính năng nhập/quản lý vector và ROI, chưa có chỉnh sửa đỉnh hình học, tạo buffer cho point/line, sửa thuộc tính hoặc bộ công cụ biên tập GIS.

## Quyết định kỹ thuật sau builder check

- Sau review triển khai: tổng JSON chuẩn hóa giới hạn 80 MiB, tính cả geometry, tên/lớp và thuộc tính kế thừa ở từng feature con trước khi sao chép. Giới hạn này ngăn GeometryCollection/MultiGeometry nhân bản thuộc tính quá lớn dù file đầu vào còn nhỏ.

- Người dùng đã duyệt thiết kế và yêu cầu triển khai ngay. Builder check độc lập không cần thêm câu hỏi người dùng.
- Import và ROI dùng SQLite, UUID riêng; ROI bất biến, giữ 24 giờ kể từ lần truy cập cuối. Xóa lớp nhập không xóa geometry của ROI/snapshot cũ; client bỏ ROI đang phụ thuộc lớp bị xóa.
- Flatten GeometryCollection/KML MultiGeometry thành các feature con độc lập, giữ thuộc tính cha và ID con ổn định; giữ MultiPolygon là một feature. Áp dụng dùng phần polygon trong tập đã chọn và thông báo số point/line không tham gia.
- Bản đầu hỗ trợ ROI vector cho đếm metadata GEE/CDSE/AWS; tìm/tải và time series của các nguồn GEE hiện có. Tìm/tải bên ngoài GEE bị chặn rõ nếu đường cắt/mask polygon chưa được triển khai.
- Từ chối geometry đi qua đường đổi ngày hoặc ngoài miền vĩ độ bản đồ hỗ trợ; thao tác polygon dùng mặt phẳng WGS84 với geodesic=False. Không tự sửa geometry lỗi. Tổng tọa độ của các feature tham gia hợp ROI cũng bị giới hạn 500.000.
- File nhập nguyên tử; mỗi file trong đợt nhiều file có trạng thái riêng. Giới hạn archive theo cả kích thước khai báo lẫn byte thực đọc; từ chối entry mã hóa, tên trùng và archive không hợp lệ; không extract ra filesystem.
- Thay selection xem trước và visibility không làm mất hiệu lực kết quả tìm. Apply/Clear/xóa lớp phụ thuộc mới đổi ROI hoạt động và hủy phản hồi cũ. Time-series và đường tải vector luôn mask ở raster cuối để giữ NoData cho lỗ/vùng ngoài polygon.
