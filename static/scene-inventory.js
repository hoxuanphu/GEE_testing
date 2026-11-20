(() => {
  'use strict';
  const get = id => document.getElementById(id);
  const supported = new Set(['sentinel2', 'landsat', 'nicfi', 'dynamic_world', 's1gee', 's5p', 's1', 's1slc', 's2', 's2aws', 's3']);
  const sourceLabels = {gee:'GEE', cdse:'CDSE', aws:'AWS Earth Search'};
  let generation = 0, controller = null;

  function invalidate() {
    generation++;
    controller?.abort();
    controller = null;
    get('btnCountImages').disabled = false;
    get('imageInventoryControls').setAttribute('aria-busy', 'false');
    get('imageInventoryControls').hidden = !supported.has(get('dataset').value);
    get('imageCountStatus').textContent = '';
    get('imageInventoryCsv').hidden = true;
    get('imageInventoryCsv').removeAttribute('href');
  }

  function numeric(id, label) {
    const raw = get(id).value.trim();
    if (!raw || !Number.isFinite(Number(raw))) throw new Error(`Nhập ${label} hợp lệ.`);
    return Number(raw);
  }

  function requestBody() {
    const dataset = get('dataset').value;
    if (!supported.has(dataset)) throw new Error('Nguồn này không có danh sách ảnh theo ngày.');
    const body = {dataset, start_date:get('start_date').value, end_date:get('end_date').value};
    if (!body.start_date || !body.end_date || body.start_date > body.end_date)
      throw new Error('Chọn khoảng ngày hợp lệ: ngày bắt đầu không sau ngày kết thúc.');
    const mode = get('selection_mode').value;
    const reference = mode === 'reference' || mode === 'rectangle' && window.SelectionState?.getSource() === 'reference';
    body.selection_mode = reference ? 'reference' : mode;
    if (mode === 'vector') {
      if (!window.VectorLayers) throw new Error('Bộ quản lý lớp vector chưa sẵn sàng.');
      Object.assign(body, window.VectorLayers.request());
    } else if (reference) {
      body.reference_id = window.SelectionState?.getReferenceDraft()?.reference_id;
      if (!body.reference_id) throw new Error('Upload hoặc chọn lại khung GeoTIFF mẫu trước khi đếm.');
    } else if (mode === 'rectangle') {
      const rectangle = rectangleEditor.get();
      for (const key of ['center_lon', 'center_lat', 'width_m', 'height_m', 'rotation_deg']) body[key] = rectangle[key];
    } else if (mode === 'point') {
      body.lon = numeric('lon', 'kinh độ'); body.lat = numeric('lat', 'vĩ độ');
      body.radius_m = numeric('radius_m', 'bán kính');
      body.rotation_deg = numeric('rotation_deg', 'góc xoay');
    } else if (mode === 'province' || mode === 'commune') {
      const key = mode === 'province' ? 'province_id' : 'commune_id';
      body[key] = get(key).value;
      if (!body[key]) throw new Error('Chọn địa giới hành chính trước khi đếm.');
    } else throw new Error('Chọn vùng ROI trước khi đếm.');
    if (dataset === 's2' || dataset === 's2aws') body.source = dataset === 's2aws' ? 'aws' : get('s2_source').value;
    if (dataset === 'sentinel2' || dataset === 's1gee') body.platform = get('mfPlatform').value || null;
    if (dataset === 's1gee') {
      body.polarization = reference ? get('mfPolarization').value : 'vvvh';
      if (reference && get('mfOrbitPass').value) body.orbit_pass = get('mfOrbitPass').value;
      if (reference && get('mfRelativeOrbit').value.trim()) body.relative_orbit = numeric('mfRelativeOrbit', 'quỹ đạo tương đối');
    }
    if (dataset === 's5p') body.product = get('s5p_product').value;
    if (dataset === 's3') body.product_type = get('s3_product_type').value;
    return body;
  }

  async function count() {
    invalidate();
    const version = generation;
    try {
      const body = requestBody();
      controller = new AbortController();
      get('btnCountImages').disabled = true;
      get('imageInventoryControls').setAttribute('aria-busy', 'true');
      get('imageCountStatus').textContent = 'Đang đếm và lấy danh sách ảnh, gồm cả ảnh có mây…';
      const response = await fetch('/api/scene-inventory', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body), signal:controller.signal});
      const data = await response.json();
      if (version !== generation) return;
      if (!response.ok) {
        const detail = typeof data.detail === 'string' ? data.detail : data.detail?.message;
        throw new Error(detail || 'Không đếm được ảnh. Kiểm tra ROI, ngày và bộ lọc rồi thử lại.');
      }
      if (!Number.isInteger(data.total) || data.total < 0 ||
          !/^\/api\/scene-inventory\/[a-z0-9-]+\/csv$/i.test(data.csv_url || ''))
        throw new Error('Kết quả danh sách không hợp lệ; hãy đếm lại.');
      const unit = data.parameters?.scene_kind === 'period_composite' ? 'composite theo kỳ' : 'ảnh';
      const source = sourceLabels[data.parameters?.source] || body.dataset;
      get('imageCountStatus').textContent = `${data.total.toLocaleString('vi-VN')} ${unit} · ${source} · ${body.start_date} → ${body.end_date}. Tính cả ảnh có mây và thiếu thông tin mây.`;
      const link = get('imageInventoryCsv');
      link.href = data.csv_url;
      link.textContent = `Tải danh sách CSV (${data.total.toLocaleString('vi-VN')} ${unit})`;
      link.hidden = false;
    } catch (error) {
      if (version === generation && error.name !== 'AbortError') get('imageCountStatus').textContent = 'Lỗi: ' + error.message;
    } finally {
      if (version === generation) {
        controller = null;
        get('btnCountImages').disabled = false;
        get('imageInventoryControls').setAttribute('aria-busy', 'false');
      }
    }
  }

  const fields = ['dataset', 'selection_mode', 'province_id', 'commune_id', 'start_date', 'end_date',
    'lon', 'lat', 'radius_m', 'rotation_deg', 'rotation_slider', 's2_source', 's3_product_type', 's5p_product',
    'mfPlatform', 'mfPolarization', 'mfOrbitPass', 'mfRelativeOrbit', 'mfFile'];
  for (const id of fields) for (const event of ['input', 'change']) get(id).addEventListener(event, invalidate);
  get('btnCountImages').addEventListener('click', count);
  window.addEventListener('pagehide', invalidate);
  window.SceneInventory = Object.freeze({count, invalidate});
  invalidate();
})();
