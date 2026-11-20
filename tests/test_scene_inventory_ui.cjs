const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const plain = value => JSON.parse(JSON.stringify(value));

function fixture() {
  const source = fs.readFileSync('static/scene-inventory.js', 'utf8');
  class Element {
    constructor() { this.value = ''; this.hidden = false; this.disabled = false; this.textContent = ''; this.attrs = {}; this.listeners = {}; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    dispatchEvent(event) { for (const fn of this.listeners[event.type] || []) fn(event); }
    setAttribute(key, value) { this.attrs[key] = value; }
    removeAttribute(key) { delete this.attrs[key]; if (key === 'href') this.href = ''; }
  }
  const elements = new Map();
  const get = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  const document = new Element(); document.getElementById = get;
  for (const [id, value] of Object.entries({dataset:'sentinel2', selection_mode:'point', lon:'107', lat:'10', radius_m:'2000',
      rotation_deg:'37', start_date:'2026-11-01', end_date:'2026-11-02', mfPlatform:'C', mfPolarization:'hh',
      mfOrbitPass:'DESCENDING', mfRelativeOrbit:'42', s2_source:'cdse', s3_product_type:'OL_2_WRR', s5p_product:'NO2'})) get(id).value = value;
  let selectionSource = 'manual';
  const window = new Element(); window.SelectionState = {
    getSource: () => selectionSource,
    getReferenceDraft: () => ({reference_id:'saved-reference'})
  };
  const rect = {center_lon:107, center_lat:10, width_m:4000, height_m:2000, rotation_deg:42};
  const fetches = [];
  const context = {window, document, console, AbortController, URL, location:{origin:'http://localhost'},
    rectangleEditor:{get:() => rect},
    fetch:(url, options) => { const call = {url, options}; fetches.push(call); return new Promise((resolve, reject) => Object.assign(call, {resolve, reject})); }};
  vm.createContext(context); vm.runInContext(source, context);
  return {get, api:window.SceneInventory, fetches, document, window, setSource:value => { selectionSource = value; }};
}
const succeed = (call, total=123) => call.resolve({ok:true, json:async () => ({total, search_id:'snapshot',
  csv_url:'/api/scene-inventory/snapshot/csv', parameters:{source:'gee', scene_kind:'scene'}})});

test('count is explicit, keeps selected ROI/platform/dates, ignores cloud thresholds and publishes CSV link', async () => {
  const f = fixture();
  assert.equal(f.fetches.length, 0);
  f.get('cloud_max').value = '10';
  const pending = f.api.count();
  assert.equal(f.get('btnCountImages').disabled, true);
  const request = plain(JSON.parse(f.fetches[0].options.body));
  assert.equal(request.rotation_deg, 37); assert.equal(request.platform, 'C');
  assert.equal(request.end_date, '2026-11-02'); assert.equal(request.lon, 107);
  assert.equal('cloud_max' in request, false); assert.equal('merge_scenes' in request, false);
  succeed(f.fetches[0]); await pending;
  assert.match(f.get('imageCountStatus').textContent, /123/);
  assert.equal(f.get('imageInventoryCsv').href, '/api/scene-inventory/snapshot/csv');
  assert.equal(f.get('imageInventoryCsv').hidden, false);
  assert.equal(f.get('btnCountImages').disabled, false);
});

test('rectangle/source and reference geometry are distinct, including SAR selection', async () => {
  const f = fixture();
  f.get('selection_mode').value = 'rectangle'; f.get('dataset').value = 's2aws';
  let pending = f.api.count(); let body = JSON.parse(f.fetches[0].options.body);
  assert.equal(body.width_m, 4000); assert.equal(body.source, 'aws'); assert.equal('radius_m' in body, false);
  succeed(f.fetches[0]); await pending;
  f.setSource('reference'); f.get('dataset').value = 's1gee'; f.get('mfPlatform').value = 'D';
  pending = f.api.count(); body = JSON.parse(f.fetches[1].options.body);
  assert.equal(body.selection_mode, 'reference'); assert.equal(body.reference_id, 'saved-reference');
  assert.equal(body.polarization, 'hh'); assert.equal(body.platform, 'D');
  assert.equal(body.orbit_pass, 'DESCENDING'); assert.equal(body.relative_orbit, 42);
  assert.equal('center_lon' in body, false);
  succeed(f.fetches[1]); await pending;
});

test('changing selection aborts the old request and hides its CSV even if an old response arrives', async () => {
  const f = fixture();
  const old = f.api.count();
  f.get('start_date').value = '2026-10-01'; f.get('start_date').dispatchEvent({type:'change'});
  assert.equal(f.fetches[0].options.signal.aborted, true);
  const fresh = f.api.count();
  succeed(f.fetches[0], 999); await old;
  assert.equal(f.get('btnCountImages').disabled, true);
  assert.equal(f.get('imageInventoryCsv').hidden, true);
  succeed(f.fetches[1], 0); await fresh;
  assert.match(f.get('imageCountStatus').textContent, /0/);
  assert.equal(f.get('btnCountImages').disabled, false);
});

test('administrative geometry omits point fields; provider failure restores controls without stale download', async () => {
  const f = fixture(); f.get('selection_mode').value = 'province'; f.get('province_id').value = 'test';
  const pending = f.api.count(); const body = JSON.parse(f.fetches[0].options.body);
  assert.equal(body.province_id, 'test'); assert.equal('lon' in body, false);
  f.fetches[0].resolve({ok:false, json:async () => ({detail:'Catalogue chưa trả đủ trang'})}); await pending;
  assert.equal(f.get('btnCountImages').disabled, false);
  assert.match(f.get('imageCountStatus').textContent, /chưa trả đủ trang/);
  assert.equal(f.get('imageInventoryCsv').hidden, true);
});

test('blank coordinates fail locally; static sources hide count; untrusted CSV links are rejected', async () => {
  const f = fixture(); f.get('lon').value = '';
  await f.api.count(); assert.equal(f.fetches.length, 0);
  assert.match(f.get('imageCountStatus').textContent, /tọa độ|kinh độ/i);
  f.get('dataset').value = 'dem_srtm'; f.api.invalidate();
  assert.equal(f.get('imageInventoryControls').hidden, true);
  f.get('dataset').value = 'sentinel2'; f.get('lon').value = '107'; f.api.invalidate();
  const pending = f.api.count();
  f.fetches[0].resolve({ok:true, json:async () => ({total:4, csv_url:'https://outside.example/file.csv', parameters:{}})});
  await pending;
  assert.equal(f.get('imageInventoryCsv').hidden, true);
});

test('vector inventory sends only immutable ROI id without hidden point or rectangle fields', async () => {
  const f = fixture();
  f.get('selection_mode').value = 'vector'; f.get('lon').value = '';
  f.get('dataset').value = 's2aws';
  f.window.VectorLayers = {request:() => ({selection_mode:'vector',vector_roi_id:'saved-vector'})};
  const pending = f.api.count();
  assert.equal(f.fetches.length, 1);
  const body = JSON.parse(f.fetches[0].options.body);
  assert.equal(body.vector_roi_id, 'saved-vector');
  assert.equal(body.source, 'aws');
  for (const key of ['lon','lat','radius_m','center_lon','reference_id']) assert.equal(key in body, false);
  succeed(f.fetches[0]); await pending;
});
