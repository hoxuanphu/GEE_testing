(() => {
  const get = id => document.getElementById(id);
  const sidebar = get('sidebar');
  const heading = document.createElement('div');
  heading.className = 'workspace-pane-heading';
  const title = document.createElement('h2');
  title.id = 'workspaceExploreTitle';
  title.tabIndex = -1;
  title.textContent = 'Tìm ảnh vệ tinh';
  heading.append(title);
  const fields = document.createElement('div');
  fields.className = 'workspace-scroll workspace-fields';
  const group = heading => {
    const section = document.createElement('section');
    section.className = 'workspace-group';
    const h = document.createElement('h3');
    h.textContent = heading;
    section.append(h);
    fields.append(section);
    return section;
  };
  const region = group('Vùng quan tâm');
  const source = group('Nguồn dữ liệu');
  const filters = group('Thời gian và bộ lọc');
  const move = (target, ids) => ids.forEach(id => { const node = get(id); if (node) target.append(node); });
  const disclosure = (parent, label) => {
    const details = document.createElement('details');
    details.className = 'workspace-disclosure';
    const summary = document.createElement('summary');
    summary.textContent = label;
    const body = document.createElement('div');
    body.className = 'workspace-details-body';
    details.append(summary, body);
    parent.append(details);
    return {details, body};
  };
  // Reparent the existing inputs so their listeners and ROI state stay active.
  const selection = get('selection_mode');
  region.append(selection.parentElement);
  move(region, ['mfRoiSource', 'referenceSelection', 'rectangleSelection', 'provinceSelection', 'communeSelection']);
  region.append(pointSelection, radiusSelection);
  move(region, ['rotationSelection']);
  const vector = disclosure(region, 'Lớp vector');
  move(vector.body, ['vectorLayersPanel']);

  source.append(get('dataset').parentElement);
  move(source, ['mfPlatformSelection', 's5pSelection', 'sarSelection', 'slcSelection', 's2SourceSelection', 's3TypeSelection', 'demHint']);
  move(filters, ['dateSelection', 'cloudSelection']);
  const advanced = disclosure(filters, 'Tùy chọn nâng cao');
  move(advanced.body, ['s2AuxiliarySelection', 'pointMergeSelection', 'compositeSelection', 'sceneGroupingSelection', 'mosaicMaskSelection', 'colorBalanceSelection', 'sarMergeSelection', 'timeSeriesSelection']);
  move(filters, ['imageInventoryControls']);
  const info = disclosure(fields, 'Thông tin và giới hạn dữ liệu');
  info.body.classList.add('workspace-source-info');
  info.body.append(sidebar.lastElementChild);

  const action = document.createElement('div');
  action.className = 'workspace-action';
  move(action, ['btnSearch', 'status', 'searchTiming']);
  const resultIds = ['plan', 'sceneCatalogResults', 'result', 's1result', 's5pResults', 'timeSeriesResults', 'matchedFrameResults'];
  move(get('workspaceResultContent'), resultIds);
  move(get('workspaceJobDetails'), ['downloadJobPanel', 'timeSeriesBatchPanel', 'esriPanel']);
  filters.append(...sidebar.children);
  sidebar.replaceChildren(heading, fields, action);

  document.querySelectorAll('label:not([for])').forEach(label => {
    const control = label.querySelector('input,select') || label.nextElementSibling;
    if (control && control.matches('input[id],select[id]')) label.htmlFor = control.id;
  });
  get('preview').alt = 'Ảnh xem trước dữ liệu vệ tinh của vùng đã chọn';
  const resultPanel = get('workspaceResults');
  const jobPanel = get('workspaceJobs');
  const panels = {explore: sidebar, results: resultPanel, jobs: jobPanel};
  const titles = {explore: title, results: get('workspaceResultsTitle'), jobs: get('workspaceJobsTitle')};
  const navButtons = document.querySelectorAll('[data-workspace-view]');
  let currentView = 'explore';
  const resultNodes = resultIds.map(get);
  const hasResults = () => resultNodes.some(node => {
    return !node.hidden && getComputedStyle(node).display !== 'none';
  });
  const mapHint = get('workspaceMapHint');
  const mapHints = {
    point: 'Chọn điểm trên bản đồ hoặc nhập tọa độ.',
    rectangle: 'Dùng nút Vẽ / vẽ lại để tạo vùng trên bản đồ.',
  };
  function updateMapHint() {
    mapHint.textContent = mapHints[selection.value] || '';
    mapHint.hidden = currentView !== 'explore' || !mapHint.textContent;
  }
  selection.addEventListener('change', () => {
    if (selection.value === 'vector') vector.details.open = true;
    updateMapHint();
  });
  map.zoomControl.setPosition('topright');
  L.control.scale({imperial: false, position: 'bottomleft'}).addTo(map);

  function showView(view, {moveFocus = false} = {}) {
    if (!Object.hasOwn(panels, view)) return;
    const previousPanel = panels[currentView];
    const focusHidden = currentView !== view && previousPanel.contains(document.activeElement);
    currentView = view;
    for (const [name, panel] of Object.entries(panels)) panel.hidden = name !== view;
    document.body.classList.toggle('has-results', view === 'results');
    navButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.workspaceView === view)));
    get('workspaceEmpty').hidden = hasResults();
    updateMapHint();
    if (moveFocus || focusHidden) titles[view].focus();
    requestAnimationFrame(() => map.invalidateSize({pan: false}));
  }
  navButtons.forEach(button => button.addEventListener('click', () => showView(button.dataset.workspaceView)));
  get('workspaceClose').addEventListener('click', () => showView('explore', {moveFocus: true}));
  let previousResults = hasResults();
  const observer = new MutationObserver(() => {
    const visible = hasResults();
    get('workspaceEmpty').hidden = visible;
    if (visible && !previousResults && currentView === 'explore') {
      showView('results', {moveFocus: document.activeElement === document.body});
    }
    previousResults = visible;
  });
  resultNodes.forEach(node => observer.observe(node, {attributes: true, attributeFilter: ['style', 'hidden']}));
  window.SatelliteWorkspace = Object.freeze({showView, hasResults});
  showView('explore');
})();
