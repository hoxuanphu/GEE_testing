(() => {
  'use strict';
  if (!['localhost', '127.0.0.1', '[::1]'].includes(location.hostname)) return;

  const banner = document.getElementById('providerWarning');
  const link = banner.querySelector('a');
  const text = link.querySelector('span');
  const providers = {
    gee: ['Google Earth Engine (GEE)', {service_account_json: 'tệp JSON service account'}],
    cdse: ['CDSE OData', {email: 'email đăng nhập', password: 'mật khẩu'}],
    s3: ['CDSE S3', {access_key: 'Access Key', secret_key: 'Secret Key'}],
  };

  function updateOverflow() {
    const distance = Math.max(0, Math.ceil(text.getBoundingClientRect().width - link.clientWidth));
    link.style.setProperty('--scroll-distance', distance + 'px');
    link.style.setProperty('--scroll-duration', Math.max(4, distance / 40) + 's');
    banner.toggleAttribute('data-overflow', distance > 1);
  }

  async function refresh() {
    try {
      const response = await fetch('/api/local-provider-settings/availability', {cache: 'no-store'});
      if (!response.ok) return;
      const status = await response.json();
      const missing = [];
      const pending = [];
      for (const [provider, [name, labels]] of Object.entries(providers)) {
        const fields = (status.missing[provider] || []).map(field => labels[field]).filter(Boolean);
        if (fields.length) missing.push(name + ' (' + fields.join(', ') + ')');
        if (status.restart_required.includes(provider)) pending.push(name);
      }
      const messages = [];
      if (missing.length) messages.push('Thiếu cấu hình tải — ' + missing.join(' · '));
      if (pending.length) messages.push('Khởi động lại để áp dụng khóa ' + pending.join(', '));
      const message = messages.join(' · ');
      text.textContent = message;
      link.title = message;
      banner.hidden = !message;
      updateOverflow();
    } catch {
      // Keep the last known message if the backend is temporarily unavailable.
    }
  }

  const observer = new ResizeObserver(updateOverflow);
  observer.observe(link);
  observer.observe(text);
  window.addEventListener('focus', refresh);
  refresh();
})();
