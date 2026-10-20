# GEE Surface Reflectance Downloader

Web nhỏ: click bản đồ hoặc nhập tọa độ → tải ảnh **Surface Directional Reflectance**
(SR, chưa chuẩn hóa BRDF) từ Google Earth Engine.

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

## Tên file ảnh GEE

Tải SAR Sentinel-1 giữ nguyên **toàn bộ ID sản phẩm** (không cắt 40 ký tự),
thêm phân cực xuất và ROI (tọa độ/bán kính, chữ nhật/góc xoay hoặc ID tỉnh/xã).
Chọn SAR theo tỉnh/xã vẫn là một cảnh được chọn theo độ phủ, không phải ảnh ghép.
Ảnh quang học tải một cảnh cũng giữ đầy đủ ID cảnh; tên composite vẫn mô tả vùng,
khoảng ngày và phương pháp ghép. File tạm trên máy chủ dùng UUID ngắn, độc lập
với tên tải xuống để tránh giới hạn đường dẫn Windows. Không thay đổi dữ liệu ảnh.

## Tải DEM / DSM từ GEE

Chọn nguồn **DEM — ALOS AW3D30**, **DEM — SRTM**, hoặc **DEM — Copernicus
GLO-30**, chọn vùng theo điểm, chữ nhật (kể cả xoay), tỉnh hoặc xã, rồi bấm
**Tải DEM GeoTIFF (30 m)**. Không áp dụng khoảng ngày, lọc mây hoặc time series.
GeoTIFF một band `elevation`, Float32, độ cao mét, NoData -9999; vùng hành chính
được cắt theo polygon. Chữ nhật xoay giữ lưới ROI; vùng lớn tự chia tile và ghép.
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

Sentinel-2/Landsat chỉ chọn cảnh có dữ liệu trên **≥99% ROI** ở chế độ
ảnh đơn (bbox với điểm không xoay; polygon với ROI xoay/chữ nhật), rồi xếp hạng
theo tỷ lệ pixel ít mây. Độ phủ tính trên mask của tất
cả các băng xuất; phần ngoài footprint được tính là thiếu dữ liệu.
Nếu không có cảnh đạt ngưỡng, app báo để bật **Ghép nhiều cảnh** (mặc định tắt).
Khi bật, app ghép các cảnh trong khoảng ngày và ngưỡng mây scene hiện có,
ưu tiên pixel đã loại mây, dùng dữ liệu gốc bù nơi không có pixel trong.
Các pixel có thể khác ngày chụp; kết quả ghép vẫn phải phủ ≥99% vùng.
Preview và tải GeoTIFF cùng dùng lựa chọn này; API nhận `merge_scenes: true`
ở `/api/scene` và `merge_scenes=true` ở `/api/download/{dataset}`.
Không thay đổi luồng time series, tỉnh/xã, NICFI, Dynamic World hoặc SAR.

Tìm ảnh Sentinel-2/Landsat theo điểm hoặc ROI chữ nhật tính độ phủ và điểm
ít mây **tuần tự từng cảnh**, không đặt `reduceRegion()` trong `collection.map()`.
Ảnh đơn bỏ qua phép tính mây nếu cảnh không đạt độ phủ; ghép cảnh chỉ kiểm tra
độ phủ của mosaic cuối. Điểm đã tính được giữ trong bộ nhớ của request và sắp
xếp phía Python, không gắn biểu thức tổng hợp vào ảnh preview/tải xuống.
Các request cùng tiến trình chia sẻ khóa tính điểm để tránh fan-out gây
`Too many concurrent aggregations`. Không cắt bớt danh sách ứng viên theo
metadata mây toàn cảnh: tiêu chí ≥99% và ít mây nhất trên ROI vẫn được giữ.
Khoảng ngày dài vẫn có thể chậm vì phải đánh giá mọi cảnh; đây không phải cache
xuyên request và không thay đổi hạn mức Earth Engine của project.

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

## Time series (GEE, theo vị trí)

Công tắc **Time series** mặc định tắt: luồng ảnh đơn giữ nguyên. Chọn vị trí,
nguồn GEE, khoảng ngày và bán kính hiện có → bật công tắc → tìm danh sách →
nhấn **Tải toàn bộ ZIP**. Hỗ trợ Sentinel-2, Landsat 8/9, NICFI, Dynamic World
và Sentinel-1 GEE; không hỗ trợ tỉnh/xã, CDSE hoặc AWS. Đổi đầu vào làm vô hiệu
kết quả cũ; danh sách phân trang 50 cảnh, không âm thầm giới hạn số cảnh.

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
- `POST /api/timeseries/search`: `selection_mode="point"`, `dataset`, `lon`,
  `lat`, `start_date`, `end_date`, `radius_m`, `polarization` (mặc định `vvvh`).
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

Kiểm chứng: unit tests dùng mock GEE và raster/ZIP thật; browser local dùng API
mock. Chưa kiểm chứng collection availability hoặc ZIP live GEE; cần tài khoản
được phép và quota/dung lượng phù hợp trước khi sử dụng dữ liệu thực tế.

## Cài đặt

```powershell
cd GEE_testing
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### Cấu hình Earth Engine

1. Đăng ký tài khoản GEE (miễn phí, non-commercial).
2. Tạo **service account** trong Google Cloud project đã bật Earth Engine API,
   download file key JSON.
3. Đặt file key vào thư mục gốc project với tên `ee_private_key.json`.
4. Copy `.env.example` → `.env` và điền:

```
GEE_SERVICE_ACCOUNT=my-service-account@my-project.iam.gserviceaccount.com
GEE_KEY_FILE=ee_private_key.json
```

(Nếu bỏ trống, app sẽ fallback sang credentials đã `earthengine authenticate` sẵn.)

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
  - `medoid` (mặc định) — per pixel chọn nguyên bộ giá trị của cảnh gần trung
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

- `composite_method` nhận `mosaic|median|medoid|base-score` khi chọn tỉnh/thành Sentinel-2 hoặc Landsat.
- Với `base-score`, tên file tải về có hậu tố `_base-score.tif`.

- `GET /api/download/{dataset}?lon=...&lat=...&start_date=...&end_date=...&radius_m=...`
  — trả về file GeoTIFF (các băng SR chính). Với chế độ tỉnh, thêm
  `&selection_mode=province&province_id=...&composite_method=medoid|median|mosaic|base-score`;
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

- Vùng tải giới hạn 10 km bán kính để tránh quota/timeout của GEE.
- File GeoTIFF được cache trong thư mục `downloads/`.
- `getDownloadURL` giới hạn ~50 MB/request; nếu cần vùng lớn hơn, hãy tách nhỏ.
