const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const source = html.slice(html.indexOf('let marker = null;'), html.indexOf('function updateCompositeVisibility()'));

function fixture(fetch) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: id === 'selection_mode' ? 'commune' : '', textContent: '', style: {},
      options: [], closest() { return {style: {}}; },
      replaceChildren(...children) { this.options = children; },
      add(option) { this.options.push(option); },
      appendChild(option) { this.options.push(option); },
    });
    return elements.get(id);
  }
  const visible = new Set();
  const fits = [];
  const overlays = [];
  const map = {
    hasLayer: layer => visible.has(layer),
    removeLayer: layer => visible.delete(layer),
    fitBounds: bounds => fits.push(bounds),
  };
  const context = vm.createContext({
    $: element, fetch, AbortController, encodeURIComponent, Event,
    Option: function(text, value) { this.text = text; this.value = value; },
    document: {createElement: () => ({textContent: ''})},
    updateCompositeVisibility() {}, map,
    mapLayerControl: {addOverlay: (...args) => overlays.push(args)},
    L: {geoJSON(feature, options) {
      const layer = {
        feature, addTo() { visible.add(this); return this; }, bringToFront() {},
        getBounds: () => feature.properties.id,
      };
      return layer;
    }},
  });
  vm.runInContext(source, context);
  return {context, element, visible, fits, overlays,
    run: code => vm.runInContext(code, context)};
}

function feature(id, name = id) {
  return {type: 'Feature', properties: {id, name, province_name: 'An Giang'},
    geometry: {type: 'Polygon', coordinates: []}};
}

test('selecting a commune draws its geometry and fits the map', async () => {
  const f = fixture(async url => ({ok: true, json: async () => feature(url.split('/').pop())}));
  f.element('commune_id').value = '001';
  await f.run("selectCommune('001')");
  assert.equal(f.visible.size, 1);
  assert.deepEqual(f.fits, ['001']);
  assert.equal(f.element('communeInfo').textContent, '001 — An Giang');
  f.element('commune_id').value = '';
  await f.run("selectCommune('')");
  assert.equal(f.visible.size, 0);
});

test('place search matches unaccented names and preserves selected commune', () => {
  const f = fixture();
  f.run("communeRows = [{id:'1',name:'An Biên',province_name:'An Giang'}, {id:'2',name:'Đà Lạt',province_name:'Lâm Đồng'}]");
  f.element('commune_search').value = 'da lat';
  f.run('filterCommunes()');
  assert.deepEqual(f.element('commune_id').options.map(option => option.value), ['', '2']);
  f.element('commune_id').value = '1';
  f.element('commune_search').value = 'lam dong';
  f.run('filterCommunes()');
  assert.equal(f.element('commune_id').value, '1');
  assert.equal(f.element('commune_id').options.length, 3);
});

test('late geometry from an older selection cannot replace the latest selection', async () => {
  const pending = [];
  const f = fixture(() => new Promise(resolve => pending.push(resolve)));
  f.element('commune_id').value = 'old';
  const old = f.run("selectCommune('old')");
  f.element('commune_id').value = 'new';
  const current = f.run("selectCommune('new')");
  pending[1]({ok: true, json: async () => feature('new')});
  await current;
  pending[0]({ok: true, json: async () => feature('old')});
  await old;
  assert.deepEqual(f.fits, ['new']);
  assert.equal(f.visible.size, 1);
});

test('switching to point mode cancels commune rendering but keeps province context', async () => {
  let resolveGeometry;
  const f = fixture(() => new Promise(resolve => { resolveGeometry = resolve; }));
  const provinces = {addTo() { f.visible.add(this); }};
  f.context.provinces = provinces;
  f.run('provinceLayer = provinces');
  f.visible.add(provinces);
  f.element('commune_id').value = '001';
  const request = f.run("selectCommune('001')");
  f.element('selection_mode').value = 'point';
  await f.run('setSelectionMode()');
  resolveGeometry({ok: true, json: async () => feature('001')});
  await request;
  assert.equal(f.visible.size, 1);
  assert.ok(f.visible.has(provinces));
  assert.equal(f.fits.length, 0);
});

test('administrative mode removes point marker and ROI', async () => {
  const f = fixture();
  f.context.stale = {};
  f.run('marker = stale; roiLayer = stale; provinceLayer = {addTo() {}}; communesLoaded = true');
  f.visible.add(f.context.stale);
  await f.run('setSelectionMode()');
  assert.equal(f.run('marker'), null);
  assert.equal(f.run('roiLayer'), null);
  assert.equal(f.visible.has(f.context.stale), false);
});

test('failed commune fetch reports an error without retaining old geometry', async () => {
  const f = fixture(async () => ({ok: false}));
  f.element('commune_id').value = '001';
  await f.run("selectCommune('001')");
  assert.equal(f.visible.size, 0);
  assert.match(f.element('communeInfo').textContent, /Không thể tải địa giới/);
});
