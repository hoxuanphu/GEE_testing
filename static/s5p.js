/* Individual-scene Sentinel-5P UI, using immutable server search snapshots. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  let snapshot = null;
  let generation = 0;
  let controller;
  async function json(url, options = {}) {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Yêu cầu không hợp lệ; kiểm tra vùng và ngày.');
    return data;
  }
  function invalidate() {
    generation++;
    controller?.abort();
    snapshot = null;
    el('s5pResults').hidden = true;
    el('s5pScenes').replaceChildren();
    el('s5pSelection').hidden = el('dataset').value !== 's5p';
  }
  function render(data) {
    snapshot = data;
    el('s5pResults').hidden = false;
    el('s5pMeta').textContent = `${data.total} ảnh ${data.parameters.product} (${data.parameters.unit}), UTC. ` +
      (data.total ? `Ảnh ${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)}.` : 'Không có ảnh; thử mở rộng khoảng ngày.') +
      (data.parameters.truncated ? ' Chỉ hiển thị 2000 ảnh đầu; thu hẹp khoảng ngày để tìm đủ.' : '');
    el('s5pPrev').disabled = data.offset === 0;
    el('s5pNext').disabled = data.offset + data.limit >= data.total;
    const rows = data.scenes.map(scene => {
      const row = document.createElement('div');
      row.style.cssText = 'padding:12px 0;overflow-wrap:anywhere';
      window.SceneThumbnails?.append(row, scene, `${data.parameters.product} · ${scene.timestamp}`);
      const label = document.createElement('p');
      label.textContent = `${scene.timestamp} — ${scene.scene_id}`;
      const download = document.createElement('button');
      download.textContent = 'Tải ảnh GeoTIFF';
      const preview = document.createElement('button');
      preview.textContent = 'Xem trước';
      const message = document.createElement('p');
      message.setAttribute('role', 'status');
      const token = generation;
      preview.onclick = async () => {
        preview.disabled = true;
        message.textContent = 'Đang tạo xem trước…';
        try {
          const result = await json(scene.preview_url);
          if (token !== generation) return;
          const image = document.createElement('img');
          image.alt = `${scene.timestamp}, ${data.parameters.product}, ${result.unit}. Màu tăng từ xanh dương đến đỏ.`;
          image.style.cssText = 'max-width:100%;display:block';
          image.src = result.url;
          image.onerror = () => { message.textContent = 'Không tải được xem trước; có thể thử tải GeoTIFF.'; };
          const vis = data.parameters.visualization;
          message.textContent = `Thang màu: ${vis.min}–${vis.max} ${result.unit} (xanh dương → đỏ). Vùng trống là pixel bị mask.`;
          row.querySelector('.s5p-expanded-preview')?.remove();
          image.className = 's5p-expanded-preview';
          row.append(image);
        } catch (error) { if (token === generation) message.textContent = error.message; }
        finally { preview.disabled = false; }
      };
      download.onclick = async () => {
        download.disabled = true;
        message.textContent = 'Đang tải GeoTIFF…';
        try {
          const response = await fetch(scene.download_url);
          if (!response.ok) {
            const error = await response.json();
            throw new Error(error.detail || 'Không tải được ảnh.');
          }
          const blob = await response.blob();
          if (token !== generation) return;
          const url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = `S5P_${data.parameters.product}_${scene.scene_id}.tif`;
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          message.textContent = 'Đã tải ảnh.';
        } catch (error) { if (token === generation) message.textContent = error.message; }
        finally { download.disabled = false; }
      };
      row.append(label, preview, download, message);
      return row;
    });
    el('s5pScenes').replaceChildren(...rows);
  }
  async function load(url, options) {
    controller?.abort();
    controller = new AbortController();
    const token = ++generation;
    el('s5pResults').hidden = false;
    el('s5pResults').setAttribute('aria-busy', 'true');
    el('s5pMeta').textContent = 'Đang tìm từng ảnh Sentinel-5P…';
    el('s5pScenes').replaceChildren();
    el('s5pPrev').disabled = el('s5pNext').disabled = true;
    try {
      const data = await json(url, {...options, signal: controller.signal});
      if (token === generation) render(data);
    } catch (error) {
      if (token === generation && error.name !== 'AbortError') el('s5pMeta').textContent = error.message;
    } finally {
      if (token === generation) el('s5pResults').setAttribute('aria-busy', 'false');
    }
  }
  el('s5pPrev').onclick = () => snapshot && load(`/api/s5p/search/${snapshot.search_id}?offset=${Math.max(0, snapshot.offset - 50)}`);
  el('s5pNext').onclick = () => snapshot && load(`/api/s5p/search/${snapshot.search_id}?offset=${snapshot.offset + 50}`);
  for (const id of ['dataset', 's5p_product', 'selection_mode', 'province_id', 'commune_id', 'start_date', 'end_date', 'lon', 'lat', 'radius_m', 'rotation_deg', 'rotation_slider']) {
    el(id)?.addEventListener('change', invalidate);
    el(id)?.addEventListener('input', invalidate);
  }
  // Rectangle editing can change hidden coordinates without a normal input event.
  el('btnSearch').addEventListener('click', () => { if (el('dataset').value !== 's5p') invalidate(); });
  window.S5P = {invalidate, search: body => load('/api/s5p/search', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  })};
  invalidate();
})();
