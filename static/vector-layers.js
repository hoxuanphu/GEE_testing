(() => {
  'use strict';
  const panel = document.getElementById('vectorLayersPanel');
  if (!panel) return;
  const mode = document.getElementById('selection_mode');
  const PAGE_SIZE = 50;
  const colors = ['#2563eb', '#be185d', '#047857', '#b45309', '#7c3aed', '#0e7490'];
  const imports = [];
  const copy = value => value == null ? null : JSON.parse(JSON.stringify(value));
  const polygon = feature => ['Polygon', 'MultiPolygon'].includes(feature.geometry.type);
  const family = feature => polygon(feature) ? 'polygon' : feature.geometry.type.includes('Point') ? 'point' : 'line';
  let serial = 0, epoch = 0, importBatch = 0, applying = false, applyAbort = null, roiSummary = '';
  let activeRoi = null, outline = null, activeImports = new Set();

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text != null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  }
  function action(tag, name, text) {
    const element = node(tag, text);
    element.setAttribute('data-action', name);
    if (tag === 'button') element.type = 'button';
    return element;
  }
  function identified(element, id) { element.id = id; return element; }
  function labelFor(input, text) {
    const label = node('label'); label.htmlFor = input.id;
    label.append(input, node('span', text));
    return label;
  }
  function listen(item, element, type, handler, row = false) {
    element.addEventListener(type, handler);
    (row ? item.rowListeners : item.listeners).push(() => element.removeEventListener(type, handler));
  }
  function releaseHandlers(handlers) { handlers.splice(0).forEach(dispose => dispose()); }
  function checkbox(id, name, text, checked, handler, item, row = false) {
    const input = identified(action('input', name), id);
    input.type = 'checkbox'; input.checked = checked;
    listen(item, input, 'change', handler, row);
    return {input, label:labelFor(input, text)};
  }
  function status(id) {
    const element = identified(node('p', '', 'vector-status'), id);
    element.setAttribute('role', 'status'); element.setAttribute('aria-live', 'polite');
    return element;
  }
  const heading = node('h3', 'Lớp vector');
  const input = identified(node('input'), 'vectorFiles');
  input.type = 'file'; input.multiple = true; input.accept = '.geojson,.json,.kml,.kmz,.gpx,.zip';
  const help = identified(node('p', 'GeoJSON, KML/KMZ, GPX hoặc Shapefile ZIP. Tối đa 20 MiB mỗi file.'), 'vectorImportHelp');
  input.setAttribute('aria-describedby', help.id);
  const toolbar = node('div', null, 'vector-toolbar');
  const applyButton = identified(node('button', 'Dùng polygon đã chọn làm ROI'), 'vectorApplyRoi'); applyButton.type = 'button';
  const clearButton = identified(node('button', 'Bỏ ROI vector'), 'vectorClearRoi'); clearButton.type = 'button';
  toolbar.append(applyButton, clearButton);
  const importStatus = status('vectorImportStatus');
  const selectionStatus = status('vectorSelectionStatus');
  const roiStatus = status('vectorRoiStatus');
  const list = identified(node('div', null, 'vector-import-list'), 'vectorImportList');
  panel.append(heading, labelFor(input, 'Nhập một hoặc nhiều file vector'), help, importStatus, list, selectionStatus, toolbar, roiStatus);

  function invalidate() {
    if (typeof invalidateSearchResults === 'function') invalidateSearchResults();
    else if (typeof window.invalidateSearchResults === 'function') window.invalidateSearchResults();
  }
  function cancelApply() {
    epoch++;
    if (applyAbort) applyAbort.abort();
    applyAbort = null; applying = false;
  }
  function updateOutline() {
    if (!outline) return;
    if (mode && mode.value === 'vector') {
      if (!map.hasLayer(outline)) outline.addTo(map);
    } else map.removeLayer(outline);
  }
  function describeRoi() {
    roiStatus.textContent = activeRoi
      ? `${mode && mode.value === 'vector' ? 'ROI đang dùng' : 'ROI vector đã lưu, chưa kích hoạt'}: ${activeRoi.name || activeRoi.roi_id}. ${roiSummary}`
      : 'Chưa có ROI vector được áp dụng.';
  }
  function clearRoi() {
    cancelApply();
    const changed = activeRoi !== null;
    if (outline) map.removeLayer(outline);
    activeRoi = null; outline = null; activeImports = new Set();
    roiSummary = ''; describeRoi();
    updateSelection();
    if (changed) invalidate();
  }
  function selected() {
    return imports.filter(item => item.alive && item.data && !item.deleting).flatMap(item =>
      item.features.filter(feature => feature.selected).map(feature => ({item, feature})));
  }
  function updateSelection() {
    const features = selected();
    const count = features.filter(({feature}) => polygon(feature.data)).length;
    selectionStatus.textContent = `Đã chọn ${features.length} đối tượng; ${count} polygon dùng được làm ROI. ${features.length - count} point/line không tham gia ROI.`;
    applyButton.disabled = applying || count === 0;
    applyButton.textContent = applying ? 'Đang tạo ROI…' : 'Dùng polygon đã chọn làm ROI';
    clearButton.disabled = !activeRoi && !applying;
  }
  async function responseData(response) {
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('Máy chủ trả dữ liệu không hợp lệ.'); }
    if (!response.ok) {
      const detail = typeof data.detail === 'string' ? data.detail
        : typeof data.detail?.message === 'string' ? data.detail.message
        : typeof data.message === 'string' ? data.message : 'Không thể hoàn tất thao tác vector.';
      throw new Error(detail);
    }
    return data;
  }
  function message(error) {
    return error && typeof error.message === 'string' ? error.message : 'Không thể hoàn tất thao tác vector.';
  }
  function style(item, feature) {
    return {color:feature.selected ? '#f59e0b' : item.color, weight:feature.selected ? 4 : 2,
      fillColor:item.color, fillOpacity:feature.selected ? 0.35 : 0.12, opacity:1, radius:feature.selected ? 7 : 5};
  }
  function showProperties(item, feature) {
    item.details.hidden = false;
    item.details.textContent = `${feature.data.name || feature.data.id} · ${feature.data.geometry.type}\nLớp: ${feature.data.layer || ''}\n${JSON.stringify(feature.data.properties || {}, null, 2)}`;
  }
  function setSelected(item, feature, value, batch = false) {
    if (!item.alive) return;
    feature.selected = value;
    feature.layer.setStyle(style(item, feature));
    if (feature.selectControl) feature.selectControl.checked = value;
    if (feature.row) feature.row.setAttribute('data-selected', String(value));
    if (value && !batch) showProperties(item, feature);
    if (!batch) updateSelection();
  }
  function updateVisibility(item, feature) {
    const visible = item.visible && feature.visible && item.alive;
    if (visible && !map.hasLayer(feature.layer)) feature.layer.addTo(map);
    if (!visible) map.removeLayer(feature.layer);
  }
  function drawFeature(item, data, index) {
    const feature = {data, index, visible:true, selected:false};
    const options = {
      bubblingMouseEvents:false,
      style:() => style(item, feature),
      pointToLayer:(_, latlng) => L.circleMarker(latlng, {...style(item, feature), bubblingMouseEvents:false}),
      onEachFeature:(_, layer) => layer.on('click', event => {
        // Leaflet's bubblingMouseEvents blocks the map's existing point-selection listener.
        if (event.originalEvent) L.DomEvent.stopPropagation(event.originalEvent);
        if (item.alive && !item.deleting) setSelected(item, feature, !feature.selected);
      })
    };
    feature.layer = L.geoJSON(data, options).addTo(map);
    return feature;
  }
  function renderRows(item) {
    releaseHandlers(item.rowListeners);
    for (const feature of item.features) { feature.selectControl = null; feature.row = null; }
    const name = item.nameFilter.value.trim().toLocaleLowerCase();
    const type = item.typeFilter.value;
    const filtered = item.features.filter(feature => (!name || String(feature.data.name || feature.data.id).toLocaleLowerCase().includes(name)) &&
      (!type || family(feature.data) === type));
    const lastPage = Math.max(0, Math.ceil(filtered.length / PAGE_SIZE) - 1);
    item.page = Math.min(item.page, lastPage);
    const rows = filtered.slice(item.page * PAGE_SIZE, (item.page + 1) * PAGE_SIZE).map(feature => {
      const row = node('div', null, 'vector-feature-row');
      row.setAttribute('data-selected', String(feature.selected));
      row.setAttribute('data-feature-id', feature.data.id);
      const prefix = `vector-${item.key}-${feature.index}`;
      const visible = checkbox(`${prefix}-visible`, 'feature-visible', 'Hiện', feature.visible, event => {
        feature.visible = event.target.checked; updateVisibility(item, feature);
      }, item, true);
      const selection = checkbox(`${prefix}-selected`, 'feature-select', 'Chọn', feature.selected, event => setSelected(item, feature, event.target.checked), item, true);
      visible.input.setAttribute('aria-label', `Hiện ${feature.data.name || feature.data.id}`);
      selection.input.setAttribute('aria-label', `Chọn ${feature.data.name || feature.data.id}`);
      feature.selectControl = selection.input; feature.row = row;
      const details = action('button', 'properties', feature.data.name || feature.data.id);
      listen(item, details, 'click', () => showProperties(item, feature), true);
      row.append(visible.label, selection.label, details, node('span', feature.data.geometry.type, 'vector-feature-type'));
      return row;
    });
    item.rows.replaceChildren(...rows);
    if (!rows.length) item.rows.append(node('p', 'Không có đối tượng phù hợp.'));
    item.pageStatus.textContent = `${filtered.length} đối tượng · Trang ${item.page + 1}/${lastPage + 1}`;
    item.previous.disabled = item.page === 0; item.next.disabled = item.page === lastPage;
  }
  function buildFile(item) {
    item.controls = node('div', null, 'vector-toolbar');
    const visible = checkbox(`vector-${item.key}-visible`, 'file-visible', 'Hiện toàn lớp', true, event => {
      item.visible = event.target.checked; item.features.forEach(feature => updateVisibility(item, feature));
    }, item);
    const all = action('button', 'select-all', 'Chọn tất cả trong file');
    const none = action('button', 'select-none', 'Bỏ chọn trong file');
    listen(item, all, 'click', () => { item.features.forEach(feature => setSelected(item, feature, true, true)); updateSelection(); });
    listen(item, none, 'click', () => { item.features.forEach(feature => setSelected(item, feature, false, true)); updateSelection(); });
    const fit = action('button', 'fit', 'Xem toàn lớp');
    listen(item, fit, 'click', () => {
      const bounds = L.geoJSON(item.data.feature_collection).getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, {padding:[20,20]});
    });
    item.controls.append(visible.label, all, none, fit);
    const filters = node('div', null, 'vector-toolbar');
    item.nameFilter = identified(action('input', 'filter-name'), `vector-${item.key}-name`); item.nameFilter.type = 'search';
    item.typeFilter = identified(action('select', 'filter-type'), `vector-${item.key}-type`);
    for (const [value, label] of [['','Mọi loại'], ['polygon','Polygon'], ['point','Point'], ['line','Line']]) {
      const option = node('option', label); option.value = value; item.typeFilter.append(option);
    }
    const filter = () => { item.page = 0; renderRows(item); };
    listen(item, item.nameFilter, 'input', filter); listen(item, item.typeFilter, 'change', filter);
    filters.append(labelFor(item.nameFilter, 'Lọc tên'), labelFor(item.typeFilter, 'Loại hình học'));
    item.rows = node('div', null, 'vector-feature-list');
    const pager = node('div', null, 'vector-pagination');
    item.previous = action('button', 'previous-page', 'Trang trước');
    item.next = action('button', 'next-page', 'Trang sau');
    item.pageStatus = node('span');
    listen(item, item.previous, 'click', () => { item.page--; renderRows(item); });
    listen(item, item.next, 'click', () => { item.page++; renderRows(item); });
    pager.append(item.previous, item.pageStatus, item.next);
    item.details = node('pre', '', 'vector-feature-details'); item.details.hidden = true;
    item.details.setAttribute('aria-label', 'Thuộc tính đối tượng'); item.details.tabIndex = 0;
    item.card.append(item.controls, filters, item.rows, pager, item.details);
    renderRows(item);
  }
  function releaseLayer(layer) {
    if (layer.eachLayer) layer.eachLayer(releaseLayer);
    layer.off();
    if (layer.clearLayers) layer.clearLayers();
  }
  function releaseFile(item) {
    item.alive = false;
    const index = imports.indexOf(item);
    if (index !== -1) imports.splice(index, 1);
    if (item.abort) item.abort.abort();
    releaseHandlers(item.rowListeners); releaseHandlers(item.listeners);
    for (const feature of item.features) {
      map.removeLayer(feature.layer); releaseLayer(feature.layer);
      feature.layer = null; feature.data = null; feature.row = null; feature.selectControl = null;
    }
    item.features = []; item.data = null; item.abort = null;
    item.card.remove(); item.card.replaceChildren();
    for (const key of ['card', 'controls', 'rows', 'details', 'nameFilter', 'typeFilter', 'previous', 'next', 'pageStatus', 'remove', 'status']) item[key] = null;
  }
  async function removeFile(item) {
    if (!item.alive || item.deleting) return;
    cancelApply();
    describeRoi();
    if (!item.data) {
      releaseFile(item); updateSelection(); return;
    }
    item.deleting = true; item.remove.disabled = true; updateSelection();
    item.status.textContent = 'Đang xóa lớp…';
    const importId = item.data.import_id;
    try {
      const response = await fetch(`/api/vectors/imports/${encodeURIComponent(importId)}`, {method:'DELETE'});
      if (!response.ok) await responseData(response);
      releaseFile(item);
      if (activeImports.has(importId)) clearRoi();
    } catch (error) {
      item.status.textContent = `Không xóa được lớp: ${message(error)}`;
    } finally {
      item.deleting = false;
      if (item.alive) item.remove.disabled = false;
      updateSelection();
    }
  }
  async function importFile(file) {
    const item = {key:++serial, color:colors[(serial - 1) % colors.length], alive:true, visible:true, page:0, features:[], listeners:[], rowListeners:[], abort:new AbortController()};
    imports.push(item);
    item.card = node('section', null, 'vector-file');
    const header = node('div', null, 'vector-file-header');
    const title = identified(node('h4', file.name), `vector-${item.key}-title`);
    item.card.setAttribute('aria-labelledby', title.id);
    item.remove = action('button', 'remove', 'Xóa lớp'); item.remove.setAttribute('aria-label', `Xóa lớp ${file.name}`);
    listen(item, item.remove, 'click', () => removeFile(item));
    header.append(title, item.remove);
    item.status = node('p', 'Đang nhập…', 'vector-status'); item.status.setAttribute('role', 'status');
    item.card.append(header, item.status); list.append(item.card);
    try {
      const body = new FormData(); body.append('file', file);
      const data = await responseData(await fetch('/api/vectors/import', {method:'POST', body, signal:item.abort.signal}));
      if (!item.alive) {
        // A canceled upload may already have reached the server; remove its unused import.
        if (data.import_id) fetch(`/api/vectors/imports/${encodeURIComponent(data.import_id)}`, {method:'DELETE'}).catch(() => {});
        return;
      }
      if (!data.import_id || !data.feature_collection || !Array.isArray(data.feature_collection.features)) throw new Error('Dữ liệu lớp vector không hợp lệ.');
      item.data = data;
      item.features = data.feature_collection.features.map((feature, index) => drawFeature(item, feature, index));
      item.status.textContent = `Đã nhập ${item.features.length} đối tượng.${data.warnings && data.warnings.length ? ' ' + data.warnings.join(' ') : ''}`;
      buildFile(item);
    } catch (error) {
      if (item.alive) item.status.textContent = `Không nhập được file: ${message(error)}`;
    } finally { updateSelection(); }
  }
  async function apply() {
    const draft = selected();
    const polygons = draft.filter(({feature}) => polygon(feature.data));
    if (applying || !polygons.length) return;
    const selections = [];
    for (const {item, feature} of polygons) {
      let selection = selections.find(value => value.import_id === item.data.import_id);
      if (!selection) { selection = {import_id:item.data.import_id, feature_ids:[]}; selections.push(selection); }
      selection.feature_ids.push(feature.data.id);
    }
    const dependencies = new Set(selections.map(value => value.import_id));
    const token = ++epoch; applying = true; applyAbort = new AbortController(); updateSelection();
    roiStatus.textContent = 'Đang tạo ROI từ polygon đã chọn…';
    try {
      const result = await responseData(await fetch('/api/vectors/rois', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({selections}), signal:applyAbort.signal}));
      if (token !== epoch || polygons.some(({item}) => !item.alive || item.deleting)) return;
      if (!result.roi_id || !result.geometry || !['Polygon','MultiPolygon'].includes(result.geometry.type)) throw new Error('Máy chủ trả ROI không hợp lệ.');
      if (outline) map.removeLayer(outline);
      activeRoi = copy(result); activeImports = dependencies;
      outline = L.geoJSON(result.geometry, {interactive:false, bubblingMouseEvents:false,
        style:{color:'#f97316', weight:4, dashArray:'8 4', fill:false}});
      applying = false; applyAbort = null;
      const excluded = draft.length - polygons.length + (Number(result.excluded_count) || 0);
      roiSummary = `${polygons.length} polygon; ${excluded} point/line không tham gia.`;
      if (mode) { mode.value = 'vector'; mode.dispatchEvent(new Event('change')); }
      updateOutline(); describeRoi(); invalidate();
    } catch (error) {
      if (token === epoch) roiStatus.textContent = `Không áp dụng được ROI: ${message(error)}${activeRoi ? ' ROI trước vẫn được giữ.' : ''}`;
    } finally {
      if (token === epoch) { applying = false; applyAbort = null; }
      updateSelection();
    }
  }
  input.addEventListener('change', () => {
    const batch = ++importBatch;
    const files = Array.from(input.files || []); input.value = '';
    importStatus.textContent = files.length ? `Đang xử lý ${files.length} file riêng biệt.` : '';
    return Promise.all(files.map(importFile)).then(() => {
      if (batch === importBatch) importStatus.textContent = 'Đã xử lý đợt nhập. Xem trạng thái từng file bên dưới.';
    });
  });
  applyButton.addEventListener('click', apply);
  clearButton.addEventListener('click', clearRoi);
  if (mode) mode.addEventListener('change', () => { cancelApply(); updateOutline(); describeRoi(); updateSelection(); });
  window.VectorLayers = Object.freeze({
    request() {
      if (!activeRoi) throw new Error('Chọn polygon và áp dụng ROI vector trước khi tiếp tục.');
      return {selection_mode:'vector', vector_roi_id:activeRoi.roi_id};
    },
    getRoi() { return copy(activeRoi); },
    clearRoi
  });
  roiStatus.textContent = 'Chưa có ROI vector được áp dụng.';
  updateSelection();
})();
