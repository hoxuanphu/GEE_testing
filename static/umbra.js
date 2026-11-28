/* Browse the local Umbra catalogue and download immutable scene selections. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const base = '/api/umbra';
  const active = () => el('dataset').value === 'umbra';
  const downloadAttempt = () => Array.from(window.crypto.getRandomValues(new Uint8Array(16)),
    byte => byte.toString(16).padStart(2, '0')).join('');
  let snapshot, controller, poll;
  let generation = 0;
  let catalogGeneration = 0;

  async function json(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw new Error(typeof body.detail === 'string'
      ? body.detail : 'Kiểm tra vùng tìm ảnh và khoảng ngày đã chọn.');
    return body;
  }

  function invalidate() {
    generation++;
    controller?.abort();
    snapshot = null;
    el('umbraResults').hidden = true;
    el('umbraResults').setAttribute('aria-busy', 'false');
    el('umbraScenes').replaceChildren();
    el('umbraSelection').hidden = !active();
    if (!active()) {
      catalogGeneration++;
      clearTimeout(poll);
    }
  }

  function limitations(skipped, state) {
    const messages = [];
    if (skipped > 0) messages.push(`Danh mục chưa đầy đủ (${skipped} tệp GEC thiếu metadata phù hợp).`);
    if (state === 'stale') messages.push('Lần đồng bộ gần nhất thất bại. Đang dùng bản cập nhật trước.');
    if (state === 'refreshing') messages.push('Đang đồng bộ danh mục Umbra.');
    return messages.join(' ');
  }

  async function catalog(refresh = false) {
    clearTimeout(poll);
    const token = ++catalogGeneration;
    const button = el('umbraRefresh');
    button.disabled = true;
    try {
      const data = await json(base + '/catalog' + (refresh ? '/refresh' : ''), {
        method: refresh ? 'POST' : 'GET',
      });
      if (token !== catalogGeneration || !active()) return;
      const ready = !!data.updated_at;
      button.textContent = ready ? 'Cập nhật danh mục Umbra' : 'Tạo danh mục Umbra';
      const message = ready
        ? `${data.count} cảnh GEC · cập nhật ${new Date(data.updated_at).toLocaleString('vi-VN')}. `
        : data.state === 'failed' ? 'Đồng bộ thất bại. Hãy thử lại. '
          : data.state === 'refreshing' ? '' : 'Tạo danh mục để tìm ảnh. ';
      const progress = data.state === 'refreshing' && data.stage === 'metadata'
        ? ` Đã kiểm tra metadata ${data.processed}/${data.total_groups} nhóm cảnh.` : '';
      el('umbraCatalogStatus').textContent = message + limitations(data.skipped, data.state) + progress;
      if (data.state === 'refreshing') poll = setTimeout(catalog, 3000);
      else button.disabled = false;
    } catch (error) {
      if (token === catalogGeneration && active()) {
        el('umbraCatalogStatus').textContent = error.message;
        button.disabled = false;
      }
    }
  }

  function render(data) {
    snapshot = data;
    const params = data.parameters;
    const uncertain = params.catalog_skipped > 0 || params.catalog_state !== 'ready';
    const found = data.total
      ? `${data.total} cảnh giao ROI · ${data.offset + 1}–${Math.min(data.offset + data.limit, data.total)}.`
      : uncertain ? 'Chưa tìm thấy cảnh trong danh mục đang dùng.'
        : 'Không có cảnh giao vùng trong khoảng ngày đã chọn.';
    el('umbraResultStatus').textContent = [found, limitations(params.catalog_skipped, params.catalog_state)]
      .filter(Boolean).join(' ');
    el('umbraPrev').disabled = data.offset === 0;
    el('umbraNext').disabled = data.offset + data.limit >= data.total;
    window.ResultFootprints?.clear();
    const rows = data.scenes.map((scene, i) => {
      const row = document.createElement('div');
      row.className = 'scene-catalog-row';
      const url = `${base}/search/${encodeURIComponent(data.search_id)}/scenes/${data.offset + i}`;
      const details = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = [scene.datetime.replace(/\.\d+\+00:00$/, 'Z').replace('T', ' ').replace(/Z$/, ' UTC'),
        scene.platform, ...(scene.polarizations || [])].filter(Boolean).join(' · ');
      const product = document.createElement('div');
      const meters = value => new Intl.NumberFormat('vi-VN', {maximumFractionDigits: 2}).format(value);
      product.textContent = [`${meters(scene.size_bytes / 1000000)} MB`,
        scene.resolution_range_m && `Range ${meters(scene.resolution_range_m)} m`,
        scene.resolution_azimuth_m && `Azimuth ${meters(scene.resolution_azimuth_m)} m`].filter(Boolean).join(' · ');
      details.append(name, product);
      const actions = document.createElement('div');
      actions.className = 'download-job-actions';
      for (const [field, suffix, label] of [
        ['download_url', '/download', 'Tải toàn cảnh GEC'],
        ['metadata_url', '/metadata', 'Tải metadata'],
      ]) {
        if (scene[field] !== url + suffix) continue;
        const link = document.createElement('a');
        link.href = scene[field];
        link.textContent = label;
        link.download = '';
        if (field === 'download_url') {
          link.addEventListener('click', () => { link.href = scene[field] + '?attempt=' + downloadAttempt(); });
        }
        actions.append(link);
      }
      const content = document.createElement('div');
      content.append(details, actions);
      if (scene.thumbnail_url === url + '/thumbnail') {
        window.SceneThumbnails?.append(row, scene, name.textContent, 'scene-catalog-with-thumbnail');
      }
      row.append(content);
      window.ResultFootprints?.add(content, scene.footprint);
      return row;
    });
    el('umbraScenes').replaceChildren(...rows);
    window.ResultFootprints?.fit(params.bbox);
  }

  async function load(path, options) {
    controller?.abort();
    controller = new AbortController();
    const token = ++generation;
    el('umbraResults').hidden = false;
    el('umbraResults').setAttribute('aria-busy', 'true');
    el('umbraResultStatus').textContent = 'Đang tìm cảnh Umbra…';
    el('umbraScenes').replaceChildren();
    el('umbraPrev').disabled = el('umbraNext').disabled = true;
    try {
      const data = await json(path, {...options, signal:controller.signal});
      if (token === generation && active()) render(data);
    } catch (error) {
      if (token === generation && active() && error.name !== 'AbortError') {
        el('umbraResultStatus').textContent = error.message;
        if (snapshot) {
          el('umbraPrev').disabled = snapshot.offset === 0;
          el('umbraNext').disabled = snapshot.offset + snapshot.limit >= snapshot.total;
        }
      }
    } finally {
      if (token === generation) el('umbraResults').setAttribute('aria-busy', 'false');
    }
  }

  const page = offset => load(`${base}/search/${encodeURIComponent(snapshot.search_id)}?offset=${offset}`);
  el('umbraPrev').onclick = () => snapshot && page(Math.max(0, snapshot.offset - snapshot.limit));
  el('umbraNext').onclick = () => snapshot && page(snapshot.offset + snapshot.limit);
  el('umbraRefresh').onclick = () => catalog(true);
  el('dataset').addEventListener('change', () => {
    invalidate();
    if (active()) catalog();
  });
  window.Umbra = {invalidate, catalog, search(body) {
    snapshot = null;
    return load(base + '/search', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
  }};
  invalidate();
  if (active()) catalog();
})();
