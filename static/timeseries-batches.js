(function () {
  'use strict';

  const controls = document.getElementById('timeSeriesBatchControls');
  const panel = document.getElementById('timeSeriesBatchPanel');
  if (!controls || !panel) return;

  const API = '/api/timeseries/batches';
  const STORAGE_KEY = 'gee.timeseries.batch_id';
  const active = new Set(['queued', 'running', 'pausing']);
  const labels = {
    queued:'Đang chờ', running:'Đang chạy', pausing:'Đang tạm dừng tại điểm an toàn',
    paused:'Đã tạm dừng', interrupted:'Bị gián đoạn — chờ tiếp tục', completed:'Hoàn tất',
    partial:'Hoàn tất một phần', failed:'Lỗi', pending:'Chờ xử lý', searching:'Đang tìm ảnh',
    downloading:'Đang tải ảnh', packaging:'Đang đóng gói', empty:'Không có ảnh'
  };
  let available = false, current = null, timer = null, revision = 0, submitting = false, commanding = false;

  function element(tag, text, id) {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = String(text);
    if (id) node.id = id;
    return node;
  }
  function validId(value) {
    return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
  }
  function message(value, fallback = 'Không thể cập nhật batch. Vui lòng thử lại.') {
    if (typeof value === 'string' && value) return value;
    if (value && typeof value.message === 'string') return value.message;
    return fallback;
  }
  function storage(method, ...args) {
    try { return window.localStorage[method](...args); } catch (_) { return null; }
  }
  async function request(url, options = {}) {
    let response;
    try { response = await fetch(url, {cache:'no-store', ...options}); }
    catch (cause) {
      const error = new Error(message(cause));
      error.retryable = true;
      throw error;
    }
    let data;
    try { data = await response.json(); } catch (_) { data = null; }
    if (!response.ok || data === null) {
      const error = new Error(data === null ? 'Phản hồi máy chủ không hợp lệ.' : message(data && data.detail));
      error.status = response.status;
      error.retryable = response.status >= 500;
      throw error;
    }
    return data;
  }
  function stopTimer() {
    if (timer !== null) window.clearTimeout(timer);
    timer = null;
  }
  function nextRevision() { stopTimer(); return ++revision; }
  function showError(error) {
    panel.hidden = false;
    errorNode.textContent = message(error);
  }
  function validateBatch(data, expectedId) {
    if (!data || !validId(data.batch_id) || (expectedId && data.batch_id !== expectedId) || !Array.isArray(data.years)) {
      throw new Error('Phản hồi batch không hợp lệ.');
    }
    return data;
  }
  function schedule(token, retryId = null) {
    stopTimer();
    if (token !== revision) return;
    const id = retryId || (current && active.has(current.status) && current.batch_id);
    if (!id) return;
    timer = window.setTimeout(() => {
      timer = null;
      return refresh(id, token);
    }, 2000);
  }
  async function refresh(id, token) {
    if (token !== revision) return;
    let retryId = null;
    try {
      const data = await request(`${API}/${id}`);
      if (token !== revision) return;
      current = validateBatch(data, id);
      render();
    } catch (error) {
      if (token !== revision) return;
      if (error.status === 404) {
        if (storage('getItem', STORAGE_KEY) === id) storage('removeItem', STORAGE_KEY);
        current = null;
        rows.replaceChildren();
        statusNode.textContent = 'Batch không còn khả dụng.';
        renderButtons();
      } else if (error.retryable) {
        // A restored ID needs retries even before its first successful status read.
        retryId = id;
      }
      showError(error);
    } finally {
      if (token === revision) schedule(token, retryId);
    }
  }
  function downloadLink(url, id, year, kind, label) {
    const expected = `${API}/${id}/years/${year}/${kind}`;
    if (!Number.isInteger(year) || year < 1 || year > 9999 || url !== expected) return null;
    const link = element('a', label);
    link.href = expected;
    return link;
  }
  function renderButtons() {
    const status = current && current.status;
    pause.hidden = !['queued', 'running', 'pausing'].includes(status);
    resume.hidden = !['paused', 'interrupted'].includes(status);
    retry.hidden = !['partial', 'failed'].includes(status);
    pause.disabled = commanding || submitting || status === 'pausing';
    resume.disabled = commanding || submitting;
    retry.disabled = commanding || submitting;
  }
  function render() {
    panel.hidden = false;
    errorNode.textContent = '';
    statusNode.textContent = labels[current.status] || String(current.status || '');
    batchIdNode.textContent = `Batch: ${current.batch_id}`;
    const parameters = current.parameters || {};
    snapshotNode.textContent = `${parameters.dataset || ''} · ${parameters.start_date || ''} → ${parameters.end_date || ''}`;
    currentYearNode.textContent = current.current_year ? `Năm hiện tại: ${current.current_year}` : '';
    if (current.error) showError(current.error);
    rows.replaceChildren();
    for (const year of current.years) {
      const row = element('tr');
      row.append(element('td', year.year), element('td', `${year.start_date} → ${year.end_date}`),
        element('td', labels[year.status] || String(year.status || '')),
        element('td', year.total === null || year.total === undefined ? 'chưa biết' : year.total),
        element('td', year.processed ?? 0), element('td', year.succeeded ?? 0), element('td', year.failed ?? 0));
      const results = element('td');
      const zip = downloadLink(year.zip_url, current.batch_id, year.year, 'download', 'Tải ZIP');
      const csv = downloadLink(year.csv_url, current.batch_id, year.year, 'csv', 'Tải CSV');
      if (zip) results.append(zip);
      if (zip && csv) results.append(element('span', ' · '));
      if (csv) results.append(csv);
      row.append(results, element('td', year.error ? message(year.error) : ''));
      rows.append(row);
    }
    renderButtons();
  }
  async function command(action) {
    if (!current || submitting || commanding) return;
    const allowed = {pause:['queued', 'running'], resume:['paused', 'interrupted'], retry:['partial', 'failed']};
    if (!allowed[action].includes(current.status)) return;
    const id = current.batch_id, token = nextRevision();
    commanding = true;
    renderButtons();
    try {
      const data = await request(`${API}/${id}/${action}`, {method:'POST'});
      if (token !== revision) return;
      current = validateBatch(data, id);
      render();
    } catch (error) {
      if (token === revision) showError(error);
    } finally {
      commanding = false;
      if (token === revision) { renderButtons(); schedule(token); }
    }
  }

  const checkbox = element('input', undefined, 'time_series_yearly');
  checkbox.type = 'checkbox';
  checkbox.disabled = true;
  const label = element('label');
  label.append(checkbox, element('span', ' Tải lần lượt theo năm (GeoTIFF)'));
  const help = element('p', 'Khi bật, nhấn “Bắt đầu tải theo năm” để tìm và tải raster tuần tự từng năm. Mỗi năm có ZIP/CSV riêng; chưa tìm thì số ảnh chưa biết. Đóng tab không dừng tải trên máy chủ.');
  help.className = 'timeseries-batch-help';
  const preview = element('p', '', 'timeSeriesBatchPreview');
  preview.className = 'timeseries-batch-help';
  function updatePreview() {
    const first = document.getElementById('start_date'), last = document.getElementById('end_date');
    const start = first && first.value, end = last && last.value;
    if (!start || !end || !/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end) || start > end) {
      preview.textContent = 'Chọn khoảng ngày hợp lệ để xem các khoảng năm.';
      return;
    }
    const firstYear = Number(start.slice(0, 4)), lastYear = Number(end.slice(0, 4));
    if (lastYear - firstYear > 200) { preview.textContent = 'Khoảng ngày quá dài để hiển thị bản xem trước.'; return; }
    const ranges = [];
    for (let year = firstYear; year <= lastYear; year++) {
      ranges.push(`${year}: ${year === firstYear ? start : `${year}-01-01`} → ${year === lastYear ? end : `${year}-12-31`}`);
    }
    preview.textContent = ranges.join(' · ');
  }
  controls.append(label, help, preview);
  for (const id of ['start_date', 'end_date']) {
    const input = document.getElementById(id);
    if (input) input.addEventListener('change', updatePreview);
  }
  checkbox.addEventListener('change', updatePreview);
  updatePreview();

  panel.className = `${panel.className || ''} timeseries-batch-panel`.trim();
  const title = element('h3', 'Tải time series theo năm');
  const statusNode = element('p', '', 'timeSeriesBatchStatus');
  statusNode.setAttribute('role', 'status');
  const errorNode = element('p', '', 'timeSeriesBatchError');
  errorNode.setAttribute('role', 'alert');
  const batchIdNode = element('p', '', 'timeSeriesBatchId');
  const snapshotNode = element('p', '', 'timeSeriesBatchSnapshot');
  const currentYearNode = element('p', '', 'timeSeriesBatchCurrentYear');
  const actions = element('div');
  actions.className = 'timeseries-batch-actions';
  const pause = element('button', 'Tạm dừng', 'timeSeriesBatchPause');
  const resume = element('button', 'Tiếp tục', 'timeSeriesBatchResume');
  const retry = element('button', 'Thử lại phần lỗi', 'timeSeriesBatchRetry');
  for (const [button, action] of [[pause, 'pause'], [resume, 'resume'], [retry, 'retry']]) {
    button.type = 'button';
    button.hidden = true;
    button.addEventListener('click', () => command(action));
    actions.append(button);
  }
  const table = element('table');
  table.className = 'timeseries-batch-table';
  const caption = element('caption', 'Tiến độ và kết quả từng năm');
  const head = element('thead'), header = element('tr');
  for (const text of ['Năm', 'Khoảng ngày', 'Trạng thái', 'Tổng ảnh', 'Đã xử lý', 'Thành công', 'Lỗi', 'Kết quả', 'Chi tiết lỗi']) {
    const cell = element('th', text); cell.setAttribute('scope', 'col'); header.append(cell);
  }
  head.append(header);
  const rows = element('tbody', undefined, 'timeSeriesBatchYears');
  table.append(caption, head, rows);
  const tableWrap = element('div');
  tableWrap.className = 'timeseries-batch-table-wrap';
  tableWrap.append(table);
  panel.append(title, statusNode, batchIdNode, snapshotNode, currentYearNode, errorNode, actions, tableWrap,
    element('p', 'Đóng tab không hủy batch. Tạm dừng có hiệu lực tại điểm an toàn; mở lại trang để xem hoặc tiếp tục batch đã lưu.'));
  panel.hidden = true;

  window.TimeSeriesBatches = {
    enabled() { return available && checkbox.checked; },
    setAvailable(value) {
      available = Boolean(value);
      controls.hidden = !available;
      checkbox.disabled = !available;
    },
    async start(body) {
      if (!this.enabled() || submitting || commanding) return null;
      const token = nextRevision();
      submitting = true;
      renderButtons();
      try {
        const data = await request(API, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
        if (token !== revision) return null;
        current = validateBatch(data);
        storage('setItem', STORAGE_KEY, current.batch_id);
        render();
        if (window.SatelliteWorkspace && typeof window.SatelliteWorkspace.showView === 'function') window.SatelliteWorkspace.showView('jobs');
        schedule(token);
        return current;
      } catch (error) {
        if (token === revision) { showError(error); schedule(token); }
        return null;
      } finally {
        submitting = false;
        renderButtons();
      }
    }
  };
  window.TimeSeriesBatches.setAvailable(false);
  const saved = storage('getItem', STORAGE_KEY);
  if (validId(saved)) refresh(saved, revision);
})();
