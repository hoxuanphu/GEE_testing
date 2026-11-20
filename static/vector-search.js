/* Vector ROI dispatch. Existing renderers keep their own immutable search/job snapshots. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const optical = new Set(['sentinel2', 'landsat', 'nicfi', 'dynamic_world']);
  const series = new Set([...optical, 's1gee']);
  const external = new Set(['s2', 's2aws', 's1', 's1slc', 's3']);
  const copy = value => JSON.parse(JSON.stringify(value));

  function isCurrent(version, roiId) {
    if (version !== searchGeneration || el('selection_mode').value !== 'vector') return false;
    try { return window.VectorLayers.request().vector_roi_id === roiId; }
    catch (_) { return false; }
  }
  function errorText(error) {
    return error && typeof error.message === 'string' ? error.message : 'Không thể hoàn tất yêu cầu vector.';
  }
  async function downloadDem(dataset, selection, version) {
    const button = el('btnSearch'); button.disabled = true;
    el('status').textContent = 'Đang tải và ghép DEM từ GEE theo polygon…';
    try {
      const params = new URLSearchParams({dataset:dataset.slice(4), selection_mode:'vector', vector_roi_id:selection.vector_roi_id});
      const response = await fetch('/api/dem/download?' + params);
      if (!response.ok) {
        const error = await response.json().catch(() => null);
        throw new Error(error && typeof error.detail === 'string' ? error.detail : 'Không thể tải DEM GeoTIFF.');
      }
      const blob = await response.blob();
      if (!isCurrent(version, selection.vector_roi_id)) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url;
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = disposition.match(/filename="([^"]+)"/);
      link.download = filename ? filename[1].replace(/[\\/\u0000-\u001f]/g, '_') : `${dataset}_30m_${Date.now()}.tif`;
      document.body.appendChild(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      el('status').textContent = 'Đã tải DEM GeoTIFF theo polygon (độ cao mét, NoData -9999).';
    } catch (error) {
      if (isCurrent(version, selection.vector_roi_id)) el('status').textContent = 'Lỗi: ' + errorText(error);
    } finally {
      if (isCurrent(version, selection.vector_roi_id)) button.disabled = false;
    }
  }
  async function compositePlan(body, version) {
    const button = el('btnSearch'); button.disabled = true;
    el('status').textContent = 'Đang lập kế hoạch composite theo polygon…';
    try {
      const plan = await requestCompositePlan(body);
      if (!isCurrent(version, body.vector_roi_id)) return;
      renderCompositePlan(plan);
      pendingCompositeBody = copy(body);
      el('status').textContent = 'Nhấn "Xác nhận và chạy" để tiếp tục.';
    } catch (error) {
      if (isCurrent(version, body.vector_roi_id)) el('status').textContent = 'Lỗi: ' + errorText(error);
    } finally {
      if (isCurrent(version, body.vector_roi_id)) button.disabled = false;
    }
  }
  async function search() {
    if (el('selection_mode').value !== 'vector') return;
    // Use the same result reset as the existing Search action; stored jobs remain on the server.
    invalidateSearchResults();
    const version = searchGeneration;
    let selection;
    try {
      selection = window.VectorLayers.request();
      const dataset = el('dataset').value;
      if (external.has(dataset)) {
        el('status').textContent = 'Nguồn này chưa hỗ trợ tìm/tải với ROI vector vì chưa có cắt/mask polygon. Đếm metadata vẫn được hỗ trợ.';
        return;
      }
      if (dataset.startsWith('dem_')) return await downloadDem(dataset, selection, version);
      const body = {...copy(opticalSceneBody('vector', null, null, null, null)), ...selection, dataset};
      if (dataset === 's5p') {
        return await window.S5P.search({
          ...selection, start_date:body.start_date, end_date:body.end_date, product:el('s5p_product').value
        });
      }
      if (el('time_series_enabled').checked && series.has(dataset)) {
        const request = {...selection, dataset, start_date:body.start_date, end_date:body.end_date, polarization:'vvvh'};
        if (dataset === 'sentinel2' || dataset === 's1gee') request.platform = window.MatchedFrame?.platform() ?? body.platform ?? null;
        if (dataset === 'sentinel2') {
          if (body.s2_band_preset !== undefined) request.s2_band_preset = body.s2_band_preset;
          if (body.s2_auxiliary_bands !== undefined) request.s2_auxiliary_bands = copy(body.s2_auxiliary_bands);
        }
        return await searchTimeSeries(request);
      }
      if (dataset === 's1gee') return await searchS1Gee(null, null);
      if (optical.has(dataset)) {
        if (['sentinel2', 'landsat'].includes(dataset)) {
          if (!el('least_cloud_enabled').checked) return await searchSceneCatalog();
          return await compositePlan(body, version);
        }
        return await runSceneSearch(body);
      }
      el('status').textContent = 'Dataset này chưa hỗ trợ tìm/tải theo ROI vector.';
    } catch (error) {
      if (!selection || isCurrent(version, selection.vector_roi_id)) el('status').textContent = 'Lỗi: ' + errorText(error);
    }
  }
  window.VectorSearch = Object.freeze({search, isCurrent});
})();
