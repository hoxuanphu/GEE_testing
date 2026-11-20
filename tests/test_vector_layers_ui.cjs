const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const plain = value => JSON.parse(JSON.stringify(value));

function fixture() {
  class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.attrs = {}; this.listeners = {}; this.value = ''; this.textContent = ''; this.checked = false; this.disabled = false; this.hidden = false; }
    append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
    appendChild(node) { this.append(node); return node; }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    remove() { this.parentElement.children = this.parentElement.children.filter(node => node !== this); }
    setAttribute(key, value) { this.attrs[key] = String(value); }
    getAttribute(key) { return this.attrs[key] ?? null; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(listener => listener !== fn); }
    dispatchEvent(event) { if (!event.target) event.target = this; return Promise.all((this.listeners[event.type] || []).map(fn => fn(event))); }
    focus() { document.activeElement = this; }
    get innerHTML() { throw new Error('HTML rendering is forbidden for vector content'); }
    set innerHTML(value) { throw new Error('HTML rendering is forbidden for vector content'); }
  }
  const panel = new Element(); panel.id = 'vectorLayersPanel';
  const mode = new Element('select'); mode.id = 'selection_mode'; mode.value = 'point';
  const walk = root => [root, ...root.children.flatMap(walk)];
  const document = {activeElement:null, createElement:tag => new Element(tag), getElementById:id => [mode, ...walk(panel)].find(node => node.id === id) || null};
  const map = {layers:new Set(), fits:[], addLayer(layer) { this.layers.add(layer); return this; }, removeLayer(layer) { this.layers.delete(layer); return this; }, hasLayer(layer) { return this.layers.has(layer); }, fitBounds(bounds) { this.fits.push(bounds); }};
  const geoLayers = [];
  const L = {DomEvent:{stopPropagation(event) { event.stopped = true; }}, circleMarker:(latlng, options) => ({latlng, options}), geoJSON(data, options = {}) {
    const features = data.type === 'FeatureCollection' ? data.features : data.type === 'Feature' ? [data] : [{geometry:data}];
    const group = {data, options, leaves:[], addTo(target) { target.addLayer(this); return this; }, getBounds:() => ({isValid:() => true}), setStyle(style) { this.style = style; for (const leaf of this.leaves) leaf.setStyle(style); }, eachLayer(fn) { this.leaves.forEach(fn); }, off() {}, clearLayers() { this.leaves = []; }};
    for (const feature of features) {
      const leaf = {feature, listeners:{}, setStyle(style) { this.style = style; }, on(type, fn) { this.listeners[type] = fn; return this; }, off() { this.listeners = {}; }};
      if (feature.geometry.type === 'Point' && options.pointToLayer) options.pointToLayer(feature, feature.geometry.coordinates);
      if (options.onEachFeature) options.onEachFeature(feature, leaf);
      group.leaves.push(leaf);
    }
    geoLayers.push(group); return group;
  }};
  const fetches = []; let invalidations = 0;
  const window = {};
  const context = {window, document, map, L, console, AbortController, Event:class { constructor(type) { this.type = type; } }, FormData:class { constructor() { this.fields = []; } append(...entry) { this.fields.push(entry); } }, invalidateSearchResults:() => invalidations++, fetch:(url, options) => { const call = {url, options}; fetches.push(call); return new Promise((resolve, reject) => Object.assign(call, {resolve, reject})); }};
  vm.createContext(context);
  const file = 'static/vector-layers.js';
  if (fs.existsSync(file)) vm.runInContext(fs.readFileSync(file, 'utf8'), context);
  const get = id => document.getElementById(id);
  const find = (action, root = panel) => walk(root).find(node => node.getAttribute('data-action') === action);
  const all = (action, root = panel) => walk(root).filter(node => node.getAttribute('data-action') === action);
  const click = node => { assert.ok(node, 'control exists'); return node.dispatchEvent({type:'click'}); };
  const change = (node, checked) => { assert.ok(node, 'control exists'); node.checked = checked; return node.dispatchEvent({type:'change'}); };
  async function upload(payload, filename = payload.filename) {
    assert.ok(window.VectorLayers, 'vector manager is available');
    get('vectorFiles').files = [{name:filename}];
    const pending = get('vectorFiles').dispatchEvent({type:'change'});
    fetches.at(-1).resolve({ok:true, json:async () => payload}); await pending;
  }
  return {api:window.VectorLayers, get, find, all, click, change, upload, map, geoLayers, fetches, mode, panel, walk, invalidations:() => invalidations};
}
const polygon = (id = 'p', name = 'Area') => ({type:'Feature', id, name, layer:'areas', properties:{name}, geometry:{type:'Polygon', coordinates:[[[0,0],[2,0],[2,2],[0,0]]]}});
const point = (id = 'q') => ({type:'Feature', id, name:'Station', layer:'stations', properties:{url:'https://outside.example/icon.png'}, geometry:{type:'Point', coordinates:[1,1]}});
const imported = (id = 'import-one', features = [polygon(), point()]) => ({import_id:id, filename:id + '.geojson', feature_collection:{type:'FeatureCollection', features}, counts:{features:features.length}, warnings:[]});
const roi = (id = 'roi-one', refs = [{import_id:'import-one', feature_ids:['p']}]) => ({roi_id:id, name:'Applied area', geometry:polygon().geometry, bbox:[0,0,2,2], feature_refs:refs});
const succeed = (call, payload) => call.resolve({ok:true, json:async () => payload});

test('imports are visible and unselected, with independent visibility, map selection and safe property text', async () => {
  const f = fixture(); assert.ok(f.api, 'vector manager is available');
  assert.equal(f.fetches.length, 0); assert.throws(() => f.api.request());
  const data = imported(); data.feature_collection.features[0].name = '<img src=x onerror=alert(1)>';
  await f.upload(data);
  assert.equal(f.map.fits.length, 0); assert.equal(f.map.layers.size, 2);
  assert.equal(f.all('feature-select').every(node => !node.checked), true);
  assert.equal(f.all('feature-visible').every(node => node.checked), true);
  await f.change(f.all('feature-visible')[0], false); assert.equal(f.map.layers.size, 1);
  const event = {originalEvent:{}}; f.geoLayers[0].leaves[0].listeners.click(event);
  assert.equal(event.originalEvent.stopped, true); assert.equal(f.all('feature-select')[0].checked, true);
  assert.ok(f.walk(f.panel).some(node => node.textContent.includes('<img src=x onerror=alert(1)>')));
  assert.ok(f.walk(f.panel).some(node => node.textContent.includes('name')));
  assert.equal(f.invalidations(), 0); assert.equal(f.fetches.length, 1);
  await f.change(f.find('file-visible'), false); assert.equal(f.map.layers.size, 0);
  await f.change(f.find('file-visible'), true); assert.equal(f.map.layers.size, 1);
});

test('Apply uses selected polygons across imports and preserves immutable ROI through draft/visibility changes', async () => {
  const f = fixture(); await f.upload(imported()); await f.upload(imported('import-two', [polygon('m', 'Islands')]));
  await f.click(f.find('select-all')); await f.change(f.all('feature-select')[2], true);
  const pending = f.click(f.get('vectorApplyRoi'));
  const call = f.fetches.at(-1); assert.equal(call.url, '/api/vectors/rois');
  assert.deepEqual(JSON.parse(call.options.body), {selections:[{import_id:'import-one', feature_ids:['p']},{import_id:'import-two', feature_ids:['m']}]});
  succeed(call, roi('active', [{import_id:'import-one', feature_ids:['p']},{import_id:'import-two', feature_ids:['m']}])); await pending;
  assert.equal(f.mode.value, 'vector'); assert.equal(f.invalidations(), 1);
  assert.deepEqual(plain(f.api.request()), {selection_mode:'vector', vector_roi_id:'active'});
  assert.match(f.get('vectorRoiStatus').textContent, /1/);
  const copied = f.api.getRoi(); copied.geometry.coordinates = []; assert.ok(f.api.getRoi().geometry.coordinates.length);
  await f.click(f.find('select-none')); await f.change(f.find('file-visible'), false);
  assert.equal(f.api.getRoi().roi_id, 'active'); assert.equal(f.invalidations(), 1);
  const outline = f.geoLayers.at(-1); assert.equal(f.map.hasLayer(outline), true);
  f.mode.value = 'point'; await f.mode.dispatchEvent({type:'change'}); assert.equal(f.map.hasLayer(outline), false);
  f.mode.value = 'vector'; await f.mode.dispatchEvent({type:'change'}); assert.equal(f.map.hasLayer(outline), true);
  f.api.clearRoi(); assert.equal(f.api.getRoi(), null); assert.equal(f.map.hasLayer(outline), false); assert.equal(f.invalidations(), 2);
});

test('rows are paged at 50 and name/type filtering and explicit fit work without fetching resources', async () => {
  const f = fixture(); await f.upload(imported('large', [...Array.from({length:65}, (_, i) => polygon(String(i), 'Area ' + i)), point()]));
  assert.equal(f.all('feature-select').length, 50);
  await f.click(f.find('next-page')); assert.equal(f.all('feature-select').length, 16);
  const filter = f.find('filter-name'); filter.value = 'Area 64'; await filter.dispatchEvent({type:'input'});
  assert.equal(f.all('feature-select').length, 1);
  filter.value = ''; await filter.dispatchEvent({type:'input'});
  const type = f.find('filter-type'); type.value = 'point'; await type.dispatchEvent({type:'change'});
  assert.equal(f.all('feature-select').length, 1); await f.click(f.find('fit')); assert.equal(f.map.fits.length, 1);
  assert.equal(f.fetches.length, 1);
});

test('removing an applied import clears ROI and a pending Apply cannot restore it', async () => {
  const f = fixture(); await f.upload(imported()); await f.click(f.find('select-all'));
  let pending = f.click(f.get('vectorApplyRoi')); succeed(f.fetches.at(-1), roi()); await pending;
  pending = f.click(f.get('vectorApplyRoi')); const applyCall = f.fetches.at(-1);
  const removal = f.click(f.find('remove')); const deleteCall = f.fetches.at(-1);
  assert.equal(deleteCall.options.method, 'DELETE'); succeed(deleteCall, {deleted:true}); await removal;
  succeed(applyCall, roi('late')); await pending;
  assert.equal(f.api.getRoi(), null); assert.equal(f.map.layers.size, 0); assert.throws(() => f.api.request());
  assert.equal(f.invalidations(), 2);
});

test('mode change and Clear discard late Apply responses even when mode returns', async () => {
  const f = fixture(); await f.upload(imported()); await f.click(f.find('select-all'));
  let pending = f.click(f.get('vectorApplyRoi')); const first = f.fetches.at(-1);
  f.mode.value = 'rectangle'; await f.mode.dispatchEvent({type:'change'});
  f.mode.value = 'point'; await f.mode.dispatchEvent({type:'change'});
  succeed(first, roi()); await pending; assert.equal(f.api.getRoi(), null); assert.equal(f.invalidations(), 0);
  assert.doesNotMatch(f.get('vectorRoiStatus').textContent, /Đang tạo/);
  pending = f.click(f.get('vectorApplyRoi')); const second = f.fetches.at(-1); f.api.clearRoi();
  succeed(second, roi()); await pending; assert.equal(f.api.getRoi(), null);
});

test('nonpolygon selection cannot apply ROI and a saved ROI is clearly inactive outside vector mode', async () => {
  const f = fixture(); await f.upload(imported('mixed', [point(), polygon('multi')]));
  await f.change(f.all('feature-select')[0], true);
  assert.equal(f.get('vectorApplyRoi').disabled, true);
  await f.click(f.get('vectorApplyRoi')); assert.equal(f.fetches.length, 1);
  await f.change(f.all('feature-select')[1], true);
  const pending = f.click(f.get('vectorApplyRoi')); succeed(f.fetches.at(-1), roi()); await pending;
  f.mode.value = 'point'; await f.mode.dispatchEvent({type:'change'});
  assert.match(f.get('vectorRoiStatus').textContent, /đã lưu|chưa kích hoạt/i);
  assert.equal(f.api.getRoi().roi_id, 'roi-one');
});

test('multiple files complete independently and removed pending imports do not reappear', async () => {
  const f = fixture(); assert.ok(f.api, 'vector manager is available');
  f.get('vectorFiles').files = [{name:'good.geojson'}, {name:'bad.kml'}];
  const pending = f.get('vectorFiles').dispatchEvent({type:'change'});
  assert.equal(f.fetches.length, 2);
  succeed(f.fetches[1], imported('second')); f.fetches[0].resolve({ok:false, json:async () => ({detail:'Invalid geometry <script>bad</script>'})}); await pending;
  assert.equal(f.all('feature-select').length, 2); assert.ok(f.walk(f.panel).some(node => node.textContent.includes('Invalid geometry')));
  f.get('vectorFiles').files = [{name:'slow.geojson'}]; const slow = f.get('vectorFiles').dispatchEvent({type:'change'}); const call = f.fetches.at(-1);
  const remove = f.all('remove').at(-1); await f.click(remove); succeed(call, imported('slow')); await slow;
  assert.equal(f.all('feature-select').length, 2); assert.equal(f.map.layers.size, 2);
});

test('Apply and delete errors retain existing ROI/layers and expose safe messages', async () => {
  const f = fixture(); await f.upload(imported()); await f.click(f.find('select-all'));
  let pending = f.click(f.get('vectorApplyRoi')); succeed(f.fetches.at(-1), roi()); await pending;
  pending = f.click(f.get('vectorApplyRoi')); f.fetches.at(-1).resolve({ok:false, json:async () => ({detail:{code:'vector_limit', message:'Too many vertices <b>limit</b>'}})}); await pending;
  assert.equal(f.api.getRoi().roi_id, 'roi-one'); assert.match(f.get('vectorRoiStatus').textContent, /Too many vertices/);
  const removal = f.click(f.find('remove')); f.fetches.at(-1).reject(new Error('network unavailable')); await removal;
  assert.equal(f.api.getRoi().roi_id, 'roi-one'); assert.equal(f.all('feature-select').length, 2);
  assert.equal(f.get('vectorApplyRoi').disabled, false);
});

test('repeated removals release detached controls and Leaflet handlers while another file stays usable', async () => {
  const f = fixture(); await f.upload(imported('keep', [polygon('kept')]));
  for (let index = 0; index < 3; index++) {
    await f.upload(imported(`removed-${index}`));
    const remove = f.all('remove').at(-1), select = f.all('feature-select').at(-1), fit = f.all('fit').at(-1);
    const card = f.get('vectorImportList').children.at(-1);
    const layers = f.geoLayers.slice(-2), leaves = layers.flatMap(layer => layer.leaves);
    const pending = f.click(remove); succeed(f.fetches.at(-1), {deleted:true}); await pending;
    assert.equal(card.children.length, 0, 'removed cards release their feature controls');
    assert.equal(leaves.every(leaf => Object.keys(leaf.listeners).length === 0), true, 'removed map shapes release click closures');
    assert.equal(layers.every(layer => layer.leaves.length === 0), true, 'removed GeoJSON groups release child layers');
    assert.equal((select.listeners.change || []).length, 0); assert.equal((fit.listeners.click || []).length, 0);
    await f.click(fit); assert.equal(f.map.fits.length, 0, 'detached controls cannot act on removed data');
    assert.equal(f.all('feature-select').length, 1); assert.equal(f.map.layers.size, 1);
  }
  await f.click(f.find('select-all'));
  const pending = f.click(f.get('vectorApplyRoi'));
  assert.deepEqual(JSON.parse(f.fetches.at(-1).options.body), {selections:[{import_id:'keep',feature_ids:['kept']}]});
  succeed(f.fetches.at(-1), roi('kept-roi', [{import_id:'keep',feature_ids:['kept']}])); await pending;
  assert.equal(f.api.request().vector_roi_id, 'kept-roi');
});

test('canceling an upload releases its card and late completion cleans up only that server import', async () => {
  const f = fixture(); await f.upload(imported('keep', [polygon('kept')]));
  f.get('vectorFiles').files = [{name:'pending.geojson'}];
  const upload = f.get('vectorFiles').dispatchEvent({type:'change'}), call = f.fetches.at(-1);
  const remove = f.all('remove').at(-1), card = f.get('vectorImportList').children.at(-1);
  await f.click(remove);
  assert.equal(call.options.signal.aborted, true); assert.equal(card.children.length, 0);
  assert.equal((remove.listeners.click || []).length, 0);
  succeed(call, imported('late')); await upload;
  assert.equal(f.fetches.at(-1).url, '/api/vectors/imports/late'); assert.equal(f.fetches.at(-1).options.method, 'DELETE');
  assert.equal(f.all('feature-select').length, 1); assert.equal(f.map.layers.size, 1);
  await f.click(f.find('fit')); assert.equal(f.map.fits.length, 1);
});

test('a failed delete retains listeners and data until a successful retry releases them', async () => {
  const f = fixture(); await f.upload(imported());
  const remove = f.find('remove'), fit = f.find('fit'), card = f.get('vectorImportList').children[0];
  let pending = f.click(remove); f.fetches.at(-1).reject(new Error('temporary failure')); await pending;
  assert.equal(remove.disabled, false); assert.ok(card.children.length > 0);
  await f.click(fit); assert.equal(f.map.fits.length, 1);
  await f.change(f.all('feature-select')[0], true); assert.equal(f.get('vectorApplyRoi').disabled, false);
  pending = f.click(remove); succeed(f.fetches.at(-1), {deleted:true}); await pending;
  assert.equal(card.children.length, 0); assert.equal(f.map.layers.size, 0); assert.equal(f.get('vectorApplyRoi').disabled, true);
});
