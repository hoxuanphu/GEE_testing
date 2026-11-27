# Satellite Workspace

Satellite Workspace là ứng dụng web cục bộ để tìm, xem trước và tải dữ liệu
viễn thám theo vùng quan tâm (ROI). Backend FastAPI phục vụ giao diện bản đồ
Leaflet và các luồng Google Earth Engine (GEE), Copernicus Data Space (CDSE),
AWS Earth Search và Esri. Khả năng tìm và tải phụ thuộc nguồn dữ liệu, vùng,
loại sản phẩm và quyền truy cập.

[Bắt đầu](#bắt-đầu) · [Giao diện](#giao-diện) · [Nguồn dữ liệu](#nguồn-dữ-liệu) ·
[Time series](#time-series) · [Tiến trình tải](#tiến-trình-tải) ·
[API và cấu trúc mã](#api-và-cấu-trúc-mã) · [Kiểm thử](#kiểm-thử) ·
[Lịch sử thay đổi](LICH_SU_THAY_DOI.md)

## Bắt đầu

### Bản clone trên Windows

Cần Python 3.12, Git LFS, kết nối mạng và quyền truy cập các nguồn muốn dùng.
Từ PowerShell tại thư mục gốc:

```powershell
git lfs install --local
git lfs pull
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Mở <http://127.0.0.1:8000>; tài liệu API tự sinh ở
<http://127.0.0.1:8000/docs>. Giao diện cần Leaflet trong `static/vendor/leaflet/`,
gồm JavaScript, CSS và thư mục ảnh đi kèm.

Bản đồ ranh giới dùng `shapefile34/vn34.json` và bộ shapefile xã/phường trong
`Shapefile_PX_VN_34/`. Sau `git lfs pull`, kiểm tra các tệp thực sự có dữ liệu,
không chỉ là tệp trỏ LFS. `convert_shapefile34.py` có thể tạo lại JSON tỉnh từ
shapefile tỉnh.

### Gói ZIP Windows

Nếu có **gói ZIP Windows đầy đủ** chứa `Start.cmd`,
`requirements-windows-py312.lock`, `wheelhouse/` và dữ liệu địa giới, giải nén
vào thư mục có quyền ghi, cài Python 3.12 x64 rồi mở `Start.cmd`. Launcher tạo
`.venv`, cài thư viện từ wheelhouse theo hash và mở trình duyệt khi API sẵn sàng.
Giữ cửa sổ launcher trong lúc dùng; nhấn `Ctrl+C` để dừng.

```powershell
.\Start.cmd --port 8018
.\Start.cmd --no-browser
```

Bản clone không tự có `wheelhouse/`; dùng cách cài đặt cho bản clone nếu chưa
có gói ZIP đầy đủ. Mã đóng gói nằm trong [tools/package_windows.py](tools/package_windows.py).

### Cấu hình nguồn

Mở **Cấu hình nguồn** trên giao diện cục bộ. GEE nhận JSON service account;
CDSE OData nhận tài khoản và mật khẩu; CDSE S3 nhận access key và secret key.
Dòng cảnh báo màu đỏ trên thanh đầu trang ghi rõ những thông tin còn thiếu.
Có thể bấm vào dòng này để mở **Cấu hình nguồn**.
Cấu hình được lưu trong `.env.providers.gee.json`, `.env.providers.cdse.json`,
`.env.providers.s3.json` ở thư mục ứng dụng. Khởi động lại backend sau khi lưu
để nạp cấu hình mới. Các tệp này chứa thông tin nhạy cảm và được Git bỏ qua.

Có thể dùng biến môi trường hoặc sao chép [.env.example](.env.example) thành
`.env` và thay bằng cấu hình của chính mình. Với GEE, cấu hình giao diện được
ưu tiên; nếu không có, backend dùng `GEE_SERVICE_ACCOUNT` và `GEE_KEY_FILE`
khi cả hai hợp lệ, hoặc credentials mặc định của môi trường. Dự án GEE và tài
khoản phải có quyền truy cập collection tương ứng. CDSE S3 dùng
`Access_Key_S3` và `Secret_Key_S3`; các endpoint OData cũ dùng `CDSE_USERNAME`
và `CDSE_PASSWORD`. Không đưa khóa hoặc mật khẩu vào Git.

Đây là ứng dụng cho máy cục bộ hoặc môi trường tin cậy: job, snapshot và API
chưa có xác thực/phân quyền người dùng. Cần bổ sung các cơ chế đó trước khi
công khai qua mạng. `GET /api/health` báo trạng thái backend; `ee_initialized`
có thể là `false` cho tới khi một thao tác cần GEE được gọi.

## Giao diện

- **Khám phá**: chọn ROI, nguồn, vệ tinh, ngày và thông số; tìm ảnh hoặc bắt đầu
  tải theo năm. **Tùy chọn nâng cao** chứa time series, band và tùy chọn ghép.
  **Đếm ảnh và xuất CSV** đọc metadata, không tải raster.
- **Kết quả**: xem danh sách, thumbnail, footprint, thông tin cảnh và các lựa
  chọn xem trước/tải. Tùy nguồn và chế độ, kết quả có thể chứa nhóm cảnh hoặc
  các lựa chọn hướng bay Sentinel-1 riêng.
- **Tiến trình tải**: xem tác vụ nền và lượt gửi tệp, lọc trạng thái, xem kết
  quả và dùng các thao tác điều khiển khả dụng. Cấu hình tải Esri cũng nằm ở đây.

Thanh đầu trang cho phép chọn giao diện sáng, tối hoặc theo hệ thống. Trên màn
hình hẹp, bảng thao tác nằm dưới bản đồ. Bản đồ nền và nguồn ảnh cần kết nối
mạng; một cảnh giao ROI không đồng nghĩa toàn ROI có dữ liệu hoặc không có mây.

### Chọn vùng

Có thể chọn điểm và bán kính, vẽ hình chữ nhật có thể xoay, chọn tỉnh/thành,
xã/phường, nhập polygon vector, hoặc dùng khung GeoTIFF mẫu. Với điểm, bán kính
xác định vùng vuông tải quanh tâm; ROI chữ nhật giữ kích thước và góc đã chọn.
Khả năng tìm/cắt khác nhau giữa các nguồn. Một số catalogue ngoài GEE tìm theo
bao ngoài của chữ nhật; ZIP sản phẩm gốc CDSE/S3 vẫn là **toàn sản phẩm**,
không cắt theo ROI.

**Lớp vector** nhận GeoJSON/JSON, KML/KMZ, GPX và Shapefile ZIP. Chọn các
polygon cần dùng rồi áp dụng làm ROI; lỗ và các phần rời được giữ khi luồng
GEE hỗ trợ. Tệp tối đa 20 MiB; shapefile ZIP cần `.shp`, `.shx`, `.dbf`,
`.prj`. Điểm/đường có thể hiển thị nhưng không thay thế polygon ROI. Luồng
vector ngoài GEE chưa hỗ trợ tìm/cắt raster tương ứng; giao diện báo lỗi thay
vì âm thầm chuyển sang vùng khác.

### Đồng khung GeoTIFF mẫu

Trong **Cách chọn vùng**, chọn **Đồng khung GeoTIFF mẫu** và tải lên một
`.tif`/`.tiff` tối đa 512 MiB. Backend chỉ đọc metadata khung, sau đó xóa tệp
upload; dữ liệu pixel mẫu không được dùng để xuất ảnh. Hỗ trợ Sentinel-2 SR
và Sentinel-1 SAR qua GEE.

- **Đồng biên** giữ CRS, hướng và biên; pixel xấp xỉ độ phân giải được chọn,
  cần CRS chiếu theo mét.
- **Đồng lưới** giữ CRS, affine và số hàng/cột của mẫu. Nội suy không làm tăng
  độ phân giải thực của dữ liệu nguồn. Đồng khung không chứng minh các đối tượng
  đã được đồng đăng ký chính xác.

Tìm và chọn tối đa 10 cảnh, kiểm tra chất lượng/độ phủ và xác nhận trước khi
xuất. Nếu bật ghép, nguồn sau chỉ bù NoData của nguồn trước; SAR chỉ ghép các
cảnh cùng hướng bay và quỹ đạo tương đối. Mây là dữ liệu, không phải NoData.
Sản phẩm gồm TIFF và manifest, có thể tải từng ảnh hoặc ZIP khi sẵn sàng.

## Nguồn dữ liệu

| Nguồn / sản phẩm | Cách dùng và giới hạn chính |
|---|---|
| Sentinel-2 SR, Landsat 8/9 SR, Planet NICFI, Dynamic World qua GEE | Tìm, xem trước và xuất theo ROI được hỗ trợ. NICFI là composite theo kỳ của nguồn; Dynamic World là dữ liệu phân loại. |
| Sentinel-1 SAR GEE | Tìm theo hướng bay và phân cực. Cảnh riêng lẻ có hai tùy chọn: tải theo vùng hoặc tải nguyên cảnh trên lưới gốc. Ảnh ghép nhiều cảnh tải theo vùng. Giá trị SAR giữ đơn vị dB của nguồn. |
| Sentinel-1 GRD/SLC, Sentinel-2 L2A, Sentinel-3 CDSE | Tìm catalogue và tải tùy sản phẩm/quyền truy cập; ZIP là nguyên sản phẩm. Gợi ý cặp SLC chỉ hỗ trợ chọn dữ liệu InSAR, không tạo interferogram. |
| Sentinel-2 L2A AWS | Tìm catalogue và tải/cắt các band COG theo luồng được hỗ trợ. |
| Sentinel-5P OFFL GEE | Tìm từng ảnh NO2, SO2, CO, CH4, O3, HCHO hoặc AAI; tải GeoTIFF một band cho từng ảnh. |
| ALOS AW3D30, SRTM, Copernicus GLO-30 qua GEE | Tải DEM/DSM 30 m theo ROI; ALOS và Copernicus là DSM, không phải DTM đất trống. |
| Esri World Imagery | Tải nền RGB dạng job theo điểm, chữ nhật, xã/phường hoặc bbox. Đây là ảnh mosaic hiển thị, không phải surface reflectance; không bảo đảm ngày chụp hay độ phân giải nguồn. |

GeoTIFF Sentinel-1 GEE nhúng thông tin cảnh nguồn trong `SAR_SOURCES`, gồm mã cảnh,
thời điểm chụp, vệ tinh, hướng bay, quỹ đạo và phân cực khi GEE cung cấp. Ảnh ghép
lưu danh sách nguồn theo thứ tự ghép. Ranh giới phủ nguồn, khi có, nằm trong
`SAR_SOURCE_FOOTPRINTS`. Có thể xem các thẻ trong QGIS hoặc bằng `gdalinfo`.

**Tải nguyên cảnh Sentinel-1 GEE** tải các tile trên lưới gốc rồi ghép ở máy chủ.
Ứng dụng nhúng metadata và tạo một Cloud Optimized GeoTIFF (COG) để gửi về
trình duyệt. Tab **Tiến trình tải** hiển thị bước tạo COG và cho phép hủy.

Với Sentinel-5P, lưới L3 khoảng 1,1 km không phải độ phân giải gốc cảm biến.
Giá trị là lượng cột khí, tỷ lệ trộn CH4 hoặc chỉ số aerosol tùy sản phẩm;
**không phải nồng độ bề mặt hay PM2.5**. Preview chỉ phục vụ hiển thị, còn
GeoTIFF giữ giá trị và mask của sản phẩm. Phần giao ROI vẫn có thể không có
pixel hợp lệ. Quyền truy cập và điều khoản sử dụng dữ liệu do từng nhà cung cấp
quy định.

### Ghép cảnh và chất lượng

Với Sentinel-2/Landsat, giao diện cung cấp RAW, mosaic, median, medoid và
base-score tùy ngữ cảnh. RAW giữ pixel/mây của cảnh ưu tiên và dùng cảnh khác
bù phần thiếu; các phương pháp còn lại có quy tắc chọn hoặc mask khác nhau.
Lựa chọn nhóm theo khoảng ngày hoặc cùng lượt chụp có thể hạn chế sự khác biệt
thời gian giữa các nguồn, nhưng không bảo đảm màu đồng nhất. Đọc kế hoạch và
cảnh báo trước khi xác nhận composite.

Độ phủ là tỷ lệ dữ liệu hợp lệ theo cách tính của luồng đó; mây toàn cảnh,
mây/bóng trong ROI và NoData là các đại lượng khác nhau. Mức mây 0% theo SCL
không bảo đảm không có sương hoặc mây mỏng. Kiểm tra manifest và kết quả trước
khi sử dụng cho phân tích.

### Đếm ảnh và xuất CSV

Mở **Đếm ảnh và xuất CSV**, chọn ROI, nguồn và khoảng ngày, rồi bấm **Đếm ảnh**.
CSV tương ứng snapshot của lần đếm đó, gồm metadata/ID và ngày theo nguồn. Đếm
metadata không tải raster, không xác nhận mọi cảnh sẽ tải thành công hoặc có dữ
liệu hợp lệ trên toàn ROI. Giới hạn và khả năng lọc phụ thuộc nguồn.

## Time series

Chọn ROI điểm, chữ nhật hoặc polygon vector và một trong các nguồn GEE:
Sentinel-2, Landsat, Sentinel-1 GEE, NICFI hoặc Dynamic World. Trong **Tùy chọn
nâng cao**, bật **Time series**. Danh sách dựa trên cảnh nguồn theo ROI/ngày;
không tự lọc theo ngưỡng mây. NICFI vẫn là composite theo kỳ của nguồn.
Danh sách một ZIP hiển thị `asset_id` đầy đủ của cảnh GEE, được lưu trước khi
gộp collection; tên tệp TIFF xuất ra là tên do ứng dụng tạo, còn manifest
giữ định danh cảnh nguồn để đối chiếu.

- **Một ZIP**: bấm **Tìm chuỗi thời gian**, xem danh sách rồi chọn **Tải toàn
  bộ ZIP**. ZIP gồm các TIFF thành công và `manifest.json`/`manifest.csv` ghi
  metadata, trạng thái và lỗi của từng cảnh. Nếu có cảnh lỗi, kết quả có thể là
  một phần; không có ZIP khi tất cả cảnh thất bại.
- **Theo năm**: bật **Tải lần lượt theo năm (GeoTIFF)** rồi bấm **Bắt đầu tải
  theo năm**. Backend tìm, tải và đóng gói từng năm theo thứ tự; mỗi năm có ZIP
  riêng và CSV riêng. Năm chưa tìm có tổng số cảnh chưa xác định. Có thể tạm
  dừng, tiếp tục từ các cảnh đã kiểm tra hoặc thử lại phần lỗi.

Hai kiểu dùng chung một worker và hàng đợi. Job chờ được xếp lại sau khi
khởi động lại; job đang chạy bị gián đoạn cần người dùng xử lý/tiếp tục theo
loại job. Batch đang chạy khi tắt bình thường được yêu cầu dừng tại checkpoint.
Snapshot cố định lựa chọn, nhưng không bảo đảm nhà cung cấp không cập nhật nội
dung cùng asset ID. Các luồng này không hỗ trợ CDSE/AWS/Sentinel-5P.

Kết quả time series thường hết hạn sau 24 giờ không hoạt động; batch theo năm
ở trạng thái tạm dừng/gián đoạn giữ 7 ngày không hoạt động. Job đang hoạt động
không bị dọn theo TTL. Hãy tải và lưu riêng kết quả cần giữ lâu dài.

## Tiến trình tải

Tab **Tiến trình tải** cho biết tác vụ đang chờ, đang chạy và đã kết thúc, kèm
số cảnh/tile, giai đoạn và liên kết kết quả còn sẵn sàng. Có thể lọc, phân
trang, hủy tác vụ được hỗ trợ, hoặc tạm dừng/tiếp tục batch theo năm. Hủy/tạm
dừng là yêu cầu tại điểm an toàn; thao tác mạng hoặc cảnh đang xử lý có thể
cần thêm thời gian. Sentinel-2 có checkpoint chặn cập nhật worker khi đánh dấu
hủy, nhưng thao tác mạng đang chờ có thể chưa kết thúc ngay.

**Xóa khỏi danh sách** ẩn tác vụ đã kết thúc; tệp vẫn theo thời hạn lưu của
luồng tương ứng. Với time series một ZIP và theo năm, **Xóa cả tệp** cho biết
số tệp/dung lượng để xác nhận trước khi xóa. Không thể xóa tác vụ đang hoạt
động; tệp đang được gửi được bảo vệ khỏi xóa đối với hai loại này.

Các request tải trực tiếp được hỗ trợ, bao gồm DEM, CDSE và AWS, cũng xuất hiện
ở dạng **Tải tệp**, kèm dung lượng đã gửi và tổng dung lượng nếu biết hoặc ước
tính. Khi tải từ danh sách cảnh GEE hoặc tải nguyên cảnh Sentinel-1 GEE, tab
hiển thị số tile đã tải, bước chờ ghép, ghép GeoTIFF và gửi tệp. Khi cảnh trước
bắt đầu ghép, cảnh kế tiếp có thể tải tile. Máy chủ giới hạn một cảnh đang tải
và một cảnh đang ghép hoặc gửi, đồng thời kiểm tra dung lượng cho cả hai cảnh.

Có thể yêu cầu hủy lượt tải trực tiếp trong tab. Nếu đang chờ một yêu cầu GEE
phản hồi, máy chủ có thể cần đợi yêu cầu đó trả về hoặc hết thời gian chờ rồi
mới dừng. Lượt tải trực tiếp lưu trạng thái trong bộ nhớ, không có checkpoint
để tiếp tục sau khi khởi động lại. Với tác vụ nền, đóng tab không tự dừng worker.

Chỉ chạy **một server phiên bản mới** cho cùng thư mục tải. Khóa liên tiến
trình ngăn server mới thứ hai cùng khôi phục job. Nếu đang có server phiên bản
cũ dùng cùng thư mục, dừng các server cũ trước khi khởi động phiên bản mới vì
chúng không có khóa này. Tránh `--reload` khi đang tải dài.

### Sentinel-2 checkpoint tùy chọn

Luồng Sentinel-2 tải nền có checkpoint đang được kiểm chứng và các cờ mặc định
tắt. Đặt trước khi khởi động server, không thay đổi giữa lượt xuất:

```powershell
$env:GEE_DOWNLOAD_SNAPSHOTS = '1'
$env:GEE_DOWNLOAD_TRANSPORT = '1'
$env:GEE_DOWNLOAD_VRT = '1'
$env:GEE_DOWNLOAD_JOBS = '1'
```

Luồng này áp dụng cho các lựa chọn Sentinel-2 được hỗ trợ, lưu snapshot và
checkpoint trong `downloads/gee-downloads/`; tiếp tục ở cấp tile đã kiểm tra,
không nối byte vào URL GEE cũ. Job dở dang sau restart được đánh dấu gián đoạn
và cần tiếp tục. Kiểm thử fixture không chứng minh hiệu năng hay thành công cho
mọi ROI, quota hoặc kết nối. Xem [thiết kế](docs/superpowers/specs/2026-11-17-gee-download-performance-design.md)
và [báo cáo](docs/superpowers/reports/2026-11-18-gee-download-performance.md).

## Dữ liệu cục bộ

Mặc định `downloads/` chứa snapshot, trạng thái tác vụ, cache và kết quả theo
luồng. Có thể đặt biến `GEE_DOWNLOADS_DIR` trước khi khởi chạy backend để dùng
thư mục khác. Dự trù đủ dung lượng cho tile, TIFF trung gian và ZIP; không tự
xóa tệp khi còn tác vụ đang hoạt động. Khi dùng Esri, cache tile có thể cũ và
cần được quản lý phù hợp với điều khoản nguồn.

Không commit dữ liệu tải, service-account key, mật khẩu, token hoặc chứng chỉ CA
nội bộ. Ảnh có giá trị 0 hợp lệ không mặc nhiên là NoData; kiểm tra mask và
metadata của từng sản phẩm. ZIP hoặc ảnh tải có thể chỉ gồm các cảnh thành công
khi trạng thái là hoàn tất một phần.

## API và cấu trúc mã

Swagger tại `/docs` và schema `/openapi.json` phản ánh route đã đăng ký. Các
endpoint sau là điểm bắt đầu; tham số ROI, đầu ra và lỗi cụ thể xem trong
schema hoặc mã tương ứng.

| Chức năng | Endpoint |
|---|---|
| Trạng thái, địa giới | `GET /api/health`, `GET /api/admin/provinces`, `GET /api/admin/communes` |
| Tìm ảnh GEE / SAR | `POST /api/scene`, `POST /api/s1gee/scene`, `POST /api/composite-plan` |
| Catalogue và đếm ảnh | `POST /api/scene-catalog`, `POST /api/scene-inventory` |
| Vector | `POST /api/vectors/import`, `POST /api/vectors/rois` |
| Sentinel-5P | `GET /api/s5p/products`, `POST /api/s5p/search` |
| Đồng khung | `POST /api/matched-frame/references`, `POST /api/matched-frame/searches`, `POST /api/matched-frame/preflight`, `POST /api/matched-frame/jobs` |
| Time series | `POST /api/timeseries/search`, `POST /api/timeseries/jobs`, `POST /api/timeseries/batches` |
| Esri / DEM | `POST /api/esri/jobs`, `GET /api/dem/download` |
| Sentinel-2 checkpoint | `POST /api/download-jobs`, `GET /api/download-jobs/{id}/file` |
| Trung tâm tác vụ | `GET /api/tasks`, `POST /api/tasks/{kind}/{id}/cancel`, `DELETE /api/tasks/{kind}/{id}` |

`GET /api/tasks` hỗ trợ `status=all|active|finished`, `offset` và `limit`.
`GET /api/tasks/{kind}/{id}/files` và `DELETE` với `delete_files=true` chỉ hỗ
trợ kiểm tra/xóa tệp time series một ZIP và theo năm.

Ứng dụng chính được phục vụ từ [static/index.html](static/index.html),
[static/workspace.js](static/workspace.js), [static/task-center.js](static/task-center.js)
và [app/main.py](app/main.py). Xử lý tác vụ nằm trong
[app/task_center.py](app/task_center.py), [app/timeseries.py](app/timeseries.py),
[app/timeseries_batches.py](app/timeseries_batches.py), [app/frame_jobs.py](app/frame_jobs.py)
và [app/esri.py](app/esri.py). `frontend/` là phần NDVI đang phát triển, không
phải giao diện đang được FastAPI phục vụ.

EODAG là catalogue tùy chọn, mặc định tắt và chỉ dành cho API; không cần cho
các luồng GEE/CDSE/AWS hiện có. Thử trong môi trường riêng với
[requirements-eodag.txt](requirements-eodag.txt), cấu hình `EODAG_ENABLED` và
allowlist provider; không coi kết quả catalogue EODAG là luồng tải/quicklook
đã được hỗ trợ. Xem [thiết kế EODAG](docs/superpowers/specs/2026-10-18-eodag-catalog-integration-design.md).

## Kiểm thử

Dùng thư mục tải tạm **trước khi import `app.main` hoặc chạy kiểm thử** để không
khôi phục, thay đổi trạng thái hoặc dọn dữ liệu của server đang hoạt động. Từ
PowerShell ở thư mục gốc, với môi trường đã cài thêm `httpx` và có Node.js:

```powershell
.\.venv\Scripts\python.exe -m pip install httpx
$env:GEE_DOWNLOADS_DIR = Join-Path $env:TEMP ('gee-test-' + [guid]::NewGuid().ToString())
.\.venv\Scripts\python.exe -m unittest tests.test_timeseries tests.test_timeseries_batches tests.test_timeseries_batch_api tests.test_task_center tests.test_frame_jobs tests.test_frame_api
node --test tests/*.cjs
```

Kiểm thử fixture/mocked xác nhận hành vi được bao phủ, không thay thế nghiệm
thu trên GEE/CDSE/AWS thật, kiểm tra quyền dữ liệu hoặc đánh giá chất lượng ảnh.
Các runner `validate_*.py` và kiểm thử live cần cấu hình, dữ liệu và môi trường
riêng; kiểm tra tham số và thư mục đầu ra trước khi chạy.
