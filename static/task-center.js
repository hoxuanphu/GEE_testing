(() => {
  'use strict';

  const get = id => document.getElementById(id);
  const list = get('taskList');
  const status = get('taskCenterStatus');
  const filter = get('taskFilter');
  const limit = 20;
  const active = new Set(['queued', 'running', 'cancelling', 'pausing']);
  const labels = {
    queued: 'Đang chờ', running: 'Đang chạy', cancelling: 'Đang hủy',
    pausing: 'Đang tạm dừng', paused: 'Đã tạm dừng', interrupted: 'Bị gián đoạn',
    completed: 'Hoàn tất', partial: 'Hoàn tất một phần', failed: 'Thất bại',
    cancelled: 'Đã hủy', expired: 'Đã hết hạn', empty: 'Không có cảnh',
  };
  const types = {
    timeseries: 'Time series · Một ZIP',
    annual: 'Time series · Theo năm',
    frame: 'Ảnh đồng khung',
    esri: 'Esri World Imagery',
    sentinel2: 'Sentinel-2 · Tải nền',
    transfer: 'Tải tệp',
  };
  const stages = {
    queued: 'Đang chờ', downloading: 'Đang tải dữ liệu', preparing: 'Đang chuẩn bị',
    assembling: 'Đang xử lý ảnh', validating: 'Đang kiểm tra ảnh',
    publishing: 'Đang lưu kết quả', packaging: 'Đang đóng gói',
    export: 'Đang xuất ảnh', archive: 'Đang đóng gói', completed: 'Hoàn tất',
    transferring: 'Đang gửi tệp tới trình duyệt',
  };
  const transferLabels = {
    queued: 'Đang chờ',
    preparing: 'Đang chuẩn bị tải',
    downloading_tiles: 'Đang tải tile từ GEE',
    waiting_assembly: 'Đang chờ ghép ảnh',
    assembling: 'Đang ghép GeoTIFF',
    writing_cog: 'Đang tạo COG',
    transferring: 'Đang gửi tệp tới trình duyệt',
    completed: 'Hoàn thành',
  };
  let offset = 0;
  let timer;
  let pending = false;
  let previous = '';
  let requestVersion = 0;
  let downloadsActive = false;

  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }

  function button(text, action, item) {
    const node = element('button', text);
    node.type = 'button';
    node.dataset.action = action;
    node.dataset.kind = item.kind;
    node.dataset.id = item.id;
    return node;
  }

  function link(label, path) {
    if (!path || !path.startsWith('/api/')) return null;
    const node = element('a', label);
    node.href = path;
    return node;
  }

  function yearDetails(item) {
    const details = element('details', undefined, 'task-years');
    details.dataset.taskDetails = item.id;
    details.append(element('summary', 'Chi tiết theo năm'));
    for (const year of item.years || []) {
      const row = element('div', undefined, 'task-year');
      const total = year.total === null ? 'chưa xác định' : year.total;
      row.append(element('span', `${year.year} · ${labels[year.status] || year.status} · ${year.processed}/${total}`));
      const zip = link('ZIP', year.zip_url);
      const csv = link('CSV', year.csv_url);
      if (zip) row.append(zip);
      if (csv) row.append(csv);
      if (year.error) row.append(element('span', year.error, 'task-error'));
      details.append(row);
    }
    return details;
  }

  function card(item) {
    const article = element('article', undefined, 'task-card');
    const heading = element('div', undefined, 'task-card-heading');
    const transfer = item.kind === 'transfer';
    const phase = transfer && ['queued', 'running', 'completed'].includes(item.status)
      ? transferLabels[item.stage] : null;
    heading.append(element('h3', item.filename || types[item.kind] || item.kind),
                   element('span', phase || labels[item.status] || item.status, 'task-state'));
    article.append(heading);
    const params = item.parameters || {};
    const info = [params.dataset, params.selection_mode,
                  params.start_date && params.end_date ? `${params.start_date} → ${params.end_date}` : null]
      .filter(Boolean).join(' · ');
    if (info) article.append(element('p', info, 'hint'));
    if (item.created) article.append(element('p', `Tạo lúc ${new Date(item.created * 1000).toLocaleString('vi-VN')}`, 'hint'));
    const total = transfer ? item.bytes_total : item.total;
    const processed = transfer ? item.bytes_sent : item.processed;
    const unit = item.kind === 'esri' || item.kind === 'sentinel2' ? 'tile' : 'cảnh';
    const knownTotal = typeof total === 'number' && total > 0;
    const tileProgress = transfer && item.stage === 'downloading_tiles' && item.tiles_total > 0;
    if (transfer) {
      const size = bytes => (bytes / 1024 ** 2).toLocaleString('vi-VN', {maximumFractionDigits: 2}) + ' MiB';
      if (item.status === 'completed') {
        article.append(element('p', `${size(processed)} · 100%`));
      } else if (knownTotal) {
        const percent = Math.min(item.bytes_estimated ? 99 : 100, Math.floor(processed * 100 / total));
        const approximation = item.bytes_estimated ? ' ước tính' : '';
        article.append(element('p', `${size(processed)} / ${size(total)} · ${percent}%${approximation}`));
      } else if (item.stage === 'transferring') {
        article.append(element('p', `${size(processed)} đã tải · chưa biết tổng dung lượng`));
      } else if (tileProgress) {
        article.append(element('p', `${item.tiles_downloaded} / ${item.tiles_total} tile`));
      }
    } else {
      const percent = knownTotal ? ` · ${Math.min(100, Math.floor((processed || 0) * 100 / total))}%` : '';
      article.append(element('p', `${processed || 0} / ${total ?? 'chưa xác định'} ${unit}${percent} · Thành công ${item.succeeded || 0} · Lỗi ${item.failed || 0}`));
    }
    if (knownTotal || (active.has(item.status) && (!transfer || item.stage !== 'queued')) ||
        (transfer && item.status === 'completed')) {
      const progress = element('progress');
      if (transfer && item.status === 'completed') {
        progress.max = 100;
        progress.value = 100;
      } else if (tileProgress) {
        progress.max = item.tiles_total;
        progress.value = item.tiles_downloaded;
      } else if (knownTotal) {
        progress.max = total;
        progress.value = Math.min(processed || 0, item.bytes_estimated ? total * .99 : total);
      }
      progress.setAttribute('aria-label', 'Tiến độ xử lý');
      article.append(progress);
    }
    if (item.stage && !transfer) article.append(element('p', stages[item.stage] || (item.kind === 'annual' ? `Năm ${item.stage}` : item.stage), 'hint'));
    if (item.error) article.append(element('p', item.error, 'task-error'));
    if (item.kind === 'annual') article.append(yearDetails(item));
    const actions = element('div', undefined, 'task-actions');
    const download = link('Tải kết quả', item.download_url);
    if (download) actions.append(download);
    if (['queued', 'running'].includes(item.status) && ['timeseries', 'annual', 'frame', 'esri', 'sentinel2', 'transfer'].includes(item.kind)) {
      actions.append(button(item.kind === 'annual' ? 'Tạm dừng' : 'Hủy tác vụ', 'cancel', item));
    }
    if (item.kind === 'annual' && ['paused', 'interrupted', 'partial', 'failed'].includes(item.status)) {
      const retry = ['partial', 'failed'].includes(item.status);
      actions.append(button(retry ? 'Thử lại phần lỗi' : 'Tiếp tục', retry ? 'retry' : 'resume', item));
    }
    if (!active.has(item.status)) {
      actions.append(button('Xóa khỏi danh sách', 'hide', item));
      if (['timeseries', 'annual'].includes(item.kind)) actions.append(button('Xóa cả tệp', 'delete-files', item));
    }
    if (actions.childNodes.length) article.append(actions);
    return article;
  }

  function render(data) {
    const expanded = new Set([...list.querySelectorAll('details[open]')].map(node => node.dataset.taskDetails));
    const focused = document.activeElement;
    const focusKey = focused?.dataset?.action ? {action: focused.dataset.action, id: focused.dataset.id} : null;
    list.replaceChildren(...data.items.map(card));
    for (const details of list.querySelectorAll('details[data-task-details]')) {
      details.open = expanded.has(details.dataset.taskDetails);
    }
    if (focusKey) {
      [...list.querySelectorAll('button[data-action]')].find(node =>
        node.dataset.action === focusKey.action && node.dataset.id === focusKey.id)?.focus();
    }
    if (!data.items.length) list.append(element('p', 'Chưa có tác vụ trong danh sách này.', 'hint task-empty'));
    status.textContent = `Đang chạy hoặc chờ: ${data.counts.active} · Đã kết thúc: ${data.counts.finished}`;
    get('taskPrevious').disabled = offset === 0;
    get('taskNext').disabled = offset + limit >= data.total;
    get('taskPage').textContent = data.total ? `${offset + 1}–${Math.min(offset + limit, data.total)} / ${data.total}` : '0 tác vụ';
  }

  async function refresh() {
    if (pending) return;
    pending = true;
    const version = ++requestVersion;
    const requestedOffset = offset;
    const requestedFilter = filter.value;
    let refreshAgain = false;
    try {
      const query = new URLSearchParams({offset: String(offset), limit: String(limit), status: filter.value});
      const response = await fetch('/api/tasks?' + query);
      if (!response.ok) throw new Error('Không đọc được danh sách tác vụ.');
      const data = await response.json();
      downloadsActive = data.counts.active > 0;
      if (version !== requestVersion) return;
      if (requestedOffset !== offset || requestedFilter !== filter.value) {
        refreshAgain = true;
        return;
      }
      if (offset && offset >= data.total) {
        offset = Math.max(0, Math.ceil(data.total / limit) * limit - limit);
        previous = '';
        refreshAgain = true;
        return;
      }
      const signature = JSON.stringify(data);
      if (signature !== previous) {
        previous = signature;
        render(data);
      }
    } catch (error) {
      previous = '';
      status.textContent = error.message + ' Sẽ thử lại.';
    } finally {
      pending = false;
      clearTimeout(timer);
      timer = setTimeout(refresh, refreshAgain ? 0 : downloadsActive && !document.hidden ? 600 : 2000);
    }
  }

  async function operate(action, kind, id) {
    if (action === 'hide' && !window.confirm('Xóa tác vụ khỏi danh sách? Các tệp kết quả vẫn được giữ theo thời hạn lưu hiện tại.')) return;
    let url = `/api/tasks/${encodeURIComponent(kind)}/${encodeURIComponent(id)}`;
    let method = 'POST';
    try {
      if (action === 'hide') method = 'DELETE';
      else if (action === 'delete-files') {
        const response = await fetch(url + '/files');
        if (!response.ok) {
          status.textContent = 'Không xác định được dung lượng tệp của tác vụ.';
          return;
        }
        const files = await response.json();
        const size = (files.bytes / 1024 / 1024).toLocaleString('vi-VN', {maximumFractionDigits: 1});
        if (!window.confirm(`Xóa tác vụ khỏi danh sách và xóa ${files.files} tệp (${size} MiB) trên máy chủ? Thao tác không thể hoàn tác.`)) return;
        url += '?delete_files=true';
        method = 'DELETE';
      }
      else if (action === 'cancel') url += '/cancel';
      else url = `/api/timeseries/batches/${encodeURIComponent(id)}/${action}`;
      const response = await fetch(url, {method});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Không thực hiện được thao tác.');
      previous = '';
      await refresh();
    } catch (error) {
      status.textContent = error.message;
    }
  }

  list.addEventListener('click', event => {
    const target = event.target.closest('button[data-action]');
    if (!target || target.disabled) return;
    target.disabled = true;
    operate(target.dataset.action, target.dataset.kind, target.dataset.id).finally(() => { target.disabled = false; });
  });
  document.addEventListener('click', event => {
    const anchor = event.target.closest('a[href]');
    if (!anchor) return;
    const url = new URL(anchor.href, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return;
    // Start a native attachment download without starting a navigation of this tab.
    anchor.download = '';
    setTimeout(refresh, 150);
  }, true);
  filter.addEventListener('change', () => { offset = 0; previous = ''; refresh(); });
  get('taskPrevious').addEventListener('click', () => { offset = Math.max(0, offset - limit); previous = ''; refresh(); });
  get('taskNext').addEventListener('click', () => { offset += limit; previous = ''; refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  document.querySelector('[data-workspace-view="jobs"]').addEventListener('click', refresh);
  window.TaskCenter = Object.freeze({
    refresh,
    download(path) {
      const anchor = link('', path);
      if (!anchor) return;
      anchor.download = '';
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
    },
  });
  refresh();
})();
