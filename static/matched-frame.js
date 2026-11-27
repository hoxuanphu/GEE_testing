(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const API = '/api/matched-frame';
  const STORAGE = 'satellite-workspace-matched-frame-job';
  const TERMINAL = new Set(['completed', 'partial', 'failed', 'interrupted']);
  const SUCCESS = new Set(['completed', 'succeeded', 'success']);
  const labels = {queued:'Đang chờ', running:'Đang xử lý', completed:'Hoàn tất', partial:'Hoàn tất một phần', failed:'Thất bại', interrupted:'Bị gián đoạn', pending:'Chưa xử lý', succeeded:'Thành công', success:'Thành công'};
  const selected = new Map();
  const dateHome = get('dateSelection').parentElement;
  const dateNext = get('dateSelection').nextElementSibling;
  const best = () => get('mfResultMode').value === 'best';
  let reference = null, snapshot = null, preflight = null, footprint = null;
  let uploadVersion = 0, searchVersion = 0, preflightVersion = 0;
  let uploadController, searchController, preflightController;
  let uploading = false, searching = false, checking = false, starting = false;
  let job = null, jobId = null, pollTimer = null, pollController = null, pollVersion = 0, pollDelay = 2000;
  let stopped = false, storageWarning = '';
  let groupPreviewClick = null;
  const selection = SelectionState;
  const mode = () => get('selection_mode').value === 'reference' || get('selection_mode').value === 'rectangle' && selection.getSource() === 'reference';
  let previousMode = get('selection_mode').value, platformDataset = null;
  const sar = () => get('dataset').value === 's1gee';
  const text = value => value == null ? 'N/A' : String(value);
  const pct = value => value == null || !Number.isFinite(Number(value)) ? 'N/A' : `${Number(value).toFixed(2)}%`;
  const node = (tag, content, className) => {
    const element = document.createElement(tag);
    if (content != null) element.textContent = String(content);
    if (className) element.className = className;
    return element;
  };
  function crsSummary(target, crs, label, dimensions) {
    const full = text(crs);
    const authority = full.match(/(?:AUTHORITY|ID)\s*\[\s*"EPSG"\s*,\s*"?(\d+)"?\s*\]\s*\]\s*$/i);
    const compact = authority ? `EPSG:${authority[1]}` : full.includes('[') || full.length > 80 ? 'CRS tùy chỉnh (xem chi tiết)' : full;
    target.append(node('p', `${label}: ${compact} · ${dimensions}`));
    if (compact !== full) {
      const details = node('details');
      details.append(node('summary', 'CRS đầy đủ (WKT)'), node('pre', full));
      target.append(details);
    }
  }
  const message = value => typeof value === 'string' ? value : value && (value.message || value.code) || 'Không có thông tin chi tiết';
  const warningLabels = {
    coverage_estimated_20m:'Độ phủ là ước lượng trên toàn ROI ở độ phân giải 20 m; kiểm chứng coverage thực sau tải.',
    coverage_estimated_100m:'Độ phủ là ước lượng trên toàn ROI ở độ phân giải 100 m; kiểm chứng coverage thực sau tải.',
    grid_alignment_is_not_coregistration:'Đồng lưới không bảo đảm đồng đăng ký đối tượng giữa các ảnh.',
    scl_does_not_guarantee_haze_free:'SCL không bảo đảm ảnh không có sương hoặc mây mỏng, kể cả khi mây SCL là 0%.',
    native_resolution_10_20_60m:'Các band Sentinel-2 có độ phân giải gốc 10/20/60 m; nội suy không tăng chi tiết ảnh.'
  };
  function expiryDate(value) {
    const date = new Date(typeof value === 'number' ? value * 1000 : value);
    return Number.isFinite(date.getTime()) ? `${date.toLocaleString('vi-VN', {timeZone:'UTC', hour12:false})} UTC` : 'N/A';
  }
  function warnings(target, values) {
    if (!Array.isArray(values) || !values.length) return;
    const list = node('ul', null, 'mf-warnings');
    values.forEach(value => list.append(node('li', warningLabels[typeof value === 'string' ? value : value?.code] || message(value))));
    target.append(list);
  }
  function safeLink(value) {
    if (typeof value !== 'string' || !value.startsWith(`${API}/`) || /[\\\r\n]/.test(value)) return null;
    const url = new URL(value, location.origin);
    return url.origin === location.origin && url.pathname.startsWith(`${API}/`) ? url.pathname + url.search : null;
  }
  function openResults() {
    get('matchedFrameResults').hidden = false;
    window.SatelliteWorkspace?.showView('results');
  }
  function clearError() {
    get('mfError').hidden = true;
    get('mfError').replaceChildren();
    get('mfFieldError').textContent = '';
    document.querySelectorAll('#referenceSelection [aria-invalid], #start_date[aria-invalid], #end_date[aria-invalid], #dataset[aria-invalid]').forEach(element => element.removeAttribute('aria-invalid'));
  }
  function error(value, field) {
    const detail = message(value);
    const summary = get('mfError');
    summary.replaceChildren(node('p', detail));
    if (field) {
      get(field).setAttribute('aria-invalid', 'true');
      const describedBy = get(field).getAttribute('aria-describedby') || '';
      if (!describedBy.split(/\s+/).includes('mfFieldError')) get(field).setAttribute('aria-describedby', `${describedBy} mfFieldError`.trim());
      get('mfFieldError').textContent = detail;
      const link = node('a', 'Sửa trường liên quan');
      link.href = `#${field}`;
      link.addEventListener('click', event => {
        event.preventDefault();
        window.SatelliteWorkspace?.showView('explore');
        for (let parent = get(field).parentElement; parent; parent = parent.parentElement) {
          if (parent.tagName === 'DETAILS') parent.open = true;
        }
        get(field).focus();
      });
      summary.append(link);
    }
    summary.hidden = false;
    openResults();
    summary.focus();
  }
  async function request(path, options = {}) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.detail;
      const failure = new Error(message(detail || `HTTP ${response.status}. Hãy thử lại.`));
      failure.code = detail?.code;
      failure.status = response.status;
      throw failure;
    }
    return body;
  }
  const post = (path, body, signal) => request(`${API}/${path}`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body), signal});
  function activeJob() { return !!jobId && (!job || !TERMINAL.has(job.status)); }
  function controls() {
    get('mfCopyRoi').disabled = !reference || uploading;
    if (mode()) {
      get('btnSearch').disabled = uploading || searching;
      get('btnSearch').textContent = searching ? 'Đang tìm cảnh đồng khung…' : 'Tìm cảnh đồng khung';
    }
    get('mfPreflight').disabled = !snapshot || !selected.size || checking || starting || activeJob();
    get('mfPrev').disabled = !snapshot || searching || snapshot.offset <= 0;
    get('mfNext').disabled = !snapshot || searching || snapshot.offset + snapshot.limit >= snapshot.total;
    get('mfAcknowledge').disabled = !preflight || preflight.can_start !== true;
    get('mfConfirm').disabled = !preflight || preflight.can_start !== true || !get('mfAcknowledge').checked || starting || activeJob();
    get('mfCancel').disabled = starting;
    get('mfClearSelection').disabled = !selected.size || starting;
    const mosaic = snapshot?.parameters.merge_scenes;
    get('mfSelected').textContent = `Đã chọn ${selected.size} / 10 cảnh (giữ qua các trang) · ${mosaic && selected.size ? '1 ảnh ghép đầu ra' : `${selected.size} ảnh đầu ra`}`;
  }
  function invalidatePreflight() {
    preflightVersion++;
    preflightController?.abort();
    checking = false;
    preflight = null;
    get('mfConfirmation').hidden = true;
    get('mfAcknowledge').checked = false;
    get('mfGroupPreview').replaceChildren();
    get('mfGroupPreview').hidden = true;
    get('mfGroupPreviewButton').hidden = true;
    get('mfGroupPreviewButton').setAttribute('aria-expanded', 'false');
    groupPreviewClick = null;
    controls();
  }
  function invalidateSearch() {
    searchVersion++;
    searchController?.abort();
    searching = false;
    snapshot = null;
    selected.clear();
    invalidatePreflight();
    get('mfSearchResults').hidden = true;
    get('mfSceneRows').replaceChildren();
    get('mfSelectedSources').replaceChildren();
    get('mfStatus').textContent = '';
    get('matchedFrameResults').hidden = !jobId;
    clearError();
  }
  function removeFootprint() {
    if (footprint) map.removeLayer(footprint);
    footprint = null;
  }
  function drawFootprint() {
    removeFootprint();
    if (mode() && reference?.footprint_geojson) {
      footprint = L.geoJSON(reference.footprint_geojson, {color:getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(), weight:2, fillOpacity:0.08, interactive:false}).addTo(map);
    }
    get('mfFit').disabled = !footprint;
  }
  function syncGrid() {
    const compatible = reference?.compatible_grid_modes || ['match_extent', 'match_grid'];
    for (const option of get('mfGridMode').options) option.disabled = !compatible.includes(option.value);
    if (!compatible.includes(get('mfGridMode').value)) {
      get('mfGridMode').value = compatible.includes('match_grid') ? 'match_grid' : compatible[0] || '';
      get('mfUploadStatus').textContent = 'Mẫu không hỗ trợ đồng biên theo mét. Đã chọn đồng lưới; kiểm tra đơn vị CRS trước tải.';
    }
    get('mfResolution').disabled = get('mfGridMode').value !== 'match_extent';
  }
  function syncForm() {
    window.SceneInventory?.invalidate();
    const rectangle = get('selection_mode').value === 'rectangle';
    get('referenceSelection').hidden = !mode();
    window.S2Layers?.updateVisibility();
    get('mfReferenceTitle').textContent = rectangle ? 'ROI từ khung mẫu đã lưu' : 'Đồng khung từ GeoTIFF mẫu';
    get('mfRoiSource').hidden = !rectangle;
    get('mfUseReference').disabled = !reference;
    get('mfClearReference').disabled = !reference;
    get('mfRoiSourceStatus').textContent = selection.getSource() === 'reference' ? `ROI từ khung mẫu: ${reference?.filename || reference?.reference_id || ''}. Giữ nguyên polygon và lưới đã chọn.` : selection.getSource() === 'manual' ? 'Đang dùng ROI thủ công; khung mẫu vẫn được lưu riêng.' : 'Chưa chọn vùng. Vẽ ROI mới hoặc upload mẫu.';
    if (typeof rectangleEditor !== 'undefined') rectangleEditor.setActive(rectangle && selection.getSource() === 'manual');
    syncPlatform();
    get('mfS1Options').hidden = !sar();
    get('mfSort').options[0].textContent = sar() ? 'Ưu tiên ngày và quỹ đạo SAR' : 'Ít mây + bóng trong ROI nhất';
    get('mfS2Options').hidden = sar();
    get('mfS2ExportOptions').hidden = sar();
    get('mfMinCoverage').disabled = get('mfAllowPartial').checked;
    if (dateHome) {
      if (mode()) get('mfDateSlot').append(get('dateSelection'));
      else dateHome.insertBefore(get('dateSelection'), dateNext);
    }
    if (mode()) {
      for (const id of ['cloudSelection', 'sarMergeSelection', 's2SourceSelection', 'slcSelection', 's3TypeSelection', 'demHint']) get(id).hidden = true;
      get('dateSelection').style.display = '';
    } else {
      for (const id of ['cloudSelection', 'sarMergeSelection', 's2SourceSelection', 'slcSelection', 's3TypeSelection', 'demHint']) get(id).hidden = false;
      updateCloudSelection();
      updateTimeSeriesVisibility();
    }
    syncGrid();
    controls();
  }
  function syncPlatform() {
    const dataset = get('dataset').value;
    get('mfPlatformSelection').hidden = !['s1gee', 'sentinel2'].includes(dataset) || !mode() && get('time_series_enabled').checked;
    if (platformDataset === dataset) return;
    platformDataset = dataset;
    const codes = dataset === 's1gee' ? ['A','B','C','D'] : ['A','B','C'];
    const options = ['', ...codes].map(code => {
      const option = node('option', code ? `Sentinel-${dataset === 's1gee' ? '1' : '2'}${code}` : 'Tất cả');
      option.value = code; return option;
    });
    get('mfPlatform').replaceChildren(...options);
    get('mfPlatform').value = selection.getPlatform(dataset) || '';
  }
  function saveDraft() {
    if (reference) selection.setReferenceDraft({...reference, grid_options:{grid_mode:get('mfGridMode').value, target_resolution_m:get('mfResolution').value, band_preset:get('mfBands').value}});
  }
  function metadata() {
    const target = get('mfReferenceMeta');
    target.replaceChildren();
    if (!reference) return;
    const meta = reference.metadata;
    const grid = meta.grid || meta;
    const affine = grid.transform || [];
    crsSummary(target, grid.crs, 'CRS', `${text(grid.width)} × ${text(grid.height)} pixel`);
    target.append(node('p', `Affine (a,b,c,d,e,f): ${affine.join(', ')}`, 'hint'));
    if (affine.length === 6) target.append(node('p', `Vector pixel mẫu: ${Math.hypot(affine[0], affine[3])} × ${Math.hypot(affine[1], affine[4])} ${text(meta.pixel_unit || grid.pixel_unit || meta.linear_units || meta.units || grid.units || 'đơn vị CRS')}`, 'hint'));
    const descriptions = meta.band_descriptions || meta.descriptions || meta.bands;
    target.append(node('p', `Band mẫu: ${Array.isArray(descriptions) ? descriptions.map(value => typeof value === 'object' && value !== null ? value.description || value.name || 'N/A' : text(value)).join(', ') : text(meta.count)} · dtype: ${text(meta.dtype || meta.dtypes?.join(', '))}`, 'hint'));
    const details = node('details');
    details.append(node('summary', 'Footprint WGS84 (toàn khung raster)'));
    details.append(node('pre', JSON.stringify(reference.footprint_geojson, null, 2)));
    target.append(details);
    warnings(target, reference.warnings);
  }
  async function upload() {
    const version = ++uploadVersion;
    const uploadContext = get('selection_mode').value;
    uploadController?.abort();
    uploading = false;
    reference = null;
    selection.clearReferenceDraft();
    removeFootprint();
    get('mfFit').disabled = true;
    get('mfReferenceMeta').replaceChildren();
    get('mfUploadStatus').textContent = '';
    invalidateSearch();
    syncGrid();
    const files = get('mfFile').files;
    if (!files?.length) { controls(); return; }
    const file = files[0];
    if (files.length !== 1 || !/\.tiff?$/i.test(file.name)) { error('Chọn đúng một GeoTIFF .tif hoặc .tiff.', 'mfFile'); return; }
    if (file.size > 512 * 1024 * 1024 || file.size === 0) { error('GeoTIFF phải lớn hơn 0 và không quá 512 MiB. Chọn tệp nhỏ hơn.', 'mfFile'); return; }
    const form = new FormData();
    form.append('file', file);
    uploadController = new AbortController();
    uploading = true;
    get('mfUploadStatus').textContent = 'Đang upload và đọc metadata…';
    controls();
    try {
      const data = await request(`${API}/references`, {method:'POST', body:form, signal:uploadController.signal});
      if (version !== uploadVersion || get('selection_mode').value !== uploadContext) return;
      if (!data.reference_id || !data.metadata || !data.footprint_geojson) throw new Error('Metadata mẫu không đầy đủ. Hãy upload lại.');
      reference = data;
      selection.setReferenceDraft(reference);
      if (uploadContext === 'rectangle') selection.activateReferenceRoi();
      get('mfUploadStatus').textContent = `Đã đọc metadata: ${file.name}. Pixel upload đã được máy chủ xóa sau khi đọc.`;
      metadata();
      syncGrid();
      saveDraft(); syncForm();
      drawFootprint();
    } catch (failure) {
      if (version === uploadVersion && failure.name !== 'AbortError') {
        get('mfUploadStatus').textContent = 'Upload thất bại. Chọn lại tệp để thử lại.';
        get('mfFile').value = '';
        error(failure, 'mfFile');
      }
    } finally {
      if (version === uploadVersion) { uploading = false; controls(); }
    }
  }
  function invalid(message, field) { const failure = new Error(message); failure.field = field; throw failure; }
  function dateValue(id, required) {
    const value = get(id).value;
    const date = new Date(`${value}T00:00:00Z`);
    if (!value && !required) return null;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date.getTime()) || date.toISOString().slice(0, 10) !== value) invalid('Chọn ngày hợp lệ.', id);
    return value;
  }
  function orbit(id) {
    if (!get(id).value.trim()) return null;
    const value = Number(get(id).value);
    if (!Number.isInteger(value) || value < 1 || value > 175) invalid('Quỹ đạo tương đối phải là số nguyên từ 1 đến 175.', id);
    return value;
  }
  function searchBody() {
    if (!reference) invalid('Upload GeoTIFF mẫu và chờ đọc metadata trước.', 'mfFile');
    const dataset = get('dataset').value;
    if (!['sentinel2', 's1gee'].includes(dataset)) invalid('Đồng khung chỉ hỗ trợ Sentinel-2 SR GEE hoặc Sentinel-1 SAR GEE. Hãy đổi nguồn.', 'dataset');
    const start = dateValue('start_date', true), end = dateValue('end_date', true);
    const days = (Date.parse(end) - Date.parse(start)) / 86400000 + 1;
    if (days < 1 || days > 366) invalid('Khoảng ngày phải đúng thứ tự và không quá 366 ngày (bao gồm ngày kết thúc).', 'end_date');
    const referenceDate = dateValue('mfReferenceDate', get('mfSort').value === 'nearest_date');
    const percentage = (id, optional = false) => {
      const raw = get(id).value.trim();
      if (!raw && optional) return null;
      const value = Number(raw);
      if (!raw || !Number.isFinite(value) || value < 0 || value > 100) invalid('Giá trị phải nằm trong 0–100% hoặc để trống nếu không giới hạn mây.', id);
      return value;
    };
    const cloud = sar() ? null : percentage('mfCloud', true);
    const minCloud = sar() ? null : percentage('mfCloudMin', true);
    if (cloud != null && minCloud != null && minCloud > cloud) invalid('Mây tối thiểu phải nhỏ hơn hoặc bằng mây tối đa.', 'mfCloudMin');
    const minCoverage = get('mfAllowPartial').checked ? 0 : percentage('mfMinCoverage');
    const pass = get('mfReferencePass').value || null;
    const refOrbit = sar() ? orbit('mfReferenceOrbit') : null;
    const prefer = sar() && get('mfPreferOrbit').checked;
    if (prefer && (!pass || refOrbit == null)) invalid('Để ưu tiên quỹ đạo mẫu, xác nhận cả hướng bay và quỹ đạo tương đối.', !pass ? 'mfReferencePass' : 'mfReferenceOrbit');
    const gridMode = get('mfGridMode').value;
    if (!(reference.compatible_grid_modes || ['match_extent', 'match_grid']).includes(gridMode)) invalid('Lưới không tương thích CRS mẫu. Hãy chọn đồng lưới.', 'mfGridMode');
    return {reference_id:reference.reference_id, selection_context:get('selection_mode').value === 'rectangle' ? 'reference_roi' : 'reference', dataset, start_date:start, end_date:end,
      grid_mode:gridMode, target_resolution_m:Number(get('mfResolution').value),
      band_preset:get('mfBands').value, polarization:get('mfPolarization').value,
      sort:get('mfSort').value, reference_date:referenceDate,
      require_full_coverage:false, min_coverage_pct:minCoverage, merge_scenes:get('mfMergeScenes').checked,
      min_cloud_shadow_pct:minCloud, max_cloud_shadow_pct:cloud,
      orbit_pass:sar() ? get('mfOrbitPass').value || null : null,
      relative_orbit:sar() ? orbit('mfRelativeOrbit') : null, platform:get('mfPlatform').value || null,
      prefer_reference_orbit:prefer, reference_orbit_pass:prefer ? pass : null, reference_relative_orbit:prefer ? refOrbit : null};
  }
  function processing(parameters) {
    if (parameters.dataset === 's1gee') return `SAR IW · ${text(parameters.polarization).toUpperCase()} · Float32 dB gốc, không log/clamp · nearest mặc định. Preview -25…0 dB, chỉ đổi hiển thị; không sửa TIFF.`;
    const bands = {full:'B1, B2, B3, B4, B5, B6, B7, B8, B8A, B9, B11, B12', rgb:'B4, B3, B2', rgbnir:'B2, B3, B4, B8'};
    return `S2 SR · ${bands[parameters.band_preset] || bands.full} · Float32, DN × 0,0001, không clamp · bilinear mặc định. Band gốc 10 m: B2/B3/B4/B8; 20 m: B5/B6/B7/B8A/B11/B12; 60 m: B1/B9. Nội suy không tăng chi tiết. Preview RGB B4/B3/B2 stretch 0–0,3, chỉ đổi hiển thị; không sửa TIFF.`;
  }
  function eligible(scene) {
    const parameters = snapshot.parameters;
    const threshold = minimumCoverage(parameters);
    const coverage = scene.band_coverage_pct;
    return scene.eligible === true && typeof coverage === 'number' && Number.isFinite(coverage) && coverage > 0 && coverage <= 100 && (parameters.merge_scenes || coverage >= threshold);
  }
  const minimumCoverage = parameters => parameters.require_full_coverage ? 99.9 : parameters.min_coverage_pct ?? 0;
  function eligibilityReason(scene) {
    if (eligible(scene)) return 'Có thể chọn';
    if (typeof scene.band_coverage_pct !== 'number' || !Number.isFinite(scene.band_coverage_pct)) return 'Không thể chọn: độ phủ chưa xác định';
    if (scene.band_coverage_pct <= 0) return 'Không thể chọn: không có dữ liệu hợp lệ trong ROI';
    const threshold = minimumCoverage(snapshot.parameters);
    if (!snapshot.parameters.merge_scenes && scene.band_coverage_pct < threshold) return `Không thể chọn: độ phủ dưới ${threshold}%`;
    return 'Không đủ điều kiện chất lượng / bộ lọc để chọn';
  }
  function sourceList(target, sources, mosaic) {
    const list = node('ol');
    sources.forEach((scene, index) => {
      const role = mosaic ? index === 0 ? 'Ảnh chính' : `Bù NoData ưu tiên ${index}` : `Ảnh ${index + 1}`;
      const item = node('li', `${role} · ${text(scene.platform)} · ${text(scene.timestamp || scene.date)} UTC · ${text(scene.asset_id || scene.scene_token)} · coverage ${pct(scene.band_coverage_pct)} · mây + bóng ROI ${pct(scene.roi_cloud_shadow_pct)}`);
      item.append(node('p', `Tile: ${text(scene.tile)} · Hướng bay: ${text(scene.orbit_pass)} · Quỹ đạo: ${text(scene.relative_orbit)} · ROI mây: ${pct(scene.roi_cloud_pct)} · bóng: ${pct(scene.roi_shadow_pct)}`, 'hint'));
      warnings(item, scene.warnings);
      list.append(item);
    });
    target.append(list);
  }
  function renderSelected() {
    const target = get('mfSelectedSources');
    target.replaceChildren();
    if (!selected.size || !snapshot) return;
    const mosaic = snapshot.parameters.merge_scenes;
    target.append(node('p', mosaic ? 'Thứ tự nguồn: nguồn 1 là ảnh chính, các nguồn sau chỉ bù NoData. Dùng nút để đổi ưu tiên.' : 'Các cảnh đã chọn (mỗi cảnh xuất một TIFF riêng).'));
    sourceList(target, [...selected.values()], mosaic);
    if (!mosaic) return;
    const entries = [...selected.entries()];
    entries.forEach((_, index) => {
      const group = node('div', null, 'row');
      for (const [offset, label] of [[-1, 'Lên'], [1, 'Xuống']]) {
        const button = node('button', `${label} · nguồn ${index + 1}`);
        button.type = 'button';
        button.disabled = starting || index + offset < 0 || index + offset >= entries.length;
        button.setAttribute('aria-label', `${label} nguồn ${index + 1} trong thứ tự ưu tiên`);
        button.addEventListener('click', () => {
          if (button.disabled) return;
          const reordered = [...selected.entries()];
          [reordered[index], reordered[index + offset]] = [reordered[index + offset], reordered[index]];
          selected.clear(); reordered.forEach(([key, value]) => selected.set(key, value));
          selectionChanged();
          get('mfSelectedSources').querySelectorAll('button')[index * 2 + (offset === -1 ? 0 : 1)]?.focus();
        });
        group.append(button);
      }
      target.append(group);
    });
  }
  function selectionChanged() {
    invalidatePreflight();
    if (snapshot) snapshot.scenes.forEach((scene, index) => {
      const checkbox = get('mfSceneRows').children[index].querySelector('input');
      checkbox.checked = selected.has(scene.scene_token);
      checkbox.disabled = starting || !eligible(scene) || (!best() && !checkbox.checked && selected.size >= 10);
    });
    renderSelected();
    controls();
  }
  function renderScenes() {
    const target = get('mfSceneRows');
    target.replaceChildren();
    if (!snapshot) return;
    if (!snapshot.scenes.length) target.append(node('p', 'Không có cảnh phù hợp. Thu hẹp/đổi khoảng ngày hoặc kiểm tra bộ lọc.'));
    snapshot.scenes.forEach((scene, index) => {
      const card = node('article', null, 'mf-scene');
      window.SceneThumbnails?.append(card, scene, scene.asset_id);
      const label = node('label', null, 'mf-scene-choice');
      const checkbox = node('input');
      checkbox.type = best() ? 'radio' : 'checkbox';
      checkbox.name = 'mfSceneChoice';
      checkbox.id = `mfScene${index}`;
      checkbox.checked = selected.has(scene.scene_token);
      checkbox.disabled = starting || !eligible(scene) || (!best() && !checkbox.checked && selected.size >= 10);
      label.htmlFor = checkbox.id;
      label.append(checkbox, node('span', `${text(scene.platform)} · ${text(scene.timestamp || scene.date)} UTC`));
      checkbox.addEventListener('change', () => {
        if (starting) return;
        if (checkbox.checked) {
          if (!eligible(scene) || (!best() && selected.size >= 10 && !selected.has(scene.scene_token))) { checkbox.checked = false; error('Chỉ chọn tối đa 10 cảnh đủ điều kiện.'); return; }
          if (best()) selected.clear();
          selected.set(scene.scene_token, scene);
        } else selected.delete(scene.scene_token);
        selectionChanged();
      });
      card.append(label, node('p', `ID: ${text(scene.asset_id)}`, 'hint'));
      card.append(node('p', `Tile: ${text(scene.tile)} · Hướng bay: ${text(scene.orbit_pass)} · Quỹ đạo: ${text(scene.relative_orbit)}`, 'hint'));
      card.append(node('p', `Coverage band (ước lượng): ${pct(scene.band_coverage_pct)} · ${eligibilityReason(scene)}`));
      card.append(node('p', `ROI mây: ${pct(scene.roi_cloud_pct)} · bóng: ${pct(scene.roi_shadow_pct)} · mây + bóng: ${pct(scene.roi_cloud_shadow_pct)}`, 'hint'));
      card.append(node('p', `Mây toàn tile: ${pct(scene.tile_cloud_pct)} · Tuyết ROI: ${pct(scene.roi_snow_pct)} · SCL hợp lệ/toàn ROI: ${pct(scene.scl_valid_pct)}`, 'hint'));
      warnings(card, scene.warnings);
      const previewButton = node('button', 'Xem preview (không sửa TIFF)');
      previewButton.type = 'button';
      previewButton.setAttribute('aria-expanded', 'false');
      const preview = node('div');
      preview.id = `mfPreview${index}`;
      preview.hidden = true;
      previewButton.setAttribute('aria-controls', preview.id);
      let loaded = false;
      previewButton.addEventListener('click', () => {
        preview.hidden = !preview.hidden;
        previewButton.setAttribute('aria-expanded', String(!preview.hidden));
        if (!loaded) {
          const image = node('img');
          image.alt = `Preview ${text(scene.platform)} ${text(scene.date)} · ${text(scene.asset_id)} · chỉ hiển thị`;
          image.width = 512; image.height = 512;
          image.loading = 'lazy';
          const url = safeLink(scene.preview_url) || `${API}/searches/${encodeURIComponent(snapshot.search_id)}/scenes/${encodeURIComponent(scene.scene_token)}/preview`;
          image.addEventListener('error', () => {
            image.hidden = true;
            preview.append(node('p', 'Preview không khả dụng. Có thể đóng/mở để thử lại; tìm kiếm và tải TIFF không bị ảnh hưởng.', 'hint'));
            loaded = false;
          });
          preview.replaceChildren(image);
          image.src = url;
          loaded = true;
        }
      });
      card.append(previewButton, preview);
      target.append(card);
    });
  }
  function renderSearch(data, parameters) {
    snapshot = {...data, parameters:{...parameters, ...data.parameters}, offset:data.offset || 0, limit:data.limit || 50, scenes:data.scenes || []};
    if (best() && !selected.size) {
      const first = snapshot.scenes.find(eligible);
      if (first) selected.set(first.scene_token, first);
    }
    get('mfSearchResults').hidden = false;
    const ranking = snapshot.parameters.sort === 'nearest_date' ? 'gần ngày tham chiếu' : snapshot.parameters.dataset === 's1gee' ? 'ưu tiên ngày và quỹ đạo SAR' : 'ít mây + bóng ROI';
    get('mfSearchMeta').textContent = `${snapshot.total} cảnh · ${snapshot.parameters.dataset === 's1gee' ? 'Sentinel-1 SAR GEE' : 'Sentinel-2 SR GEE'} · ${snapshot.parameters.start_date} → ${snapshot.parameters.end_date} (gồm ngày cuối) · ${snapshot.parameters.grid_mode} · ${ranking} · snapshot cố định`;
    const threshold = minimumCoverage(snapshot.parameters);
    get('mfSearchMeta').append(node('p', `${best() ? 'Ảnh tốt nhất: tự chọn cảnh đủ điều kiện đầu bảng; có thể chọn lại.' : 'Danh sách: chọn 1–10 cảnh.'} Độ phủ tối thiểu ${threshold}% · ${snapshot.parameters.merge_scenes ? '1 ảnh ghép fill_missing; ngưỡng áp dụng trên ảnh cuối, cho phép nguồn thiếu vùng.' : 'Không ghép; ngưỡng áp dụng từng cảnh.'}`));
    get('mfProcessing').textContent = processing(snapshot.parameters);
    get('mfPage').textContent = snapshot.total ? `Cảnh ${snapshot.offset + 1}–${Math.min(snapshot.offset + snapshot.scenes.length, snapshot.total)} / ${snapshot.total}` : '0 cảnh';
    renderScenes();
    renderSelected();
    controls();
  }
  async function search(offset) {
    if (!mode()) return;
    const paging = Number.isInteger(offset) && snapshot;
    const previous = snapshot;
    let body;
    if (!paging) {
      invalidateSearch();
      try { body = searchBody(); } catch (failure) { error(failure, failure.field); return; }
    } else clearError();
    const version = ++searchVersion;
    searchController?.abort();
    searchController = new AbortController();
    searching = true;
    get('mfStatus').textContent = 'Đang tìm và tính chất lượng cảnh trong ROI…';
    for (const id of ['result', 's1result', 'plan', 'timeSeriesResults']) get(id).style.display = 'none';
    get('sceneCatalogResults').hidden = true;
    openResults(); controls();
    try {
      const data = paging
        ? await request(`${API}/searches/${encodeURIComponent(previous.search_id)}?offset=${offset}&limit=50`, {signal:searchController.signal})
        : await post('searches', body, searchController.signal);
      if (version !== searchVersion || !mode()) return;
      renderSearch(data, paging ? previous.parameters : body);
      get('mfStatus').textContent = best()
        ? selected.size ? 'Đã chọn ảnh tốt nhất đủ điều kiện theo xếp hạng. Xem thông tin rồi kiểm tra trước tải.' : 'Chưa có ảnh đủ điều kiện trên trang này. Thử trang tiếp theo hoặc điều chỉnh bộ lọc.'
        : 'Đã nhận snapshot cảnh. Chọn 1–10 cảnh rồi kiểm tra trước tải.';
      if (!snapshot.total && snapshot.parameters.platform) {
        const platform = snapshot.parameters.platform;
        const allowed = snapshot.parameters.dataset === 's1gee' ? ['A','B','C','D'] : ['A','B','C'];
        if (allowed.includes(platform)) get('mfStatus').textContent = `Không có ảnh Sentinel-${snapshot.parameters.dataset === 's1gee' ? '1' : '2'}${platform} trong khoảng thời gian/vùng đã chọn.`;
      }
    } catch (failure) {
      if (version === searchVersion && failure.name !== 'AbortError') {
        if (failure.code === 'stale_search') invalidateSearch();
        get('mfStatus').textContent = failure.status === 404 && !paging ? 'Mẫu không tồn tại hoặc đã hết hạn. Hãy upload lại GeoTIFF mẫu.' : 'Tìm cảnh thất bại. Thử lại hoặc chỉnh bộ lọc; nếu quá nhiều ứng viên, thu hẹp khoảng ngày.';
        error(failure);
      }
    } finally { if (version === searchVersion) { searching = false; controls(); } }
  }
  const bytes = value => value != null && Number.isFinite(Number(value)) ? `${(Number(value) / 1048576).toLocaleString('vi-VN', {maximumFractionDigits:2})} MiB` : 'N/A';
  function gridSummary(target, grid) {
    crsSummary(target, grid.crs, 'CRS đầu ra', `${text(grid.width)} × ${text(grid.height)} pixel · ${Number(grid.width) * Number(grid.height)} pixel/cảnh`);
    target.append(node('p', `Affine chính xác (a,b,c,d,e,f): ${(grid.transform || []).join(', ')}`, 'hint'));
    if (grid.transform?.length === 6) {
      const [a,b,,d,e] = grid.transform;
      target.append(node('p', `Hai vector pixel thực: ${Math.hypot(a,d)} × ${Math.hypot(b,e)} ${text(grid.pixel_unit || grid.linear_units || grid.units || reference?.metadata?.pixel_unit || reference?.metadata?.linear_units || 'đơn vị CRS')}. ${snapshot.parameters.grid_mode === 'match_grid' ? 'Giữ nguyên lưới mẫu, không làm tròn affine.' : `Mục tiêu xấp xỉ ${snapshot.parameters.target_resolution_m} m; không ép pixel vuông hoặc nới mép.`}`));
    }
  }
  function validQuality(data) {
    if (!snapshot.parameters.merge_scenes) return data.selection_quality == null;
    const quality = data.selection_quality;
    const assets = [...selected.values()].map(scene => scene.asset_id);
    const threshold = minimumCoverage(snapshot.parameters);
    return quality?.measurement_version === 1 && quality.method === 'output_grid_mask' && quality.estimated === true &&
      typeof quality.band_coverage_pct === 'number' && Number.isFinite(quality.band_coverage_pct) && quality.band_coverage_pct >= 0 && quality.band_coverage_pct <= 100 &&
      data.can_start === (quality.band_coverage_pct > 0 && quality.band_coverage_pct >= threshold) &&
      Number.isInteger(quality.valid_pixel_count) && Number.isInteger(quality.total_pixels) && quality.total_pixels > 0 && quality.valid_pixel_count >= 0 && quality.valid_pixel_count <= quality.total_pixels &&
      Math.abs(100 * quality.valid_pixel_count / quality.total_pixels - quality.band_coverage_pct) < 0.000001 &&
      Array.isArray(quality.ordered_asset_ids) && JSON.stringify(quality.ordered_asset_ids) === JSON.stringify(assets) &&
      typeof quality.roi_fingerprint === 'string' && quality.roi_fingerprint.length > 0 &&
      (!snapshot.parameters.roi_fingerprint || quality.roi_fingerprint === snapshot.parameters.roi_fingerprint) && quality.merge_policy === 'primary_first_fill_nodata';
  }
  function groupPreview(data) {
    const button = get('mfGroupPreviewButton'), preview = get('mfGroupPreview');
    const href = safeLink(data.preview_url);
    if (!snapshot.parameters.merge_scenes || !href || new URL(href, location.origin).pathname !== `${API}/preflights/${encodeURIComponent(data.preflight_id)}/preview`) return;
    button.hidden = false;
    const version = preflightVersion;
    let loaded = false;
    groupPreviewClick = () => {
      if (version !== preflightVersion) return;
      preview.hidden = !preview.hidden;
      button.setAttribute('aria-expanded', String(!preview.hidden));
      if (preview.hidden || loaded) return;
      const image = node('img');
      image.alt = `Preview nhóm ${selected.size} nguồn theo thứ tự ưu tiên; chỉ hiển thị, không sửa TIFF.`;
      image.width = 512; image.height = 512; image.loading = 'lazy';
      image.addEventListener('error', () => {
        if (version !== preflightVersion) return;
        preview.replaceChildren(node('p', 'Preview nhóm không khả dụng. Đóng/mở để thử lại; kết quả kiểm tra độ phủ vẫn giữ nguyên.', 'hint'));
        loaded = false;
      });
      preview.replaceChildren(image); loaded = true; image.src = href;
    };
  }
  async function checkPreflight() {
    if (!snapshot || !selected.size || selected.size > 10 || checking || starting || activeJob()) return;
    invalidatePreflight(); clearError();
    const version = preflightVersion;
    const searchId = snapshot.search_id;
    preflightController = new AbortController();
    checking = true;
    get('mfStatus').textContent = 'Đang kiểm tra lưới, dung lượng và cảnh báo…'; controls();
    try {
      const data = await post('preflight', {search_id:searchId, scene_tokens:[...selected.keys()]}, preflightController.signal);
      if (version !== preflightVersion || !snapshot || snapshot.search_id !== searchId) return;
      if (data.preflight_version !== 2 || typeof data.can_start !== 'boolean' ||
          (data.can_start ? data.blocking_reason !== null : data.blocking_reason !== 'insufficient_selection_coverage') || !validQuality(data) ||
          !data.preflight_id || !data.digest || !data.output_grid || !data.estimate ||
          !Number.isInteger(data.output_grid.width) || !Number.isInteger(data.output_grid.height) ||
          data.output_grid.width < 1 || data.output_grid.height < 1 ||
          !Array.isArray(data.output_grid.transform) || data.output_grid.transform.length !== 6 ||
          !data.output_grid.transform.every(Number.isFinite) || !data.output_grid.crs ||
          !Number.isFinite(data.estimate.uncompressed_bytes) || !Number.isFinite(data.estimate.disk_reserve_bytes)) {
        throw new Error('Kiểm tra trước tải thiếu digest/lưới/dung lượng hợp lệ. Hãy thử lại.');
      }
      preflight = data;
      const summary = get('mfPreflightSummary');
      const mosaic = snapshot.parameters.merge_scenes;
      const outputCount = data.estimate.output_count ?? (mosaic ? 1 : selected.size);
      const sourceCount = data.estimate.source_count ?? selected.size;
      summary.replaceChildren(node('p', `${sourceCount} nguồn → ${outputCount} ảnh đầu ra · ${snapshot.parameters.grid_mode} · ngày/ID không đổi khi tải.`));
      if (mosaic) summary.append(node('p', `Độ phủ hợp mask ước lượng: ${pct(data.selection_quality.band_coverage_pct)} · yêu cầu ${minimumCoverage(snapshot.parameters)}%. ${data.can_start ? 'Đạt ngưỡng trước tải.' : 'Chưa đủ phủ. Giữ nguồn để xem preview hoặc chọn lại; chưa thể tạo job.'}`, 'workspace-notice'));
      sourceList(summary, [...selected.values()], mosaic);
      summary.append(node('p', mosaic ? 'Ghép fill_missing cục bộ: nguồn 1 là ảnh chính, các nguồn tiếp theo bù NoData theo đúng thứ tự trên. Giữ mọi pixel hợp lệ (kể cả mây); chỉ xuất MỘT TIFF ghép.' : 'Không ghép: mỗi cảnh đã chọn xuất một TIFF riêng. Vùng thiếu giữ NoData.', 'workspace-notice'));
      summary.append(node('p', `Độ phủ thực tối thiểu: ${minimumCoverage(snapshot.parameters)}% ${mosaic ? 'trên ảnh ghép cuối cùng, không yêu cầu từng nguồn đạt ngưỡng' : 'cho từng ảnh đầu ra'}.`));
      gridSummary(summary, data.output_grid);
      summary.append(node('p', processing(snapshot.parameters), 'hint'));
      summary.append(node('p', `Chưa nén: ${bytes(data.estimate.uncompressed_bytes)} · Dự trữ đĩa cần: ${bytes(data.estimate.disk_reserve_bytes)}`));
      summary.append(node('p', 'Giới hạn: 10 nguồn, 100 triệu pixel/ảnh, cạnh ≤30.000; 4 GiB chưa nén/job. Coverage thực từng band được kiểm tra sau tải; ảnh không đạt ngưỡng có thể thất bại.', 'workspace-notice'));
      summary.append(node('p', 'Nội suy lên lưới mịn không tăng chi tiết; đồng lưới không phải đồng đăng ký đối tượng. SCL 0% không loại trừ haze/mây mỏng. Không thay cảnh đã chọn; phần thiếu sau xử lý vẫn là NoData.', 'workspace-notice'));
      warnings(summary, data.warnings);
      summary.append(node('p', `Digest xác nhận: ${data.digest}${data.expires_at != null ? ` · hết hạn: ${expiryDate(data.expires_at)}` : ''}`, 'hint'));
      get('mfConfirmation').hidden = false;
      groupPreview(data);
      get('mfStatus').textContent = data.can_start ? 'Kiểm tra hoàn tất. Đọc cảnh báo và xác nhận rõ trước khi tải.' : 'Nhóm chưa đủ độ phủ yêu cầu. Xem preview hoặc điều chỉnh nguồn/ngưỡng rồi kiểm tra lại.';
      get('mfConfirmTitle').focus();
    } catch (failure) {
      if (version === preflightVersion && failure.name !== 'AbortError') {
        if (failure.code === 'stale_search') invalidateSearch();
        get('mfStatus').textContent = failure.code === 'stale_search' ? 'Kết quả đã cũ. Hãy tìm lại với khung mẫu đã lưu.' : 'Chưa tạo job. Có thể kiểm tra lại trước tải.'; error(failure);
      }
    } finally { if (version === preflightVersion) { checking = false; controls(); } }
  }
  function persist() {
    try { localStorage.setItem(STORAGE, jobId); }
    catch (_) { storageWarning = `Trình duyệt chặn lưu job. Giữ ID ${jobId} để tra cứu; không thể tự khôi phục sau reload.`; }
    get('mfStorageWarning').textContent = storageWarning;
  }
  async function startJob() {
    if (!preflight || preflight.preflight_version !== 2 || preflight.can_start !== true || !get('mfAcknowledge').checked || starting || activeJob()) return;
    const confirmed = {preflight_id:preflight.preflight_id, digest:preflight.digest, acknowledged:true};
    starting = true; controls(); renderScenes(); renderSelected(); clearError();
    get('mfStatus').textContent = 'Đang gửi xác nhận tạo job nền…';
    try {
      // Do not abort job creation on form edits: the server may already have accepted it.
      const data = await post('jobs', confirmed);
      if (!data.job_id) throw new Error('Máy chủ chưa trả ID job. Hãy xác nhận lại cùng preflight để lấy job đã tạo.');
      jobId = data.job_id; job = data; pollDelay = 2000;
      persist();
      renderJob(data);
      invalidatePreflight();
      get('mfStatus').textContent = 'Đã tạo job nền với snapshot đã xác nhận.';
      if (!TERMINAL.has(data.status)) schedulePoll();
    } catch (failure) { if (failure.code === 'stale_search') invalidateSearch(); error(failure); }
    finally { starting = false; controls(); renderScenes(); renderSelected(); }
  }
  function downloadLink(target, url, label) {
    const href = safeLink(url);
    if (!href) return;
    const link = node('a', label); link.href = href; link.setAttribute('download', ''); target.append(link);
  }
  function renderJob(data) {
    job = data;
    get('mfJobPanel').hidden = false;
    get('matchedFrameResults').hidden = false;
    get('mfJobStatus').textContent = `Job ${jobId} · ${labels[data.status] || text(data.status)} · ${data.processed || 0}/${data.total || 0} đã xử lý · ${data.succeeded || 0} thành công · ${data.failed || 0} lỗi · Giai đoạn: ${text(data.stage)}`;
    get('mfProgress').max = data.total || 1;
    get('mfProgress').value = data.processed || 0;
    const scenes = get('mfJobScenes'); scenes.replaceChildren();
    const mosaic = data.parameters?.merge_scenes === true;
    if (mosaic) {
      scenes.append(node('p', 'Một ảnh ghép fill_missing: giữ ảnh chính, chỉ bù NoData theo thứ tự nguồn, giữ nguyên mây.', 'workspace-notice'));
      sourceList(scenes, data.sources || [], true);
    }
    for (const scene of data.scenes || []) {
      const row = node('article', null, 'mf-scene');
      const acquired = scene.timestamp || scene.date;
      const sceneLabel = acquired ? `${scene.platform ? `${scene.platform} · ` : ''}${acquired} UTC` : scene.filename || scene.scene_token || 'Cảnh';
      row.append(node('p', `${mosaic ? 'Ảnh ghép · ảnh chính: ' : ''}${text(sceneLabel)} · ${labels[scene.status] || text(scene.status)}`));
      if (acquired && scene.filename) row.append(node('p', scene.filename, 'hint'));
      if (scene.error) row.append(node('p', message(scene.error), 'mf-failure'));
      if (scene.report) {
        const details = node('details'); details.append(node('summary', 'Báo cáo kiểm chứng / coverage thực'), node('pre', JSON.stringify(scene.report, null, 2))); row.append(details);
      }
      if (SUCCESS.has(scene.status)) downloadLink(row, scene.download_url, `Tải TIFF · ${text(scene.filename || 'cảnh đã kiểm chứng')}`);
      scenes.append(row);
    }
    const zip = get('mfZip'); zip.hidden = true; zip.removeAttribute('href');
    if (['completed', 'partial'].includes(data.status) && data.succeeded > 0) {
      const href = safeLink(data.download_url);
      if (href) { zip.href = href; zip.hidden = false; zip.setAttribute('download', ''); }
    }
    if (data.error) scenes.append(node('p', message(data.error), 'mf-failure'));
    get('mfRetryJob').hidden = true;
    get('mfStorageWarning').textContent = storageWarning;
    if (TERMINAL.has(data.status)) { clearTimeout(pollTimer); pollTimer = null; }
    controls();
  }
  function schedulePoll(delay = pollDelay) {
    clearTimeout(pollTimer); pollTimer = null;
    if (stopped || document.hidden || !jobId || job && TERMINAL.has(job.status)) return;
    pollTimer = setTimeout(pollJob, delay);
  }
  async function pollJob() {
    clearTimeout(pollTimer); pollTimer = null;
    if (stopped || document.hidden || !jobId || pollController || job && TERMINAL.has(job.status)) return;
    const version = pollVersion;
    pollController = new AbortController();
    try {
      const data = await request(`${API}/jobs/${encodeURIComponent(jobId)}`, {signal:pollController.signal});
      if (version !== pollVersion) return;
      renderJob(data); pollDelay = 2000;
    } catch (failure) {
      if (version !== pollVersion || failure.name === 'AbortError') return;
      get('mfJobPanel').hidden = false;
      get('matchedFrameResults').hidden = false;
      get('mfJobStatus').textContent = `Job ${jobId}: ${failure.message}${failure.status === 404 ? ' Job đã hết hạn hoặc không tồn tại.' : ' Sẽ tự thử lại khi tab hiển thị.'}`;
      get('mfRetryJob').hidden = false;
      pollDelay = Math.min(15000, pollDelay * 2);
      if (failure.status === 404) {
        job = {status:'interrupted'};
        try { localStorage.removeItem(STORAGE); } catch (_) { /* Storage may be unavailable. */ }
        controls();
      }
    } finally {
      if (version === pollVersion) { pollController = null; schedulePoll(); }
    }
  }
  get('mfFile').addEventListener('change', upload);
  get('mfFit').addEventListener('click', () => { if (footprint) { window.SatelliteWorkspace?.showView('explore'); map.fitBounds(footprint.getBounds(), {padding:[24,24]}); } });
  const fields = ['dataset', 'start_date', 'end_date', 'mfGridMode', 'mfResolution', 'mfBands', 'mfPolarization', 'mfSort', 'mfReferenceDate', 'mfAllowPartial', 'mfCloud', 'mfCloudMin', 'mfMinCoverage', 'mfMergeScenes', 'mfResultMode', 'mfOrbitPass', 'mfRelativeOrbit', 'mfPlatform', 'mfPreferOrbit', 'mfReferencePass', 'mfReferenceOrbit'];
  fields.forEach(id => {
    const changed = () => {
      if (id === 'mfPlatform') selection.setPlatform(get('dataset').value, get('mfPlatform').value || null);
      if (typeof invalidateSearchResults === 'function') invalidateSearchResults();
      saveDraft(); invalidateSearch(); syncForm();
    };
    get(id).addEventListener('change', changed);
    if (['mfCloud', 'mfCloudMin', 'mfMinCoverage', 'mfRelativeOrbit', 'mfReferenceOrbit', 'start_date', 'end_date', 'mfReferenceDate'].includes(id)) get(id).addEventListener('input', changed);
  });
  get('time_series_enabled').addEventListener('change', syncForm);
  get('selection_mode').addEventListener('change', () => {
    saveDraft();
    const nextMode = get('selection_mode').value;
    if (nextMode === 'rectangle') {
      if (previousMode === 'reference' && reference) selection.activateReferenceRoi();
      else if (selection.getSource() == null) selection.activateManualRoi();
    }
    previousMode = nextMode;
    invalidateSearch();
    if (!mode()) {
      uploadVersion++; uploadController?.abort(); uploading = false;
      get('btnSearch').disabled = false;
      if (!reference) {
        get('mfFile').value = '';
        get('mfUploadStatus').textContent = 'Upload đã dừng. Chọn lại GeoTIFF khi quay lại.';
      }
    }
    syncForm(); drawFootprint();
  });
  function changeSource(manual) {
    uploadVersion++; uploadController?.abort(); uploading = false;
    if (manual) selection.activateManualRoi();
    else selection.activateReferenceRoi();
    if (typeof invalidateSearchResults === 'function') invalidateSearchResults();
    invalidateSearch(); syncForm(); drawFootprint();
  }
  get('mfDrawManual').addEventListener('click', () => { changeSource(true); if (typeof rectangleEditor !== 'undefined') rectangleEditor.arm(); });
  get('mfCopyRoi').addEventListener('click', () => {
    if (!reference || uploading) return;
    try {
      const rect = RectangleROI.fromFootprint(reference.footprint_geojson);
      rectangleEditor.cancel();
      rectangleEditor.set(rect);
      get('selection_mode').value = 'rectangle';
      get('selection_mode').dispatchEvent(new Event('change', {bubbles:true}));
      changeSource(true);
      window.SatelliteWorkspace?.showView('explore');
      rectangleEditor.fit();
      get('status').textContent = 'Đã copy khung sang ROI chữ nhật độc lập. Có thể xoay, đổi kích thước, di chuyển hoặc xóa ROI; xóa mẫu GeoTIFF vẫn giữ khung này.';
      get('rectangleFit').focus();
    } catch (failure) { error(failure, 'mfCopyRoi'); }
  });
  get('mfUseReference').addEventListener('click', () => { if (reference) changeSource(false); });
  get('mfClearReference').addEventListener('click', () => {
    uploadVersion++; uploadController?.abort(); uploading = false; reference = null; selection.clearReferenceDraft();
    get('mfFile').value = ''; get('mfReferenceMeta').replaceChildren(); get('mfUploadStatus').textContent = 'Đã xóa mẫu. Upload mẫu mới để dùng lại.';
    invalidateSearch(); syncForm(); drawFootprint();
  });
  get('mfGroupPreviewButton').addEventListener('click', () => groupPreviewClick?.());
  get('mfPrev').addEventListener('click', () => { if (snapshot && !searching) search(Math.max(0, snapshot.offset - 50)); });
  get('mfNext').addEventListener('click', () => { if (snapshot && !searching) search(snapshot.offset + snapshot.limit); });
  get('mfClearSelection').addEventListener('click', () => { selected.clear(); selectionChanged(); });
  get('mfPreflight').addEventListener('click', checkPreflight);
  get('mfAcknowledge').addEventListener('change', controls);
  get('mfConfirm').addEventListener('click', startJob);
  get('mfCancel').addEventListener('click', () => { invalidatePreflight(); get('mfPreflight').focus(); });
  get('mfRetryJob').addEventListener('click', () => { if (!pollController) { job = null; pollJob(); } });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      clearTimeout(pollTimer); pollTimer = null;
      pollVersion++; pollController?.abort(); pollController = null;
    } else { pollJob(); }
  });
  window.addEventListener('pagehide', () => {
    stopped = true; clearTimeout(pollTimer); pollVersion++; pollController?.abort(); pollController = null;
    uploadVersion++; searchVersion++; preflightVersion++;
    uploadController?.abort(); searchController?.abort(); preflightController?.abort();
    uploading = false; searching = false; checking = false;
  });
  window.addEventListener('pageshow', () => { stopped = false; controls(); pollJob(); });
  window.MatchedFrame = Object.freeze({search, isActive:mode, platform:() => get('mfPlatform').value || null, needsRoi:() => selection.getSource() == null});
  syncForm();
  try { jobId = localStorage.getItem(STORAGE); } catch (_) { storageWarning = 'Trình duyệt chặn lưu trữ; job không tự khôi phục sau reload.'; }
  if (jobId) {
    get('mfJobPanel').hidden = false; openResults(); pollJob();
  }
})();
