/* Runs only when a person opens /__browser and starts the real-browser check. */
(() => {
  'use strict';
  const frame = document.getElementById('workspace');
  const start = document.getElementById('start');
  const status = document.getElementById('status');
  const evidence = document.getElementById('evidence');
  const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
  const clone = value => JSON.parse(JSON.stringify(value));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  async function until(read, label, timeout = 30000) {
    const deadline = performance.now() + timeout;
    while (performance.now() < deadline) {
      const value = read();
      if (value) return value;
      await pause(250); // DOM only; this is not a network poll.
    }
    throw new Error(`Hết thời gian: ${label}`);
  }
  const digest = async buffer => [...new Uint8Array(await crypto.subtle.digest('SHA-256', buffer))]
    .map(byte => byte.toString(16).padStart(2, '0')).join('');
  async function ready() {
    try {
      await until(() => frame.contentWindow?.MatchedFrame && frame.contentWindow?.SelectionState,
                  'nạp controller và Leaflet thật');
      start.disabled = false;
      status.textContent = 'Đã nạp giao diện. Chọn Bắt đầu để chạy một lượt tuần tự.';
    } catch (error) { status.textContent = error.message; }
  }
  frame.addEventListener('load', ready, {once: true});
  start.addEventListener('click', async () => {
    start.disabled = true;
    const proof = {complete: false, checks: {}, started_at: Date.now() / 1000, user_agent: navigator.userAgent};
    const win = frame.contentWindow, doc = frame.contentDocument;
    const el = id => doc.getElementById(id);
    const set = (id, value) => {
      el(id).value = value;
      el(id).dispatchEvent(new win.Event('change', {bubbles: true}));
    };
    const trace = [];
    const originalFetch = win.fetch.bind(win);
    win.fetch = async (input, options = {}) => {
      const url = new URL(typeof input === 'string' ? input : input.url, win.location.href);
      assert(url.origin === location.origin, 'Harness chỉ cho phép fetch cùng localhost.');
      const entry = {path: url.pathname, method: options.method || 'GET'};
      if (typeof options.body === 'string') entry.body = JSON.parse(options.body);
      const response = await originalFetch(input, options);
      if (entry.path.startsWith('/api/matched-frame/')) {
        entry.status = response.status;
        if (response.headers.get('content-type')?.includes('application/json')) entry.response = await response.clone().json();
        trace.push(entry);
      }
      return response;
    };
    const stage = text => { status.textContent = text; evidence.textContent = JSON.stringify(proof, null, 2); };
    const draft = () => win.SelectionState.getReferenceDraft();
    const footprintVisible = polygon => {
      let found = false;
      const scan = value => {
        if (!value) return;
        if (value.type === 'Polygon' && equal(value.coordinates, polygon.coordinates)) found = true;
        if (value.geometry) scan(value.geometry);
        for (const child of value.features || []) scan(child);
      };
      win.eval('map').eachLayer(layer => { if (layer.toGeoJSON) scan(layer.toGeoJSON(false)); });
      return found;
    };
    try {
      stage('1/6 · Upload mẫu EQC một lần…');
      set('selection_mode', 'reference'); set('dataset', 's1gee');
      set('start_date', '2026-10-01'); set('end_date', '2026-10-25');
      const fixture = await fetch('/__browser/reference.tif');
      assert(fixture.ok, 'Không tải được fixture EQC.');
      const files = new win.DataTransfer();
      files.items.add(new win.File([await fixture.arrayBuffer()], 'eqc-browser-reference.tif', {type: 'image/tiff'}));
      el('mfFile').files = files.files;
      el('mfFile').dispatchEvent(new win.Event('change', {bubbles: true}));
      await until(() => draft()?.reference_id, 'upload mẫu');
      set('mfGridMode', 'match_grid'); set('mfPlatform', 'D'); set('mfResultMode', 'list');
      set('mfMinCoverage', '99.9');
      el('mfMergeScenes').checked = false;
      el('mfMergeScenes').dispatchEvent(new win.Event('change', {bubbles: true}));
      const original = clone(draft());
      proof.reference_id = original.reference_id;
      proof.reference_snapshot = original;
      assert(footprintVisible(original.footprint_geojson), 'Polygon mẫu chưa hiện đúng trên Leaflet.');

      stage('2/6 · Chuyển ROI từ mẫu, kiểm polygon chính xác…');
      set('selection_mode', 'rectangle');
      assert(win.MatchedFrame.isActive() && win.SelectionState.getSource() === 'reference', 'ROI chưa dùng nguồn mẫu.');
      assert(!win.eval('rectangleEditor.active'), 'Handles ROI thủ công còn bật khi dùng mẫu.');
      assert(equal(draft().footprint_geojson, original.footprint_geojson) && footprintVisible(original.footprint_geojson),
             'Polygon WGS84 đổi sau khi chuyển mode.');
      proof.checks.reference_roi_exact_geometry = true;

      stage('3/6 · Tạo ROI thủ công rồi phục hồi khung mẫu…');
      el('mfDrawManual').click();
      const rectangle = {center_lon: 104.1, center_lat: 21.8, width_m: 1234, height_m: 2345, rotation_deg: 17};
      for (const [key, value] of Object.entries(rectangle)) el('rect_' + key).value = String(value);
      el('rect_rotation_deg').dispatchEvent(new win.Event('input', {bubbles: true}));
      assert(equal(win.eval('rectangleEditor.get()'), rectangle), 'Editor không giữ ROI thủ công đã nhập.');
      assert(equal(draft(), original), 'Vẽ ROI thủ công làm đổi bản nháp mẫu.');
      const manual = clone(win.SelectionState.getManualDraft());
      el('mfUseReference').click();
      assert(footprintVisible(original.footprint_geojson), 'Không phục hồi polygon mẫu.');
      assert(equal(win.SelectionState.getManualDraft(), manual), 'Dùng mẫu ghi đè bản nháp ROI thủ công.');
      proof.checks.manual_draft_independent = true;
      set('selection_mode', 'reference');
      assert(equal(draft(), original) && el('mfGridMode').value === 'match_grid', 'Metadata hoặc lưới mẫu bị đổi.');
      proof.checks.grid_and_metadata_restored = true;
      assert(el('mfPlatform').value === 'D' && el('start_date').value === '2026-10-01' && el('end_date').value === '2026-10-25',
             'Ngày hoặc platform bị mất khi đổi mode.');
      proof.checks.dates_and_platform_retained = true;

      stage('4/6 · Tìm bằng reference_id cũ từ ROI…');
      set('selection_mode', 'rectangle');
      el('lon').value = ''; el('lat').value = '';
      el('btnSearch').click();
      const search = await until(() => trace.find(x => x.path.endsWith('/searches') && x.method === 'POST'), 'tìm ảnh', 330000);
      assert(search.status === 200, JSON.stringify(search.response));
      assert(search.body.reference_id === original.reference_id && search.body.selection_context === 'reference_roi',
             'Search không dùng reference_id/context đã lưu.');
      assert(search.body.platform === 'D' && search.body.grid_mode === 'match_grid', 'Search đổi vệ tinh/lưới.');
      proof.search_id = search.response.search_id;
      proof.checks.search_uses_original_reference = true;
      const chosen = await until(() => [...doc.querySelectorAll('#mfSceneRows input[name="mfSceneChoice"]')].find(x => !x.disabled),
                                 'render cảnh đủ điều kiện');
      chosen.click();

      stage('5/6 · Preflight và tải nền…');
      await until(() => !el('mfPreflight').disabled, 'bật preflight');
      el('mfPreflight').click();
      const preflight = await until(() => trace.find(x => x.path.endsWith('/preflight') && x.method === 'POST'), 'preflight', 150000);
      assert(preflight.status === 200 && preflight.response.can_start === true, JSON.stringify(preflight.response));
      proof.preflight_id = preflight.response.preflight_id;
      await until(() => !el('mfAcknowledge').disabled, 'hiện xác nhận');
      el('mfAcknowledge').click();
      assert(!el('mfConfirm').disabled, 'Không bật xác nhận tải sau acknowledge.');
      el('mfConfirm').click();
      const created = await until(() => trace.find(x => x.path.endsWith('/jobs') && x.method === 'POST'), 'tạo job');
      assert(created.status === 200, JSON.stringify(created.response));
      proof.job_id = created.response.job_id;
      const terminal = await until(() => trace.findLast(x => x.path.endsWith('/jobs/' + proof.job_id)
        && ['completed', 'failed', 'partial', 'interrupted'].includes(x.response?.status)) ||
        (created.response.status === 'completed' && created), 'job hoàn tất', 600000);
      assert(terminal.response.status === 'completed', JSON.stringify(terminal.response));
      proof.checks.job_completed = true;

      stage('6/6 · Kiểm TIFF/ZIP browser tải và phục hồi mode…');
      const result = terminal.response;
      await until(() => doc.querySelector(`a[href="${result.scenes[0].download_url}"]`), 'link TIFF đã render');
      proof.download_sha256 = {};
      for (const [kind, url] of [['tiff', result.scenes[0].download_url], ['zip', result.download_url]]) {
        const downloaded = await originalFetch(url);
        assert(downloaded.status === 200, `Không tải được ${kind}.`);
        proof.download_sha256[kind] = await digest(await downloaded.arrayBuffer());
      }
      proof.checks.tiff_and_zip_downloaded = true;
      set('selection_mode', 'reference');
      assert(equal(draft(), original) && footprintVisible(original.footprint_geojson), 'Mẫu đổi sau khi job hoàn tất.');
      proof.upload_count = trace.filter(x => x.path === '/api/matched-frame/references' && x.method === 'POST').length;
      assert(proof.upload_count === 1, 'Có upload mẫu lần hai.');
      proof.checks.one_reference_upload = true;
      proof.complete = true;
    } catch (error) {
      proof.error = error.message;
    } finally {
      win.fetch = originalFetch;
      proof.finished_at = Date.now() / 1000;
      evidence.textContent = JSON.stringify(proof, null, 2);
      try {
        const response = await fetch('/__browser/result', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(proof)});
        const saved = await response.json();
        assert(response.ok && saved.saved, JSON.stringify(saved));
        status.textContent = saved.complete ? 'Đạt: browser và artifact server đã được kiểm tra; proof đã lưu.' : `Chưa đạt: ${proof.error}`;
      } catch (error) { status.textContent = `Chưa ghi nhận hoàn tất: ${error.message}`; }
    }
  }, {once: true});
})();
