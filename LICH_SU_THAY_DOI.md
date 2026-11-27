# Lịch sử thay đổi

Tệp này lưu các mốc phát triển và ghi chú kiểm chứng từng có trong README.
[README.md](README.md) mô tả cách sử dụng và giới hạn theo mã hiện tại. Nội dung
bên dưới là tư liệu lịch sử: trạng thái, giao diện, kết quả thử nghiệm và lệnh
có thể đã thay đổi; không dùng chúng làm bằng chứng rằng phiên bản hiện tại đã
được nghiệm thu. Trước khi chạy kiểm thử, tham khảo README hiện tại và đặt
`GEE_DOWNLOADS_DIR` vào thư mục tạm riêng.

## Các mốc trong Git

Các mốc dưới đây lấy từ lịch sử commit của repository; xem `git log` để biết
đầy đủ các thay đổi và phạm vi từng commit.

| Ngày | Commit | Nội dung được ghi trong commit |
|---|---|---|
| 2026-10-14 | `13338f0` | Tài liệu thiết lập, dữ liệu cục bộ và thiết kế time series. |
| 2026-10-14 | `46097cc` | Dữ liệu địa giới tỉnh và xã, quản lý bằng Git LFS. |
| 2026-10-16 | `cdf7b9f` | Xuất ROI xoay và catalogue lọc mây. |
| 2026-10-20 | `2d1d386`, `c73ac3f`, `9b641b6`, `7e6ec12` | Tích hợp catalogue EODAG, ROI chữ nhật, DEM; bổ sung giao diện, kiểm thử và tài liệu. |
| 2026-11-18 | `edecea9` | Mở rộng Satellite Workspace và các luồng ảnh vệ tinh. |
| 2026-11-20 | `657a06f` | Lựa chọn hướng bay Sentinel-1 và tăng giới hạn upload ảnh mẫu. |
| 2026-11-21 | `344c8fb` | Cấu hình nguồn và ảnh xem trước cảnh. |

Những thay đổi trong working tree chưa commit không nằm trong bảng commit. Bản
README hiện tại được rút gọn trong phiên làm việc ngày 2026-11-21; các ghi chú
lịch sử dưới đây được trích từ README tại commit `344c8fb`. Có thể xem nguyên
văn bản đã commit bằng `git show 344c8fb:README.md`.

## Ghi chú trích từ README trước đây

Các đoạn trích được giữ nguyên để bảo toàn ngữ cảnh và giới hạn của những lần
kiểm chứng. Những đường dẫn, câu lệnh và mô tả hành vi trong phần trích có thể
không còn phù hợp với phiên bản đang chạy.


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

### Smoke GEE ngày 2026-10-19

Smoke GEE trực tiếp ngày 2026-10-19: ROI tâm 105.193488, 20.785500, kích thước
38.019 × 19.600 km, khoảng ngày 19/09–19/10/2026. Ảnh đơn trả thông báo không
có cảnh phủ ≥99% (không còn lỗi aggregation). Bật ghép trả HTTP 200 sau 37,37 s,
16 cảnh, độ phủ 100%; preview 512 × 247 tải thành công. ROI nhỏ 200 × 100 m,
xoay 25° tại cùng tâm/ngày, tìm ảnh đơn trả HTTP 200 sau 18,79 s, cảnh
`20260929T032539_20260929T033813_T48QWH`, độ phủ 100%. Chưa tải GeoTIFF lớn
trong phép kiểm tra này.

### Kiểm chứng time series theo năm ngày 2026-11-13

Kiểm chứng ngày 2026-11-13: Chrome thật qua 17 bước với backend/worker thật
và nguồn thử xác định; GEE thật tìm/tải hai cảnh ngày 2023-12-29 và 2024-01-03,
mỗi năm một ZIP/CSV. Hai TIFF 12 băng cùng lưới; pixel ngoài polygon và trong
lỗ đều NoData. Đây là smoke test ROI nhỏ, chưa phải benchmark hàng trăm cảnh
hoặc ROI lớn. Bằng chứng cục bộ ở `downloads/yearly-validation/`.

### Kiểm chứng tìm time series ngày 2026-11-14

Kiểm chứng luồng tìm một lần: unit tests dùng mock GEE và raster/ZIP thật; browser local dùng API
mock. Ngày 2026-11-14, API local với GEE thật trả 600 cảnh Sentinel-2 trong
2019-01-01..2026-11-14 cho ROI 200 × 400 m, tâm 105.85/21.03, xoay 25°, trong
khoảng 7,3 giây; đọc được trang cuối của snapshot. Phép thử này chỉ đọc metadata,
chưa xác nhận ZIP live GEE hoặc quyền/khả dụng của các collection khác.
