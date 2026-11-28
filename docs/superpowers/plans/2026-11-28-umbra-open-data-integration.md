# Phương án tích hợp Umbra Open Data

**Mục tiêu.** Thêm nguồn **Umbra Open Data** vào Satellite Workspace để tìm cảnh SAR theo ROI và ngày chụp, xem footprint và ảnh xem trước nếu có, rồi tải GeoTIFF GEC nguyên gốc cùng metadata của cảnh đã chọn.

**Kiến trúc đề xuất.** Lập chỉ mục metadata cục bộ từ bucket S3 công khai. FastAPI phục vụ tìm kiếm từ SQLite và dùng snapshot để cố định cảnh được chọn. Leaflet dùng lại bộ chọn ROI và lớp footprint. Luồng tải truyền tệp từ S3 qua middleware Tiến trình tải hiện có.

**Phạm vi.** Bản đầu hỗ trợ một cảnh GEC mỗi lượt tải, giữ tên file nguồn. SICD, SIDD, CPHD, cắt GeoTIFF theo ROI, ghép cảnh và tải theo năm nằm ở các đợt mở rộng. Dùng `requests`, `sqlite3`, Shapely, Rasterio và Pillow trong môi trường hiện tại. Truy cập bucket công khai không cần thông tin đăng nhập AWS.

## Cơ sở lựa chọn

| Phương án | Đánh đổi |
| --- | --- |
| Duyệt cây STAC tĩnh của Umbra | Dễ thử nhưng tại thời điểm kiểm tra, gốc chỉ liên kết năm 2024 và 2025. Hai Item lấy mẫu có `href` rỗng. |
| Lập chỉ mục bằng `ListObjectsV2` và metadata cùng thư mục ảnh | **Đề xuất.** Dùng đường dẫn và kích thước object công khai thật, bao quát ba nhóm dữ liệu đã quan sát. Cần chuẩn hóa hai kiểu metadata và theo dõi độ đầy đủ khi làm mới. |
| Dùng `umbra-py` làm phụ thuộc | Có sẵn chức năng tìm và tải. Cần đánh giá nguồn chỉ mục, chính sách cập nhật và tương thích với gói Windows trước khi dựa vào đó. Có thể dùng để đối chiếu trong đợt khảo sát. |

- Bucket `umbra-open-data-catalog` ở `us-west-2` trả danh sách công khai dưới `open-data/`, `sar-data/tasks/` và `sar-data/task-data/`. Đã thấy STAC sidecar `.stac.v2.json` và metadata cũ `_METADATA.json`. Một số sidecar trỏ vào bucket xử lý nội bộ và dùng tên file khác với object công khai. Nguồn tải phải là danh sách object công khai cùng thư mục với sidecar đã ghép. [AWS Registry](https://registry.opendata.aws/umbra-open-data/) · [Catalogue STAC](https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/stac/catalog.json) · [Hướng dẫn catalogue của Umbra](https://docs.canopy.umbra.space/docs/archive-catalog-searching-via-stac-api)
- Đã đọc 32 byte đầu qua HTTP `206` ở một GEC thuộc mỗi prefix và ở một cảnh chụp ngày 13/01/2026. Kích thước mẫu khoảng 211–525 MB theo hệ thập phân. Đây là kiểm tra truy cập và định dạng đầu tệp. Tải hết file, mở ảnh trong QGIS và kiểm kê độ phủ Việt Nam vẫn thuộc bước nghiệm thu. [Sidecar mẫu 2025](https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/sar-data/tasks/3e56976b-fa2b-4035-bb71-385efde84c4a/2025-06-22-23-57-52_UMBRA-10/2025-06-22-23-57-52_UMBRA-10.stac.v2.json) · [Sidecar mẫu 2026](https://s3.us-west-2.amazonaws.com/umbra-open-data-catalog/open-data/02ae23a5-9fb5-41a2-aa9f-ce2cf968834a/2026-01-13-14-11-30_UMBRA-07/2026-01-13-14-11-30_UMBRA-07.stac.v2.json)
- GEC là ảnh SAR đã định vị địa lý theo ellipsoid. Giữ nguyên lưới ảnh nguồn. Hiển thị riêng độ phân giải theo hướng range và azimuth khi có metadata. Lưu ghi công Umbra và giấy phép CC BY 4.0 trong metadata xuất. [Định dạng sản phẩm](https://docs.canopy.umbra.space/docs/delivered-product-types) · [Điều khoản Umbra Open Data](https://umbra.space/open-license)

## Luồng sử dụng

1. Chọn **Umbra Open Data** trong **Khám phá**, đặt ROI và khoảng ngày. Dùng các chế độ điểm và bán kính, hình chữ nhật, tỉnh, xã hoặc polygon vector hiện có. Giao diện nguồn Umbra chỉ hiện các điều khiển áp dụng cho nguồn này.
2. Nếu chưa có chỉ mục, hiện **Tạo danh mục Umbra** và trạng thái cập nhật. Nếu đã có, hiện thời điểm cập nhật gần nhất. Khi lỗi đồng bộ, chỉ rõ đang dùng bản trước hoặc chưa thể tìm kiếm.
3. Trả danh sách theo ngày chụp mới nhất, 50 cảnh mỗi trang. Mỗi kết quả có footprint, thời gian UTC, vệ tinh, phân cực, độ phân giải nếu có và dung lượng GEC. Chọn cảnh để làm nổi footprint và tải GEC hoặc metadata. Dùng ROI để tìm cảnh giao vùng. Nút tải ghi rõ **Tải toàn cảnh GEC**.
4. Chỉ thông báo **Không có cảnh giao vùng trong khoảng ngày đã chọn** khi có chỉ mục hợp lệ cho phạm vi đang tra cứu. Nếu có hồ sơ thiếu metadata hoặc chưa đồng bộ đủ prefix, hiển thị giới hạn của kết quả. Một cảnh giao ROI có thể chỉ phủ một phần ROI.

## Hợp đồng tích hợp đề xuất

| API | Hành vi |
| --- | --- |
| `GET /api/umbra/catalog` | Trạng thái đồng bộ, thời điểm cập nhật thành công, phạm vi prefix, số cảnh hợp lệ và số hồ sơ chưa lập được chỉ mục. |
| `POST /api/umbra/catalog/refresh` | Trả `202` và bắt đầu đồng bộ metadata trong nền. Nếu đã có lượt chạy, trả trạng thái lượt hiện tại. |
| `POST /api/umbra/search` | Nhận các trường ROI hiện hành và ngày `YYYY-MM-DD`. Lọc theo ngày UTC, gồm cả hai ngày đầu cuối, và giao footprint thật. Cố định kết quả thành snapshot. |
| `GET /api/umbra/search/{search_id}?offset=0&limit=50` | Đọc trang từ snapshot. Trả `search_id`, `offset`, `limit`, `total`, `parameters`, `scenes`. `parameters` có `roi_geometry`, `bbox`, ngày tìm và phiên bản chỉ mục. |
| `GET /api/umbra/search/{search_id}/scenes/{index}/download` | Truyền đúng object GEC của dòng đã chọn. Kiểm tra ETag và kích thước. Yêu cầu tìm lại nếu object đã thay đổi hoặc biến mất. |
| `GET /api/umbra/search/{search_id}/scenes/{index}/metadata` | Trả JSON gồm metadata gốc nếu đọc được, footprint, giờ chụp, định danh, tên và kích thước object, link công khai và ghi công CC BY 4.0. |
| `GET /api/umbra/search/{search_id}/scenes/{index}/thumbnail` | Trả ảnh xem trước nhỏ nếu có thumbnail hoặc có thể đọc overview của GEC trong giới hạn tài nguyên. |

Tìm kiếm trả `503` khi chưa có chỉ mục dùng được. ROI hoặc ngày không hợp lệ trả `422`. Snapshot hoặc dòng không tồn tại trả `404`. Object đã đổi hoặc bị xóa trả `409` với yêu cầu tạo kết quả tìm mới.

## Trình tự triển khai và nghiệm thu

### 1. Đồng bộ danh mục

- [x] Tạo `app/umbra_catalog.py`. Liệt kê ba prefix bằng `ListObjectsV2` có continuation token. Gom nhóm theo thư mục acquisition và xác nhận GEC dựa trên object thực có trong nhóm. Ưu tiên `.stac.v2.json`, dùng `_METADATA.json` cho hồ sơ cũ. Phân biệt GEC, CSI và các biến thể đã xử lý. Chỉ hợp nhất bản sao khi xác nhận cùng cảnh và cùng sản phẩm; giữ riêng biến thể khác hoặc trường hợp ghép còn mơ hồ.
- [x] Lưu SQLite dưới `GEE_DOWNLOADS_DIR/umbra/`. Lưu `id`, dấu thời gian UTC, footprint WGS84 và bbox, metadata về vệ tinh/phân cực/độ phân giải, đường dẫn object, kích thước, ETag và nguồn sidecar. Dùng giới hạn timeout, số lần thử lại và số request đồng thời. Tái sử dụng bản ghi metadata chưa đổi ETag. Hoàn thành danh sách ba prefix trước khi công bố phiên bản mới trong một giao dịch. Lỗi mạng giữ phiên bản đang phục vụ. Hồ sơ lỗi hoặc chưa ghép được được đếm và hiện trạng thái thiếu dữ liệu.
- [x] Trong `tests/test_umbra_catalog.py`, kiểm tra nhiều trang, prefix có dấu cách, sidecar cũ và mới, `href` rỗng hoặc trỏ bucket khác, cùng collect có nhiều biến thể và lỗi giữa lần đồng bộ. Xác nhận object CSI không bị nhận nhầm là GEC.
- [x] Đo thời gian lập chỉ mục và cập nhật lại trên bucket thật. Bản đầu làm mới theo thao tác người dùng.

### 2. Tìm kiếm ROI và cố định lựa chọn

- [x] Tạo `app/umbra_api.py`, đăng ký trong `app/main.py` trước `app.mount`. Dùng `app/geo.py`, `app/admin_boundaries.py`, `app/commune_boundaries.py` và `app/vector_api.py` để lấy geometry WGS84 bằng các helper cục bộ. Dùng bbox để lọc ứng viên và Shapely để kiểm tra giao polygon. Chuẩn hóa geometry qua kinh tuyến 180° trước khi kiểm tra giao. Sắp xếp theo `(datetime UTC giảm dần, id tăng dần)`.
- [x] Dùng `TimeSeriesStore` với thư mục snapshot riêng và móc dọn snapshot theo vòng đời hiện có. Lưu đường dẫn object dưới dạng `object_path` cùng ETag, dung lượng và tên tệp. Bộ lọc `_safe` của store bỏ trường có tên chứa `key` hoặc `url`. Sinh link trong API khi đọc kết quả. Khi làm mới chỉ mục, các trang và object đã chọn trong snapshot cũ vẫn xác định cùng cảnh.
- [x] Trong `tests/test_umbra_api.py`, xác nhận hình chữ nhật xoay không trả cảnh chỉ giao bbox, polygon nhiều phần và qua kinh tuyến 180° trả đúng cảnh, ngày kết thúc được tính đủ, phân trang giữ cùng thứ tự sau refresh. Kiểm tra các trạng thái chỉ mục chưa sẵn sàng, danh mục cũ, ROI hợp lệ nhưng không có cảnh và snapshot hết hạn.

### 3. Tải GEC và metadata

- [x] Tạo `app/umbra_transfer.py`. Dựng URL HTTPS từ bucket và đường dẫn đã lưu trong snapshot, mã hóa từng đoạn đường dẫn và chỉ cho phép ba prefix đã định. Truyền file bằng các khối có kích thước giới hạn. Gửi `If-Match` với ETag và kiểm tra độ dài trước khi trả tệp. Giữ tên GEC gốc đã làm sạch để dùng trong `Content-Disposition`. Khi mất kết nối hoặc thiếu byte, kết thúc lượt tải ở trạng thái lỗi hoặc gián đoạn. Người dùng có thể tải lại cảnh từ snapshot.
- [x] Thêm route tải Umbra vào `DOWNLOAD_PATH` trong `app/task_center.py` để báo byte, trạng thái và hủy qua middleware hiện có. Tận dụng `Content-Length` thật. Giữ ETag opaque khi kiểm tra phiên bản. ETag multipart không dùng như checksum MD5 của file. Metadata xuất dùng đường dẫn công khai đã xác minh, kèm thông tin nguồn và giấy phép.
- [x] Kiểm tra cảnh được chọn ở trang sau vẫn tải đúng object. Dùng dữ liệu thử để kiểm tra object đổi ETag, sai độ dài, mất kết nối, hủy lượt tải và đóng kết nối upstream. Xác nhận giao diện Tiến trình tải chỉ báo hoàn tất khi đã gửi đủ byte.

### 4. Giao diện và ảnh xem trước

- [x] Thêm lựa chọn nguồn trong `static/index.html` và bộ điều khiển `static/umbra.js`. Dùng `static/s5p.js` làm mẫu cho cách tổ chức một nguồn riêng. Tái sử dụng `static/scene-footprints.js` và `static/scene-thumbnails.js` cho thao tác vẽ footprint và hiển thị thumbnail. Dùng cùng snapshot khi đổi trang, chọn cảnh và tải. Loại bỏ kết quả của request cũ khi người dùng đã đổi ROI hoặc nguồn.
- [x] Ưu tiên thumbnail công khai thực có của cảnh. Nếu thiếu, thử đọc overview COG bằng trình đọc HTTP Range có bộ đếm byte và render JPEG tối đa 512 px, cache theo object/ETag. Kiểm tra số byte thực tế bằng nguồn thử có ghi nhận Range. Khi máy chủ không đáp ứng Range, ảnh thiếu overview hoặc vượt ngân sách đọc, chỉ hiển thị footprint. Mục tiêu là xem nhanh trong danh sách mà không tải toàn bộ GeoTIFF.
- [x] Bổ sung kiểm tra UI trong `tests/test_umbra_ui.cjs` cho trạng thái đang đồng bộ, chỉ mục cũ, không có cảnh, đổi ROI/nguồn khi request cũ còn chạy, xem footprint và liên kết tải đúng dòng.

### 5. Nghiệm thu trên Windows

- [x] Chạy `./.venv/Scripts/python.exe -m unittest tests.test_umbra_catalog tests.test_umbra_api tests.test_task_center tests.test_scene_catalog tests.test_s5p` và `node --test tests/test_umbra_ui.cjs tests/test_s5p_ui.cjs tests/test_scene_footprints_ui.cjs` sau khi bổ sung các test Umbra. Kiểm tra một lượt tìm kiếm và tải cũ để phát hiện ảnh hưởng từ việc thêm route vào middleware.
- [x] Dùng một cảnh 2025 đã xác minh và một cảnh 2026 trong `open-data/` để thử theo footprint và ngày, rồi chạy một ROI không giao các cảnh trong bộ mẫu. Tải trọn một GEC qua giao diện, so tên và số byte với object đã chọn, mở bằng Rasterio/QGIS và kiểm tra CRS, footprint và metadata. Thử hủy một lượt đang tải và kiểm tra trình duyệt không báo hoàn tất tệp dở. Khi công bố số cảnh ở Việt Nam, thống kê riêng từ phiên bản chỉ mục có trạng thái đầy đủ và polygon Việt Nam xác định.
- [x] Cập nhật `README.md` và `LICH_SU_THAY_DOI.md` theo chức năng đã chạy. Ghi cách đồng bộ, tải GEC, giấy phép, mức phủ phụ thuộc catalogue và thời điểm kiểm tra live.

## Bằng chứng triển khai ngày 2026-11-28

- **Kiểm thử tập trung:** 70 kiểm thử Python và 56 kiểm thử Node đạt, gồm Umbra và các luồng catalogue, ROI và Tiến trình tải liên quan. Khi chạy toàn bộ, ba lỗi Python về time series/vector và hai lỗi Node trong `test_admin_map.cjs` tái hiện trên baseline `91cc143`.
- **Chrome và S3 thật:** kiểm thử với năm nhóm cảnh mẫu từ ba prefix công khai, gồm sidecar mới và metadata cũ. Tìm được cảnh dùng làm mẫu ngày 19/12/2025 và 13/01/2026, bật footprint, đọc JPEG, lấy metadata và xác nhận ROI đối chứng không giao cảnh mẫu. Tìm và tải ảnh cắt Sentinel-2 AWS qua catalogue cũ trả HTTP 200. Rasterio đọc được ảnh cắt 5 band.
- **Tệp GEC:** Chrome tải đủ `2025-12-19-12-49-51_UMBRA-07_GEC.tif` với 56.325.902 byte. Rasterio đọc được ảnh một band EPSG:4326 và phạm vi raster bao phủ footprint metadata. QGIS 3.44.14 mở lớp qua provider GDAL và xác nhận CRS EPSG:4326.
- **Hủy:** hủy cảnh 525.465.602 byte trong giao diện **Tiến trình tải** sau khi máy chủ gửi 1.048.576 byte. Chrome báo hủy. Yêu cầu gửi lại cùng mã lượt tải trả HTTP 409. Bấm tải lại tạo mã lượt mới.
- **Phạm vi:** các lượt chạy kiểm chứng dùng thư mục tạm riêng. Kết quả mẫu xác nhận luồng sản phẩm, còn số cảnh và thời gian đồng bộ toàn bucket được đo riêng. Chưa thống kê độ phủ Việt Nam.
- **Danh mục toàn bucket:** lần đầu hoàn tất lúc 11:41 giờ Việt Nam, khoảng 16 phút trên máy kiểm thử. Lần cập nhật sau ghi nhận hoàn tất trong khoảng 2 phút 20 giây và trả cùng kết quả. Chỉ mục ghi 17.177 cảnh GEC, báo 5 tệp chưa ghép được metadata phù hợp và chiếm khoảng 133 MB trên đĩa. Tìm lại hai ROI mẫu trên chỉ mục toàn bucket trả cảnh thật ngày 22/06/2025 và 13/01/2026.
