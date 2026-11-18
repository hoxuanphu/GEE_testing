# Phương án sửa và nghiệm thu tính năng đồng khung GeoTIFF

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Khắc phục các vấn đề còn lại về preflight/ROI/preview/độ phủ; bổ sung giữ khung GeoTIFF khi chuyển sang tải theo ROI và chọn vệ tinh cụ thể, ví dụ Sentinel-1A/1B/1C.

**Architecture:** Giữ đường ROI WGS84 và tải/warp một cảnh đã được nghiệm thu. Thêm identity ROI có phiên bản, preflight đo hợp mask và preview nhóm. Lưu bản nháp khung độc lập với chế độ chọn vùng; ROI lấy từ mẫu dùng lại FrameService và polygon chuẩn. Bộ lọc vệ tinh được lưu theo dataset, áp dụng trước tìm cảnh và giữ nguyên trong snapshot/download/ghép.

**Tech Stack:** FastAPI, Pydantic, Earth Engine, rasterio/GDAL, NumPy, SQLite, JavaScript thuần, unittest, node:test.

**Spec:** Yêu cầu người dùng trong phiên này và [báo cáo kiểm tra lại](2026-10-25-matched-frame-recheck.md), đặc biệt các mục 3A–3D. Hai kế hoạch cũ `plan/feature-matched-frame-download-1.md` và `plan/refactor-gee-frame-export-1.md` chỉ là tài liệu đối chiếu cho phần đã triển khai.

**Trạng thái thực hiện:** Đã hoàn thiện các tác vụ 1–8 ngày 2026-10-25, gồm nghiệm thu HTTP/GEE `run-4d565e50473c` và nghiệm thu giao diện thật `run-a3f270f8d2ad`. Browser do người dùng mở; harness chạy controller/Leaflet thật, giữ đúng bản nháp và chỉ upload mẫu một lần; TIFF/ZIP được server kiểm lại checksum và độ phủ 100%. Bộ kiểm tra browser có thêm 8 test offline. Xem [báo cáo thực hiện](2026-10-25-matched-frame-implementation.md) để biết bằng chứng và giới hạn. Baseline bên dưới được giữ làm lịch sử, không phải kết quả bản sửa.

**Kiểm tra tiếp trong ngày:** Đã đối chiếu lại code, chạy 393 test Python (OK, 3 skip) và 91 test JS (đạt). Bổ sung phần còn thiếu của tác vụ 7: report/manifest ghi vệ tinh thực của từng cảnh khi chọn “Tất cả”, gồm cả nguồn tải lỗi. Xem [báo cáo kiểm tra và bổ sung](2026-10-25-matched-frame-followup.md). Proof live ở trên được giữ là bằng chứng trước bản bổ sung metadata này; lượt bổ sung được kiểm bằng test offline.

## Global Constraints

- Giữ giới hạn hiện có: 366 ngày/search, 500 ứng viên, 10 nguồn/job, 100 triệu pixel/ảnh, 30.000 pixel/cạnh, 4 GiB output chưa nén/job, TTL 24 giờ.
- Giữ Float32, NaN, S2 scale 0.0001 đúng một lần, SAR dB nguyên gốc, mask nearest và zero hợp lệ.
- Không giảm ngưỡng phủ tự động, không đổi nguồn/thứ tự và không dùng lỗi IAM/quota để kích hoạt fallback CRS.
- Mask preflight được đo trước tải các band ảnh. Độ phủ dữ liệu hữu hạn trong TIFF cuối vẫn là điều kiện công bố; đo mask không hứa loại bỏ mọi lỗi dữ liệu/mạng sau đó.
- Không sửa lịch sử job hoặc chạy thao tác ghi vào SQLite production trong quá trình nghiệm thu. Dùng kho riêng và bảo toàn các thay đổi có sẵn trong worktree.
- Chuyển chế độ chọn vùng không xóa metadata/footprint mẫu đã upload xong; chỉ thay mẫu hoặc thao tác xóa mẫu rõ ràng mới thay đổi bản nháp đó. Khung gốc không bị sửa khi người dùng vẽ ROI thủ công.
- Bộ lọc vệ tinh là điều kiện tìm và tải dữ liệu, không chỉ là label giao diện. Không tự chuyển về tất cả vệ tinh khi lựa chọn không có ảnh trong khoảng ngày/ROI.

## 1. Hiện trạng đã kiểm tra

| Hạng mục | Mã hiện tại | Việc còn lại |
|---|---|---|
| Khung tìm kiếm | Đã xác nhận GEE nhận đúng bounds quanh 21,7 độ Bắc; đã có test EQC | Giữ test hiện hữu; thêm identity/version cho snapshot |
| Xuất CRS tùy chỉnh | S1 VV/VH và S2 RGB live completed; đúng lưới EQC 1614×1619, phủ thực 100% | Kiểm tra hồi quy sau sửa, không viết lại exporter |
| Khoảng mây/chọn ảnh tốt nhất | Đã có mã và test giao diện | Giữ hành vi và bổ sung đúng ca ngưỡng phủ 0/N/A |
| Ngưỡng phủ 0 | `_scene_row()` cho eligible khi coverage=None | Chỉ cho chọn khi độ phủ xác định và >0, kể cả ngưỡng 0 |
| Ghép | Hai nguồn SAR được preflight 200 nhưng ảnh cuối chỉ phủ 95.402106%, dưới yêu cầu 99.9% | Đo hợp mask trước tải; preflight không sẵn sàng phải chặn tạo job |
| Kết quả cũ | Copy nguyên snapshot ROI cũ vào kho riêng: preflight vẫn 200 | Chặn phiên bản ROI thiếu/sai; TTL không thay thế version |
| Preview nhóm | Chỉ có endpoint preview từng cảnh | Thêm preview từ preflight, đúng mask chung và thứ tự nguồn |
| Chuyển từ mẫu sang ROI | Biến reference còn trong bộ nhớ khi đổi mode, nhưng footprint bị ẩn; rectangle dùng một hình học riêng | Tách bản nháp mẫu và ROI đang dùng; cho ROI dùng đúng polygon của mẫu |
| Chọn vệ tinh | Đồng khung có mfPlatform A/B/C cho SAR, backend nhận platform; ROI SAR thường chưa truyền xuyên suốt | Dùng selector nhất quán theo dataset ở cả hai chế độ, lọc cả nguồn bù vùng và đường tải |

Kiểm tra offline tại lần lập phương án này:

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_frame*.py'
# 65 tests, OK
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_reference_frame.py'
# 5 tests, OK
node --test tests/test_matched_frame_ui.cjs
# 37 tests, pass
```

Baseline hiện tại là **70 test Python, 37 test giao diện**, cộng bằng chứng live trong `downloads/matched_frame_audit/run-831314d4b2a8/proof.json`. API live được gọi qua TestClient nạp mã hiện tại; không xác nhận server/UI bên ngoài đã nạp mã. Bản sửa phải giữ hai ca tải đơn thành công và chuyển ca nhóm thiếu phủ từ lỗi sau tải thành trạng thái bị chặn trước tạo job.

## 2. Hành vi cần chốt

1. Upload mẫu, đọc metadata, suy biên từ toàn bộ raster affine, densify cạnh rồi chuyển WGS84. Không lấy alpha/NoData của ảnh mẫu làm ROI.
2. Hiển thị đúng polygon đó trên bản đồ. Polygon dùng cho search, metric, preview phải cùng hình học và cùng phiên bản.
3. Chọn khoảng ngày, gồm cả ngày kết thúc. Giữ giới hạn 366 ngày và tối đa 500 cảnh ứng viên.
4. Sentinel-2: khoảng mây là mây + bóng trong ROI trên phần SCL hợp lệ, không phải mây toàn tile. N/A không phải 0%. Sentinel-1 không áp dụng bộ lọc mây.
5. Không ghép: ngưỡng phủ áp dụng từng cảnh. Chế độ ảnh tốt nhất chỉ chọn cảnh đạt điều kiện; không có cảnh đạt thì thông báo rõ.
6. Có ghép: người dùng chọn thứ tự 1–10 nguồn; nguồn đầu là ảnh chính, nguồn sau chỉ bù pixel thiếu đồng thời trên các band đã chọn. Độ phủ yêu cầu áp dụng ảnh cuối. Không cộng tỷ lệ phủ từng nguồn.
7. Giữ khả năng ghép khác ngày đang có, nhưng hiển thị ngày/giờ từng nguồn, khoảng ngày của sản phẩm và cảnh báo trước tải. SAR tiếp tục bắt buộc cùng hướng bay và quỹ đạo tương đối; không âm thầm đổi nguồn/quỹ đạo.
8. Mây vẫn là dữ liệu, không tự biến thành NoData. Bộ lọc mây hiện áp dụng từng nguồn; không quảng bá ảnh ghép là sạch mây hoặc báo phần trăm mây ghép nếu chưa tính trực tiếp.
9. Một cảnh hoặc một nhóm đã chọn tương ứng một TIFF đầu ra. Không ghép thì nhiều cảnh tương ứng nhiều TIFF. ZIP chứa các TIFF thành công và manifest.
10. Lưới xuất mặc định tiếp tục là đồng biên với độ phân giải đã chọn; đồng lưới giữ nguyên CRS, affine và kích thước mẫu. Các thông số lưới đặt trong phần nâng cao.
11. Preflight luôn công bố `can_start`; nhóm thiếu phủ vẫn được xem nguồn/preview, nhưng UI không cho xác nhận tạo job và backend từ chối nếu client cố gửi. Thay nguồn, thứ tự hoặc ngưỡng phải tạo preflight/digest mới.
12. Không có độ phủ hợp lệ không đồng nghĩa độ phủ 0%. Hàng coverage=None/0 không được chọn, kể cả khi ngưỡng người dùng là 0; dữ liệu null/NaN ngoài hợp đồng phải bị từ chối trước tính ngưỡng.
13. Khi từ “Đồng khung GeoTIFF mẫu” chuyển sang “ROI”, nếu đã đọc mẫu thành công thì mặc định dùng **ROI từ khung mẫu đã lưu**: giữ polygon WGS84, thông tin mẫu và lựa chọn vệ tinh/ngày. Không yêu cầu upload lại hoặc nhập tâm/kích thước để thay thế polygon.
14. Trong chế độ ROI, người dùng có thể chọn “Vẽ ROI mới” để dùng rectangle editor hiện có. Mẫu gốc vẫn lưu riêng; có thao tác “Dùng lại khung mẫu”. Quay về đồng khung phục hồi CRS/affine/kích thước và lựa chọn lưới của mẫu, không lấy rectangle vừa vẽ ghi đè lên mẫu.
15. ROI từ mẫu là polygon densified chính xác đã tính trên server; không đổi sang bbox hoặc xấp xỉ bằng center/width/height/rotation. Đường này tiếp tục dùng FrameService cho Sentinel-1 SAR GEE và Sentinel-2 SR GEE cùng các chế độ xuất lưới hiện có; đổi chế độ UI không tự thay đổi lưới đã chọn.
16. Selector vệ tinh có “Tất cả” và từng vệ tinh phù hợp dataset: Sentinel-1A/B/C/D cho SAR GEE (D đã có trong hợp đồng backend hiện tại), Sentinel-2A/B/C cho S2 SR GEE. Không phụ thuộc việc vệ tinh có ảnh trong khoảng ngày đang chọn để tự đổi giá trị. Ví dụ chọn S1B mà không có dữ liệu thì trả không có kết quả đúng bộ lọc.
17. Giá trị API tiếp tục là `platform=null` cho tất cả hoặc một mã `A/B/C/D` phù hợp dataset; UI hiển thị tên đầy đủ. Trong lượt này chọn một vệ tinh hoặc tất cả, không thêm đa chọn. Áp dụng cả đồng khung, ROI từ mẫu và ROI thủ công của các đường GEE nêu trên; nguồn/dataset chưa hỗ trợ phải ẩn selector hoặc báo không hỗ trợ, không âm thầm bỏ tham số.
18. Đổi vệ tinh hoặc nguồn ROI làm mất hiệu lực search/selection/preflight/preview đang chờ; giữ bản nháp mẫu. Job đã tạo giữ nguyên bộ lọc, danh sách nguồn và lưới của snapshot ban đầu.

## 3. Phân công theo tệp

| Tệp | Trách nhiệm trong phương án |
|---|---|
| `app/reference_frame.py` | Hình học WGS84 chuẩn và fingerprint có phiên bản |
| `app/frame_models.py` | Hợp đồng ngưỡng phủ, phiên bản xử lý và dữ liệu chất lượng nhóm |
| `app/frame_search.py` | Search/metric/preview dùng đúng ROI; tính metric hợp nhất từ mask |
| `app/frame_validity.py` (mới) | Tải mask có giới hạn, lập lưới mask dùng chung, đo hợp mask trên lưới xuất; không tải reflectance/dB để kiểm phủ |
| `app/frame_jobs.py` | Chặn snapshot cũ; cố định thứ tự nguồn và kết quả đánh giá nhóm trong preflight |
| `app/frame_export.py` | Ngưỡng phủ thực, xuất nhóm và provenance đầy đủ |
| `app/frame_projection.py` | Tái sử dụng đường tải/warp hiện tại; bổ sung kiểm thử khi cần |
| `app/frame_store.py` | Giữ history; bảo toàn danh sách nguồn, báo cáo và trạng thái job |
| `app/frame_api.py` | Trả lỗi snapshot cũ và preview nhóm theo preflight đã cố định |
| `static/selection-state.js` (mới) | Bản nháp khung mẫu, ROI thủ công và nguồn ROI đang chọn, độc lập với mode hiển thị |
| `app/platforms.py` (mới) | Validate và ánh xạ platform theo dataset sang thuộc tính GEE |
| `app/main.py`, `app/imagery.py`, `app/s1_mosaic.py` | Truyền bộ lọc vệ tinh qua search/download/nguồn bù của luồng ROI GEE hiện có |
| `static/index.html`, `static/matched-frame.js` | Quy trình gọn; thông tin nhóm, lỗi cụ thể, thứ tự nguồn |
| `tests/test_frame*.py`, `tests/test_reference_frame.py`, `tests/test_matched_frame_ui.cjs` | Kiểm thử hồi quy theo từng tác vụ |
| `validate_matched_frame_live.py` | Chuyển các ca audit đã có thành runner nghiệm thu với assertion cho hành vi trước/sau sửa |

## 4. Các tác vụ triển khai

### Tác vụ 1 — Cố định ROI và ngăn dùng snapshot cũ

**Files:** `app/reference_frame.py`, `app/frame_search.py`, `app/frame_jobs.py`, `app/frame_api.py`; test trong `tests/test_frame_search.py`, `tests/test_frame_jobs.py`, `tests/test_frame_api.py`.

**Interfaces:** Đặt trong `app/reference_frame.py`: `reference_roi_identity(frame: dict) -> dict` trả `roi_version=2`, `roi_geojson` và `roi_fingerprint`; `validate_roi_identity(parameters: dict) -> None` tính lại identity từ `parameters['reference']`, ném `FrameError('stale_search', 'Kết quả tìm dùng phiên bản khung cũ; hãy tìm lại.', 409)` khi thiếu/sai phiên bản, geometry hoặc fingerprint. `_frame_roi()` dùng geometry đã xác thực; search mới tính identity trên server trước RPC.

- [x] Giữ test EQC đã có và mở rộng kiểm identity: tâm lon=104.035720835673, lat=21.7423783969593; affine `[9.993915937710756, 0, -8065.090161732581, 0, -9.99956743608598, 8094.649839511601]`; kích thước 1614×1619. Assert thay affine/geometry/fingerprint làm validation thất bại, còn polygon hợp lệ vẫn quanh 21,7 độ Bắc.
- [x] Thêm test snapshot không có `roi_version`: preflight, bắt đầu job mới từ preflight cũ và thực thi job cũ chưa chạy đều phải dừng trước RPC/export. Job đã hoàn tất vẫn đọc history/download theo TTL hiện có.
- [x] Chạy test mới và xác nhận ca snapshot cũ thất bại trước khi sửa.
- [x] Lưu ba trường ROI vào parameters của search mới và sao chép nguyên vào preflight. Fingerprint là SHA-256 của JSON canonical `{version, geometry}`; geometry do server tính lại từ metadata tham chiếu, không nhận polygon do client tùy ý thay thế.
- [x] Kiểm tra identity trước đọc cache preview, trước preflight, trước tạo job và trước exporter trong worker. Snapshot cũ chỉ được xem lịch sử, UI yêu cầu tìm lại; không sửa metadata hoặc job cũ để giả thành kết quả mới.
- [x] Cập nhật fixture search/preflight hợp lệ trong test để tạo identity qua helper server mới. Giữ fixture không có version riêng cho ca âm; không tự thêm version trong `FrameStore.save_search()` vì như vậy sẽ che lỗi snapshot lịch sử.
- [x] Worker nhận queued job chứa preflight cũ phải chuyển failed với `stale_search`, trước khi tạo staging/gọi GEE. Giữ nguyên job completed/partial và link artifact đã công bố theo TTL, không vô hiệu hóa hồi tố.
- [x] UI gặp `stale_search` xóa selection/preflight của search đó, giữ metadata tham chiếu còn dùng được, hiển thị nút tìm lại. Không tự gán version mới vào snapshot cũ.
- [x] Chạy lại các test của tác vụ; kiểm tra thêm mẫu UTM, geographic và affine xoay để bảo toàn đường đang hoạt động.

Ví dụ assertion trọng tâm:

```python
polygon = reference_footprint(eqc_frame)['footprint_geojson']
latitudes = [p[1] for p in polygon['coordinates'][0]]
self.assertGreater(min(latitudes), 21.6)
self.assertLess(max(latitudes), 21.9)
# Test EE graph phải kiểm tra geometry được gửi đi, không chỉ helper GDAL.
```

### Tác vụ 2 — Thống nhất lọc chất lượng và chọn ảnh

**Files:** `app/frame_models.py`, `app/frame_search.py`, `app/frame_export.py`, `static/matched-frame.js`; test trong `tests/test_frame_search.py`, `tests/test_frame_export.py`, `tests/test_matched_frame_ui.cjs`.

**Interface đề xuất:** `effective_min_coverage(parameters: dict) -> float` đặt tại `frame_models.py`, dùng ở search/preflight/export. Giữ tương thích: `require_full_coverage=true` tương đương ngưỡng 99.9; nếu false dùng `min_coverage_pct`. Frontend tiếp tục gửi false cùng ngưỡng người dùng chọn.

**Ca lỗi phải chuyển từ FAIL sang PASS:**

```python
def test_zero_threshold_still_rejects_unknown_or_empty_coverage(self):
    params = {'dataset': 'sentinel2', 'require_full_coverage': False,
              'min_coverage_pct': 0}
    for coverage in (None, 0):
        raw = raw_scene(band_coverage_pct=coverage, covers_roi=False)
        self.assertFalse(search._scene_row(raw, params)['eligible'])
```

- [x] Thêm test các ngưỡng 0, 95, 99.9, 100; mây min=max, min>max, thiếu metric. Ở ngưỡng phủ 0 vẫn phải loại cảnh không có dữ liệu hoặc không xác định độ phủ; toàn NoData không bao giờ là thành công.
- [x] Thêm test best mode: bỏ qua cảnh ít mây nhưng không đạt phủ; không tự chọn cảnh không đạt khi danh sách rỗng; phân trang không tạo thêm lựa chọn; lựa chọn cố định của người dùng không bị đổi bởi phản hồi cũ.
- [x] Chạy test mới, sửa các điểm lệch giữa `_scene_row()`, `eligible()` phía JS và `_check_coverage()` qua helper chung phía Python; frontend đọc ngưỡng từ snapshot.
- [x] Giữ `least_cloud` là xếp tăng theo mây+bóng trong ROI, rồi độ phủ, các tie-break hiện hữu. SAR dùng quy tắc ngày/quỹ đạo hiện có và label tương ứng, không hiển thị SAR là ít mây nhất.
- [x] Kết quả thể hiện ngày UTC, nguồn, tile/quỹ đạo, mây ROI, mây tile tách biệt và phủ ước lượng. Hiển thị lý do khi hàng không chọn được.
- [x] Chạy lại test backend/UI liên quan trước sang tác vụ tiếp theo.

### Tác vụ 3 — Đo hợp mask và khóa tạo job từ preflight không đạt

**Phụ thuộc:** Tác vụ 1 và 2. Đây là sửa lỗi chính còn lại; không phụ thuộc preview nhóm.

**Files:** tạo `app/frame_validity.py`, `tests/test_frame_validity.py`; sửa `app/frame_models.py`, `app/frame_search.py`, `app/frame_jobs.py`, `app/frame_api.py`, `static/matched-frame.js`; mở rộng test jobs/API/UI.

**Interfaces:**

- `measure_frame_selection(scenes: list[dict], parameters: dict, *, deadline: float) -> dict` trong `frame_validity.py`; nhận nguồn theo đúng thứ tự snapshot, ROI identity và output_grid đã xác thực.
- `FrameSelectionQuality`: `measurement_version=1`, `method='output_grid_mask'`, `estimated=true`, `band_coverage_pct` (float hữu hạn 0..100 trên mask chung tất cả band), `valid_pixel_count`, `total_pixels`, `ordered_asset_ids`, `roi_fingerprint`, `merge_policy='primary_first_fill_nodata'`.
- Preflight mới thêm `preflight_version=2`, `can_start: bool`, `blocking_reason: str | None`, `selection_quality: FrameSelectionQuality | None`. Không ghép có quality=None; vẫn có version/can_start và kiểm eligibility từng cảnh.
- Digest dùng helper duy nhất `preflight_digest_payload(preflight: dict) -> dict` trong `frame_jobs.py`, chứa search_id, parameters, scenes có thứ tự, preflight_version, can_start, blocking_reason và selection_quality; dùng cùng helper khi tạo, start và worker.

**Quy tắc API:** Preflight nhóm đo thành công trả 200 để xem kết quả, kể cả khi thiếu phủ. Nhóm không đạt có `can_start=false`, `blocking_reason='insufficient_selection_coverage'`. `POST /jobs` với preflight đó trả 422 cùng mã này trước `store.create_job()`; UI không bật xác nhận. Preflight thiếu version mới bị từ chối 409 `stale_preflight`. Nếu phép đo timeout/quota/lỗi upstream thì trả lỗi tương ứng, không lưu preflight sẵn sàng và không dùng số liệu từng nguồn thay cho hợp mask.

- [x] Viết test trước sửa: hai nguồn mỗi nguồn phủ 60%, chồng cùng vùng thì hợp 60% và can_start=false với ngưỡng 95%; bù vùng thì hợp 100% và can_start=true. Ngưỡng 0 vẫn đòi hợp mask có ít nhất một pixel hợp lệ.
- [x] Test nhiều band: từng nguồn phải có mask giao các band; ảnh cuối lấy hợp những mask chung đó. Không được ghép band A từ nguồn 1 với band B từ nguồn 2 để tạo pixel hợp lệ giả.
- [x] Test UI và API: preflight 200/can_start=false không được coi là đã được phép tải; POST /jobs cố tình gửi digest của nó vẫn bị chặn và mock `store.create_job` không được gọi. Test sửa quality, thứ tự, grid hoặc can_start làm digest/identity không khớp.
- [x] Chạy test mới để xác nhận các ca thất bại trước triển khai. Giữ fixture 99.85%/99.95% quanh ngưỡng 99.9%, không làm tròn trước so sánh.
- [x] Trong `frame_validity.py`, tải **mask**, không tải các band phản xạ/dB. Lưới EPSG dùng mask nearest trên output_grid; lưới EQC dùng đúng transport grid và chuỗi nearest của exporter rồi warp mask cục bộ về output_grid. GEE chỉ nhận CRS chuẩn cho raster transport; ROI tìm kiếm vẫn là WGS84.
- [x] Dùng cùng quy tắc dựng mask/lưới trong preflight và exporter, tách helper dùng chung khi cần. Tính `valid(source)=AND(mask của từng band)` rồi `valid(group)=OR(valid(source))`; đếm trên toàn bộ width×height lưới xuất. Không cộng coverage nguồn, không suy mask từ zero/độ sáng/mây và không dùng lưới mosaic mặc định 1 độ.
- [x] Không dùng riêng ước lượng S1 100 m/S2 20 m để quyết định nhóm đạt ngưỡng sát 99.9%; số liệu thô chỉ để hiển thị/chẩn đoán. Phép quyết định nhóm dùng mask trên lưới xuất. Vẫn ghi `estimated=true` vì độ phủ giá trị hữu hạn trong TIFF cuối có thể khác mask do xử lý giá trị/rìa.
- [x] Tải/warp theo cửa sổ, chỉ giữ một số cửa sổ byte mask trong RAM, warp_mem_limit=64 MiB, một phép đo nhóm hoạt động/service. Kiểm đĩa trước staging; dọn tệp mask riêng trong finally. Tổng deadline 120 giây bao gồm RPC/retry/warp; tối đa lưới 100 triệu pixel hiện có. Quá ngân sách trả timeout/resource error và gợi ý tăng pixel/giảm vùng, không âm thầm nới ngân sách hoặc hạ ngưỡng.
- [x] Lưu quality/version/can_start trong snapshot preflight. Start và worker kiểm version, digest, identity và can_start trước tải; không gọi đo lại hoặc đổi kết quả đã xác nhận trong một preflight.
- [x] UI hiển thị phủ mask của nhóm so với yêu cầu, ví dụ “95,40% / yêu cầu 99,9% — chọn thêm cảnh bù vùng hoặc đổi ngưỡng và kiểm tra lại”. Giữ nguồn đang chọn, không tự xóa lựa chọn/hạ ngưỡng khi nhóm thiếu phủ. Khi mạng lỗi có thể thử lại preflight.
- [x] UI chỉ bật xác nhận khi `preflight_version===2` và `can_start===true`; nhóm ghép còn phải có quality đúng version, danh sách nguồn và phủ hữu hạn. Response thiếu các trường mới phải báo server/response chưa tương thích, không suy luận được phép tải từ HTTP 200. Thay form/selection trong lúc phép đo đang chạy phải vô hiệu hóa response cũ như cơ chế preflightVersion hiện có.
- [x] Không ghép vẫn đi đường tải đã có, không bắt buộc thêm lượt tải mask cho từng cảnh. Chạy lại các test frame và UI, kiểm job idempotency và phục hồi history sau thay đổi digest.

### Tác vụ 4 — Preview nhóm theo đúng nguồn và thứ tự

**Phụ thuộc:** Tác vụ 3 để có preflight v2, nguồn và quality cố định.

**Files:** `app/frame_search.py`, `app/frame_api.py`, `app/frame_jobs.py`, `app/frame_store.py`, `static/index.html`, `static/matched-frame.js`; test trong `tests/test_frame_search.py`, `tests/test_frame_api.py`, `tests/test_frame_jobs.py`, `tests/test_matched_frame_ui.cjs`.

**Interface:** `make_frame_selection_preview(scenes: list[dict], parameters: dict) -> bytes`; API `GET /api/matched-frame/preflights/{preflight_id}/preview` trả PNG. Đọc danh sách/thứ tự từ preflight, không nhận asset IDs tùy ý từ request. Endpoint cho xem cả nhóm can_start=false để người dùng biết phần thiếu.

- [x] Thêm test hai nguồn khác màu, có vùng giao và vùng thiếu: đổi thứ tự nguồn đổi đúng pixel vùng giao, không đổi phần bù. Tất cả band của vector pixel phải từ cùng nguồn; mây hợp lệ vẫn giữ.
- [x] Mask chọn nguồn dựa trên **tất cả band xuất**, kể cả khi preview chỉ hiển thị RGB hoặc một band SAR. Đặt min/max hiển thị chung cho mọi nguồn; S2 scale một lần như đường preview hiện tại.
- [x] Dựng graph preview trên lưới CRS chuẩn dùng chung và dùng ROI WGS84; xử lý ưu tiên nguồn đầu một cách tường minh vì EE mosaic mặc định ưu tiên ảnh cuối. Preview 512 pixel mô phỏng cùng chính sách và nguồn; sai số rìa do độ phân giải preview phải được ghi rõ, không tuyên bố PNG là kiểm tra pixel TIFF.
- [x] Endpoint kiểm ROI/version/digest trước đọc cache, giới hạn PNG 8 MiB và deadline 120 giây. Cache theo preflight ID đã cố định; không tái sử dụng preview nguồn đầu hoặc preview search cũ. Dọn cache theo TTL và lease đang đọc.
- [x] UI tải preview nhóm theo yêu cầu tại bước preflight, có danh sách nguồn/thứ tự/ngày. Khi đổi nguồn/thứ tự/ngưỡng, bỏ preview cũ cùng preflight. Phản hồi mạng cũ không ghi đè preview mới.
- [x] Preview lỗi chỉ làm mất ảnh xem trước và cho thử lại; không đổi can_start, quality hoặc selection. Nhóm thiếu phủ vẫn không thể tạo job dù preview thành công.
- [x] Chạy test graph, cache/TTL, stale response và UI nhóm đủ/thiếu phủ.

### Tác vụ 5 — Giữ đúng TIFF, nguồn ảnh và lỗi sau tải

**Phụ thuộc:** Tác vụ 3; mở rộng test preview khi tác vụ 4 hoàn tất.

**Files:** `app/frame_export.py`, `app/frame_projection.py`, `app/frame_jobs.py`, `app/frame_store.py`; test trong `tests/test_frame_export.py`, `tests/test_frame_projection.py`, `tests/test_frame_jobs.py`.

- [x] Kiểm tra các test hiện có và thêm ca thiếu: EPSG đi trực tiếp; CRS EQC đi warp; 400 projection được phân biệt với 400 tham số khác và 401/403/429. Không fallback CRS để che lỗi xác thực hoặc quota.
- [x] Giữ pipeline Float32, S2 scale 0.0001 đúng một lần, SAR giữ dB, zero hợp lệ, mask nearest và NaN. Kiểm tra TIFF sau warp và sau ghép bằng `validate_frame_export()`.
- [x] Chạy test ghép cùng fixture tác vụ 3: dữ liệu số, mask và độ phủ thực phải đúng; kiểm phủ ảnh cuối bằng cùng ngưỡng request, không bắt nguồn thành phần phải phủ đủ khung. Một ca mask preflight đạt nhưng dữ liệu hữu hạn sau warp không đạt vẫn phải failed, giữ report thực và không công bố TIFF.
- [x] Kiểm tra metadata nhóm: danh sách IDs, thời gian từng nguồn, nguồn chính, thứ tự bù vùng, chính sách `primary_first_fill_nodata`, chiến lược export từng nguồn. Tên/ngày của nguồn chính không được làm người dùng tưởng cả nhóm là một lần chụp.
- [x] Bảo toàn danh sách nguồn và report trong SQLite, manifest, ZIP và API khi reload. Không để bước lọc report bỏ mất provenance nhóm.
- [x] Test disk reserve gồm nguồn tạm, TIFF cuối và ZIP; lỗi mạng/warp/đĩa không công bố file chưa kiểm tra và phải dọn staging riêng. Giữ giới hạn 10 nguồn, 4 GiB output chưa nén và giới hạn lưới hiện tại.
- [x] Chạy toàn bộ nhóm test frame, reference và giao diện sau thay đổi.

### Tác vụ 6 — Giữ khung mẫu và dùng khung đó khi chuyển sang ROI

**Phụ thuộc:** Tác vụ 1; chạy kiểm tra tích hợp preflight/preview sau tác vụ 3–4. Tác vụ này chỉ thay cách giữ và chọn nguồn ROI, không xây exporter mới.

**Files:** tạo `static/selection-state.js`, `tests/test_selection_state.cjs`; sửa `static/index.html`, `static/matched-frame.js`, `static/workspace.js`, `app/frame_models.py`, `app/frame_jobs.py`; mở rộng `tests/test_matched_frame_ui.cjs`, `tests/test_rectangle_ui.cjs`, `tests/test_frame_api.py`.

**Interfaces:**

- `SelectionState.setReferenceDraft(data)`, `getReferenceDraft()`, `activateReferenceRoi()`, `activateManualRoi()`, `clearReferenceDraft()`; trả snapshot sao chép, không đưa tham chiếu mutable của metadata cho rectangle editor.
- Reference draft chứa `reference_id`, metadata CRS/affine/width/height, `footprint_geojson`, compatible_grid_modes, saved grid options. Manual ROI draft lưu riêng các trường rectangle hiện có. ROI active có `source='reference'|'manual'`.
- `MatchedFrame.isActive()` đúng khi UI ở đồng khung hoặc ở ROI với source=reference; dùng điều kiện này cho search dispatch, form, footprint và kiểm response trễ. Request FrameSearchRequest thêm `selection_context: Literal['reference','reference_roi']='reference'`, cố định trong parameters/digest/manifest. Geometry vẫn được server dựng từ reference_id, không nhận polygon tự do từ browser.

- [x] Viết test trước sửa: upload xong → chuyển rectangle/ROI → polygon vẫn hiển thị và request dùng reference_id cũ; không upload lại, không gọi RectangleROI.body() cho nguồn reference. Điểm đỉnh/densified ring trước và sau chuyển mode bằng nhau.
- [x] Test mẫu xoay/shear và EQC: không thay bằng bbox hoặc hình chữ nhật suy ra từ tâm. Test chuyển ROI thủ công rồi quay lại: mẫu/CRS/affine/lưới vẫn nguyên, manual rectangle có thể phục hồi độc lập.
- [x] Nạp selection-state.js trước các controller sử dụng nó. Chuyển mode chỉ hủy request đang chờ và selection/preflight cũ, không xóa draft đã hoàn tất; upload chưa xong bị abort thì không được tạo draft giả. Không thay hành vi job đang chạy.
- [x] Khi từ reference sang rectangle có draft hợp lệ, chọn roi source=reference và render polygon mẫu. Giữ nút “Vẽ ROI mới”, “Dùng lại khung mẫu” và tóm tắt tên mẫu/khung trong chế độ ROI. Rectangle editor chỉ bật handles khi source=manual; thao tác vẽ không sửa draft mẫu.
- [x] Search dispatch kiểm `MatchedFrame.isActive()` trước validate lon/lat và rectangle fields. ROI từ mẫu gửi request matched-frame với selection_context=reference_roi và các grid options đã lưu; manual ROI vẫn dùng đường rectangle cũ. Không đọc tọa độ điểm ẩn để từ chối request ROI từ mẫu.
- [x] Refactor điều kiện mode trong matched-frame để form/ngày/preview hoạt động ở cả hai ngữ cảnh; giữ layout label “ROI từ khung mẫu” để người dùng biết vùng đang dùng. Không yêu cầu đăng ký lại reference khi chỉ đổi context.
- [x] Khi đổi/upload mẫu mới hoặc xóa mẫu rõ ràng, cập nhật draft và invalidation đúng source đang dùng. Xóa mẫu đang làm ROI phải đưa UI về trạng thái cần chọn vùng, không âm thầm tìm theo một rectangle cũ. Reference hết TTL trả thông báo cần upload lại; không dựng UUID mới từ cache browser.
- [x] Test response upload/search/preflight về muộn sau đổi source không thay active ROI; thay đổi mode/source không xóa lựa chọn platform/ngày và không sửa snapshot job cũ. Chạy node tests selection state, rectangle và matched-frame.

### Tác vụ 7 — Chọn vệ tinh cụ thể trong đồng khung và ROI GEE

**Phụ thuộc:** Có thể làm sau tác vụ 2; tích hợp với ROI từ mẫu sau tác vụ 6. Không bỏ điều kiện quỹ đạo khi thêm platform.

**Files:** tạo `app/platforms.py`, `tests/test_platforms.py`; sửa `app/frame_models.py`, `app/frame_search.py`, `app/frame_jobs.py`, `app/main.py`, `app/imagery.py`, `app/s1_mosaic.py`, `static/index.html`, `static/matched-frame.js`, `static/selection-state.js`; test trong `tests/test_frame_search.py`, `tests/test_frame_jobs.py`, `tests/test_rectangle_downloads.py`, `tests/test_s1_mosaic.py`, `tests/test_matched_frame_ui.cjs`.

**Interfaces:**

- `validate_platform(dataset: str, platform: str | None) -> str | None` và `apply_platform_filter(collection, dataset: str, platform: str | None)` trong platforms.py. Null là tất cả; không tự coi chuỗi rỗng/giá trị không hợp lệ là tất cả trên API.
- S1: lọc `platform_number == 'A'|'B'|'C'|'D'`; S2: lọc `SPACECRAFT_NAME == 'Sentinel-2A'|'Sentinel-2B'|'Sentinel-2C'`. Giá trị D với S2 hoặc mã không biết trả 422 trước RPC. Giữ nhãn dataset tách biệt vệ tinh; không đổi sentinel2/s1gee thành mã vệ tinh mới.
- Thêm `platform: str | None=None` đã validate vào S1SceneRequest, request/query ROI GEE và các helper `_s1_collection`, `best_s1_scene`, `best_s1_scene_for_roi`, `prepare_s1`; tham số mới đặt cuối/keyword để không làm lệch các caller hiện hữu. Ghi platform vào response và download parameters/URL của đường ROI thường, snapshot/manifest của đường matched-frame.

- [x] Viết test collection giả có A/B/C/D: chọn C chỉ lấy C trước limit/ranking; all giữ mọi platform phù hợp. Chọn S1B trong tập chỉ có A/C trả rỗng và không gọi lại với all. S2D/mã sai trả 422 trước GEE.
- [x] Tạo selector vệ tinh chung theo dataset, có thể đưa mfPlatform ra khỏi phần SAR nâng cao và tái sử dụng node/binding thay vì tạo hai input mâu thuẫn. Hiển thị tên đầy đủ; lưu lựa chọn từng dataset để reference↔ROI không mất S1C, đổi sang S2 không mang nhầm lựa chọn không hợp lệ của S1.
- [x] Dùng helper platform cho frame search trước aggregate/limit; giữ lọc IW, phân cực, hướng bay và relative orbit. Các metric/best scene chỉ được tính trên tập đã lọc, không tìm tất cả rồi mới đổi label.
- [x] Truyền platform qua `/api/s1gee/scene`, đường tải GEE `/api/s1gee/download` và helper ROI: `_s1_collection` → best_s1_scene/best_s1_scene_for_roi → prepare_s1. `prepare_s1` đang tạo collection bù riêng nên phải lọc platform cho cả nguồn phụ; chọn C không được bù bằng A dù cùng relative orbit.
- [x] Với S2 ROI thường, truyền platform qua SceneRequest và đường download/composite GEE đang dùng `_s2_collection`; map mã API sang SPACECRAFT_NAME đúng một lần. Nếu một nhánh/provider chưa hỗ trợ thì trả validation rõ, không bỏ bộ lọc. Không mở rộng bộ lọc sang mọi catalogue CDSE/AWS/Esri trong tác vụ này.
- [x] Preflight frame kiểm từng source có platform metadata phù hợp khi đã chọn cụ thể; thiếu hoặc khác platform thì `incompatible_platforms` trước job, kể cả request tự gửi token từ nguồn khác. Download/worker kiểm lại nguồn trong snapshot, không dùng dropdown hiện tại để đổi nguồn job. Nhãn/tags/manifest ghi platform thực từng nguồn; all có thể có nhiều platform nhưng vẫn phải đạt điều kiện quỹ đạo hiện hữu.
- [x] Test thay A→C làm invalid search/preflight/preview; job A đang chạy vẫn A. Test nguồn chính C và nguồn bù A bị loại ở cả FrameService và prepare_s1. Kiểm query download còn platform=C và selection ROI/source vẫn đúng sau reload trạng thái job.
- [x] Kết quả rỗng hiển thị “Không có ảnh Sentinel-1B trong khoảng thời gian/vùng đã chọn” với tên từ whitelist. Không khẳng định lịch vận hành vệ tinh hoặc tự đổi khoảng ngày để tạo kết quả.
- [x] Chạy tests platform, frame, ROI download và s1_mosaic; thêm test không truyền platform vẫn giữ hành vi all cũ.

### Tác vụ 8 — Nghiệm thu lỗi còn lại và hai tính năng mới bằng GEE

**Phụ thuộc:** Tác vụ 1–7. Không đánh dấu hoàn tất chỉ bằng baseline test đã qua.

**Files:** tạo `validate_matched_frame_live.py`; cập nhật `README.md` phần chạy/nghiệm thu và hai kế hoạch cũ bằng liên kết tới phương án này.

- [x] Runner mới lưu trong `downloads/matched_frame_acceptance/<run-id>/`; đọc mẫu hoặc metadata còn lưu, không chạy lại script replay snapshot cũ để chứng minh luồng mới.
- [x] Tái sử dụng logic hai runner audit đã có, nhưng đổi assertion cho bản sửa: `check_current.py` từng cho preflight snapshot cũ 200 thì phải thành 409 `stale_search`; ca `check_mosaic.py` từng tạo job failed thì preflight mới phải can_start=false và POST /jobs bị chặn trước exporter.
- [x] Với mẫu EQC thực: tạo search mới, chọn cảnh thực sự giao vùng Việt Nam bằng ROI chuẩn; xác minh bounds và metadata trước tải. Asset từng tìm nhầm gần xích đạo phải không còn xuất hiện trong kết quả tìm mới.
- [x] Nghiệm thu S1 và S2 riêng: search → preview → preflight → job → GET TIFF/ZIP qua API. Với min phủ 99.9, chọn cảnh thực sự đạt; không hạ ngưỡng để làm ca phủ đầy thành công.
- [x] Nghiệm thu một lưới EPSG và một lưới EQC mẫu; kiểm CRS tương đương, affine sai số tối đa 1e-8 theo đơn vị CRS, width/height, thứ tự band, dtype, giá trị hữu hạn biến thiên và actual coverage.
- [x] Nghiệm thu nhóm có ít nhất hai nguồn bù vùng; kiểm preview nhóm, thứ tự ưu tiên, một TIFF cuối và manifest đầy đủ nguồn. Nếu khoảng ngày thử không có nhóm phù hợp, chọn fixture địa lý/thời gian khác có bằng chứng và ghi rõ thay đổi; không thay bằng mock rồi kết luận đã đạt live.
- [x] Ca âm bắt buộc giữ nguyên mẫu EQC, hai cảnh S1 ngày 2026-10-18 và 2026-10-06, ASCENDING/relative orbit 128, yêu cầu 99.9%. Đường hiện tại cho phủ cuối 95.402106%; bản sửa phải báo hợp mask không đạt trước tải giá trị và không tạo job. Không hạ ngưỡng cho ca này. Thử riêng ngưỡng 95% trên cùng nhóm: nếu phép đo đạt thì cho tạo job mới và kiểm actual coverage đạt 95%.
- [x] Snapshot cũ phải được chép vào kho thử với TTL còn hiệu lực để kiểm phiên bản; không chấp nhận 404 hết hạn làm bằng chứng đã sửa stale_search. Snapshot mới bị sửa fingerprint/geometry cũng phải 409 trước RPC.
- [x] Trên UI: upload mẫu EQC → chuyển ROI từ mẫu → tìm/tải bằng reference_id cũ → quay về đồng khung. So sánh polygon/metadata/grid options và kiểm không upload lần hai. Vẽ một manual ROI khác rồi dùng lại mẫu; hai bản nháp không bị ghi đè. Bằng chứng: `downloads/matched_frame_browser/run-a3f270f8d2ad/browser-proof.json`.
- [x] Nghiệm thu chọn platform thực sự có dữ liệu trong khoảng ngày thử ở cả reference và ROI. Mọi source trả về/được tải phải đúng platform; ghi asset IDs và metadata platform trong proof. Chọn platform không có dữ liệu trong khoảng ngày đó phải trả rỗng, không fallback all. Dùng fixture offline để kiểm đủ A/B/C/D khi nguồn live không có tất cả trong cùng vùng/ngày.
- [x] Chạy ca âm: snapshot cũ, không có cảnh đạt ngưỡng, ngưỡng phủ thực không đạt, SAR khác quỹ đạo. Mỗi ca phải trả đúng lý do và không cung cấp link TIFF thành công.
- [x] Xác định PID, command line, port và thời điểm khởi động server. Dùng tiến trình nghiệm thu riêng trên port trống nếu server hiện tại không thuộc phiên quản lý; ghi log revision/hash mã của tiến trình thử. Chạy lại qua API phục vụ giao diện sau khi chủ server triển khai/reload bản mới.
- [x] Giữ `proof.json` chứa run ID, search/preflight/job IDs, ordered assets, checksums mã đã nạp, HTTP status tải, đường dẫn artifact trong thư mục nghiệm thu, grid checks, actual coverage và mã lỗi ca âm. Chỉ kết luận sửa thành công khi artifact bền vững sau request và ca dương/âm đều đạt.

## 5. Lệnh kiểm tra

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_reference_frame.py'
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_frame*.py'
node --test tests/test_matched_frame_ui.cjs
node --test tests/test_selection_state.cjs tests/test_rectangle_ui.cjs
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_platforms.py'
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_rectangle_downloads.py'
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_s1_mosaic.py'
git diff --check
# Sau khi viết runner và có môi trường GEE hợp lệ:
.venv/Scripts/python.exe validate_matched_frame_live.py
```

Chạy test mới thất bại trước khi sửa từng lỗi; sau đó chạy lại phần liên quan. Pattern `test_frame*.py` bao gồm `test_frame_validity.py` mới. Các lệnh test mới selection-state/platform chỉ chạy sau khi tạo file ở tác vụ 6–7. Test offline và kết quả live được ghi riêng. Không lấy baseline 70 Python/37 UI làm kết quả xác nhận cho bản sửa sau. Các test helper/graph cần chạy offline bằng EE fake hiện có; live chỉ dùng kho nghiệm thu riêng.

## 6. Thứ tự và tiêu chí bàn giao

1. **P0 — Chặn dữ liệu không tin cậy:** Tác vụ 1–2. Snapshot cũ không đi tiếp; coverage None/0 không được chọn ở ngưỡng 0. Hai ca tải đơn đã thành công vẫn qua regression.
2. **P1 — Sửa lỗi ghép được chấp nhận rồi thất bại:** Tác vụ 3 và 5. Nhóm 95.40% bị chặn trước tải giá trị ở ngưỡng 99.9%; nhóm đủ phủ tải được, ngưỡng cuối vẫn kiểm trên TIFF.
3. **P1 — Hiển thị đúng nhóm:** Tác vụ 4. Có preview nhóm cho cả preflight đủ/thiếu phủ, đúng ưu tiên nguồn và không làm thay đổi quyền tạo job.
4. **P1 — Hai tính năng bổ sung:** Tác vụ 6–7. Chuyển ROI không mất khung/metadata; chọn vệ tinh được giữ và lọc tới đúng dữ liệu tải/nguồn bù.
5. **Bàn giao:** Tác vụ 8 sau các mốc trên, gồm proof live mới và xác minh đúng tiến trình nạp mã. Giữ luồng mẫu/ROI → vệ tinh → ngày → chất lượng/độ phủ → bật/tắt ghép → tìm → chọn → tải; không thêm thao tác xác nhận mới ngoài preflight hiện có.

Repo có nhiều thay đổi và tệp chưa được theo dõi từ trước. Trước triển khai phải ghi baseline, giữ nguyên việc của người dùng và review diff theo từng tệp. Lượt lập phương án không tạo commit, không restart server, không thay lịch sử job và không sửa mã ứng dụng.

## 7. Kiểm tra lại sau khi lập phương án

Xem [báo cáo kiểm tra ngày 2026-10-25](2026-10-25-matched-frame-recheck.md). Đã xác nhận live tìm mới/tải một cảnh S1 và S2 RGB thành công trên lưới EQC mẫu; tái hiện nhóm SAR được preflight chấp nhận nhưng tải xong chỉ phủ 95.402106%, không đạt ngưỡng 99.9%. Những kết quả này cập nhật mức ưu tiên của kế hoạch, không đánh dấu các tác vụ triển khai phía trên đã hoàn tất.

## 8. Ma trận nghiệm thu bản sửa

| Ca | Trước sửa đã quan sát | Bản sửa bắt buộc | Tác vụ |
|---|---|---|---|
| Search/tải đơn S1 + S2 RGB trên EQC mẫu | completed, coverage 100% | Tiếp tục completed; đúng grid và TIFF/ZIP 200 | 1, 5, 8 |
| Nhóm SAR orbit 128, ngày 18 và 06 tháng 10, yêu cầu 99.9% | Preflight 200 rồi job failed ở 95.402106% | Preflight 200/can_start=false; POST /jobs 422; không gọi exporter | 3, 8 |
| Cùng nhóm, yêu cầu 95% | Chưa kiểm live ở baseline | Đo lại trong preflight mới, nếu đạt thì completed và TIFF phủ >=95% | 3, 5, 8 |
| Snapshot ROI cũ còn TTL | Preflight 200 trong kho copy | 409 stale_search trước RPC; history completed giữ nguyên | 1, 8 |
| Preflight thiếu version mới hoặc bị sửa can_start/quality | Chưa có hợp đồng này | 409 stale_preflight trước tạo job; worker không tải nếu bypass start | 3 |
| Coverage None/0 với ngưỡng 0 | None hiện eligible=true | Không thể chọn; positive coverage vẫn chọn được | 2 |
| Preview cả nhóm | Chưa có | PNG theo nguồn/thứ tự cố định; preview lỗi không đổi can_start | 4 |
| Hai nguồn 60%, trùng vùng và bù vùng | Chưa có kiểm hợp trước tải | Nhóm trùng chỉ 60%; nhóm bù 100%, không cộng phần trăm | 3, 5 |
| Mask đạt nhưng dữ liệu TIFF không đạt | Phải giữ kiểm tra cuối | failed có actual coverage, không download URL thành công | 5 |
| Đồng khung → ROI từ mẫu → đồng khung | Reference còn trong bộ nhớ nhưng chưa làm nguồn rectangle | Không upload lại; polygon/CRS/affine/grid options được giữ | 6, 8 |
| Vẽ ROI mới rồi dùng lại khung mẫu | Chưa có bản nháp tách nguồn | Manual ROI và mẫu không ghi đè nhau; dispatch đúng source | 6 |
| Chọn Sentinel-1C trong đồng khung và ROI | Đồng khung có selector; ROI chưa truyền đủ | Kết quả, TIFF và mọi nguồn bù đều C; không chỉ đổi label | 7, 8 |
| Chọn Sentinel-1B nhưng không có dữ liệu trong khoảng ngày | Có nguy cơ nhầm với kết quả all | Trả rỗng đúng bộ lọc, không fallback sang A/C/D | 7, 8 |
| Đổi vệ tinh lúc job đang chạy | Chưa nghiệm thu | Search/preflight mới bị invalidation; job cũ giữ nguồn/filters ban đầu | 6, 7 |

**Không đổi tiêu chí để làm test qua:** Không tự giảm 99.9 xuống 95, không đánh dấu snapshot cũ là phiên bản mới, không tạo job để đo thử khả năng đạt phủ và không coi HTTP 200 của preflight là quyền tải khi can_start=false.
