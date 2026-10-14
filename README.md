# GEE Surface Reflectance Downloader

Web nhỏ: click bản đồ hoặc nhập tọa độ → tải ảnh **Surface Directional Reflectance**
(SR, chưa chuẩn hóa BRDF) từ Google Earth Engine.

## Dữ liệu và cấu hình cục bộ

Repository không kèm thông tin đăng nhập, chứng chỉ CA nội bộ, dữ liệu ranh giới
hoặc ảnh đã tải. Sau khi clone:

- Sao chép `.env.example` thành `.env` và điền thông tin đăng nhập của bạn;
  đặt khóa service account theo `GEE_KEY_FILE`. Không commit các khóa này.
- Bổ sung `shapefile34/vn34.json` để dùng ranh giới tỉnh/thành. Có thể tạo file
  bằng `convert_shapefile34.py` từ bộ shapefile tỉnh/thành tương ứng.
- Bổ sung bộ `Shapefile_PX_VN_34/Việt Nam (phường xã) - 34` gồm các file
  `.shp`, `.shx`, `.dbf`, `.prj`, `.cpg` để dùng ranh giới xã/phường.
- Chỉ sử dụng hoặc phân phối dữ liệu ranh giới khi có quyền phù hợp. Các thư mục
  dữ liệu này được giữ ngoài Git; file shapefile xã/phường vượt 100 MiB.
- Nếu mạng nội bộ yêu cầu CA riêng, bổ sung `vingroup_cas.pem` trên máy;
  `combined_cas.pem` được ứng dụng tạo cục bộ. Không commit chứng chỉ nội bộ.

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

Sentinel-2/Landsat chỉ chọn cảnh có dữ liệu trên **≥99% bbox** ở chế độ
ảnh đơn, rồi xếp hạng theo tỷ lệ pixel ít mây. Độ phủ tính trên mask của tất
cả các băng xuất; phần ngoài footprint được tính là thiếu dữ liệu.
Nếu không có cảnh đạt ngưỡng, app báo để bật **Ghép nhiều cảnh** (mặc định tắt).
Khi bật, app ghép các cảnh trong khoảng ngày và ngưỡng mây scene hiện có,
ưu tiên pixel đã loại mây, dùng dữ liệu gốc bù nơi không có pixel trong.
Các pixel có thể khác ngày chụp; kết quả ghép vẫn phải phủ ≥99% vùng.
Preview và tải GeoTIFF cùng dùng lựa chọn này; API nhận `merge_scenes: true`
ở `/api/scene` và `merge_scenes=true` ở `/api/download/{dataset}`.
Không thay đổi luồng time series, tỉnh/xã, NICFI, Dynamic World hoặc SAR.

Ở chế độ tỉnh, `/api/composite-plan` chỉ lập kế hoạch cho composite
Sentinel-2/Landsat dạng phản xạ; Dynamic World tự động tạo composite mode và
không hiển thị % mây theo scene.

Cảnh được chọn là cảnh có **tỷ lệ pixel trong suốt cao nhất** trong khoảng ngày
người dùng chọn tại vị trí đó (revisit 5–16 ngày, nên khoảng cách 10 ngày là đủ dùng).

## Esri World Imagery

Panel **Esri World Imagery** hoạt động độc lập với luồng GEE/CDSE, không cần
khởi tạo Earth Engine. Nhập bbox WGS84 theo thứ tự west, south, east, north
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
Trạng thái lưu trong `downloads/esri`; khi server khởi động lại, job chưa xong
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
của nguồn và phụ thuộc quyền truy cập. Vùng tải là bbox chữ nhật: footprint
chứa tâm không bảo đảm phủ đủ bbox, phần thiếu coverage giữ nodata.

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
- **Tải về** cần token password-grant: đăng ký miễn phí tại
  [dataspace.copernicus.eu](https://dataspace.copernicus.eu/), rồi điền email +
  mật khẩu đăng nhập CDSE vào `.env`:

  ```
  CDSE_USERNAME=you@example.com
  CDSE_PASSWORD=...
  ```

  Lưu ý: OAuth Client ID/Secret tạo trên dashboard
  (https://shapps.dataspace.copernicus.eu/dashboard/#/account/settings) là
  credential của **Sentinel Hub** — token của nó bị endpoint tải ZIP OData từ chối
  với lỗi `DAT-ZIP-609 "Token audience not allowed"`. Tài khoản phải tắt 2FA
  thì mới lấy token bằng password-grant được.

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
