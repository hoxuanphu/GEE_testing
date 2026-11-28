const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'umbra.js'), 'utf8');

function fixture(fetch) {
  const elements = new Map(), footprints = [];
  const node = () => ({value:'', children:[], attributes:{}, listeners:{},
    setAttribute(k,v) {this.attributes[k]=v;}, replaceChildren(...v) {this.children=v;},
    append(...v) {this.children.push(...v);}, addEventListener(event, callback) {this.listeners[event]=callback;}});
  const get = id => {
    if (!elements.has(id)) elements.set(id, node());
    return elements.get(id);
  };
  let random = 0;
  const window = {crypto:{getRandomValues: bytes => bytes.fill(++random)},
    ResultFootprints:{add(_, footprint) {footprints.push(footprint);}, clear() {}, fit() {}}};
  vm.runInNewContext(source, {document:{getElementById:get, createElement:node},
    window, fetch, AbortController, setTimeout, clearTimeout, Date, Intl});
  get('dataset').value = 'umbra';
  return {get, footprints, api:window.Umbra};
}

const reply = data => ({ok:true, json:async () => data});
const footprint = {type:'Polygon', coordinates:[]};
const result = {search_id:'test', total:51, offset:50, limit:50, parameters:{
  catalog_state:'ready', catalog_skipped:0}, scenes:[{datetime:'2026-01-01T00:00:00Z',
    platform:'Umbra-07', polarizations:['HH'], size_bytes:525000000, footprint,
    download_url:'/api/umbra/search/test/scenes/50/download',
    metadata_url:'/api/umbra/search/test/scenes/50/metadata'}]};

test('shows footprint, downloads the selected snapshot row, and indicates a stale catalogue', async () => {
  const f = fixture(async url => url.endsWith('/catalog')
    ? reply({state:'stale', updated_at:'2026-01-01T00:00:00Z', count:100, skipped:2}) : reply(result));
  await f.api.catalog();
  assert.match(f.get('umbraCatalogStatus').textContent, /bản cập nhật trước/);
  await f.api.search({selection_mode:'point'});
  const row = f.get('umbraScenes').children[0];
  assert.deepEqual(f.footprints, [footprint]);
  const actions = row.children[0].children[1];
  assert.equal(actions.children[0].href, '/api/umbra/search/test/scenes/50/download');
  assert.equal(actions.children[1].href, '/api/umbra/search/test/scenes/50/metadata');
  const download = actions.children[0];
  download.listeners.click();
  const first = download.href;
  assert.match(first, /\?attempt=[\da-f]{32}$/);
  download.listeners.click();
  assert.notEqual(download.href, first);
  assert.equal(f.get('umbraNext').disabled, true);
});

test('discards late results and distinguishes a partial empty search from no coverage', async () => {
  let finish;
  const f = fixture(() => new Promise(resolve => {finish = resolve;}));
  const old = f.api.search({});
  f.get('dataset').value = 's5p';
  f.api.invalidate();
  finish(reply(result));
  await old;
  assert.equal(f.get('umbraResults').hidden, true);
  assert.deepEqual(f.get('umbraScenes').children, []);
  const partial = fixture(async () => reply({...result, total:0, offset:0, scenes:[],
    parameters:{catalog_state:'ready', catalog_skipped:5}}));
  await partial.api.search({});
  assert.match(partial.get('umbraResultStatus').textContent, /chưa đầy đủ/);
});

test('shows a running refresh and disables a duplicate refresh', async () => {
  const f = fixture(async () => reply({state:'refreshing'}));
  await f.api.catalog();
  assert.match(f.get('umbraCatalogStatus').textContent, /Đang đồng bộ/);
  assert.equal(f.get('umbraRefresh').disabled, true);
  f.get('dataset').value = 's5p';
  f.api.invalidate();
});
