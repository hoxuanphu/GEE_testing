const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const plain = value => JSON.parse(JSON.stringify(value));

function fixture() {
  const elements = new Map(), calls = [], downloads = [], revoked = [];
  const get = id => {
    if (!elements.has(id)) elements.set(id, {value:'', checked:false, disabled:false, hidden:false, style:{}, textContent:'', removeAttribute(key) { delete this[key]; }});
    return elements.get(id);
  };
  get('selection_mode').value = 'vector'; get('dataset').value = 'sentinel2';
  get('start_date').value = '2026-11-01'; get('end_date').value = '2026-11-02';
  get('least_cloud_enabled').checked = true; get('s5p_product').value = 'NO2';
  let active = 'roi-a';
  const request = () => { if (!active) throw new Error('Apply polygon first'); return {selection_mode:'vector', vector_roi_id:active}; };
  const window = {VectorLayers:{request, getRoi:() => active ? {roi_id:active} : null}, MatchedFrame:{platform:() => 'C'},
    S5P:{search:body => { calls.push({kind:'s5p', body:plain(body)}); return Promise.resolve(); }}};
  const pending = kind => body => { const call = {kind, body:plain(body)}; calls.push(call); return new Promise((resolve, reject) => Object.assign(call, {resolve, reject})); };
  const context = {window, document:{getElementById:get, body:{appendChild() {}}, createElement:() => ({click() { downloads.push(this); }, remove() {}})},
    URL:{createObjectURL:() => 'blob:local-result', revokeObjectURL:url => revoked.push(url)}, URLSearchParams, setTimeout:fn => { fn(); }, console,
    searchGeneration:0, pendingCompositeBody:null,
    invalidateSearchResults() { context.searchGeneration++; for (const id of ['result','s1result','plan']) get(id).style.display = 'none'; get('preview').removeAttribute('src'); context.pendingCompositeBody = null; get('status').textContent = ''; get('btnSearch').disabled = false; },
    opticalSceneBody:() => ({...request(), dataset:get('dataset').value, start_date:get('start_date').value, end_date:get('end_date').value,
      platform:'C', s2_auxiliary_bands:['NDVI'], merge_scenes:true, composite_method:'median'}),
    searchTimeSeries:body => { calls.push({kind:'timeseries', body:plain(body)}); return Promise.resolve(); },
    runSceneSearch:body => { calls.push({kind:'scene', body:plain(body)}); return Promise.resolve(); },
    searchSceneCatalog:() => { calls.push({kind:'catalog'}); return Promise.resolve(); },
    searchS1Gee:(lon,lat) => { calls.push({kind:'sar', lon,lat}); return Promise.resolve(); },
    requestCompositePlan:pending('plan'), renderCompositePlan:plan => calls.push({kind:'render-plan', plan}),
    fetch:(url, options) => { const call = {kind:'fetch', url,options}; calls.push(call); return new Promise((resolve,reject) => Object.assign(call,{resolve,reject})); }};
  vm.createContext(context);
  if (fs.existsSync('static/vector-search.js')) vm.runInContext(fs.readFileSync('static/vector-search.js','utf8'),context);
  return {api:window.VectorSearch,get,calls,context,downloads,revoked,setRoi:value => { active=value; }};
}

test('S2/Landsat plan is explicit, keeps polygon snapshot and waits for existing confirm handler', async () => {
  const f = fixture(); assert.ok(f.api, 'vector search helper is available');
  f.get('result').style.display = 'block'; f.get('preview').src = 'old-preview';
  const pending = f.api.search();
  assert.equal(f.get('result').style.display, 'none'); assert.equal(f.get('preview').src, undefined);
  assert.equal(f.get('btnSearch').disabled, true); assert.equal(f.calls[0].kind, 'plan');
  assert.equal(f.calls[0].body.vector_roi_id,'roi-a');
  f.calls[0].resolve({selected_scene_count:2}); await pending;
  assert.equal(f.calls[1].kind,'render-plan'); assert.equal(f.context.pendingCompositeBody.vector_roi_id,'roi-a');
  assert.equal(f.get('btnSearch').disabled,false); assert.equal(f.calls.some(call => call.kind === 'scene'),false);
});

test('cloud listing, nonplan optical and SAR dispatch to their existing renderers', async () => {
  const f=fixture(); assert.ok(f.api, 'vector search helper is available');
  f.get('least_cloud_enabled').checked=false; await f.api.search(); assert.equal(f.calls.at(-1).kind,'catalog');
  f.get('dataset').value='nicfi'; await f.api.search(); assert.equal(f.calls.at(-1).kind,'scene');
  f.get('dataset').value='dynamic_world'; await f.api.search(); assert.equal(f.calls.at(-1).body.selection_mode,'vector');
  f.get('dataset').value='s1gee'; await f.api.search(); assert.deepEqual(f.calls.at(-1),{kind:'sar',lon:null,lat:null});
});

test('time series and S5P bodies whitelist fields and preserve satellite selection', async () => {
  const f=fixture(); assert.ok(f.api, 'vector search helper is available');
  f.get('time_series_enabled').checked=true; await f.api.search();
  assert.deepEqual(f.calls.at(-1).body,{selection_mode:'vector',vector_roi_id:'roi-a',dataset:'sentinel2',start_date:'2026-11-01',end_date:'2026-11-02',polarization:'vvvh',platform:'C',s2_auxiliary_bands:['NDVI']});
  f.get('dataset').value='s1gee'; await f.api.search();
  assert.equal(f.calls.at(-1).body.platform,'C'); assert.equal('s2_auxiliary_bands' in f.calls.at(-1).body,false);
  f.get('dataset').value='s5p'; await f.api.search();
  assert.deepEqual(f.calls.at(-1).body,{selection_mode:'vector',vector_roi_id:'roi-a',start_date:'2026-11-01',end_date:'2026-11-02',product:'NO2'});
});

test('external vector search is explicitly blocked without falling back to point or fetching', async () => {
  const f=fixture(); assert.ok(f.api, 'vector search helper is available');
  for(const dataset of ['s2','s2aws','s1','s1slc','s3']) {
    f.get('dataset').value=dataset; await f.api.search();
    assert.match(f.get('status').textContent,/polygon|vector/i); assert.match(f.get('status').textContent,/chưa hỗ trợ/i);
  }
  assert.equal(f.calls.length,0);
  f.setRoi(null); await f.api.search(); assert.equal(f.calls.length,0); assert.match(f.get('status').textContent,/Apply polygon/);
});

test('late plan success/failure cannot replace a changed ROI or another search generation', async () => {
  const f=fixture(); assert.ok(f.api, 'vector search helper is available');
  let pending=f.api.search(); const first=f.calls.at(-1); f.setRoi('roi-b'); first.resolve({}); await pending;
  assert.equal(f.context.pendingCompositeBody,null); assert.equal(f.calls.filter(call=>call.kind==='render-plan').length,0);
  pending=f.api.search(); const second=f.calls.at(-1); f.context.searchGeneration++; f.get('status').textContent='new search'; second.reject(new Error('old failure')); await pending;
  assert.equal(f.get('status').textContent,'new search');
});

test('DEM downloads use only vector reference and dataset, retain filename and discard late blobs', async () => {
  const f=fixture(); assert.ok(f.api, 'vector search helper is available'); f.get('dataset').value='dem_srtm';
  let pending=f.api.search(); let call=f.calls.at(-1);
  const url=new URL(call.url,'http://localhost');
  assert.deepEqual(Object.fromEntries(url.searchParams),{dataset:'srtm',selection_mode:'vector',vector_roi_id:'roi-a'});
  call.resolve({ok:true,blob:async()=>({}),headers:{get:()=> 'attachment; filename="SRTM_polygon.tif"'}}); await pending;
  assert.equal(f.downloads[0].download,'SRTM_polygon.tif'); assert.equal(f.revoked.length,1); assert.equal(f.get('btnSearch').disabled,false);
  pending=f.api.search(); call=f.calls.at(-1); f.setRoi('roi-b');
  call.resolve({ok:true,blob:async()=>({}),headers:{get:()=> ''}}); await pending;
  assert.equal(f.downloads.length,1);
});

test('DEM errors and missing ROI are visible safe text, with controls restored', async () => {
  const f=fixture(); assert.ok(f.api, 'vector search helper is available'); f.get('dataset').value='dem_srtm';
  const pending=f.api.search(); f.calls.at(-1).resolve({ok:false,json:async()=>({detail:'Provider failed <script>bad</script>'})}); await pending;
  assert.match(f.get('status').textContent,/Provider failed <script>/); assert.equal(f.get('btnSearch').disabled,false);
  f.setRoi(null); await f.api.search(); assert.equal(f.calls.length,1);
});
