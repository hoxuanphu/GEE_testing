(() => {
  'use strict';
  const panel = document.getElementById('downloadJobPanel');
  const download = document.getElementById('dl');
  if (!panel || !download) return;
  const KEY = 'gee.download.job_id';
  const ID = /^[a-f0-9]{32}$/;
  const active = new Set(['queued', 'running']);
  const stages = {queued:'Đang chờ', preparing:'Đang chuẩn bị', downloading:'Đang tải dữ liệu',
    assembling:'Đang xử lý ảnh', validating:'Đang kiểm tra ảnh', publishing:'Đang lưu kết quả',
    completed:'Hoàn tất', paused:'Đã tạm dừng', interrupted:'Bị gián đoạn', failed:'Tải chưa thành công',
    cancelled:'Đã hủy', expired:'Kết quả đã hết hạn'};
  let selection = null, current = null, timer = null, submitting = false, actionPending = false;
  const error = document.createElement('p');
  error.setAttribute('role', 'alert');
  const storage = (method, ...args) => { try { return window.localStorage[method](...args); } catch { return null; } };
  const node = (tag, text) => { const element = document.createElement(tag); if (text) element.textContent = text; return element; };
  const clearTimer = () => { if (timer !== null) window.clearTimeout(timer); timer = null; };
  function schedule() {
    clearTimer();
    if (current && active.has(current.state)) timer = window.setTimeout(poll, document.hidden ? 5000 : 1000);
  }
  async function request(url, options = {}) {
    const response = await window.fetch(url, options);
    const data = await response.json();
    if (!response.ok) { const failure = new Error(typeof data.detail === 'string' ? data.detail : 'Không thể xử lý lượt tải.'); failure.status = response.status; throw failure; }
    return data;
  }
  function render(job) {
    if (!job || !ID.test(job.id)) throw new Error('Trạng thái lượt tải không hợp lệ.');
    current = Object.freeze({...job});
    storage('setItem', KEY, job.id);
    panel.hidden = false;
    panel.replaceChildren(node('h3', 'Tải ảnh Sentinel-2'), node('p', stages[job.stage] || stages[job.state] || 'Đang theo dõi'));
    const total = Number(job.tiles_total) || 0, verified = Number(job.tiles_verified) || 0;
    panel.append(node('p', `Dữ liệu đầu vào: ${verified}/${total} phần · ${((Number(job.verified_bytes) || 0) / 1048576).toFixed(1)} MiB đã kiểm tra`));
    const progress = node('progress');
    progress.setAttribute('aria-label', 'Dữ liệu đầu vào');
    progress.max = Math.max(total, 1);
    if (job.stage === 'downloading') progress.value = verified;
    else if (job.state === 'completed') progress.value = progress.max;
    else progress.removeAttribute('value');
    panel.append(progress);
    const actions = node('div'); actions.className = 'download-job-actions';
    for (const [action, text, visible] of [
      ['pause', 'Tạm dừng', active.has(job.state)],
      ['resume', 'Tiếp tục', ['paused', 'interrupted', 'failed'].includes(job.state)],
      ['cancel', 'Hủy lượt tải', ['queued', 'running', 'paused', 'interrupted', 'failed'].includes(job.state)]]) {
      if (!visible) continue;
      const button = node('button', text); button.type = 'button'; button.disabled = actionPending;
      button.addEventListener('click', () => control(action)); actions.append(button);
    }
    const file = `/api/download-jobs/${job.id}/file`;
    if (job.state === 'completed' && job.file_url === file) {
      const link = node('a', 'Tải GeoTIFF đã hoàn tất'); link.href = file; actions.append(link);
    }
    if (job.expires_at) panel.append(node('p', `Lưu đến: ${new Date(job.expires_at * 1000).toLocaleString('vi-VN')}`));
    if (job.error_code) error.textContent = 'Lượt tải chưa hoàn tất. Có thể tiếp tục với các phần dữ liệu đã kiểm tra.';
    panel.append(actions, error);
    schedule();
  }
  async function poll() {
    clearTimer();
    if (!current) return;
    const id = current.id;
    try {
      const job = await request(`/api/download-jobs/${id}`);
      if (current?.id === id) { error.textContent = ''; render(job); }
    } catch (failure) {
      if (current?.id !== id) return;
      error.textContent = failure.message;
      if ([404, 410].includes(failure.status)) { storage('removeItem', KEY); current = null; clearTimer(); }
      else schedule();
    }
  }
  async function control(action) {
    if (!current || actionPending) return;
    actionPending = true; clearTimer();
    try { error.textContent = ''; const job = await request(`/api/download-jobs/${current.id}/${action}`, {method:'POST'}); actionPending = false; render(job); }
    catch (failure) { error.textContent = failure.message; actionPending = false; schedule(); }
  }
  async function start() {
    if (!selection || submitting) return;
    const token = selection;
    submitting = true; error.textContent = ''; panel.hidden = false;
    panel.replaceChildren(node('p', 'Đang tạo lượt tải…'), error);
    window.SatelliteWorkspace?.showView('jobs');
    try {
      const job = await request('/api/download-jobs', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({selection_token:token})});
      render(job);
    } catch (failure) { error.textContent = failure.message; }
    finally { submitting = false; }
  }
  function bindSelection(result) {
    selection = result?.download_job_available && typeof result.selection_token === 'string' ? result.selection_token : null;
  }
  download.addEventListener('click', event => { if (selection) { event.preventDefault(); start(); } });
  window.DownloadJobs = Object.freeze({start, bindSelection});
  const saved = storage('getItem', KEY);
  if (saved && ID.test(saved)) { current = {id:saved, state:'running'}; panel.hidden = false; panel.append(error); poll(); }
})();
