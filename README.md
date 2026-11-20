# GEE Testing — Satellite Workspace

Ứng dụng web chạy cục bộ để tìm, xem trước và tải dữ liệu viễn thám theo vùng
quan tâm. Backend FastAPI phục vụ giao diện bản đồ trong `static/` và kết nối
Google Earth Engine (GEE); một số luồng sử dụng thêm CDSE, AWS hoặc Esri.
Ảnh phản xạ SR chưa được chuẩn hóa BRDF. Quyền truy cập và khả dụng của từng
nguồn phụ thuộc tài khoản, dữ liệu và dịch vụ tương ứng.

## Khả năng chính

- Chọn ROI theo điểm, hình chữ nhật có thể xoay, tỉnh/xã hoặc polygon vector;
  tìm cảnh, xem preview và tải GeoTIFF theo khả năng của từng nguồn.
- Sử dụng Sentinel-2, Landsat, NICFI, Dynamic World, Sentinel-1 và Sentinel-5P
  qua GEE; có các luồng tìm/tải CDSE, Sentinel-2 AWS, Esri và tải DEM/DSM.
- Ghép cảnh quang học RAW hoặc composite, tùy chọn nhóm cảnh theo ngày/lượt
  chụp và cân bằng màu RAW; xem các giới hạn tại [Độ phủ và ghép cảnh](#độ-phủ-và-ghép-cảnh-theo-vị-trí).
- Nhập vector làm ROI; tìm, đếm và xuất danh sách cảnh; tải chuỗi thời gian
  theo snapshot hoặc từng năm; tải ảnh đồng khung với GeoTIFF mẫu.
- Luồng tải Sentinel-2 có checkpoint được điều khiển bằng cờ và đang kiểm chứng;
  EODAG là catalogue tùy chọn, mặc định tắt. Chi tiết ở các mục tương ứng bên dưới.

Giao diện đang phục vụ tại `/` là `static/index.html`. Thư mục `frontend/` là
phần NDVI workbench đang phát triển: hiện thiếu `frontend/src/main.tsx` được
`frontend/index.html` tham chiếu và backend chưa đăng ký `/api/analysis`; không
coi đây là ứng dụng đã sẵn sàng chạy. `app/analysis_models.py` mới định nghĩa
các mô hình yêu cầu liên quan.

## Bắt đầu nhanh

1. Clone repo và lấy dữ liệu Git LFS như mục [Dữ liệu và cấu hình cục bộ](#dữ-liệu-và-cấu-hình-cục-bộ).
2. Tạo môi trường Python và cài `requirements.txt` theo mục [Cài đặt](#cài-đặt).
3. Cấu hình Earth Engine và các tài khoản nguồn dữ liệu cần dùng; không đưa khóa
   hoặc tệp `.env` thật vào Git.
4. Chạy `uvicorn app.main:app --reload --port 8000`, mở
   <http://127.0.0.1:8000>. Tài liệu API FastAPI có tại
   <http://127.0.0.1:8000/docs> khi server đang chạy.

Đây là ứng dụng local/trusted: các job và snapshot không cung cấp xác thực hay
phân quyền người dùng. Cần thiết kế bổ sung trước khi đưa ra mạng công cộng.

## Giao diện Satellite Workspace

Giao diện giữ nguyên API và các nguồn dữ liệu hiện có:

- **Khám phá**: chọn vùng, nguồn và bộ lọc; ghép cảnh/composite/time series nằm
  trong **Tùy chọn nâng cao**. Góc xoay ROI vẫn ở nhóm vùng quan tâm.
- **Kết quả**: xem cảnh, preview, xác nhận composite và tải; panel tự mở khi có
  kết quả mới. Tác vụ time series tiếp tục được theo dõi tại đây.
- **Tác vụ tải / Esri**: cấu hình và theo dõi tải Esri từ vùng chọn trong Khám phá.
  Thay đổi vùng không thay đổi snapshot của tác vụ đã tạo.
- Trên màn hình nhỏ, kết quả mở riêng để tránh che khuất bản đồ và biểu mẫu.

Bộ chọn **Giao diện** trên thanh đầu trang có **Sáng**, **Tối**, và **Theo hệ thống**
(mặc định). Lựa chọn được lưu trên trình duyệt và giữ lại khi tải lại trang;
chế độ hệ thống tự cập nhật khi giao diện hệ điều hành thay đổi. Nếu trình duyệt
chặn lưu trữ, vẫn có thể đổi giao diện trong lần mở hiện tại. Giao diện tối chỉ
đổi màu các bảng và công cụ, không chỉnh màu ảnh vệ tinh hoặc dữ liệu bản đồ.

Lớp trình bày nằm trong `static/workspace.css`, `static/workspace.js` và
`static/theme.js`; không thay đổi thuật toán ROI hoặc luồng API của ứng dụng.

## Sentinel-5P OFFL: tải từng ảnh GEE

Chọn **Sentinel-5P OFFL**, sản phẩm, ngày và vùng điểm / hình chữ nhật / tỉnh / xã;
nhấn **Tìm ảnh**, xem từng scene (ID + thời điểm UTC), **Xem trước** hoặc
**Tải ảnh GeoTIFF**. Mỗi nút tải xuất đúng một asset, một band float32, cắt theo
ROI của snapshot tìm kiếm; không ghép cảnh, không tính trung bình thời gian.
Ngày kết thúc bao gồm cả ngày đó. Có phân trang 50 ảnh; tìm kiếm giới hạn 2000
ảnh và báo nếu bị cắt, khi đó cần thu hẹp khoảng ngày. Không hỗ trợ GeoTIFF mẫu
cho nguồn này. Khoảng mây, ít mây nhất, ghép cảnh và time series không áp dụng.

| Sản phẩm | Collection `COPERNICUS/S5P/OFFL/` | Band | Đơn vị |
|---|---|---|---|
| NO2 | `L3_NO2` | `tropospheric_NO2_column_number_density` | mol/m² |
| SO2 | `L3_SO2` | `SO2_column_number_density` | mol/m² |
| CO | `L3_CO` | `CO_column_number_density` | mol/m² |
| CH4 | `L3_CH4` | `CH4_column_volume_mixing_ratio_dry_air` | ppb |
| O3 | `L3_O3` | `O3_column_number_density` | mol/m² |
| HCHO | `L3_HCHO` | `tropospheric_HCHO_column_number_density` | mol/m² |
| AAI | `L3_AER_AI` | `absorbing_aerosol_index` | không đơn vị |

Lưới L3 **1113.2 m (~1.1 km) không phải độ phân giải gốc cảm biến**. Các giá trị
là lượng cột khí quyển (NO2/HCHO tầng đối lưu, O3 tổng cột), tỷ lệ trộn CH4 cột
khí khô hoặc chỉ số aerosol; **không phải nồng độ bề mặt hay PM2.5**. Không đổi
đơn vị hoặc loại giá trị âm. Giữ mask chất lượng của catalog; phần ngoài ROI và
pixel thiếu là NoData (-9999). Footprint giao ROI không đảm bảo có pixel hợp lệ,
đặc biệt CH4. Preview dùng thang màu cố định có nhãn đơn vị, không đổi giá trị tải.
OFFL có độ trễ và phụ thuộc quyền truy cập/khả dụng GEE. Xem
[catalog chính thức](https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S5P_OFFL_L3_NO2).

API dùng chung xác thực EE và quy ước ROI/grid hiện có:

- `GET /api/s5p/products`: collection, band, đơn vị và cảnh báo.
- `POST /api/s5p/search`: `product`, `start_date`, `end_date`, `selection_mode`
  và các trường ROI hiện có. Ví dụ:

```json
{"product":"NO2","start_date":"2026-01-01","end_date":"2026-01-03","selection_mode":"point","lon":106.7,"lat":10.8,"radius_m":2000}
```

- `GET /api/s5p/search/{search_id}?offset=50&limit=50`: trang snapshot;
  `scenes` chứa `scene_id`, `asset_id`, `timestamp`, `date`, `download_url`, `preview_url`.
- `GET /api/s5p/search/{search_id}/scenes/{index}/preview`: JSON URL PNG + đơn vị.
- `GET /api/s5p/search/{search_id}/scenes/{index}/download`: GeoTIFF của ảnh đã chọn.
  API không nhận asset ID/URL tải tùy ý. Hình chữ nhật và điểm xoay dùng grid hiện có.
  Lỗi EE trả 503 (khởi tạo) / 502 (tìm, preview, tải), không giả dữ liệu.

Kiểm thử offline: `.venv\\Scripts\\python.exe -m unittest tests.test_s5p` và
`node --test tests\\test_s5p_ui.cjs`. Không cần credentials hoặc gọi EE trực tiếp.

## Tải ảnh đồng khung từ GeoTIFF mẫu

Trong **Khám phá → Cách chọn vùng → Theo GeoTIFF mẫu**, chọn một ảnh `.tif`/`.tiff`
(tối đa 512 MiB). Backend đọc metadata và xóa tệp upload ngay sau đó; không nhận
đường dẫn máy chủ tùy ý và không gửi pixel ảnh mẫu sang GEE. ROI là toàn khung
bốn góc affine, kể cả khung xoay/shear, không phải vùng alpha hoặc mask đường.

Nguồn hỗ trợ: **Sentinel-2 SR GEE** và **Sentinel-1 SAR GEE**. Hai cách xuất:

- **Đồng biên** (mặc định): giữ CRS, hướng và biên khung; pixel xấp xỉ 10 m
  (có 20/30/60 m). CRS mẫu phải projected có đơn vị mét. Số hàng/cột được làm
  tròn lên và pixel thực điều chỉnh để không nới khung; không hứa pixel đúng 10 m.
- **Đồng lưới mẫu**: giữ CRS, affine và số hàng/cột chính xác; CRS địa lý được
  hỗ trợ ở chế độ này. Nội suy lên pixel nhỏ không tăng độ phân giải thực.

Khung affine được densify và chuyển sang **một polygon WGS84** bằng GDAL;
hiển thị bản đồ, tìm cảnh, tính mây/độ phủ và preview dùng cùng biên này.
Không gửi polygon bằng WKT tùy chỉnh sang GEE: EQC có `latitude_of_origin`
có thể bị GEE diễn giải lệch về xích đạo dù bản đồ hiển thị đúng.

Chọn khoảng ngày, khoảng mây + bóng trong ROI, tỷ lệ phủ tối thiểu; chọn ảnh
ít mây nhất đạt điều kiện hoặc xem danh sách và chọn tối đa **10 cảnh** từ snapshot.
Bật **ghép cảnh** để tạo một TIFF: cảnh được chọn trước là cảnh chính, cảnh sau
chỉ bù pixel NoData trên lưới đích, không thay pixel hợp lệ hay xóa mây. Mỗi pixel
lấy đủ các band từ cùng một nguồn. SAR chỉ ghép cùng hướng bay/quỹ đạo tương đối.
Độ phủ tối thiểu được kiểm tra trên kết quả ghép, không bắt từng mảnh phủ đủ.
Tắt ghép xuất từng cảnh độc lập; manifest ghi các cảnh/ngày thành phần khi ghép.
Kiểm tra lưới, dung lượng và cảnh báo trước khi xác nhận tải nền. Thay đổi form
không thay scene/lưới của job đã tạo. Tải từng GeoTIFF đã kiểm tra hoặc ZIP gồm
các cảnh thành công và `manifest.json`; job có thể hoàn tất một phần khi một
cảnh lỗi. Tiến độ có thể xem lại khi tải lại trang; không hỗ trợ resume giữa tile.

- Sentinel-2 mặc định **12 band**: B1,B2,B3,B4,B5,B6,B7,B8,B8A,B9,B11,B12;
  preset RGB là B4/B3/B2, RGB+NIR là B2/B3/B4/B8. Float32, DN × 0.0001,
  không clamp. B1/B9 gốc 60 m; B5/B6/B7/B8A/B11/B12 gốc 20 m; các kênh còn
  lại 10 m. Bilinear chỉ nội suy vào lưới đích, không tạo chi tiết 10 m cho kênh 60 m.
- Sentinel-1 IW giữ **native dB**, nearest, không log10 hoặc clamp; hỗ trợ VV/VH,
  VV, HH/HV, HH. Có lọc hướng bay, relative orbit, platform và ưu tiên quỹ đạo
  tham chiếu đã xác nhận. Chỉ ghép khi bật tùy chọn và chọn rõ các cảnh; có thể
  khác ngày nhưng không ghép khác track. Luồng tải SAR cũ giữ nguyên.
- Zero hợp lệ được giữ; thiếu dữ liệu nguồn giữ NaN/NoData, chỉ lấp lỗ khi bật ghép.
  Độ phủ tìm kiếm là ước lượng; manifest ghi thêm tỷ lệ hợp lệ thực theo band
  sau tải. Ngưỡng tối thiểu tùy chọn 0–100%, mặc định ≥99,9%; kết quả dưới ngưỡng
  bị báo lỗi. API cũ `require_full_coverage=true` vẫn yêu cầu ≥99,9%.
- Mây ROI lấy SCL 8/9/10, bóng mây 3, tuyết 11 báo riêng; phần trăm chất lượng
  dùng vùng SCL hợp lệ, coverage dùng toàn ROI. Không nhầm với tỷ lệ mây toàn tile.
  **0% SCL không đảm bảo không sương/mây mỏng hoặc ảnh nhìn đẹp**. Mây được giữ
  trong TIFF, không lọc bỏ. Preview S2 RGB stretch 0–0.3 và SAR -25..0 dB chỉ
  để hiển thị, không thay dữ liệu xuất. Đồng khung không phải đồng đăng ký đối tượng.

Tính năng dùng SQLite trong `downloads/matched_frame`, một export worker và một
search tại một thời điểm. Giới hạn: tìm tối đa 366 ngày/500 cảnh; 30.000 pixel/cạnh,
100 triệu pixel/cảnh, 4 GiB chưa nén/job; dung lượng trống tối thiểu 512 MiB và
reserve gồm output/ZIP, staging Float32 + mask từng band + halo và bộ nhớ warp
(estimator version 2; snapshot cũ được tính lại reserve khi chạy, không sửa snapshot).
Không tự giảm chất lượng khi vượt quota.

### CRS tùy chỉnh và projection fallback

CRS EPSG chuẩn vẫn đi đường trực tiếp. CRS không có mã EPSG đi qua lưới WGS84
trung gian ngay từ đầu (`custom_reference_projection`), tránh việc writer chấp nhận
nhưng diễn giải WKT khác GDAL. Với EPSG chuẩn, chỉ HTTP 400 `INVALID_ARGUMENT`
có lỗi writer `Unable to write GeoTIFFs in projection` kích hoạt fallback;
401/403, quota, timeout, asset và tham số khác không kích hoạt fallback. Body lỗi
chỉ đọc tối đa 4097 byte để phân loại; thông báo upstream không được công bố.
UI hiện có hiển thị text an toàn theo lỗi backend, không suy quyền từ HTTP 502.

Fallback chọn UTM WGS84 nếu khung thuộc một múi, ngoài ra EPSG:4326; densify
64 đoạn/cạnh, snap ra ngoài và halo 2 pixel. Không cấp phát quá 100 triệu pixel,
30.000 pixel/cạnh hoặc 4096 tile trung gian. Tile ≤2048/cạnh, ≤16 MiB giá trị.
Giá trị và mask từng band được tải nearest, ghi mosaic trên đĩa; warp cửa sổ
về **đúng CRS/affine/width/height ban đầu**, mask nearest rồi giao tại đích.
S1 giữ Float32 dB/nearest; S2 đã scale một lần, chỉ bilinear tại warp cuối.
Warp/cache giới hạn 64 MiB mỗi phần, một thread; không warp từng tile riêng.
`PROCESSING` cũ không đổi; `EXPORT_PROVENANCE` version 1 trong TIFF và
`export_provenance` từng report/manifest ghi chiến lược và chuỗi nội suy thực tế.
TIFF chỉ được công bố atomic sau kiểm tra lại grid, metadata và coverage.

Phạm vi an toàn: CRS authority WGS84 hoặc spherical Equirectangular rõ ràng.
Với spherical EQC, phép đổi dùng góc địa lý của bản đồ cầu (kiểm tra round-trip),
**không** khẳng định datum được đo đạc hoặc đồng đăng ký WGS84. Không gán datum
mới cho TIFF đích. CRS/datum khác bị từ chối `unsupported_reference_crs` thay vì
ballpark. Khung cắt kinh tuyến 180 bị từ chối `unsupported_transport_extent`;
hãy chia ROI thay vì tải bbox gần toàn cầu. Đây là giới hạn có chủ đích của bản sửa.

Wire NoData dùng `-999999.25` (Float32 chính xác, có phần thập phân) để tránh
EE suy giá trị cực tiểu Float32 dạng số nguyên và tràn về Int64 minimum.
Decoder nhận cả sentinel cũ bị tràn, không tính nó là pixel hợp lệ; validator
không cho công bố TIFF có sentinel chưa chuyển NaN. Không dùng zero làm NoData.

Job lỗi là lịch sử bất biến: tạo search/preflight/job mới, không sửa job cũ.
Nếu snapshot hết hạn hoặc sai phiên bản ROI, tìm lại và dùng cùng metadata mẫu.
`validate_frame_projection_live.py` và `downloads/projection_validation` là runner/
bằng chứng lịch sử về projection; bản sửa mới được nghiệm thu bằng
`validate_matched_frame_live.py` với search mới như mô tả bên dưới.
Server đang chạy phải được chủ sở hữu reload/restart để nạp code mới; xác minh
CLI không chứng minh server đã reload.
Metadata/search/kết quả hết hạn sau 24 giờ không hoạt động, không xóa job active
hoặc tệp đang tải. Restart đánh dấu job đang chạy là interrupted, cần tạo lại.
Đây là ứng dụng **localhost một người dùng**, chưa có auth/ownership đa người dùng.
Cài dependency theo [requirements.txt](./requirements.txt), có `python-multipart`.

API mới đặt dưới `/api/matched-frame`: upload `references`, tìm `searches`,
xác nhận `preflight`, tạo/theo dõi `jobs`, tải `jobs/{id}/files/{token}` hoặc
`jobs/{id}/download`. ID và scene token chỉ hợp lệ trong snapshot tương ứng;
không nhận URL tải ký hoặc asset/CRS tùy ý tại endpoint tạo job.

### Bản sửa matched-frame ngày 2026-10-25

Khi chuyển từ **Theo GeoTIFF mẫu** sang **ROI**, ứng dụng dùng lại polygon WGS84,
metadata và lưới đã chọn của mẫu. **Vẽ ROI mới** dùng bản nháp chữ nhật riêng;
**Dùng lại khung mẫu** phục hồi polygon gốc, không cần upload lại. Chỉ thay hoặc
xóa mẫu rõ ràng mới thay bản nháp mẫu; thông tin tham chiếu vẫn tuân theo TTL server.

Selector vệ tinh dùng chung cho đồng khung và ROI GEE: Sentinel-1A/B/C/D,
Sentinel-2A/B/C hoặc **Tất cả**. Lựa chọn được giữ riêng theo dataset và áp dụng
trước khi tìm/xếp hạng, kể cả nguồn bù vùng SAR và danh sách S2 GEE. Không có ảnh
đúng vệ tinh thì trả rỗng; không tự tìm lại với tất cả vệ tinh.
Manifest giữ bộ lọc yêu cầu ở `platform` và vệ tinh thực của từng cảnh ở
`reports[i].platform`, kể cả khi chọn **Tất cả** hoặc có cảnh tải lỗi.

Search mới có identity ROI phiên bản 2; snapshot thiếu/sai identity trả
`409 stale_search` và cần tìm lại. Preflight phiên bản 2 luôn có `can_start`.
Với nhóm ghép, server tải mask từng band, đo hợp mask chung trên đúng lưới xuất
trong ngân sách 120 giây; không cộng phần trăm từng nguồn. Nhóm thiếu phủ vẫn
có preview nhưng không bật xác nhận; POST job trả `422 insufficient_selection_coverage`.
Preview nhóm 512 pixel chỉ mô phỏng thứ tự nguồn. Mask là ước lượng; độ phủ hữu
hạn chung các band của TIFF cuối vẫn phải đạt ngưỡng trước khi công bố tệp.

Nghiệm thu bằng GEE thật và server HTTP riêng:

```powershell
.venv/Scripts/python.exe -W ignore validate_matched_frame_live.py
```

Runner đọc fixture lịch sử từ kho audit bằng SQLite read-only, upload georeference
mẫu, tạo search mới và lưu proof/TIFF/ZIP tại `downloads/matched_frame_acceptance/<run-id>`.
Nếu fixture ở nơi khác, truyền `--audit-store <audit-store>/frames.sqlite3`.
Nếu runner dừng giữa chừng, có thể dùng `--resume-run <run-directory>`; runner
chỉ dùng lại artifact đã kiểm checksum/độ phủ khi hash mã app/static vẫn khớp.
Runner đặt `GEE_DOWNLOADS_DIR` cho tiến trình server con trước khi nạp ứng dụng;
mọi store của tiến trình đó nằm trong thư mục nghiệm thu, không dùng kho production.
`server.json` ghi PID, port, thời điểm khởi động và hash mã thực sự nạp; server này
không chứng minh một server có sẵn đã reload. Không cần sửa cấu hình dữ liệu khi
chạy ứng dụng bình thường.

Xem [kế hoạch sửa](docs/superpowers/plans/2026-10-25-matched-frame-repair.md)
và [báo cáo thực hiện](docs/superpowers/plans/2026-10-25-matched-frame-implementation.md),
cùng [kiểm tra bổ sung](docs/superpowers/plans/2026-10-25-matched-frame-followup.md)
để phân biệt các kiểm tra đã đạt và phần nghiệm thu còn lại.

Nghiệm thu luồng giao diện trong browser thật:

```powershell
.venv/Scripts/python.exe -W ignore validate_matched_frame_browser.py
```

Mở URL localhost runner in ra và bấm **Bắt đầu nghiệm thu** một lần. Browser
chạy tuần tự với giao diện thật, kiểm giữ mẫu/ROI thủ công, tìm S1D bằng reference_id
cũ rồi tải TIFF/ZIP. Runner không mở hoặc attach Chrome. Proof chỉ được đánh dấu
hoàn tất sau khi server kiểm lại job, lưới, coverage và checksum artifact với
dữ liệu browser đã tải. Bằng chứng ở `downloads/matched_frame_browser/<run-id>/`;
mọi store đều tách khỏi production. Dừng runner sau khi đã ghi proof; chạy lại
lệnh sẽ tạo một lượt nghiệm thu riêng.

## Dữ liệu và cấu hình cục bộ

Repository không kèm thông tin đăng nhập, chứng chỉ CA nội bộ hoặc ảnh đã tải.
Dữ liệu ranh giới tỉnh/thành và xã/phường được quản lý bằng **Git LFS** vì file
shapefile xã/phường vượt 100 MiB. Cài Git LFS trước khi clone; sau khi clone chạy:

```powershell
git lfs install --local
git lfs pull
```

- Sao chép `.env.example` thành `.env` và điền thông tin đăng nhập của bạn;
  đặt khóa service account theo `GEE_KEY_FILE`. Không commit các khóa này.
- `shapefile34/` chứa bộ shapefile tỉnh/thành và `vn34.json` dùng trong ứng dụng.
  Có thể tạo lại JSON bằng `convert_shapefile34.py`. Bản ZIP trùng dữ liệu không
  được commit.
- `Shapefile_PX_VN_34/` chứa bộ shapefile xã/phường gồm các file `.shp`, `.shx`,
  `.dbf`, `.prj`, `.cpg`.
- Chỉ sử dụng hoặc phân phối dữ liệu ranh giới khi có quyền phù hợp. Khi push,
  Git LFS tải dữ liệu lên riêng; dung lượng lưu trữ và tải xuống chịu hạn mức
  Git LFS của tài khoản GitHub. Thư mục `shapefile/` cũ vẫn được giữ ngoài Git.
- Nếu mạng nội bộ yêu cầu CA riêng, bổ sung `vingroup_cas.pem` trên máy;
  `combined_cas.pem` được ứng dụng tạo cục bộ. Không commit chứng chỉ nội bộ.

Một số lệnh nghiệm thu lịch sử bên dưới nhắc đến `validate_*.py` ở thư mục gốc.
Đó là script kiểm tra cục bộ, không nằm trong danh sách dự kiến push; người clone
repo không nên giả định chúng có sẵn. Các script trong `tests/` và `tools/` là
những thành phần kiểm tra riêng được giữ trong danh sách dự kiến push.

## Ghép bù vùng thiếu SAR GEE

Chọn **Sentinel-1 SAR GEE** → bật **Ghép nhiều cảnh SAR (bù vùng thiếu)**.
Áp dụng điểm, chữ nhật và tỉnh/xã; không áp dụng time series hoặc ZIP CDSE.
API `POST /api/s1gee/scene` và `GET /api/s1gee/download` thêm
`merge_scenes=true` (mặc định false). Link tải giữ lựa chọn này.

Kết quả tìm kiếm trên giao diện hiển thị riêng hướng bay ASCENDING và
DESCENDING khi có dữ liệu. Chọn hướng bay để xem preview, độ phủ và danh sách
cảnh tương ứng; link tải giữ các ID cảnh đã chọn và kiểm tra lại chúng khi tải.

Giữ pixel cảnh chính; chỉ lấp phần thiếu bằng cảnh IW cùng phân cực, hướng bay
và quỹ đạo tương đối, ưu tiên gần thời điểm cảnh chính. Xét tối đa 24 cảnh bổ
sung, chỉ giữ cảnh tăng độ phủ, dừng khi đạt ≥99%. Không lấy trung bình dB và
không tự trộn track khác. Giữ xử lý dB/clamp hiện có. Độ phủ tính từ mask chung
các band trên toàn ROI, kể cả phần không có footprint; ước lượng ở lưới 100 m.
Nếu vẫn dưới 99%, hiển thị cảnh báo nhưng vẫn cho tải phần có dữ liệu; cần mở
rộng khoảng ngày/thu nhỏ ROI. Kết quả liệt kê số cảnh, ngày và ID sử dụng.
Mosaic có tên vùng/khoảng ngày/số cảnh, không giả là một sản phẩm nguyên gốc.
Link tải giữ danh sách cảnh nhưng chưa phải snapshot bất biến của toàn bộ yêu cầu.
Ghép khác ngày vẫn có thể gây đường nối/chênh lệch tán xạ; không thay thế chuỗi
SAR cùng quỹ đạo dùng cho phân tích biến động.

## Tên file ảnh GEE

Tải SAR Sentinel-1 giữ nguyên **toàn bộ ID sản phẩm** (không cắt 40 ký tự),
thêm phân cực xuất và ROI (tọa độ/bán kính, chữ nhật/góc xoay hoặc ID tỉnh/xã).
Chọn SAR theo tỉnh/xã mặc định là một cảnh; bật tùy chọn ghép SAR để bù độ phủ.
Ảnh quang học tải một cảnh cũng giữ đầy đủ ID cảnh; tên composite vẫn mô tả vùng,
khoảng ngày và phương pháp ghép. File tạm trên máy chủ dùng UUID ngắn, độc lập
với tên tải xuống để tránh giới hạn đường dẫn Windows. Không thay đổi dữ liệu ảnh.

## Tải DEM / DSM từ GEE

Chọn nguồn **DEM — ALOS AW3D30**, **DEM — SRTM**, hoặc **DEM — Copernicus
GLO-30**, chọn vùng theo điểm, chữ nhật (kể cả xoay), tỉnh hoặc xã, rồi bấm
**Tải DEM GeoTIFF (30 m)**. Không áp dụng khoảng ngày, lọc mây hoặc time series.
GeoTIFF một band `elevation`, Float32, độ cao mét, NoData -9999; vùng hành chính
được cắt theo polygon. DEM được reproject sang **WGS 84 / UTM theo tâm vùng**
(EPSG:326xx ở Bắc bán cầu, EPSG:327xx ở Nam bán cầu; ngoài giới hạn UTM dùng
WGS 84 / UPS EPSG:32661 hoặc 32761). Lưới Bắc–Nam, pixel 30 × 30 m; ROI xoay
được giữ bằng mask NoData ngoài polygon, không dùng CRS Equirectangular tùy chỉnh.
Vùng trải nhiều múi dùng múi theo tâm vùng. Vùng lớn tự chia tile và ghép.
File DEM đã tải trước bản sửa không tự đổi CRS, cần tải lại. SAR/quang học và
các lưới xoay khác không thay đổi. Chuyển hệ ngang không đổi mốc độ cao đứng.
Tên file DEM gồm nguồn, độ phân giải, ROI (tọa độ/kích thước/góc hoặc ID hành
chính), thời điểm UTC và mã ngẫu nhiên 12 ký tự. Mỗi lượt tải có tên riêng;
giao diện giữ tên do backend trả về, không thay bằng tên cố định. Các luồng tải
khác không thay đổi.

| Nguồn | Asset GEE | Band nguồn | Lưu ý |
|---|---|---|---|
| ALOS AW3D30 | `JAXA/ALOS/AW3D30/V4_1` | `DSM` | DSM gồm cây/công trình, mốc cao EGM96 |
| SRTM | `USGS/SRTMGL1_003` | `elevation` | Phủ khoảng 56°S–60°N, mốc cao EGM96 |
| Copernicus GLO-30 | `COPERNICUS/DEM/GLO30_2024_1` | `DEM` | DSM, mốc cao EGM2008 |

Không phải DTM đất trống; các nguồn có thể khác mốc cao và thời kỳ thu nhận.
Không nội suy lấp vùng thiếu. Xuất lưới 30 m không làm tăng độ chính xác gốc.
API: `GET /api/dem/download?dataset=alos&lon=107&lat=11&radius_m=2000`;
`dataset=alos|srtm|copernicus`, các trường ROI giống API tải GEE hiện có, không
cần ngày. Cần cấu hình tài khoản GEE như các nguồn ảnh khác.

## Lỗi kiểu dữ liệu khi liệt kê ảnh GEE

Nếu gặp `'Element' object has no attribute 'select'`, cập nhật mã và khởi động
lại backend. Luồng chuẩn bị ảnh catalogue/time-series ép lại `ee.Image` sau các
phép biến đổi có `copyProperties`, vì SDK có thể trả `ee.Element` không có
phương thức chọn băng. Việc ép kiểu không thay đổi băng, hệ số scale hoặc metadata.

## ROI chữ nhật — giai đoạn 2 (API GEE)

GEE hỗ trợ `selection_mode="rectangle"` với `center_lon`, `center_lat`,
`width_m`, `height_m` và `rotation_deg`. Kích thước tính bằng mét, góc xoay theo
chiều kim đồng hồ; không truyền đồng thời `lon`/`lat` hoặc ID tỉnh/xã.
Ví dụ body cho `POST /api/scene`:

```json
{
  "dataset": "sentinel2",
  "selection_mode": "rectangle",
  "center_lon": 107.1,
  "center_lat": 11.2,
  "width_m": 12000,
  "height_m": 6000,
  "rotation_deg": 25,
  "start_date": "2026-09-19",
  "end_date": "2026-10-19"
}
```

Tìm kiếm, đánh giá độ phủ/mây và preview dùng polygon thật; link tải trong kết
quả giữ thông số chữ nhật để xuất GeoTIFF theo lưới chữ nhật xoay. Sentinel-1
GEE cũng hỗ trợ ROI này. Time series và các đường crop/tải CDSE/AWS/Esri
đã hỗ trợ qua API ở giai đoạn 4; giao diện vẽ được bổ sung ở giai đoạn sau.

### Giai đoạn 3: catalogue và snapshot chữ nhật

`POST /api/scene-catalog` nhận cùng các trường chữ nhật ở ví dụ trên, thêm
`cloud_min`/`cloud_max` nếu cần. Không truyền `radius_m`, `lon`, `lat`,
`province_id` hoặc `commune_id` với mode chữ nhật.

- `dataset=sentinel2|landsat`: GEE tìm các cảnh giao polygon thật, không tự
  chọn ít mây hay tạo composite. Lọc mây dùng metadata toàn cảnh như catalogue cũ.
- `dataset=s2, source=cdse|aws`: tìm theo bbox bao ngoài polygon xoay.
  `dataset=s2aws` vẫn chọn AWS như trước.
- `dataset=s2, source=eodag, provider=earth_search|planetary_computer`:
  dùng bbox bao ngoài; vẫn cần bật/cài EODAG riêng và vẫn catalogue-only.
- Snapshot lưu thông số chữ nhật, `roi_geometry`, `bbox` và lưới xuất GEE
  (`output_grid`). Phân trang và mở lại sau restart không tìm lại provider.
- Link tải đúng cảnh GEE dùng polygon/lưới đã lưu, cả góc 0° và góc xoay khác.
- Từ giai đoạn 4, catalogue chữ nhật CDSE/AWS có link tải crop; redirect giữ
  thông số chữ nhật đã lưu, không chuyển sang tỉnh/xã hoặc bỏ ROI. Snapshot
  chữ nhật tạo ở giai đoạn 3 cũng dùng được các link này sau khi nâng cấp.
  EODAG vẫn trả HTTP 409 catalogue-only.
- Mode điểm/tỉnh/xã, link tải và snapshot cũ giữ nguyên hành vi.

Kiểm thử: `.venv\Scripts\python.exe -m unittest tests.test_rectangle_catalog tests.test_scene_catalog tests.test_eodag_catalog`.

### Giai đoạn 4: time series và tải/cắt theo chữ nhật (API)

Các API sau nhận `selection_mode=rectangle` cùng `center_lon`, `center_lat`,
`width_m`, `height_m`, `rotation_deg` như ví dụ. Không trộn với tâm/bán kính điểm
hoặc ID hành chính. Góc 0° vẫn xuất đúng lưới rộng/cao độc lập.

| API | Hành vi |
|---|---|
| `POST /api/timeseries/search` | Body thêm dataset, ngày và polarization khi cần; dùng polygon thật, snapshot giữ ROI/lưới. Các job xuất cùng một lưới kể cả khi cảnh đầu lỗi. |
| `GET /api/s2/aws/download` | Query thêm `product_id`; đọc COG window + halo rồi resample về lưới chữ nhật. |
| `GET /api/s2/bands` | Query thêm `product_id`, `action=download`, `bands`; dùng CDSE Nodes, giữ cơ chế token OData cũ, không tự chuyển sang S3. |
| `GET /api/s1/crop` | Query thêm `product_id`, `scale_m` (10–500 m); warp measurement GRD có GCP trực tiếp về lưới chữ nhật. |
| `GET /api/s1/crop-mosaic` | Query thêm `product_ids` (1–6 ID), `scale_m`; ghép các GRD trên cùng lưới chữ nhật. |
| `POST /api/esri/jobs` | Body thêm zoom và giới hạn tile; snapshot/resume dùng polygon và lưới chữ nhật, tile phủ envelope cùng halo. |

Time series giữ manifest/ZIP và mask dữ liệu thiếu như trước. Các băng phân loại
(SCL, Dynamic World) dùng nearest-neighbor; dữ liệu liên tục dùng nội suy theo
pipeline hiện tại. Crop GRD CDSE vẫn là số đo thô, không phải sigma0 đã hiệu chuẩn.
ZIP sản phẩm gốc CDSE/S3 vẫn tải toàn sản phẩm, không cắt hay đóng gói lại theo ROI.
EODAG vẫn catalogue-only.

### Giai đoạn 5: vẽ và chỉnh ROI trên bản đồ

Chọn **Vẽ hình chữ nhật (ROI)** trong chế độ chọn vùng, kéo trên bản đồ để vẽ.
Sau khi vẽ, kéo tâm/thân để di chuyển, góc hoặc cạnh để thay đổi kích thước đối
xứng quanh tâm, tay nắm xoay để đổi góc. Cũng có thể nhập tâm, chiều rộng, chiều
cao và góc trực tiếp. **Esc** hủy thao tác đang kéo; các nút **Vẽ / vẽ lại**, **Xóa ROI**
và **Xem toàn ROI** dùng để thay hoặc xem vùng chọn. Mỗi chiều tối đa 90 km;
đây là hình chữ nhật theo hệ mét cục bộ, không phải hình chữ nhật trắc địa.

Trong **Đồng khung từ GeoTIFF mẫu**, upload mẫu rồi bấm **Copy khung sang ROI
chữ nhật** để chuyển sang bộ chỉnh ROI. Bản copy độc lập với mẫu: xóa mẫu đã lưu
hoặc thay GeoTIFF không làm mất ROI đã copy. Có thể xoay, đổi kích thước, di
chuyển và xóa bằng các tay nắm/nút như khung vẽ tay. Footprint cong hoặc méo
do phép chiếu/affine được bao bằng hình chữ nhật xoay theo hệ mét cục bộ;
không giữ lưới pixel của mẫu. Giới hạn mỗi chiều vẫn là 90 km.

- GEE quang học/Sentinel-1, catalogue S2, time series và Esri gửi cùng thông số
  chữ nhật; không quy đổi thành vùng vuông tâm/bán kính. Sửa ROI làm vô hiệu
  kết quả đang chờ; phản hồi cũ không ghi đè lựa chọn mới.
- Kết quả catalogue giữ link snapshot với vùng đã tìm. ZIP nguyên sản phẩm
  được ghi rõ không cắt ROI. Tải crop CDSE Nodes vẫn cần xác thực OData riêng.
- Tìm kiếm chữ nhật S1 GRD/SLC, cặp InSAR và Sentinel-3 chưa có API tìm vùng
  phù hợp nên được báo chưa hỗ trợ. Với S1 GRD có thể nhập UUID sản phẩm đã biết
  để tạo link crop/mosaic chữ nhật; không áp dụng cho SLC.
- Esri có lựa chọn riêng **Hình chữ nhật ROI đang chỉnh ở trên**; bbox nhập tay
  vẫn là một mode độc lập. Không nhầm ROI đang chỉnh với vùng của job cũ.
- Khởi động lại backend để nạp API mới, sau đó Ctrl+F5 để tải các script mới.
  Backend cũ chưa hỗ trợ `rectangle` sẽ không chạy được chỉ bằng tải lại trang.

Kiểm thử giao diện: `node --test tests/test_rectangle_ui.cjs tests/test_admin_map.cjs`.

### Giai đoạn 6: kiểm chứng tích hợp ROI

- Chạy toàn bộ test Python: `.venv\Scripts\python.exe -m unittest discover tests`.
  Các test SDK/smoke EODAG có thể được skip khi chưa cài SDK hoặc chưa opt-in;
  không coi các test này là đã kiểm chứng provider thật.
- `tests/test_rectangle_e2e.py` gọi HTTP tìm ảnh rồi đi theo link tải trả về
  cho Sentinel-2, Landsat và Sentinel-1 GEE ở góc 0°, 25°, 90°, 360°.
  Provider được mock; raster được xuất thật từ fixture. Kiểm tra shape, affine,
  CRS, mask, pixel 0 hợp lệ, halo và dọn file trung gian.
- Đã sửa lỗi tích hợp Sentinel-1 GEE download: góc 0° phải dùng polygon chữ nhật,
  không dùng tọa độ điểm rỗng. Export GEE lấy thêm halo, không clip mất pixel
  nội suy trước khi warp; lưới đích vẫn giữ đúng footprint ROI.
- Smoke ngày 2026-10-19: trình duyệt tìm AWS thật được 2 cảnh tại ROI tâm
  107.14828, 11.17714, rộng 200 m, cao 100 m, xoay 25°, ngày 01–10/10/2026.
  Tải cảnh `S2C_48PYT_20261008_0_L2A` qua link snapshot trả HTTP 200;
  GeoTIFF có 10 hàng × 20 cột, B02/B03/B04/B08/SCL, CRS và affine khớp ROI,
  mask băng đầu hợp lệ 100%. Đây là kiểm tra hình học/tải, không xác nhận ảnh ít mây.
- Browser kiểm tra sửa kích thước không đổi snapshot cũ, ẩn kết quả cũ và chuyển
  lại mode điểm/xã hoạt động. Live GEE bị Web Gateway công ty chặn ngay bước
  khởi tạo; chưa xác minh live export GEE, CDSE hoặc Esri trong đợt kiểm tra này.
  Không tắt TLS hoặc vượt chính sách mạng để khắc phục.

Kiểm thử: `.venv\Scripts\python.exe -m unittest tests.test_rectangle_downloads tests.test_rectangle_catalog tests.test_esri_rectangle tests.test_rotated_exports tests.test_timeseries`.

## Đo thời gian tìm ảnh GEE

`POST /api/scene` và `POST /api/composite-plan` trả header `Server-Timing`
(milliseconds) và `X-Search-Request-ID`. Giao diện hiển thị thời gian chờ phản hồi
và từng giai đoạn ngay dưới trạng thái tìm kiếm:

- `ee_init`: khởi tạo/xác thực Earth Engine; lần đầu trong process có thể chậm.
- `selection`: chuẩn bị vùng điểm/tỉnh/xã.
- `scene_search`: chọn ảnh hoặc tạo composite, gồm các truy vấn GEE bên trong.
- `preview_url`: xin URL preview, không gồm thời gian trình duyệt tải ảnh PNG.
- `composite_plan`: truy vấn lập kế hoạch, là request riêng trước khi xác nhận.
- `total`: thời gian backend từ lúc nhận request đến khi bắt đầu trả response,
  gồm validation, chờ thread worker và serialize; không gồm mạng tới trình duyệt.

Terminal Uvicorn ghi dòng `search_timing` với mã request, route, HTTP status và
thời lượng, không ghi tọa độ, credential hoặc URL provider. Các giai đoạn được
đo cả khi thất bại; chưa đến giai đoạn nào thì header không có giai đoạn đó.
Không thay đổi thuật toán chọn ảnh, số lần gọi GEE hoặc nội dung JSON trả về.
Để nhận header backend mới, khởi động lại server; Ctrl+F5 để nhận giao diện mới.
Kiểm thử: `.venv\Scripts\python.exe -m unittest tests.test_request_timing`.

## Địa giới hành chính trên bản đồ

- Địa giới 34 tỉnh/thành được bật khi mở bản đồ, có tên khi rê chuột;
  bật/tắt tại mục **Địa giới tỉnh/thành** trong bộ chọn lớp bản đồ.
- Chế độ **Theo địa phận hành chính** cho phép chọn tỉnh trong danh sách hoặc
  bấm trực tiếp trên bản đồ. Ở chế độ điểm, bấm bản đồ vẫn chọn tọa độ như cũ.
- Chế độ **Theo xã/phường** có ô tìm tên xã/phường hoặc tỉnh/thành, hỗ trợ nhập
  không dấu. Chọn một đơn vị để tải và tô ranh giới, tự phóng bản đồ đến vùng đó.
  Chỉ tải hình học của đơn vị được chọn, không tải toàn bộ ranh giới xã cả nước.
- Đổi xã/phường hoặc chế độ sẽ bỏ hình học cũ; phản hồi đến muộn không thay thế
  lựa chọn mới. Nếu tải lỗi, thông báo hiện ngay dưới danh sách xã/phường.
- Kiểm thử tương tác địa giới không cần cài thêm gói: `node --test tests/test_admin_map.cjs`.

## Kiểm tra đúng lượt tải và góc xoay

Ảnh đã tải và job Esri cũ không thay đổi khi chỉnh góc trên bản đồ. Sau khi đổi góc cần tìm ảnh/tạo lượt tải mới. Kết quả GEE hiển thị **Góc lưới xuất**; trạng thái Esri hiển thị góc đã lưu của job và cảnh báo khi đó là job bbox Bắc–Nam cũ. GeoTIFF xoay có các hệ số affine ngoài đường chéo khác 0 (trừ góc trùng trục); phần mềm GIS vẫn có thể hiển thị bản đồ hướng Bắc do tôn trọng georeference.

## Chọn ảnh theo khoảng ngày và phần trăm mây

- Mặc định bật **Tự chọn ảnh ít mây nhất**, giữ cách tìm ảnh/composite hiện tại.
- Tắt tùy chọn để liệt kê Sentinel-2 GEE/CDSE/AWS hoặc Landsat GEE theo ngày bắt đầu–kết thúc (bao gồm cả ngày kết thúc) và khoảng **Mây tối thiểu–tối đa**, mặc định **0–100%**.
- Kết quả mới nhất trước, 50 ảnh/trang, có ngày chụp, % mây toàn cảnh, vệ tinh/ID và liên kết tải **đúng cảnh đó**, không chạy lại chọn ảnh ít mây nhất. Vùng tải và góc xoay được giữ trong snapshot tìm kiếm.
- % mây là metadata toàn cảnh, không phải % mây riêng vùng chọn. Cảnh không có metadata mây bị loại khỏi khoảng lọc; 0% là giá trị hợp lệ.
- Catalogue ngoài GEE có giới hạn an toàn: CDSE tối đa 500 kết quả, AWS tối đa 4.000. Khi chạm giới hạn, giao diện yêu cầu thu hẹp khoảng ngày; không trình bày danh sách bị giới hạn là đầy đủ.
- **Time series** là chế độ độc lập, vẫn lấy mọi cảnh theo hành vi cũ, không chịu bộ lọc này. Sentinel-1/NICFI/Dynamic World không hiện tùy chọn mây mới.

## Optional EODAG catalogue MVP (API only)

EODAG is disabled by default and is not needed for existing GEE/CDSE/AWS searches
or downloads. Install `requirements-eodag.txt` in a **separate** environment; it
includes the base requirements and pins EODAG 4.9.2. Do not upgrade the running
application's environment to try this feature.

- Set `EODAG_ENABLED=true` only after isolated SDK/provider smoke checks pass.
- `EODAG_PROVIDERS=earth_search,planetary_computer` is the administrator allowlist.
- `EODAG_CONFIG_FILE` optionally names a server-side configuration file. Local SDK
  configuration/cache belongs under `downloads/eodag`, not global user config.
- `EODAG_MAX_RESULTS=4000` accepts 1–4000; pages request 100 provider items, with
  a 200-page/120-second budget and per-request timeout at most 30 seconds, no retries.
- `EODAG_QUICKLOOK_MAX_BYTES=10485760` is reserved and ignored in this release.

POST `/api/scene-catalog` with `dataset=s2`, `source=eodag`, a required `provider`,
and normal selection/date/cloud fields. Optional `eodag_collection` must be
`S2_MSI_L2A_COG` for Earth Search or `S2_MSI_L2A` for Planetary Computer; native
collection is `sentinel-2-l2a` for both. Provider/collection fields are rejected on
legacy requests; EODAG cannot be combined with `s2aws` or GEE datasets.

Dates include the end day; filtering uses the selection bbox (not exact polygon
coverage). Missing cloud values are excluded, metadata is not harmonized or
inferred, and scenes remain provider-specific immutable snapshots, 50 per page.
`total` counts stored qualifying scenes. Inspect `parameters.completion`,
`truncated`, `truncation_reason`, and `warnings` before treating a result as complete.
Disabled/missing SDK/config returns 503; first-page provider failure returns 502;
a failure after successful pages returns a partial snapshot. There is no provider
fallback, discovery, quicklook, UI, or download handoff. EODAG rows never have a
`download_url`; direct snapshot download attempts return 409.

Offline verification: `python -m unittest discover -s tests -q` in both isolated
base and optional environments, then `python -m pip check`. For opt-in live
catalogue-only checks run `python -m unittest discover -s tests -p test_eodag_smoke.py -v`
with `EODAG_SMOKE=true` and `EODAG_ENABLED=true`. The smoke test prints identity,
metadata, and completion evidence for each provider; it never fetches assets.
Live provider failures block EODAG enablement, not the default application.

Implementation validation in this workspace: base-only offline tests pass. Optional
installation could not complete because the package proxy returned HTTP 403
`MediaTypeBlocked` for a dependency wheel (after using the existing corporate CA).
Real SDK/provider checks therefore remain unverified; keep EODAG disabled until
those checks pass. The existing base environment also reports unrelated missing
`google-crc32c` and an `opentelemetry-api` version conflict in `pip check`.

## Nguồn dữ liệu

| Dataset | GEE ID | Scale | Cloud mask |
|---|---|---|---|
| Sentinel-2 SR | `COPERNICUS/S2_SR_HARMONIZED` | 10 m | SCL band |
| Landsat 8/9 SR | `LANDSAT/LC08/C02/T1_L2`, `LANDSAT/LC09/C02/T1_L2` | 30 m | QA_PIXEL |
| Planet NICFI | `projects/planet-nicfi/assets/basemaps/asia` | 4.77 m | composite tháng (đã khử mây) |
| Dynamic World land cover | `GOOGLE/DYNAMICWORLD/V1` | 10 m | dự đoán đã mask mây/bóng mây |
| Sentinel-1 SAR GEE | `COPERNICUS/S1_GRD` | 10 m | SAR xuyên mây (không cần) |

Trong **Band Sentinel-2 SR**, chọn **RGB + NIR · 4 band (B2, B3, B4, B8)** để
xuất một GeoTIFF phản xạ Float32 với đúng thứ tự này. Cả bốn band có độ phân giải
gốc 10 m. Preset áp dụng cho ảnh đơn, danh sách ảnh, composite và chuỗi thời gian
GEE, kể cả chế độ theo năm. API dùng `s2_band_preset=rgbnir`; lựa chọn được ghi
vào thông số lượt tải hoặc snapshot tìm kiếm. Gửi danh sách lớp phụ có phần tử
cùng preset 4 band sẽ bị từ chối.

Tải Sentinel-2 GEE thông thường và chuỗi thời gian mặc định xuất **26 band**:
12 band phổ (B2, B3, B4, B8, B11, B12, B1, B5, B6, B7, B8A, B9),
tiếp theo AOT, WVP, SCL, TCI_R, TCI_G, TCI_B, MSK_CLDPRB, MSK_SNWPRB,
QA10, QA20, QA60, MSK_CLASSI_OPAQUE, MSK_CLASSI_CIRRUS, MSK_CLASSI_SNOW_ICE.
Mục **Lớp phụ Sentinel-2** cho phép bật/tắt riêng 14 lớp phụ, hoặc chọn/bỏ chọn
tất cả; 12 băng phổ luôn được giữ. Lựa chọn áp dụng cho ảnh đơn, composite,
danh sách ảnh và chuỗi thời gian GEE, được lưu trong snapshot khi tìm ảnh.
API nhận `s2_auxiliary_bands` là danh sách tên lớp trong JSON (danh sách rỗng
để chỉ xuất băng phổ), hoặc chuỗi phân cách bằng dấu phẩy trong liên kết tải
`/api/download/sentinel2` (giá trị rỗng để bỏ tất cả lớp phụ). Bỏ tham số giữ
mặc định 26 band. Thay đổi lựa chọn trên giao diện yêu cầu tìm lại ảnh.
Sáu kênh đầu giữ thứ tự cũ để tương thích công cụ RGB/chỉ số.
Band phổ là phản xạ Float32 (DN × 0.0001); các band phụ giữ giá trị nguồn
(AOT/WVP cần nhân 0.001 để lấy giá trị vật lý). Tất cả band dùng lưới xuất
10 m, không làm tăng độ phân giải thực của band gốc 20/60 m; phân loại/QA
được lấy mẫu nearest-neighbour. Band thiếu hoặc bị mask theo thời kỳ là
NoData (-9999), không phải 0; QA10/QA20 vốn luôn rỗng trong GEE.
Composite median dùng mode cho SCL/QA để tránh tạo mã phân loại/bit không hợp lệ;
medoid chọn theo 6 band phổ cũ và giữ toàn bộ band của pixel được chọn.
Level-2A không có B10. Chế độ khớp khung (matched-frame) vẫn giữ preset 12
band phổ riêng; thay đổi này không áp dụng cho nguồn CDSE/AWS.

**Sentinel-1 GEE**: tải GeoTIFF VV/VH (dB) cắt theo vùng, không cần tài khoản
CDSE — dùng ngay service account GEE hiện có. Chọn cảnh mới nhất, ưu tiên orbit
DESCENDING. Nút "Sentinel-1 SAR CDSE" (cũ) vẫn giữ để tải ZIP GRD đầy đủ.

**Planet NICFI** cần đăng ký miễn phí (non-commercial): làm theo
[hướng dẫn](https://docs.planet.com/platform/integrations/google-earth-engine/nicfi-gee/),
sau đó cấp quyền đọc asset cho service account đang dùng (email trong
`ee_private_key.json`). Băng: `B`, `G`, `R`, `N` (NIR), DN ÷ 10000 = reflectance.

**Dynamic World** là land-cover phân loại sẵn 10 m từ `GOOGLE/DYNAMICWORLD/V1`.
Băng `label` có 9 lớp: `0 water`, `1 trees`, `2 grass`, `3 flooded vegetation`,
`4 crops`, `5 shrub and scrub`, `6 built`, `7 bare`, `8 snow and ice`.
Ở chế độ điểm, app chọn dự đoán mới nhất phủ vùng; ở chế độ tỉnh, app lấy
**mode theo từng pixel** trong toàn bộ khoảng ngày. GeoTIFF là categorical
`uint8`, dùng nearest-neighbour và `nodata=255` (class `0` vẫn là nước, không
phải nodata). Khi tải theo tỉnh, phần ngoài polygon trong hình chữ nhật tải
cũng được điền `255`, nên không bị nhầm với nước. Dynamic World phù hợp lập bản
đồ land-cover tổng quát gần thời điểm hiện tại nhưng không phân biệt trực tiếp
lúa với các loại cây trồng khác.
Sản phẩm dùng giấy phép CC-BY 4.0; khi phát hành kết quả cần ghi công Dynamic
World Project (Google, National Geographic Society và WRI) cùng dữ liệu
Copernicus Sentinel.

### Độ phủ và ghép cảnh theo vị trí

### Nhập lớp vector và dùng polygon làm ROI

Trong mục **Lớp vector**, chọn một hoặc nhiều file **GeoJSON/JSON, KML/KMZ,
GPX hoặc Shapefile ZIP**. Shapefile cần đủ `.shp`, `.shx`, `.dbf`, `.prj`
cùng tên; tọa độ được chuyển về WGS84. GeometryCollection/MultiGeometry được
tách thành các đối tượng con để chọn riêng; MultiPolygon giữ thành một đối tượng.

Mỗi file và từng polygon/point/line có điều khiển hiển thị riêng. Bấm đối tượng
trên bản đồ hoặc chọn trong danh sách để xem thuộc tính; có lọc tên/loại,
chọn tất cả/bỏ chọn, xem toàn lớp và xóa lớp. Danh sách phân trang 50 đối tượng.

Chọn một hoặc nhiều polygon, rồi bấm **Dùng polygon đã chọn làm ROI**.
ROI là hợp hình học chính xác, giữ lỗ và các phần rời; point/line được chọn
không tham gia ROI. Sau đó dùng **Đếm ảnh**, tải CSV hoặc tìm/tải ảnh như thường.
Chọn thêm/bỏ chọn hoặc bật/tắt lớp chỉ thay bản xem trước; bấm áp dụng để đổi
ROI đang dùng. Áp dụng ROI mới, bỏ ROI hoặc xóa lớp chứa polygon đang dùng
sẽ xóa kết quả tìm/đếm cũ. Bản nháp ROI thủ công và khung GeoTIFF mẫu vẫn được giữ riêng.

Đếm metadata hỗ trợ GEE/CDSE/AWS theo nguồn hiện có. Tìm/tải và time series
theo vector hỗ trợ các nguồn GEE của ứng dụng; GeoTIFF cuối được mask để pixel
ngoài polygon và trong lỗ là NoData. Tìm/tải vector ngoài GEE hiện bị chặn rõ
vì chưa có đường cắt polygon tương ứng. Nhập file và đếm không tự tải raster.

Mỗi file tối đa **20 MiB**, **10.000 đối tượng**, **500.000 vị trí tọa độ**;
ZIP/KMZ tối đa **100 entry / 80 MiB giải nén**. Dữ liệu chuẩn hóa, kể cả thuộc
tính lặp trên các đối tượng con, tối đa **80 MiB**. ROI hợp cũng giới hạn
500.000 tọa độ. Không tự sửa topology, đoán CRS, tải NetworkLink/icon hay
giải nén ra đường dẫn trong archive. Từ chối hình học qua đường đổi ngày hoặc
ngoài miền vĩ độ bản đồ. Chưa hỗ trợ chỉnh đỉnh/thuộc tính hoặc buffer point/line.

Import và ROI có mã bất biến, lưu cục bộ trong SQLite và hết hạn sau 24 giờ
không được truy cập trên server. Snapshot tìm kiếm/job giữ bản sao hình học;
xóa lớp sau đó không đổi hình học của job đã tạo. Các lớp nhập hiện không tự
khôi phục trên giao diện sau khi tải lại trang.

### Đếm ảnh và tải danh sách tên ảnh / ngày chụp

Chọn ROI, nguồn ảnh và khoảng ngày, rồi bấm **Đếm ảnh**. Kết quả tính mọi
cảnh **giao ROI**, bao gồm cả ngày cuối, ảnh có mây (kể cả 100%) và ảnh thiếu
metadata mây. Giữ lựa chọn vệ tinh, sản phẩm và phân cực/quỹ đạo SAR nếu có;
không áp dụng ngưỡng mây hay ngưỡng phủ kín ROI của bước tìm/tải ảnh.
ROI chữ nhật giữ đúng góc xoay; ROI mẫu dùng footprint WGS84 từ metadata mẫu.

Bấm **Tải danh sách CSV** để lấy tên ảnh (`name`), ngày chụp UTC (`date`),
thời gian UTC (`timestamp`), mã cảnh (`asset_id`), nguồn (`source`) và % mây
toàn cảnh khi có (`cloud_pct`, để trống nếu thiếu). CSV dùng UTF-8 BOM để mở
trong Excel và chứa toàn bộ kết quả đã đếm, không chỉ 50 dòng đầu. Tên có
ký tự bắt đầu công thức bảng tính được thêm dấu nháy đơn khi xuất CSV.
Danh sách được giữ cố định cho lần đếm đó; đổi ROI/ngày/nguồn/bộ lọc liên quan
sẽ xóa kết quả hiển thị và cần bấm đếm lại. Snapshot hết hạn sau 24 giờ không
hoạt động và được dọn khi truy vấn danh sách tiếp theo.

Hỗ trợ nguồn GEE hiện có (Sentinel-2, Landsat, Sentinel-1, NICFI, Dynamic World,
Sentinel-5P), Sentinel-2 AWS/CDSE và Sentinel-1 GRD/SLC, Sentinel-3 CDSE.
NICFI đếm composite theo kỳ; DEM/bản đồ nền tĩnh không có số cảnh theo ngày.
Đếm và xuất CSV chỉ đọc metadata, không tải raster hoặc khởi chạy job.
Riêng GEE, bước đếm đọc collection gốc, không chạy chuẩn bị/scale các băng
ảnh của luồng tải. Truy vấn metadata dùng transport riêng với giới hạn thời
gian, không thay đổi deadline GEE toàn ứng dụng. Lỗi quá thời gian, kết nối
và quyền truy cập được phân biệt trong phản hồi API (`detail.code`,
`detail.source`, `detail.message`) để giao diện hiển thị rõ nguồn lỗi.
Tổng là số cảnh catalogue giao ROI: một cảnh có thể chỉ phủ một phần vùng,
có pixel thiếu/QA hoặc cần quyền truy cập khi tải. Đây không phải số ảnh đã
kiểm chứng tải thành công. Các bản sản phẩm khác ID được tính riêng theo nguồn.
Giới hạn 20.000 cảnh mỗi truy vấn; CDSE/AWS còn có ngân sách 120 giây/200 trang.
Nếu nguồn trả thiếu trang, lặp trang hoặc vượt giới hạn, API báo lỗi để thu hẹp
khoảng ngày, không công bố tổng từ một danh sách thiếu.

API: `POST /api/scene-inventory` nhận ROI/ngày/dataset và bộ lọc liên quan,
trả `total`, `search_id`, `parameters`, 50 metadata dòng đầu và `csv_url`.
`GET /api/scene-inventory/{search_id}/csv` tải toàn bộ danh sách của snapshot.

Sentinel-2/Landsat chỉ chọn cảnh có dữ liệu trên **≥99% ROI** ở chế độ
ảnh đơn (bbox với điểm không xoay; polygon với ROI xoay/chữ nhật), rồi xếp hạng
theo tỷ lệ pixel ít mây. Độ phủ tính trên mask chung của sáu băng phổ
(Sentinel-2: B2/B3/B4/B8/B11/B12; Landsat: SR_B2–SR_B7);
phần ngoài footprint được tính là thiếu dữ liệu.
Nếu không có cảnh đạt ngưỡng, app báo để bật **Ghép nhiều cảnh** (mặc định tắt).
Khi bật cho điểm/chữ nhật, app dùng **RAW**: cảnh có điểm độ trong tốt nhất
giữ nguyên pixel trên phần có dữ liệu; các cảnh khác chỉ bù footprint/NoData thiếu,
kể cả ở ngày khác trong cùng MGRS tile (Sentinel-2) hoặc WRS path/row (Landsat).
RAW bắt đầu với cảnh ưu tiên của mỗi tile; nếu chưa đạt 99% thì bổ sung ngày
theo từng đợt, dừng khi đủ phủ hoặc đã dùng hết ứng viên. Cảnh bổ sung chỉ bù chỗ
thiếu, không thay pixel hợp lệ của các cảnh đã chọn.
Điểm dùng mean băng xanh trên ROI ở lưới 1 km; đây là chỉ báo độ trong/sương/mây,
không phải phép tính phần trăm mây SCL/QA chính xác trên ROI. Giữ nguyên pixel mây
và giá trị 0 hợp lệ; không lấy cảnh khác để vá pixel mây. Nếu mọi cảnh đều thiếu dữ
liệu tại một chỗ thì chỗ đó vẫn là NoData. Vùng chồng lấp ưu tiên cảnh có điểm độ
trong tốt hơn trong đợt lựa chọn ban đầu. Các băng tại một pixel thuộc cùng cảnh; băng phụ trợ thiếu vẫn là
NoData, không lấy từ ngày khác. Danh sách cảnh/ngày chỉ ghi nguồn thực sự đóng góp
vào ROI. Kết quả RAW điểm/chữ nhật vẫn phải phủ ≥99% vùng; đây là độ phủ dữ liệu, không
phải tỷ lệ diện tích sạch mây. ROI chữ nhật/xoay giữ halo cho bước chuyển lưới.
Preview và tải GeoTIFF cùng dùng lựa chọn này; API nhận `merge_scenes: true`
ở `/api/scene`; link tải ghi `merge_scenes=true&composite_method=raw` và response
ghi `composite_method: "raw"`. Tỉnh/xã/vector cũng mặc định RAW; vẫn có thể chọn
rõ `medoid`, `median`, `mosaic` hoặc `base-score` khi cần. Không thay đổi luồng
time series, NICFI, Dynamic World hoặc SAR.

**Chọn cảnh gần ngày / cùng lượt chụp:** trong *Tùy chọn nâng cao*, bật ghép nhiều
cảnh rồi chọn RAW hoặc Mosaic ổn định. “Chọn nhóm cảnh để ghép” có ba lựa chọn:
toàn khoảng tìm kiếm (mặc định), gần ngày/điều kiện chụp tương đồng, và chỉ cùng
lượt chụp. Gần ngày mặc định 3 ngày; có thể chọn 1/3/5/7 ngày. Khoảng cách tính
từ cảnh sớm nhất đến cảnh muộn nhất của cả nhóm, không cộng nối các khoảng ngày.
Không tự nới thời gian để lấp thiếu. Các điều khiển áp dụng cho điểm/chữ nhật
có bật ghép và cho tỉnh/xã/vector, không áp dụng time series hoặc các composite khác.

Nhóm ưu tiên đủ footprint ROI, cùng lượt chụp, cùng vệ tinh/quỹ đạo và góc
chiếu sáng/quan sát gần nhau. Sentinel-2 dùng spacecraft + datatake; Landsat dùng
spacecraft + WRS path + ngày UTC, cho phép khác tile/row. Cùng quỹ đạo qua các
chu kỳ khác ngày không được coi là cùng lượt chụp. Thiếu metadata bị giảm ưu
tiên và được báo trong `scene_grouping.missing_metadata_fields`; không giả định
thông số bị thiếu là giống nhau. Thông số giống nhau vẫn có thể khác màu do
mây/sương hoặc thay đổi bề mặt; cân bằng màu RAW vẫn là tùy chọn riêng.

Giới hạn 400 ứng viên metadata (vượt giới hạn trả lỗi, không cắt im lặng), tối
đa 5 nhóm trong shortlist và 3 nhóm kiểm tra pixel. Footprint chỉ dùng xếp hạng;
nhóm bật giới hạn ngày phải đạt ≥99% dữ liệu phổ trước mask. Chế độ Mosaic ổn
định không bật nhóm ngày giữ cách trả phần phủ thiếu cũ, kèm phần trăm thực tế.

**Mosaic ổn định: mask hoặc không mask mây.** Mặc định mask mây/bóng mây theo
SCL/QA. Không mask giữ pixel mây và giá trị 0 hợp lệ. Cả hai cách đều chọn một
cảnh mỗi MGRS tile/WRS path-row; đổi mask không đổi nguồn đã chọn trong cùng
lượt tìm. Nơi bị mask mà không có cảnh đã chọn khác che phủ sẽ là NoData,
không tự lấy thêm ngày như RAW. Vì vậy độ phủ sau mask có thể thấp hơn 99%.

API `/api/scene`, `/api/composite-plan` và `/api/download/{dataset}` nhận
`scene_grouping=any|near_date|same_pass`, `max_scene_span_days=1|3|5|7` và
`mosaic_mask_clouds=true|false` (chỉ Mosaic ổn định). Mặc định tương thích:
`any`, 3, `true`. Point/rectangle có `merge_scenes=true` hỗ trợ
`composite_method=raw|mosaic`. Các lựa chọn được giữ trong download URL và
snapshot; sửa ngày/nhóm/mask trên link snapshot bị từ chối. Snapshot bộ nhớ
`mosaic_snapshot` tối đa 32 mục/một giờ trong tiến trình; hết hạn/khởi động lại
cần tìm ảnh lại. Khi bật luồng frozen download hiện có, các lựa chọn cũng nằm
trong request/graph bền vững và quá trình tải nền.

Response giữ thời gian UTC, platform/orbit/góc thu nhận khi có, span ngày,
`data_coverage_pct` trước mask và `output_coverage_pct` sau xử lý. TIFF ghi
`SCENE_GROUPING`, `SOURCE_SCENES`, `MOSAIC_MASK_CLOUDS` và
`COMPOSITE_METHOD=mosaic_masked|mosaic_unmasked` ở cả đường tải cũ và tải nền.
Kiểm tra GEE giả lập nhỏ: `.venv/Scripts/python.exe tests/live_coherent_mosaic.py`.
Chưa có nghiệm thu màu sắc trên ảnh thực tế/ROI của người dùng.

**RAW + cân bằng màu:** bật sẵn tùy chọn “Cân bằng màu giữa các cảnh” khi ghép
RAW Sentinel-2/Landsat trên giao diện. Tắt tùy chọn để giữ nguyên giá trị phổ.
Cân bằng dùng các vị trí trùng nhau, sạch mây/bóng mây trên vùng chồng lấp để
ước lượng gain/offset riêng từng băng phổ. Cảnh ưu tiên làm tham chiếu; cảnh
đã cân bằng đáng tin cậy có thể làm tham chiếu cho cảnh nối tiếp. Không trộn
nguồn ở đường nối, không cắt mây khỏi đầu ra, không sửa SCL/QA hoặc băng phụ trợ.
Màu có thể vẫn khác khi khác mùa, bề mặt thay đổi, còn sương hoặc thiếu overlap.

API cũ mặc định `color_balance=false`; bật bằng `color_balance: true` trong
`POST /api/scene` cho RAW composite hoặc point/rectangle `merge_scenes=true`.
Response `color_balance` ghi phương pháp `clear_overlap_affine_v1`, cảnh tham
chiếu, hệ số/trạng thái từng băng, số mẫu và lý do bỏ qua. Link tải giữ ảnh GEE
và các hệ số đã dùng cho preview qua `color_snapshot`, tối đa một giờ và 32
snapshot trong tiến trình hiện tại. Link hết hạn/bị thu hồi khi đầy cache hoặc
khởi động lại trả 410 và yêu cầu tìm ảnh lại; tham số vùng/ngày không khớp trả
409, không âm thầm tính lại. Cache này dành cho ứng dụng chạy một worker; triển
khai nhiều worker cần kho snapshot dùng chung. Gọi download trực tiếp với
`color_balance=true` không có snapshot vẫn được, nhưng tính ảnh mới.

Lấy tối đa khoảng 4096 mẫu ở lưới 60 m, seed 42; cần ít nhất 64 cặp hợp lệ mỗi
băng. Fit robust từ percentile 10–90 và lọc residual hai lượt; gain trong
[0.5, 2], offset trong [-0.1, 0.1] đơn vị phản xạ. Bỏ qua vùng có span <0.01,
median absolute residual >0.025 hoặc không giảm sai khác ít nhất 10%; sai khác
≤0.002 giữ hệ số đồng nhất. Giới hạn 32 lượt lấy mẫu và ba lượt nối tham chiếu.
Không dùng histogram toàn cảnh khi thiếu mẫu. Cảnh/băng bị bỏ qua giữ nguyên giá trị.

TIFF có hậu tố `_balanced.tif`, tag `COLOR_BALANCE` chứa báo cáo JSON và
`COMPOSITE_METHOD=raw_color_balanced`; NoData vẫn được giữ. Đây là hiệu chỉnh
giá trị phổ nhằm ưu tiên hình ảnh đều màu, không còn phổ nguồn nguyên trạng và
chưa được kiểm định cho chỉ số định lượng/huấn luyện. Preview và TIFF theo link
snapshot dùng cùng ảnh đã hiệu chỉnh. Kiểm tra GEE giả lập có thể chạy bằng
`.venv/Scripts/python.exe tests/live_color_balance.py`; không thay thế nghiệm
thu trực quan trên ROI/ngày thực tế của người dùng.

Ảnh đơn Sentinel-2/Landsat vẫn tính độ phủ và điểm SCL/QA **tuần tự từng cảnh**,
dùng khóa tổng hợp chung và bỏ qua tính mây cho cảnh thiếu độ phủ. Ghép RAW đi
qua xếp hạng trên GEE và lấy metadata trong một lần, không chạy vòng `.getInfo()`
SCL/QA cho từng cảnh phía Python. RAW dùng histogram nguồn ở lưới 10 m (Landsat:
30 m) để xác định cảnh đóng góp, rồi kiểm tra độ phủ dữ liệu trên mosaic cuối
cho điểm/chữ nhật. Điểm xếp hạng được chốt trước, không tính lại khi xuất ảnh.
Khoảng ngày dài vẫn chịu chi phí truy vấn/tính điểm của GEE; chưa có cache xuyên
request, và tốc độ thực tế cần đo trên cùng ROI/khoảng ngày/platform.

Smoke GEE trực tiếp ngày 2026-10-19: ROI tâm 105.193488, 20.785500, kích thước
38.019 × 19.600 km, khoảng ngày 19/09–19/10/2026. Ảnh đơn trả thông báo không
có cảnh phủ ≥99% (không còn lỗi aggregation). Bật ghép trả HTTP 200 sau 37,37 s,
16 cảnh, độ phủ 100%; preview 512 × 247 tải thành công. ROI nhỏ 200 × 100 m,
xoay 25° tại cùng tâm/ngày, tìm ảnh đơn trả HTTP 200 sau 18,79 s, cảnh
`20260929T032539_20260929T033813_T48QWH`, độ phủ 100%. Chưa tải GeoTIFF lớn
trong phép kiểm tra này.

Ở chế độ tỉnh, `/api/composite-plan` chỉ lập kế hoạch cho composite
Sentinel-2/Landsat dạng phản xạ; Dynamic World tự động tạo composite mode và
không hiển thị % mây theo scene.

Cảnh được chọn là cảnh có **tỷ lệ pixel trong suốt cao nhất** trong khoảng ngày
người dùng chọn tại vị trí đó (revisit 5–16 ngày, nên khoảng cách 10 ngày là đủ dùng).

## Esri World Imagery

Panel **Esri World Imagery** hoạt động độc lập với luồng GEE/CDSE, không cần
khởi tạo Earth Engine. Mặc định dùng **Vị trí + bán kính vùng tải**: nhập tọa độ
hoặc click bản đồ, marker tự cập nhật và khung vuông được vẽ theo bán kính.
Bán kính 3.000 m tạo bbox khoảng 6 × 6 km; công thức khớp `point_bbox` của GEE.
Thay tọa độ/bán kính cập nhật khung ngay; pan/zoom bản đồ không đổi vùng tải này.
Muốn chọn vùng khác, dùng chế độ bbox WGS84 theo thứ tự west, south, east, north
(hoặc lấy khung bản đồ); để tải theo xã, chọn chế độ xã/phường và chọn đơn vị
trong danh sách. Tải chạy ở nền, có tiến độ, hủy và tiếp tục/thử lại; trình duyệt
nhớ job gần nhất qua localStorage. File tải là GeoTIFF RGB EPSG:3857.

API:
- `POST /api/esri/jobs`: nhận `bbox` **hoặc** `commune_id`, `zoom` (1–19,
  mặc định 18), `workers` (1–8, mặc định 4), `max_tiles` (1–20.000, mặc định 4.000).
- `GET /api/esri/jobs/{job_id}`: trạng thái và object `progress` với số tile
  `processed`, `total`, `succeeded`, `failed`, `cached`.
- `POST /api/esri/jobs/{job_id}/cancel`: hủy job.
- `POST /api/esri/jobs/{job_id}/resume`: tiếp tục job gián đoạn hoặc thử lại tile lỗi.
- `GET /api/esri/jobs/{job_id}/download`: tải GeoTIFF đã hoàn tất; kết quả
  `partial` vẫn có thể tải nhưng chưa đủ ảnh. Job chưa có sản phẩm trả 409.

Giới hạn cứng: 500 triệu pixel raster, 20.000 tile và 8 luồng. Bộ tải kiểm tra
chỗ trống và dự trù dung lượng bảo thủ (6 GiB/lượt tải, 12 GiB cho vùng lưu
Esri của service); nếu vượt giới hạn cần giảm zoom/vùng tải hoặc quản lý dữ liệu
cũ khi không có job hoạt động. Không tự xóa ảnh người dùng.

Chỉ một job Esri hoạt động trong mỗi tiến trình server; dùng một worker Uvicorn.
Trạng thái lưu trong `downloads/esri` chỉ khi bắt đầu/kết thúc lượt chạy;
không ghi file tiến độ sau mỗi tile. Khi đang tải, tiến độ trên giao diện được
đọc từ RAM. Nếu server dừng đột ngột, số tile tiến độ trong file có thể cũ,
nhưng cache vẫn được kiểm tra và tái sử dụng khi tiếp tục.
Khi server khởi động lại, job chưa xong
được đánh dấu `interrupted` để người dùng tiếp tục. Cache tái sử dụng tile đã
kiểm tra; vùng ngoài polygon và tile thiếu được đánh dấu bằng raster mask,
không coi pixel RGB đen là NoData. Không tự ghi đè sản phẩm CLI nếu chưa cho phép.

Hai script CLI dùng chung bộ tải: `esri_tiles_download.py` bổ sung `--workers`
và `--overwrite`; `commune_esri_download.py` bổ sung `--max-tiles` và
`--overwrite`. Tên xã được chuẩn hóa dấu, bao gồm Đ/đ; nếu tên trùng cần lọc
`--tinh` để tránh chọn nhầm. Không có tham số `--cache-dir`.

Nguồn này là ảnh mosaic hiển thị, **không phải surface reflectance**. Không có
lọc ngày/mây, ảnh lịch sử hay ngày chụp bảo đảm. Zoom chỉ xác định grid đầu ra,
không chứng minh độ phân giải ảnh gốc. Kiểm tra tile không bảo đảm phát hiện mọi
placeholder chưa biết. Cache có thể chứa ảnh cũ nếu Esri cập nhật nguồn; cần quản
lý/xóa cache khi muốn tải mới, chỉ khi không có job hoạt động. Kết quả ghi nguồn
và thống kê thiếu tile; sử dụng/phân phối phải tuân thủ điều khoản Esri và nhà
cung cấp, không suy ra quyền tải/phân phối từ việc endpoint truy cập được.

## Time series (GEE, điểm / chữ nhật / vector)

### Tự tìm và tải lần lượt từng năm

Trong **Tùy chọn nâng cao**, bật **Time series** và **Tải lần lượt theo năm
(GeoTIFF)**, rồi bấm **Bắt đầu tải theo năm**. Việc bật checkbox chưa tải ảnh.
Batch chụp cố định ROI, nguồn, ngày, vệ tinh/phân cực và bands đã chọn; sau đó
chạy nền trên máy backend, kể cả khi đóng tab. Theo dõi ở mục **Tác vụ**.

Hệ thống hoàn tất **tìm → tải → đóng ZIP của năm trước**, rồi mới tìm năm sau.
Năm đầu/cuối được cắt đúng khoảng ngày; ngày cuối được tính. Danh sách năm
chưa tìm hiển thị **chưa biết**. Bước tìm chỉ đọc metadata collection gốc,
chia theo tháng; cửa sổ quá nặng được chia nhỏ đến ngày, không công bố danh
sách thiếu là đầy đủ. Không tự lọc mây hoặc tạo composite năm; NICFI vốn là
composite theo kỳ của nguồn. Quy tắc cảnh theo vùng giữ như time series thường.

Mỗi năm có **ZIP riêng**, gồm từng TIFF thành công và `manifest.json`/`manifest.csv`,
và link **CSV riêng** chứa tên/mã ảnh, ngày chụp, trạng thái và lỗi. Năm không
có cảnh chỉ có CSV header. Năm có cảnh lỗi được ghi partial/failed rõ ràng;
ZIP partial chứa các cảnh thành công. File nằm dưới
`downloads/timeseries/batches/<batch_id>/<year>/`; link tải về trình duyệt có
tên `timeseries_<dataset>_<year>.zip` hoặc `.csv`. Không có ZIP gộp mọi năm.

**Tạm dừng** có hiệu lực ở điểm an toàn giữa thao tác/cảnh; **Tiếp tục** bỏ qua
TIFF đã hoàn tất sau khi kiểm tra checksum/cấu trúc/lưới. **Thử lại phần lỗi**
giữ danh sách năm đã tìm đầy đủ và chỉ tải phần thiếu/lỗi/hỏng. Grid được lưu
chung xuyên các năm; ROI vector vẫn mask chính xác cả lỗ và phần rời. Sau
restart bất ngờ, batch chờ ở interrupted và cần bấm Tiếp tục. Khi tắt ứng dụng
bình thường, batch được yêu cầu tạm dừng tại checkpoint trước khi đóng worker.
Mở lại trang khôi phục
batch gần nhất qua ID lưu trong trình duyệt, không tự khởi chạy lại tải.

Batch và time series thường dùng chung **một worker**: một năm/một cảnh tại
một thời điểm. Transport batch tải từng tile tuần tự, tối đa một kết nối tile;
không dùng pool 6 kết nối của luồng tải cũ. Mỗi cửa sổ metadata có ngân sách
120 giây, tối đa 3 lần thử; tải tile 180 giây trong ngân sách 900 giây/cảnh.
Giới hạn: 20.000 cảnh/năm, 16 MiB/tile, 20.000 tile/cảnh, 30.000 pixel/cạnh
và 100 triệu pixel/cảnh. Giữ bands/độ phân giải; vượt giới hạn hoặc thiếu đĩa
thì báo lỗi, không tự giảm chất lượng. Chia năm giảm phạm vi mỗi thao tác,
không bảo đảm nguồn GEE/quota/kết nối luôn đáp ứng.

Kết quả batch terminal được dọn sau 24 giờ không hoạt động; paused/interrupted
giữ 7 ngày không hoạt động. Batch đang chạy giữ cả những năm đã hoàn tất.
Đây là tính năng local/một process, hỗ trợ Sentinel-2, Landsat, Sentinel-1 GEE,
NICFI và Dynamic World; không mở rộng sang CDSE/AWS/S5P.

API mới:

- `POST /api/timeseries/batches`: tham số ROI/ngày/nguồn như time series, trả batch ID ngay; không tìm catalogue trên request tạo batch.
- `GET /api/timeseries/batches/{id}`: tiến độ theo năm và link kết quả đã sẵn sàng.
- `POST /api/timeseries/batches/{id}/pause`, `/resume`, `/retry`: điều khiển batch đã lưu.
- `GET /api/timeseries/batches/{id}/years/{year}/download` hoặc `/csv`: kết quả riêng từng năm.

Kiểm chứng ngày 2026-11-13: Chrome thật qua 17 bước với backend/worker thật
và nguồn thử xác định; GEE thật tìm/tải hai cảnh ngày 2023-12-29 và 2024-01-03,
mỗi năm một ZIP/CSV. Hai TIFF 12 băng cùng lưới; pixel ngoài polygon và trong
lỗ đều NoData. Đây là smoke test ROI nhỏ, chưa phải benchmark hàng trăm cảnh
hoặc ROI lớn. Bằng chứng cục bộ ở `downloads/yearly-validation/`.

### Tìm danh sách rồi tải một ZIP

Công tắc **Time series** mặc định tắt: luồng ảnh đơn giữ nguyên. Chọn vị trí,
nguồn GEE, khoảng ngày và bán kính hiện có → bật công tắc → tìm danh sách →
nhấn **Tải toàn bộ ZIP**. Hỗ trợ Sentinel-2, Landsat 8/9, NICFI, Dynamic World
và Sentinel-1 GEE; không hỗ trợ tỉnh/xã, CDSE hoặc AWS. Đổi đầu vào làm vô hiệu
kết quả cũ; danh sách phân trang 50 cảnh, không âm thầm giới hạn số cảnh.

Bước tìm chỉ đọc metadata từ collection gốc sau khi lọc ROI/ngày/vệ tinh/phân
cực; scale, chọn band và xử lý pixel chỉ thực hiện khi tải cảnh. Truy vấn
metadata có ngân sách 120 giây sau khi khởi tạo phiên GEE, tối đa 3 lần thử trong cùng ngân sách và giới
hạn 20.000 cảnh. Vượt giới hạn hoặc hết thời gian sẽ báo lỗi, không lưu danh
sách bị cắt thành kết quả đầy đủ; hãy thu hẹp ngày hoặc dùng tải theo năm.
Giới hạn này cũng áp dụng truy vấn GEE của catalogue theo khoảng mây.
Deadline được kiểm tra giữa các lần đọc mạng và loại kết quả đến muộn; không
bảo đảm ngắt tức thời một lần đọc socket đang chờ. Khởi tạo phiên GEE dùng
cơ chế hiện có, nằm ngoài ngân sách truy vấn metadata này.

Timeout trả HTTP 504; lỗi quyền GEE trả 502, vượt quota trả 503, vượt giới hạn
kết quả trả 422; lỗi lưu snapshot cục bộ trả 500. Phản hồi lỗi tìm GEE có
`X-Search-Request-ID`; terminal ghi cùng ID, giai đoạn, thời gian, loại lỗi
và vị trí stack, không ghi thông điệp chứa URL/token từ nhà cung cấp.

Ngày đầu/cuối đều bao gồm. Lấy mọi asset chứa điểm tâm, kể cả nhiều cảnh cùng
ngày và mây 100%; không chọn best scene hoặc composite nhiều ngày. % mây là
metadata toàn cảnh, không phải % mây trong ROI. NICFI vẫn là composite theo kỳ
của nguồn và phụ thuộc quyền truy cập. Với chế độ point, vùng tải là hình vuông cục bộ quanh tâm; có thể xoay 0–360° theo chiều kim đồng hồ. Góc xoay được giữ xuyên suốt tìm kiếm, preview và liên kết tải; backend trả polygon ROI chính thức và bbox chỉ là bao ngoài. Raster đầu ra xoay cả lưới pixel theo ROI, giữ nguyên CRS/georeference tương ứng; dữ liệu thiếu thật do mây, footprint hoặc nguồn vẫn có thể là nodata. Góc 0°/360° tương thích lưới cũ.

ZIP gồm từng GeoTIFF thành công và `manifest.json`/`manifest.csv` (tham số,
asset ID, thời gian UTC, bands, scale/units, cloud/orbit/track, lỗi và grid).
Các raster dùng grid cảnh thành công đầu tiên, EPSG:4326, cùng transform/shape;
ảnh liên tục warp bilinear, Dynamic World nearest-neighbour uint8/nodata 255
(class water 0 giữ nguyên). Warp có thể thay đổi pixel. Sentinel-1 giữ dB,
không log10 lần nữa; không ép DESCENDING/latest.

API:
- `POST /api/timeseries/search`: nhận ROI điểm, chữ nhật hoặc vector cùng
  `dataset`, ngày và các tùy chọn theo nguồn; với điểm dùng `lon`, `lat`,
  `radius_m`, và có thể chọn `polarization` cho Sentinel-1.
- `GET /api/timeseries/search/{search_id}?offset=0&limit=50`: snapshot SQLite,
  không tìm lại GEE.
- `POST /api/timeseries/jobs`: chỉ nhận `{ "search_id": "..." }`.
- `GET /api/timeseries/jobs/{job_id}`: queued/running/completed/partial/failed/
  interrupted, processed/total/succeeded/failed; UI poll mỗi 2 giây.
- `GET /api/timeseries/jobs/{job_id}/download`: ZIP khi completed/partial.

Tải tuần tự, tối đa **một job active**, triển khai **một worker ứng dụng**.
Nhấn lặp không tạo job trùng. Lỗi một cảnh vẫn thử cảnh sau; partial chỉ chứa
cảnh thành công, all-failed không cung cấp ZIP. Thiếu đĩa dừng an toàn và báo
failed, không công bố ZIP thiếu là completed. ZIP được publish bằng rename
atomic; TIFF và manifest giữ đến hết hạn. SQLite/files ở
`downloads/timeseries`; dọn sau 24 giờ không hoạt động (startup và mỗi giờ),
không xóa job active. Restart đánh dấu queued/running interrupted, không tự
resume; tìm/job snapshot vẫn lưu bền. UUID không phải authentication: chỉ dùng
local/trusted, public deployment cần ownership/authentication riêng.

Kiểm chứng luồng tìm một lần: unit tests dùng mock GEE và raster/ZIP thật; browser local dùng API
mock. Ngày 2026-11-14, API local với GEE thật trả 600 cảnh Sentinel-2 trong
2019-01-01..2026-11-14 cho ROI 200 × 400 m, tâm 105.85/21.03, xoay 25°, trong
khoảng 7,3 giây; đọc được trang cuối của snapshot. Phép thử này chỉ đọc metadata,
chưa xác nhận ZIP live GEE hoặc quyền/khả dụng của các collection khác.

## Cài đặt

```powershell
cd GEE_testing
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Cấu hình Earth Engine

1. Đăng ký Earth Engine và cấu hình Google Cloud project/quyền truy cập phù hợp
   với loại hình sử dụng của bạn.
2. Tạo **service account** trong Google Cloud project đã bật Earth Engine API,
   download file key JSON.
3. Đặt file key vào thư mục gốc project với tên `ee_private_key.json`.
4. Copy `.env.example` → `.env` và điền:

```
GEE_SERVICE_ACCOUNT=my-service-account@my-project.iam.gserviceaccount.com
GEE_KEY_FILE=ee_private_key.json
```

(Nếu thiếu service account hoặc tệp key tương ứng, app gọi `ee.Initialize()`
với credentials mặc định đã cấu hình, ví dụ qua `earthengine authenticate`.)

## Chạy

```powershell
uvicorn app.main:app --reload --port 8000
```

Mở http://127.0.0.1:8000 → click bản đồ → **Tìm ảnh** → **Tải GeoTIFF**.

### Tải theo xã/phường

Chọn chế độ **Theo xã/phường** trên giao diện rồi chọn đơn vị từ danh sách
3.321 xã/phường lấy trực tiếp từ `Shapefile_PX_VN_34`. Ứng dụng dùng đúng
polygon xã/phường làm ROI nên file GeoTIFF chỉ bao phủ vùng nhỏ đã chọn, thay
cho toàn bộ tỉnh. API danh sách và geometry:

- `GET /api/admin/communes` — danh sách mã, tên xã/phường và tỉnh.
- `GET /api/admin/communes/{commune_id}` — GeoJSON của một xã/phường.

Chế độ này dùng được với Sentinel-2, Landsat, NICFI, Dynamic World và
Sentinel-1 GEE. Khi dùng Sentinel-2/Landsat, composite vẫn có bước lập kế
hoạch như chế độ tỉnh nhưng chạy trên ROI xã/phường nhỏ hơn.

## API

- `GET /api/health` — trạng thái + EE đã init chưa.
- `POST /api/scene` — body:

```json
{
  "lon": 107.0, "lat": 16.0,
  "dataset": "sentinel2",
  "start_date": "2026-08-01", "end_date": "2026-08-31",
  "radius_m": 2000
}
```

  Trả về thông tin cảnh + URL preview PNG + URL tải GeoTIFF.

- `POST /api/composite-plan` — xem trước chi phí composite khi chọn **tỉnh/thành** +
  Sentinel-2/Landsat, trước khi chạy. Body giống `/api/scene` (thêm
  `selection_mode: "province"`, `province_id`, `composite_method`). Trả về:
  - `mosaic`: số scene ứng viên, số scene sẽ tải (mỗi tile 1 scene) + danh sách
    `{scene_id, date, cloud}` từng scene.
  - `median`/`medoid`: số cảnh ứng viên, số cảnh sẽ chọn (tối đa 20 cảnh
    trong nhất theo mean băng xanh), khoảng ngày, diện tích ROI và thời gian
    ước lượng (`estimated_seconds` — heuristic thô).
  - `base-score`: danh sách các cảnh được giữ làm nền/fallback (tối đa 6 cảnh
    tốt nhất trên mỗi MGRS/WRS tile).

- `POST /api/scene` với `selection_mode: "province"` + `composite_method`:
  - `raw` (**mặc định**) — ưu tiên cảnh có mean băng xanh thấp ở lưới 1 km trên ROI,
    giữ mây/pixel gốc, chỉ bù vùng thiếu dữ liệu từ ngày khác, kể cả cùng tile.
  - `medoid` — per pixel chọn nguyên bộ giá trị của cảnh gần trung
    vị nhất: màu sắc giữ đúng một cảnh gốc, không bị phai như median. Khi có
    hơn 20 cảnh, app ưu tiên 20 cảnh có mean băng xanh thấp nhất trên ROI
    (ít sương/khói nhất; `CLOUDY_PIXEL_PERCENTAGE` không phát hiện được khói).
    Để không vượt giới hạn bộ nhớ của GEE ("User memory limit exceeded" khi
    tải vùng tỉnh), medoid chia tile nhỏ hơn (~4 triệu pixel/tile).
  - `median` — trung vị per-pixel của 20 cảnh trong nhất được chọn theo cùng
    tiêu chí mean băng xanh (mask mây từng cảnh); khử mây residual tốt hơn,
    nhưng màu có thể phai hơn medoid.
  - `mosaic` — mosaic ổn định theo không gian: mỗi tile (MGRS/WRS) lấy 1 scene trong nhất theo mean băng xanh trên ROI (fallback về cloud metadata), mask mây SCL/QA rồi ghép; tránh việc medoid đổi ngày theo từng pixel.
  - `base-score` — mỗi tile lấy tối đa 6 scene có score tốt nhất (mean băng xanh thấp nhất), mask mây rồi ghép theo thứ tự: scene tốt nhất làm nền, các scene còn lại chỉ lấp pixel bị mask. Vì vậy phần lớn tile giữ màu/ngày của scene nền, nhưng vùng được lấp có thể đến từ ngày khác; nếu mọi scene đều bị mask thì pixel vẫn là nodata, không đưa mây vào ảnh. Khi tải vùng tỉnh lớn, app tự chia tile nhỏ để giảm lỗi bộ nhớ do graph nhiều cảnh.
  Với `median`/`medoid`/`base-score`, UI bắt buộc xác nhận sau khi xem kế hoạch chi phí.

  `base-score` phù hợp khi ưu tiên ảnh nhìn đồng nhất hơn mosaic: mosaic hiện tại chỉ giữ một scene cho mỗi tile nên mây còn sót có thể để lại lỗ; base-score dùng các scene tốt kế tiếp để lấp đúng các lỗ đó.

- `composite_method` nhận `raw|mosaic|median|medoid|base-score` khi chọn
  tỉnh/thành Sentinel-2 hoặc Landsat; mặc định là `raw`.
- Với `base-score`, tên file tải về có hậu tố `_base-score.tif`.

- `GET /api/download/{dataset}?lon=...&lat=...&start_date=...&end_date=...&radius_m=...`
  — trả về file GeoTIFF (các băng SR chính). Với chế độ tỉnh, thêm
  `&selection_mode=province&province_id=...&composite_method=raw|medoid|median|mosaic|base-score`;
  file cache đặt tên theo phương thức (`..._mosaic.tif` / `..._median.tif` /
  `..._medoid.tif` / `..._base-score.tif`).

- `GET /api/s1/scenes?lon=...&lat=...&start_date=...&end_date=...&limit=20`
  — tìm ảnh Sentinel-1 IW GRD trên Copernicus catalogue (public, không cần auth).
    Trả về danh sách `{id, name, date, size_gb, online, cog}`.

- `GET /api/s1/slc/scenes?lon=...&lat=...&start_date=...&end_date=...&limit=50`
  — tìm ảnh Sentinel-1 IW **SLC** (dữ liệu pha cho InSAR). Trả về metadata
    đầy đủ từ OData Attributes: `timestamp`, `relative_orbit`/`track`,
    `orbit_direction`/`pass`, `polarization`, `operational_mode`,
    `slice_number`, `total_slices` — trường catalogue không trả về là `null`.
    ZIP nguyên cảnh ~4–8 GB, không có bản COG.

- `GET /api/s1/pairs?lon=...&lat=...&start_date=...&end_date=...&min_days=6&max_days=48&max_pairs=20&track=...&orbit_direction=ASCENDING|DESCENDING`
  — gợi ý cặp SLC cho InSAR. Ghép các cảnh **cùng relative orbit (track) +
    cùng hướng bay**, cách nhau `min_days..max_days` ngày; mode/polarization
    nếu cả hai cảnh có metadata thì phải giống nhau. Master = cảnh sớm hơn.
    Perpendicular baseline **không có** trong catalogue CDSE nên luôn `null`
    — cặp chỉ là gợi ý dữ liệu, chưa tạo interferogram.

- `GET /api/s1/download?product_id=...&filename=...`
  — stream file ZIP GRD/SLC đầy đủ của cảnh S1 từ CDSE (cần credentials, xem dưới).
    Endpoint mở stream và đọc chunk đầu **trước khi** trả header 200 — nếu lỗi
    (vd proxy chặn file lớn) người dùng nhận HTTP 502 kèm thông điệp rõ ràng
    thay vì file ZIP rỗng.

## Copernicus Data Space — Sentinel-1 SAR

Ngoài S2/Landsat qua GEE, app còn tìm và tải ảnh radar Sentinel-1 (IW GRD)
từ Copernicus Data Space Ecosystem (CDSE):

- **Tìm kiếm** dùng OData catalogue public — không cần tài khoản.
- **Tải ZIP qua S3 và cắt GRD qua S3** chỉ dùng key tạo sẵn, không cần
  tài khoản/mật khẩu. Đăng nhập [S3 Keys Manager](https://eodata-s3keysmanager.dataspace.copernicus.eu/),
  chọn **Add Credentials**, chọn ngày hết hạn và lưu Secret Key (chỉ hiện một lần).
  Điền vào `.env` cục bộ (đã được Git bỏ qua) hoặc biến môi trường backend:

  ```dotenv
  Access_Key_S3=<access-key>
  Secret_Key_S3=<secret-key>
  ```

  `.env.example` chỉ chứa mẫu và không được app nạp. Không đưa key thật vào Git,
  frontend hoặc log. App không tự tạo/xoá key và không dùng mật khẩu dự phòng
  khi thiếu key. Sau khi thay key, khởi động lại backend. HTTP 403 cần kiểm tra
  key hết hạn, bị thu hồi hoặc quyền truy cập; app không tự cấp lại key.
  ZIP được app đóng gói từ các object S3, không đảm bảo giống từng byte ZIP OData.
- **Endpoint OData cũ** vẫn cần token password-grant (`CDSE_USERNAME`,
  `CDSE_PASSWORD`) và độc lập với S3; S3 key không xác thực được endpoint này.
  OAuth Client ID/Secret của Sentinel Hub cũng không thay thế token OData.
  CDSE hỗ trợ 2FA qua `totp`, nhưng luồng OData cũ trong app chưa hỗ trợ trường này;
  không nên tắt 2FA để dùng luồng cũ, hãy dùng S3.

Lưu ý: sản phẩm S1 là file ZIP GRD **đầy đủ của cảnh** (~1–2 GB, footprint ~250 km),
không cắt theo vùng chọn như S2/Landsat. Mỗi cảnh thường có 2 bản: chuẩn `.SAFE`
và `COG` (Cloud Optimized GeoTIFF, nhỏ hơn ~25%) — chọn COG nếu chỉ cần xem/phân tích nhanh.

## Sentinel-1 SLC — chuẩn bị dữ liệu InSAR

InSAR cần **pha radar**, chỉ có trong sản phẩm **SLC** (Single Look Complex) —
GRD (cả trên GEE lẫn CDSE) đã mất pha nên không dùng được. App hỗ trợ:

1. **Tìm SLC**: dataset "Sentinel-1 SLC CDSE (InSAR)" → tìm theo điểm, khoảng ngày.
2. **Gợi ý cặp**: các cảnh cùng track + cùng hướng bay, Δt trong khoảng chọn
   (mặc định 6–48 ngày). Δt nhỏ thì ít mất tương quan (decorrelation).
3. **Tải từng ZIP SLC** (~4–8 GB/cảnh).

Từ cặp SLC đã tải, xử lý tiếp bằng công cụ ngoài (app chưa tạo interferogram):

- **SNAP + SNAPHU** (ESA, miễn phí, GUI): coregistration → interferogram →
  khử pha địa hình (SRTM) → unwrapping → displacement. Có tutorial chính thức ESA.
- **ASF HyP3** (API đám mây): gửi cặp ảnh, nhận sản phẩm unwrapped.
- **LiCSBAS + LiCSAR** (COMET): time series subsidence nhiều năm từ
  interferogram đã xử lý sẵn.

Lưu ý chọn cặp: baseline vuông góc (perpendicular baseline) chưa có trong
catalogue CDSE — kiểm tra bằng SNAP/ASF; baseline > ~150 m dễ mất tương quan.
S1B hỏng cuối 2021, hiện chỉ có S1A/S1C nên tần suất cặp 12 ngày hạn chế.

## Lưu ý

- Mỗi chế độ ROI giữ giới hạn kích thước riêng; chữ nhật hiện tối đa 90 km/cạnh.
- File GeoTIFF được cache trong thư mục `downloads/`.
- `getDownloadURL` giới hạn 32 MiB/request. Bộ tải chia theo băng và chia ô
  theo diện tích (mặc định tối đa 8 triệu pixel/ô, hoặc nhỏ hơn với graph phức tạp);
  số ô có thể vượt 8 × 8 khi vùng lớn.
- Ảnh ghép GEE và đầu ra chuyển lưới dùng **BigTIFF**, nén DEFLATE, khối
  256 × 256 và lưu theo băng. Phần mở rộng vẫn là `.tif`; phần mềm đọc cần hỗ trợ
  BigTIFF (Rasterio/GDAL và QGIS hỗ trợ). Không giảm độ phân giải hoặc số băng.
- Ghép/chuyển lưới xử lý từng khối, không cấp phát toàn bộ ảnh nhiều băng trong RAM.
  Mỗi lượt tải giữ tối đa 6 tile mạng đồng thời; GEE và bộ đệm GDAL vẫn dùng tài nguyên
  riêng. Ổ đĩa cần chỗ cho tile tải xuống, ảnh nguồn đã ghép và ảnh đầu ra trong lúc xử lý.
- Chỉ công bố ảnh ghép/chuyển lưới sau khi đóng tệp thành công. Lỗi ghi cục bộ ở
  `/api/download/{dataset}` trả thông báo JSON và ghi traceback vào log server;
  tệp dở dang của lần thử được dọn. Đường tương thích cũ cần gửi lại yêu cầu nếu bị ngắt.
  BigTIFF không loại bỏ quota/thời gian xử lý của GEE, dung lượng đĩa hoặc giới hạn ROI.

## Luồng tải Sentinel-2 có checkpoint (đang kiểm chứng)

Áp dụng cho ROI điểm/chữ nhật, ảnh đơn và ghép RAW. Các cờ dưới đây mặc định tắt;
chỉ bật sau khi kiểm tra phù hợp với môi trường triển khai. Chưa có kết luận về mức
giảm tổng thời gian tải GEE trên ROI nhiều GiB.

```powershell
$env:GEE_DOWNLOAD_SNAPSHOTS = '1' # giữ nguyên lựa chọn đã xem trước
$env:GEE_DOWNLOAD_TRANSPORT = '1' # stream/checkpoint cho link GET mang selection_token
$env:GEE_DOWNLOAD_VRT = '1'       # ghép ảo, dùng chung warp, ghi/nén TIFF đích theo khối
$env:GEE_DOWNLOAD_JOBS = '1'      # tác vụ nền và nút tạm dừng/tiếp tục trên giao diện
```

Thiết lập trước khi chạy server; tránh dùng `--reload` trong lượt tải dài.
Không thay đổi các cờ giữa lúc đang xuất ảnh. Tắt cờ tạo tác vụ mới vẫn giữ API
trạng thái và tải tệp đã hoàn tất. Link không có token tiếp tục dùng đường cũ.

- Kết quả tìm có `selection_token`; vùng, băng, graph và thứ tự nguồn được lưu trong
  `downloads/gee-downloads/state.sqlite3`. Lựa chọn mới hết hạn sau 24 giờ; job giữ
  snapshot riêng đến hết thời hạn lưu. Token sai/hết hạn trả 409/410, không tìm lại cảnh.
- `POST /api/download-jobs` với `{ "selection_token": "..." }` tạo tác vụ;
  GET theo ID để xem trạng thái; POST `pause`, `resume`, `cancel` để điều khiển.
  GET/HEAD `/api/download-jobs/{id}/file` chỉ phục vụ tệp đã hoàn tất, hỗ trợ Range/206.
- Tải tối đa 6 ô đồng thời trong một bulk export được nhận. Mỗi ô có tối đa 3 lần thử
  trong ngân sách 900 giây, gồm tạo resource, tải và kiểm tra. Không cộng thời gian
  chờ hàng đợi vào ngân sách xử lý của cảnh annual. Preview không chờ hàng đợi bulk.
- Tiếp tục ở **cấp ô đã tải và kiểm tra**, không nối byte vào URL GEE hết hạn.
  Nếu tạm dừng khi ghép TIFF, tiếp tục sẽ ghép lại từ các ô đã kiểm tra.
  Restart chuyển tác vụ dở dang thành `interrupted`, cần bấm Tiếp tục.
- Checkpoint và báo cáo đo nằm trong `downloads/gee-downloads/`; cache tệp hoàn tất
  mặc định 72 giờ/32 GiB, checkpoint tạm dừng/gián đoạn tối đa 7 ngày. Tệp đang gửi
  cho trình duyệt được giữ lease đến hết response. Cần dung lượng dự phòng cho ô và TIFF đích.
- RAW giữ mây, pixel 0 và mask riêng từng băng. Không đổi độ phân giải, số băng hay
  quy tắc chọn nguồn. Snapshot cố định lựa chọn; không bảo đảm nhà cung cấp không
  cập nhật nội dung của cùng một asset ID.

Kiểm tra offline: `python -m unittest discover tests` và `node --test tests/*.cjs`.
Benchmark local: `python tools/benchmark_gee_download.py --case user-roi-12 --mode optimized --repeat 5 --cache warm --output downloads/download-performance/local`.
Đây là bộ ô tổng hợp cục bộ, không phải tải đầy đủ ROI vệ tinh. Thêm `--live` mới gọi
GEE; `cold` trong chế độ live là cache ứng dụng, không phải cache hệ điều hành.
Kiểm tra vật lý trên 4 GiB: `python tools/validate_large_download.py --output downloads/download-performance/physical-big`.
Bằng chứng và gate đang mở nằm trong [kế hoạch triển khai](docs/superpowers/plans/2026-11-17-gee-download-performance.md).
