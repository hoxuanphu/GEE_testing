(() => {
  const key = 'satellite-workspace-theme';
  const modes = ['system', 'light', 'dark'];
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  let mode = 'system';
  let select;
  try {
    const saved = localStorage.getItem(key);
    if (modes.includes(saved)) mode = saved;
  } catch (_) {
    // Private browsing or storage policies can block persistence; switching still works.
  }
  function apply() {
    document.documentElement.dataset.theme = mode === 'system' ? (media.matches ? 'dark' : 'light') : mode;
    if (select) select.value = mode;
  }
  apply();
  const syncSystem = () => { if (mode === 'system') apply(); };
  media.addEventListener('change', syncSystem);
  document.addEventListener('visibilitychange', syncSystem);
  window.addEventListener('focus', syncSystem);
  window.addEventListener('storage', event => {
    if (event.key !== key && event.key !== null) return;
    mode = modes.includes(event.newValue) ? event.newValue : 'system';
    apply();
  });
  document.addEventListener('DOMContentLoaded', () => {
    select = document.getElementById('workspaceTheme');
    if (!select) return;
    select.value = mode;
    select.addEventListener('change', () => {
      mode = modes.includes(select.value) ? select.value : 'system';
      apply();
      try { localStorage.setItem(key, mode); } catch (_) { /* Keep the in-memory preference. */ }
    });
  });
})();
