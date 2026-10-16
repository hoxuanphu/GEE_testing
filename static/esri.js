(() => {
  const el = id => document.getElementById(id);
  let jobId = localStorage.getItem('esriJobId');
  let timer;
  let actionPending = false;
  const active = status => ['queued', 'running'].includes(status);
  async function request(url, body) {
    const response = await fetch(url, body === undefined ? {} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) {
      const error = new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail));
      error.status = response.status;
      throw error;
    }
    return data;
  }
  function render(job) {
    const running = active(job.status);
    el('esriStart').disabled = running || actionPending;
    el('esriCancel').disabled = !running || actionPending;
    el('esriResume').disabled = !['partial', 'failed', 'cancelled', 'interrupted'].includes(job.status) || actionPending;
    const progress = job.progress || job;
    const angle = Number(job.request?.rotation_deg) || 0;
    const gridNote = job.request?.bbox
      ? ' — Job bbox cũ/khung nhập riêng, lưới Bắc–Nam. Đổi góc không thay đổi file job này; tạo lượt tải mới.'
      : ` — Góc lưới của job: ${angle}°; giữ nguyên theo thời điểm tạo job.`;
    el('esriStatus').textContent = `${job.status}: ${progress.processed || 0}/${progress.total || 0} tile; thành công ${progress.succeeded || 0}, lỗi ${progress.failed || 0}, cache ${progress.cached || 0}` + (job.error ? ` — ${job.error}` : '') + (job.status === 'partial' ? ' — Ảnh chưa đầy đủ; có thể thử lại các tile thiếu.' : '') + gridNote;
    el('esriDownload').hidden = !['completed', 'partial'].includes(job.status);
    el('esriDownload').href = `/api/esri/jobs/${encodeURIComponent(job.job_id)}/download`;
    clearTimeout(timer);
    timer = undefined;
    if (running) timer = setTimeout(poll, 1500);
  }
  async function poll() {
    if (!jobId) return;
    try { render(await request(`/api/esri/jobs/${encodeURIComponent(jobId)}`)); }
    catch (error) {
      el('esriStatus').textContent = `Không đọc được tiến độ: ${error.message}`;
      if (error.status === 404) {
        jobId = null;
        localStorage.removeItem('esriJobId');
        el('esriStart').disabled = false;
        el('esriCancel').disabled = el('esriResume').disabled = true;
        el('esriDownload').hidden = true;
      } else timer = setTimeout(poll, 5000);
    }
  }
  el('esriMode').addEventListener('change', async () => {
    const commune = el('esriMode').value === 'commune';
    const point = el('esriMode').value === 'point';
    el('esriBboxFields').hidden = commune || point;
    el('esriPointInfo').hidden = !point;
    if (point) {
      el('selection_mode').value = 'point';
      el('selection_mode').dispatchEvent(new Event('change'));
    }
    if (commune) {
      el('selection_mode').value = 'commune';
      el('selection_mode').dispatchEvent(new Event('change'));
    }
  });
  el('esriMapBounds').addEventListener('click', () => {
    const bounds = map.getBounds();
    el('esriWest').value = Math.max(-180, bounds.getWest());
    el('esriEast').value = Math.min(180, bounds.getEast());
    el('esriSouth').value = Math.max(-85.05112878, bounds.getSouth());
    el('esriNorth').value = Math.min(85.05112878, bounds.getNorth());
  });
  el('esriStart').addEventListener('click', async () => {
    if (actionPending) return;
    actionPending = true;
    el('esriStart').disabled = true;
    try {
      const body = {zoom: Number(el('esriZoom').value), workers: Number(el('esriWorkers').value), max_tiles: Number(el('esriMaxTiles').value)};
      if (el('esriMode').value === 'commune') {
        body.commune_id = el('commune_id').value;
        if (!body.commune_id) throw new Error('Chọn xã/phường trước khi tải.');
      } else if (el('esriMode').value === 'point') {
        const lon = Number(el('lon').value), lat = Number(el('lat').value);
        const radius = Number(el('radius_m').value);
        body.lon = lon; body.lat = lat; body.radius_m = radius;
        body.rotation_deg = rotationValue();
        if (![lon, lat, radius, body.rotation_deg].every(Number.isFinite) || body.rotation_deg < 0 || body.rotation_deg > 360) {
          throw new Error('Nhập tọa độ, bán kính và góc xoay hợp lệ ở phần vị trí.');
        }
        updatePositionMap();
      } else {
        const ids = ['esriWest', 'esriSouth', 'esriEast', 'esriNorth'];
        if (ids.some(id => el(id).value.trim() === '')) throw new Error('Nhập đủ 4 tọa độ bbox.');
        body.bbox = ids.map(id => Number(el(id).value));
        if (!body.bbox.every(Number.isFinite)) throw new Error('Tọa độ không hợp lệ.');
      }
      const job = await request('/api/esri/jobs', body);
      jobId = job.job_id;
      localStorage.setItem('esriJobId', jobId);
      actionPending = false;
      render(job);
    } catch (error) { el('esriStatus').textContent = error.message; }
    finally { actionPending = false; if (!timer) el('esriStart').disabled = false; }
  });
  for (const action of ['cancel', 'resume']) {
    el(action === 'cancel' ? 'esriCancel' : 'esriResume').addEventListener('click', async () => {
      if (!jobId || actionPending) return;
      actionPending = true;
      el('esriCancel').disabled = el('esriResume').disabled = true;
      try {
        const job = await request(`/api/esri/jobs/${encodeURIComponent(jobId)}/${action}`, {});
        actionPending = false;
        render(job);
      } catch (error) { el('esriStatus').textContent = error.message; timer = setTimeout(poll, 1500); }
      finally { actionPending = false; }
    });
  }
  if (jobId) poll();
})();
