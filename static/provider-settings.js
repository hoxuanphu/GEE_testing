(() => {
  'use strict';
  const api = '/api/local-provider-settings';
  const notice = document.getElementById('providerStatus');
  const forms = [...document.querySelectorAll('[data-provider]')];
  const messages = {
    active: 'Đã nạp cấu hình cục bộ.',
    restart_required: 'Đã lưu. Khởi động lại backend để áp dụng.',
    not_saved: 'Chưa lưu qua giao diện.',
    error: 'Không đọc được cấu hình đã lưu.',
  };

  function setStatus(element, message, error = false) {
    element.textContent = message;
    element.toggleAttribute('data-error', error);
  }

  if (!['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)) {
    setStatus(notice, 'Mở ứng dụng bằng localhost hoặc 127.0.0.1 để cấu hình nguồn dữ liệu.', true);
    return;
  }

  async function loadStatus() {
    try {
      const response = await fetch(api, {cache: 'no-store'});
      if (!response.ok) throw new Error();
      const states = await response.json();
      for (const form of forms) {
        const state = states[form.dataset.provider];
        setStatus(form.querySelector('[data-state]'), messages[state] || '', state === 'error');
        form.querySelector('button').disabled = false;
      }
    } catch {
      setStatus(notice, 'Không tải được cấu hình. Kiểm tra backend rồi tải lại trang này.', true);
    }
  }

  async function readForm(form) {
    const fields = new FormData(form);
    if (form.dataset.provider !== 'gee') return Object.fromEntries(fields);
    const file = fields.get('key_file');
    if (!file?.size || file.size > 32 * 1024) return null;
    try {
      return JSON.parse(await file.text());
    } catch {
      return null;
    }
  }

  for (const form of forms) form.addEventListener('submit', async event => {
    event.preventDefault();
    const status = form.querySelector('[data-state]');
    const button = form.querySelector('button');
    if (button.disabled) return;
    button.disabled = true;
    setStatus(status, 'Đang lưu…');
    try {
      const payload = await readForm(form);
      if (!payload) {
        setStatus(status, 'Chọn tệp JSON service account hợp lệ, tối đa 32 KB.', true);
        return;
      }
      const response = await fetch(`${api}/${form.dataset.provider}`, {
        method: 'PUT', cache: 'no-store', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) {
        setStatus(status, typeof data.detail === 'string' ? data.detail : 'Không lưu được cấu hình.', true);
        return;
      }
      form.reset();
      setStatus(status, messages[data.state] || 'Đã lưu cấu hình.');
    } catch {
      setStatus(status, 'Không lưu được cấu hình. Kiểm tra kết nối tới backend.', true);
    } finally {
      button.disabled = false;
    }
  });
  loadStatus();
})();
