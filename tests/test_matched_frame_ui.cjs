const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static', 'index.html'), 'utf8');
const script = fs.readFileSync(path.join(root, 'static', 'matched-frame.js'), 'utf8');
const workspace = fs.readFileSync(path.join(root, 'static', 'workspace.js'), 'utf8');
const css = fs.readFileSync(path.join(root, 'static', 'workspace.css'), 'utf8');
const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]).find(code => code.includes('const $ ='));
const API = '/api/matched-frame';
const STORAGE = 'satellite-workspace-matched-frame-job';
const plain = value => JSON.parse(JSON.stringify(value));
const response = (data, status = 200) => ({ok:status < 400, status, headers:{get:() => ''}, json:async () => data});
const frame = id => ({reference_id:id, metadata:{crs:'EPSG:32648', transform:[3,0,398712,0,-3,2406162], width:1508, height:1157, band_descriptions:['B4'], dtype:'float32', linear_units:'metre'}, footprint_geojson:{type:'Polygon',coordinates:[[[105,12],[106,12],[106,13],[105,13],[105,12]]]}, compatible_grid_modes:['match_extent','match_grid'], warnings:['Pixel nguồn không tăng chi tiết khi nội suy.']});
const scene = (index, extra = {}) => ({scene_token:`scene-${index}`, asset_id:`COPERNICUS/S2_SR_HARMONIZED/${index}`, date:'2026-10-01', timestamp:'2026-10-01T02:00:00Z', platform:'Sentinel-2A', tile:'48PXS', orbit_pass:null, relative_orbit:null, band_coverage_pct:100, roi_cloud_pct:0, roi_shadow_pct:2, roi_cloud_shadow_pct:2, eligible:true, warnings:[], preview_url:`${API}/searches/search-1/scenes/scene-${index}/preview`, ...extra});
const preflightData = {preflight_id:'preflight-1', preflight_version:2, can_start:true, blocking_reason:null, selection_quality:null, digest:'exact-digest', output_grid:{crs:'EPSG:32648', transform:[9.986754966887418,0,398712,0,-9.974137931034482,2406162], width:453,height:348,units:'metre'}, estimate:{uncompressed_bytes:7566912,disk_reserve_bytes:536870912}, warnings:['Coverage trước tải chỉ là ước lượng.']};
const mergeQuality = ids => ({measurement_version:1,method:'output_grid_mask',estimated:true,band_coverage_pct:100,valid_pixel_count:100,total_pixels:100,ordered_asset_ids:ids,roi_fingerprint:'roi',merge_policy:'primary_first_fill_nodata'});
const jobData = (status = 'running', extra = {}) => ({job_id:'job-1', status, total:2, processed:0, succeeded:0, failed:0, stage:'export', scenes:[], ...extra});
const tick = () => new Promise(setImmediate);
class Element {
  constructor(id = '', tag = 'div') {
    this.id=id; this.tagName=tag.toUpperCase(); this.value=''; this.checked=false; this.disabled=false; this.hidden=false;
    this.style={}; this.dataset={}; this.children=[]; this.listeners={}; this.attributes={}; this.files=[]; this.options=[];
    this.classList={toggle() {}}; this._text='';
  }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(value) { this._html=value; this.children=[]; }
  get innerHTML() { return this._html || ''; }
  append(...children) {
    for (const child of children) {
      if (child.parentElement) child.parentElement.children=child.parentElement.children.filter(value => value !== child);
      child.parentElement=this;
      this.children.push(child);
    }
  }
  insertBefore(child, next) {
    if (child.parentElement) child.parentElement.children=child.parentElement.children.filter(value => value !== child);
    const index=this.children.indexOf(next);
    this.children.splice(index < 0 ? this.children.length : index,0,child);
    child.parentElement=this;
  }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this._text=''; this.children=children; }
  add(child) { this.append(child); }
  addEventListener(name, fn) { (this.listeners[name] ||= []).push(fn); }
  removeEventListener(name, fn) { this.listeners[name]=(this.listeners[name] || []).filter(value => value !== fn); }
  async fire(name, extra = {}) {
    for (const fn of this.listeners[name] || []) await fn({target:this,type:name,preventDefault() {},...extra});
    await tick();
  }
  dispatchEvent(event) {
    for (const fn of this.listeners[event.type] || []) fn({target:this,preventDefault() {},...event});
    return true;
  }
  setAttribute(name, value) { this.attributes[name]=String(value); }
  getAttribute(name) { return this.attributes[name] || null; }
  removeAttribute(name) { delete this.attributes[name]; delete this[name]; }
  focus() { this.focused=true; }
  closest() { return new Element(); }
  querySelectorAll(selector) { return this.children.flatMap(child => [...(child.tagName === selector.toUpperCase() ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.children.find(child => child.tagName === selector.toUpperCase()) || this.children.map(child => child.querySelector(selector)).find(Boolean); }
}
function fixture({handler, saved, blocked = false, legacy = false} = {}) {
  const elements=new Map(), requests=[], fits=[], views=[], visible=new Set(), timers=new Map(), writes=[];
  let timerId=0;
  const el = id => { if (!elements.has(id)) elements.set(id,new Element(id)); return elements.get(id); };
  for (const match of html.matchAll(/<(\w+)\b[^>]*id="([^"]+)"[^>]*>/g)) {
    const element=el(match[2]); element.tagName=match[1].toUpperCase();
    element.value=(match[0].match(/\bvalue="([^"]*)"/) || [,''])[1];
    element.checked=/\bchecked\b/.test(match[0]); element.hidden=/\bhidden\b/.test(match[0]);
  }
  for (const match of html.matchAll(/<select\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    const element=el(match[1]);
    element.options=[...match[2].matchAll(/<option value="([^"]*)"[^>]*>(.*?)<\/option>/g)].map(value => Object.assign(new Element(), {value:value[1], textContent:value[2]}));
    element.value=element.options[0]?.value || '';
  }
  el('selection_mode').value=legacy ? 'point' : 'reference';
  el('start_date').value='2026-10-01'; el('end_date').value='2026-10-22';
  const doc=Object.assign(new Element(), {hidden:false, documentElement:new Element(), body:new Element(), createElement:tag => new Element('',tag), getElementById:el,
    querySelectorAll:selector => selector.includes('[aria-invalid]') ? [...elements.values()].filter(element => element.attributes['aria-invalid']) : [...elements.values()].filter(element => ['INPUT','SELECT'].includes(element.tagName))});
  const win=new Element(); win.SatelliteWorkspace={showView:value => views.push(value)};
  const container=new Element(); container.ownerDocument=doc;
  doc.body.classList = {add() {}, remove() {}};
  const map={createPane:() => new Element(), setView() {return this;}, on() {}, off() {}, fitBounds:value => fits.push(value), hasLayer:value => visible.has(value), removeLayer:value => visible.delete(value), getContainer:() => container, getCenter:() => ({lat:12,lng:105})};
  map.latLngToContainerPoint=([lat,lng]) => ({x:lng*100,y:-lat*100});
  map.containerPointToLatLng=([x,y]) => ({lng:x/100,lat:-y/100});
  for (const key of ['dragging','doubleClickZoom','scrollWheelZoom','boxZoom','keyboard','touchZoom']) map[key]={enabled:() => true, enable() {},disable() {}};
  const layer = () => ({children:[], eachLayer() {}, addTo(target) { if (target.children) target.children.push(this); else visible.add(this); return this; }, getBounds:() => ['footprint'], clearLayers() {this.children=[];}, getElement:() => null, bindTooltip() {},on() {},setLatLng() {}});
  const L={map:() => map,tileLayer:layer,layerGroup:layer,featureGroup:layer,polygon:layer,polyline:layer,marker:layer,geoJSON:layer,divIcon:value => value, control:{layers:() => ({addTo() {return this;}, addOverlay() {}})}};
  const defaultHandler=(url, options) => {
    if (url === `${API}/references`) return response(frame('reference-1'));
    if (url === `${API}/searches`) return response({search_id:'search-1', total:12, offset:0, limit:50, parameters:JSON.parse(options.body), scenes:Array.from({length:12},(_,i) => scene(i))});
    if (url === `${API}/preflight`) return response({...preflightData,selection_quality:el('mfMergeScenes').checked ? mergeQuality(JSON.parse(options.body).scene_tokens.map(token=>scene(Number(token.split('-')[1])).asset_id)) : null});
    if (url === `${API}/jobs`) return response(jobData());
    if (url.startsWith(`${API}/jobs/`)) return response(jobData('completed',{processed:2,succeeded:2,download_url:`${API}/jobs/job-1/download`}));
    return response({});
  };
  const context=vm.createContext({document:doc, window:win, map, L, getComputedStyle:() => ({getPropertyValue:() => '#0f766e'}), console,
    RectangleROI:require('../static/rectangle.js'),
    fetch:async (url,options={}) => {
      if (url.startsWith('/api/admin/')) return response(url.includes('geojson') ? {type:'FeatureCollection',features:[]} : {provinces:[],communes:[]});
      requests.push({url,options,body:typeof options.body === 'string' ? JSON.parse(options.body) : options.body});
      return handler ? await handler(url,options,defaultHandler) : defaultHandler(url,options);
    },
    FormData:class {constructor() {this.entries=[];} append(...args) {this.entries.push(args);}},
    AbortController, URL, URLSearchParams, encodeURIComponent, Option:class {}, Event:class {constructor(type) {this.type=type;}},
    location:{origin:'http://localhost'}, performance:{now:() => 100},
    localStorage:{getItem() {if (blocked) throw new Error('blocked'); return saved || null;},setItem(key,value) {if(blocked) throw new Error('blocked'); writes.push({key,value});}, removeItem() {}},
    setTimeout:(fn,delay) => {const id=++timerId; timers.set(id,{fn,delay}); return id;}, clearTimeout:id => timers.delete(id),
    requestAnimationFrame:fn => {fn();return 1;}, cancelAnimationFrame() {}, updateCloudSelection() {},updateTimeSeriesVisibility() {}});
  const dateHome=new Element('dateHome'),dateNext=new Element('dateNext');
  dateHome.append(el('dateSelection'),dateNext);
  el('dateSelection').nextElementSibling=dateNext;
  if (legacy) {
    vm.runInContext(fs.readFileSync(path.join(root,'static','scene-footprints.js'),'utf8'), context);
    vm.runInContext(inline,context);
  }
  const selectionFile=path.join(root,'static','selection-state.js');
  if (fs.existsSync(selectionFile)) vm.runInContext(fs.readFileSync(selectionFile,'utf8'),context);
  vm.runInContext(script,context);
  const f={el,doc,win,context,requests,fits,views,visible,timers,writes,
    async upload(name='sample.tif',size=1024) {el('mfFile').files=[{name,size}]; await el('mfFile').fire('change');},
    async search() {await win.MatchedFrame.search();},
    async select(index) {const checkbox=el('mfSceneRows').children[index].children[0].children[0]; checkbox.checked=true; await checkbox.fire('change');},
    async preflight() {await el('mfPreflight').fire('click');},
    async confirm() {el('mfAcknowledge').checked=true; await el('mfAcknowledge').fire('change');await el('mfConfirm').fire('click');},
    async timer() {const [id,timer]=timers.entries().next().value || []; if(timer) {timers.delete(id);await timer.fn();await tick();} return timer;}};
  return f;
}
test('copy activates independent manual ROI, survives deleting the sample and uses normal ROI search', async () => {
  const ROI = require('../static/rectangle.js');
  const rect = {center_lon:105,center_lat:12,width_m:8000,height_m:2000,rotation_deg:30};
  const sample = {...frame('copy-source'),footprint_geojson:ROI.geometry(rect)};
  const f = fixture({legacy:true,handler:(url,options,fallback) => url === `${API}/references` ? response(sample) : fallback(url,options)});
  f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  assert.equal(f.el('mfCopyRoi').disabled,true);
  await f.upload(); assert.equal(f.el('mfCopyRoi').disabled,false);
  await f.el('mfCopyRoi').fire('click');
  assert.equal(f.el('selection_mode').value,'rectangle');
  assert.equal(f.win.MatchedFrame.isActive(),false);
  assert.equal(vm.runInContext('SelectionState.getSource()',f.context),'manual');
  assert.equal(vm.runInContext('rectangleEditor.active && !rectangleEditor.armed',f.context),true);
  const copied = plain(vm.runInContext('rectangleEditor.get()',f.context));
  for (const key of ROI.fields) assert.ok(Math.abs(copied[key]-rect[key]) < 1e-7,key);
  assert.equal(vm.runInContext('rectangleEditor.group.children.length',f.context),12);
  await f.el('mfClearReference').fire('click');
  assert.equal(vm.runInContext('SelectionState.getReferenceDraft()',f.context),null);
  assert.deepEqual(plain(vm.runInContext('rectangleEditor.get()',f.context)),copied);
  assert.deepEqual(plain(vm.runInContext('SelectionState.getManualDraft()',f.context)),copied);
  assert.equal(vm.runInContext('rectangleEditor.active',f.context),true);
  assert.equal(f.el('mfCopyRoi').disabled,true);
  f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  f.el('selection_mode').value='rectangle'; await f.el('selection_mode').fire('change');
  assert.deepEqual(plain(vm.runInContext('rectangleEditor.get()',f.context)),copied);
  assert.equal(vm.runInContext('rectangleEditor.active && !rectangleEditor.armed',f.context),true);
  for (const [key,value] of Object.entries({rotation_deg:65,width_m:9000,center_lon:105.01})) {
    f.el(`rect_${key}`).value=String(value); await f.el(`rect_${key}`).fire('input');
  }
  await f.el('btnSearch').fire('click');
  const request = f.requests.find(r => r.url === '/api/scene');
  assert.ok(request); assert.equal(request.body.selection_mode,'rectangle');
  assert.equal(request.body.rotation_deg,65); assert.equal(request.body.width_m,9000);
  assert.equal(request.body.center_lon,105.01); assert.ok(!('reference_id' in request.body));
  await f.el('rectangleClear').fire('click');
  assert.throws(() => vm.runInContext('rectangleEditor.get()',f.context));
  assert.equal(vm.runInContext('rectangleEditor.group.children.length',f.context),0);
});

test('failed copy leaves the current ROI and selection mode intact', async () => {
  const f=fixture({legacy:true});
  f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  await f.upload(); // This fixture spans over 90 km, beyond the manual editor limit.
  await f.el('mfCopyRoi').fire('click');
  assert.equal(f.el('selection_mode').value,'reference');
  assert.equal(f.el('mfError').hidden,false);
  assert.match(f.el('mfError').children[0].textContent,/90/);
});

test('copy is disabled during replacement upload and late search cannot restore copied frame results', async () => {
  const ROI=require('../static/rectangle.js');
  const footprint_geojson=ROI.geometry({center_lon:105,center_lat:12,width_m:3000,height_m:2000,rotation_deg:40});
  let resolveSearch, resolveUpload, uploads=0;
  const f=fixture({legacy:true,handler:(url,options,fallback) => {
    if (url === `${API}/references`) {
      if (++uploads === 1) return response({...frame('first'),footprint_geojson});
      return new Promise(resolve => {resolveUpload=resolve;});
    }
    if (url === `${API}/searches`) return new Promise(resolve => {resolveSearch=resolve;});
    return fallback(url,options);
  }});
  f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  await f.upload();
  const pending=f.search(); await tick();
  await f.el('mfCopyRoi').fire('click');
  resolveSearch(response({search_id:'stale',total:1,offset:0,limit:50,scenes:[scene(0)]})); await pending;
  assert.equal(f.el('mfSearchResults').hidden,true);
  const copied=plain(vm.runInContext('rectangleEditor.get()',f.context));
  f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  const replacement=f.upload('replacement.tif'); await tick();
  assert.equal(f.el('mfCopyRoi').disabled,true);
  await f.el('mfCopyRoi').fire('click'); assert.equal(f.el('selection_mode').value,'reference');
  resolveUpload(response({...frame('second'),footprint_geojson})); await replacement;
  assert.deepEqual(plain(vm.runInContext('rectangleEditor.get()',f.context)),copied);
  assert.equal(f.el('mfCopyRoi').disabled,false);
});

test('reference ROI bypasses hidden point and rectangle fields and preserves exact frame/grid', async () => {
  const f=fixture({legacy:true}); f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  await f.upload(); f.el('mfGridMode').value='match_grid'; await f.el('mfGridMode').fire('change');
  f.el('lon').value=''; f.el('lat').value='';
  f.el('selection_mode').value='rectangle'; await f.el('selection_mode').fire('change');
  assert.equal(f.win.MatchedFrame.isActive?.(),true);
  assert.equal(vm.runInContext('rectangleEditor.active',f.context),false);
  assert.equal(f.el('mfFit').disabled,false);
  await f.el('btnSearch').fire('click');
  assert.equal(f.requests.at(-1).url,`${API}/searches`);
  assert.equal(f.requests.at(-1).body.reference_id,'reference-1');
  assert.equal(f.requests.at(-1).body.selection_context,'reference_roi');
  assert.equal(f.requests.at(-1).body.grid_mode,'match_grid');
  assert.deepEqual(plain(vm.runInContext('SelectionState.getReferenceDraft().footprint_geojson',f.context)), frame('reference-1').footprint_geojson);
  await f.el('mfDrawManual').fire('click');
  assert.equal(f.win.MatchedFrame.isActive(),false);
  assert.equal(vm.runInContext('rectangleEditor.active',f.context),true);
  vm.runInContext('rectangleEditor.set({center_lon:105,center_lat:12,width_m:1000,height_m:2000,rotation_deg:23})',f.context);
  assert.equal(vm.runInContext('SelectionState.getManualDraft()?.rotation_deg',f.context),23);
  await f.el('mfUseReference').fire('click');
  assert.equal(f.win.MatchedFrame.isActive(),true);
  await f.el('mfDrawManual').fire('click');
  assert.equal(vm.runInContext('rectangleEditor.get().rotation_deg',f.context),23);
  await f.el('mfUseReference').fire('click'); await f.el('mfClearReference').fire('click');
  await f.el('btnSearch').fire('click');
  assert.equal(f.requests.filter(r => r.url==='/api/scene').length,0);
  assert.match(f.el('status').textContent,/chọn vùng|mẫu/i);
});
test('platform selector retains each dataset value and normal ROI passes selected satellite', async () => {
  const f=fixture({legacy:true});
  f.el('dataset').value='s1gee'; await f.el('dataset').fire('change');
  assert.deepEqual(f.el('mfPlatform').children.map(n=>n.value),['','A','B','C','D']);
  f.el('mfPlatform').value='D'; await f.el('mfPlatform').fire('change');
  f.el('dataset').value='sentinel2'; await f.el('dataset').fire('change');
  assert.equal(f.el('mfPlatform').value,'');
  f.el('mfPlatform').value='C'; await f.el('mfPlatform').fire('change');
  f.el('selection_mode').value='rectangle'; await f.el('selection_mode').fire('change');
  vm.runInContext('rectangleEditor.set({center_lon:105,center_lat:12,width_m:1000,height_m:2000,rotation_deg:0})',f.context);
  await f.el('btnSearch').fire('click'); assert.equal(f.requests.at(-1).body.platform,'C');
  f.el('dataset').value='s1gee'; await f.el('dataset').fire('change'); assert.equal(f.el('mfPlatform').value,'D');
  await f.el('btnSearch').fire('click'); assert.equal(f.requests.at(-1).body.platform,'D');
});
test('zero threshold rejects unknown, zero and malformed coverage even when server eligibility is true', async () => {
  const f=fixture({handler:(url,options,next)=>url.endsWith('/searches') ? response({search_id:'s',total:5,parameters:JSON.parse(options.body),scenes:[null,0,NaN,'25',0.1].map((coverage,i)=>scene(i,{band_coverage_pct:coverage}))}) : next(url,options)});
  await f.upload(); f.el('mfMinCoverage').value='0'; await f.search();
  assert.deepEqual(f.el('mfSceneRows').children.map(row=>row.children[0].children[0].disabled),[true,true,true,true,false]);
});
test('preflight v2 false retains ordered sources and group preview but cannot acknowledge or start', async () => {
  const quality={measurement_version:1,method:'output_grid_mask',estimated:true,band_coverage_pct:60,valid_pixel_count:60,total_pixels:100,ordered_asset_ids:[scene(0).asset_id,scene(1).asset_id],roi_fingerprint:'roi',merge_policy:'primary_first_fill_nodata'};
  const f=fixture({handler:(url,options,next)=>url.endsWith('/preflight') ? response({...preflightData,preflight_version:2,can_start:false,blocking_reason:'insufficient_selection_coverage',selection_quality:quality,preview_url:`${API}/preflights/preflight-1/preview`}) : next(url,options)});
  await f.upload();f.el('mfMergeScenes').checked=true;await f.search();await f.select(0);await f.select(1);await f.preflight();
  assert.equal(f.el('mfAcknowledge').disabled,true);assert.equal(f.el('mfConfirm').disabled,true);
  assert.match(f.el('mfPreflightSummary').textContent,/60\.00%/);assert.match(f.el('mfPreflightSummary').textContent,/99.9/);
  assert.match(f.el('mfSelected').textContent,/2 \/ 10/);
  assert.equal(f.el('mfGroupPreview').children.length,0);await f.el('mfGroupPreviewButton').fire('click');
  const img=f.el('mfGroupPreview').children[0]; assert.equal(img.src,`${API}/preflights/preflight-1/preview`);
  await img.fire('error'); assert.equal(f.el('mfConfirm').disabled,true);
  await f.confirm();assert.equal(f.requests.filter(r=>r.url===`${API}/jobs`).length,0);
  f.el('mfPlatform').value='A';await f.el('mfPlatform').fire('change');assert.equal(f.el('mfGroupPreview').children.length,0);
});
test('legacy preflight and malformed merge quality cannot authorize a job', async () => {
  for (const extra of [{preflight_version:1,can_start:true},{preflight_version:2,can_start:true,selection_quality:null}]) {
    const f=fixture({handler:(url,options,next)=>url.endsWith('/preflight') ? response({...preflightData,...extra}) : next(url,options)});
    await f.upload();f.el('mfMergeScenes').checked=true;await f.search();await f.select(0);await f.preflight();await f.confirm();
    assert.equal(f.requests.some(r=>r.url===`${API}/jobs`),false);assert.equal(f.el('mfConfirmation').hidden,true);
  }
});
test('stale_search preflight clears stale sources while keeping reference draft for a fresh search', async () => {
  const f=fixture({handler:(url,options,next)=>url.endsWith('/preflight') ? response({detail:{code:'stale_search',message:'Hãy tìm lại.'}},409) : next(url,options)});
  await f.upload();await f.search();await f.select(0);await f.preflight();
  assert.equal(f.el('mfSearchResults').hidden,true);assert.equal(f.el('mfPreflight').disabled,true);
  await f.search();assert.equal(f.requests.at(-1).body.reference_id,'reference-1');
});
test('replacing a sample while using reference ROI updates its draft and ignores response after manual switch', async () => {
  const pending=[];
  const f=fixture({handler:(url,options,next)=>url.endsWith('/references') && pending.length ? new Promise(resolve=>pending.push(resolve)) : next(url,options)});
  await f.upload();f.el('selection_mode').value='rectangle';await f.el('selection_mode').fire('change');
  pending.push(null);const replacement=f.upload('new.tif'); pending[1](response(frame('new-reference')));await replacement;
  await f.search();assert.equal(f.requests.at(-1).body.reference_id,'new-reference');
  const late=f.upload('late.tif');await f.el('mfDrawManual').fire('click');pending[2](response(frame('late')));await late;
  assert.equal(f.win.MatchedFrame.isActive(),false);
  assert.equal(vm.runInContext('SelectionState.getReferenceDraft()',f.context),null);
});
test('positive merge measurement permits job after failed preview and rejects tampered quality', async () => {
  for (const change of [null,{band_coverage_pct:59,valid_pixel_count:59},{ordered_asset_ids:[scene(1).asset_id]},{roi_fingerprint:null},{total_pixels:0},{band_coverage_pct:'100'}]) {
    const f=fixture({handler:(url,options,next)=>url.endsWith('/preflight') ? response({...preflightData,selection_quality:{...mergeQuality([scene(0).asset_id]),...change},preview_url:`${API}/preflights/preflight-1/preview`}) : next(url,options)});
    await f.upload();f.el('mfMergeScenes').checked=true;await f.search();await f.select(0);await f.preflight();
    if (!change) {
      await f.el('mfGroupPreviewButton').fire('click');await f.el('mfGroupPreview').children[0].fire('error');
      await f.confirm();assert.equal(f.requests.some(r=>r.url===`${API}/jobs`),true);
    } else {await f.confirm();assert.equal(f.requests.some(r=>r.url===`${API}/jobs`),false);}
  }
});
test('empty chosen satellite search names the satellite and never falls back to ALL', async () => {
  const f=fixture({handler:(url,options,next)=>url.endsWith('/searches') ? response({search_id:'empty',total:0,parameters:JSON.parse(options.body),scenes:[]}) : next(url,options)});
  await f.upload();f.el('dataset').value='s1gee';await f.el('dataset').fire('change');f.el('mfPlatform').value='B';await f.el('mfPlatform').fire('change');
  await f.search();assert.match(f.el('mfStatus').textContent,/Không có ảnh Sentinel-1B/);
  assert.equal(f.requests.filter(r=>r.url.endsWith('/searches')).length,1);assert.equal(f.requests.at(-1).body.platform,'B');
});
test('late search and preflight responses after switching to manual ROI cannot restore frame results', async () => {
  let resolveSearch,resolveCheck;
  const f=fixture({handler:(url,options,next)=>url.endsWith('/searches') ? new Promise(resolve=>resolveSearch=resolve) : url.endsWith('/preflight') ? new Promise(resolve=>resolveCheck=resolve) : next(url,options)});
  await f.upload();f.el('selection_mode').value='rectangle';await f.el('selection_mode').fire('change');
  const late=f.search();await f.el('mfDrawManual').fire('click');resolveSearch(response({search_id:'late',total:1,scenes:[scene(0)]}));await late;
  assert.equal(f.el('mfSearchResults').hidden,true);await f.el('mfUseReference').fire('click');
  const fresh=f.search();resolveSearch(response({search_id:'fresh',total:1,scenes:[scene(0)]}));await fresh;await f.select(0);
  const check=f.preflight();await f.el('mfDrawManual').fire('click');resolveCheck(response(preflightData));await check;
  assert.equal(f.el('mfConfirmation').hidden,true);assert.equal(f.win.MatchedFrame.isActive(),false);
  assert.equal(vm.runInContext('SelectionState.getReferenceDraft().reference_id',f.context),'reference-1');
});
test('SAR ranking labels describe date/orbit and unknown coverage explains why selection is disabled', async () => {
  const f=fixture({handler:(url,options,next)=>url.endsWith('/searches') ? response({search_id:'s',total:1,parameters:JSON.parse(options.body),scenes:[scene(0,{band_coverage_pct:null})]}) : next(url,options)});
  await f.upload();f.el('dataset').value='s1gee';await f.el('dataset').fire('change');await f.search();
  assert.doesNotMatch(f.el('mfSearchMeta').textContent,/ít mây/i);
  assert.match(f.el('mfSort').options[0].textContent,/ngày|quỹ đạo/);
  assert.match(f.el('mfSceneRows').children[0].textContent,/không xác định|chưa xác định/i);
});
test('reference ROI keeps EQC/sheared affine and every densified polygon vertex across manual and frame modes', async () => {
  const polygon={type:'Polygon',coordinates:[[[104.01,21.71],[104.035,21.713],[104.06,21.72],[104.07,21.75],[104.04,21.77],[104.01,21.71]]]};
  const affine=[9.9939,2,-8065,1,-9.9995,8094];
  const f=fixture({handler:(url,options,next)=>url.endsWith('/references') ? response({...frame('eqc'),metadata:{...frame('eqc').metadata,crs:'PROJCRS[EQC]',transform:affine},footprint_geojson:polygon}) : next(url,options)});
  await f.upload();f.el('mfGridMode').value='match_grid';await f.el('mfGridMode').fire('change');
  f.el('selection_mode').value='rectangle';await f.el('selection_mode').fire('change');await f.el('mfDrawManual').fire('click');await f.el('mfUseReference').fire('click');
  f.el('selection_mode').value='reference';await f.el('selection_mode').fire('change');await f.search();
  const draft=plain(vm.runInContext('SelectionState.getReferenceDraft()',f.context));
  assert.deepEqual(draft.footprint_geojson,polygon);assert.deepEqual(draft.metadata.transform,affine);assert.equal(f.requests.at(-1).body.grid_mode,'match_grid');
  assert.equal(f.requests.filter(r=>r.url.endsWith('/references')).length,1);
});
test('legacy require_full_coverage snapshot keeps its effective 99.9 threshold over min=0', async () => {
  const f=fixture({handler:(url,options,next)=>url.endsWith('/searches') ? response({search_id:'full',total:1,parameters:{...JSON.parse(options.body),require_full_coverage:true,min_coverage_pct:0},scenes:[scene(0,{band_coverage_pct:85})]}) : next(url,options)});
  await f.upload();await f.search();assert.equal(f.el('mfSceneRows').children[0].children[0].children[0].disabled,true);
  assert.match(f.el('mfSearchMeta').textContent,/99.9%/);
});
test('normal time series hides unsupported satellite selector and retains value on return', async () => {
  const f=fixture({legacy:true});f.el('dataset').value='sentinel2';await f.el('dataset').fire('change');
  f.el('mfPlatform').value='C';await f.el('mfPlatform').fire('change');
  f.el('time_series_enabled').checked=true;await f.el('time_series_enabled').fire('change');
  assert.equal(f.el('mfPlatformSelection').hidden,true);
  f.el('time_series_enabled').checked=false;await f.el('time_series_enabled').fire('change');
  assert.equal(f.el('mfPlatformSelection').hidden,false);assert.equal(f.el('mfPlatform').value,'C');
});
test('reference dispatch precedes old coordinate validation and never calls legacy search APIs', async () => {
  const f=fixture({legacy:true});
  f.el('selection_mode').value='reference'; await f.el('selection_mode').fire('change');
  f.el('lon').value='';f.el('lat').value='';f.el('rotation_deg').value='bad';
  await f.upload();
  assert.equal(f.el('cloudSelection').hidden,true);
  assert.equal(f.el('btnSearch').textContent,'Tìm cảnh đồng khung');
  await f.el('btnSearch').fire('click');
  assert.equal(f.requests.at(-1).url,`${API}/searches`);
  assert.equal(f.el('referenceSelection').hidden,false);
  assert.equal(vm.runInContext('rectangleEditor.active',f.context),false);
  assert.equal(vm.runInContext('pointSelection.style.display',f.context),'none');
  assert.ok(f.requests.every(item => item.url.startsWith(API)));
});
test('multipart upload renders metadata and explicit footprint fit without automatic recenter', async () => {
  const f=fixture(); await f.upload();
  assert.equal(f.requests[0].url,`${API}/references`); assert.equal(f.requests[0].options.method,'POST');
  assert.equal(f.requests[0].body.entries[0][0],'file'); assert.ok(!f.requests[0].options.headers);
  assert.match(f.el('mfReferenceMeta').textContent,/EPSG:32648.*1508 × 1157/);
  assert.match(f.el('mfReferenceMeta').textContent,/B4/); assert.equal(f.fits.length,0);assert.equal(f.visible.size,1);
  await f.el('mfFit').fire('click'); assert.equal(f.fits.length,1); assert.equal(f.views.at(-1),'explore');
  f.el('selection_mode').value='point';await f.el('selection_mode').fire('change');assert.equal(f.visible.size,0);
});
test('upload validation and structured API errors have focusable summary and linked field error', async () => {
  const f=fixture(); await f.upload('x.vrt');assert.equal(f.requests.length,0); assert.match(f.el('mfError').textContent,/GeoTIFF/);assert.equal(f.el('mfError').focused,true);
  await f.upload('large.tif',512*1024*1024+1);assert.equal(f.requests.length,0);
  await f.upload('limit.tif',512*1024*1024);assert.equal(f.requests.length,1);
  const bad=fixture({handler:() => response({detail:{code:'invalid_crs',message:'CRS không hợp lệ. Chọn tệp khác.'}},422)});
  await bad.upload();assert.match(bad.el('mfError').textContent,/CRS không hợp lệ/);assert.equal(bad.el('mfFile').attributes['aria-invalid'],'true');assert.equal(bad.el('mfFile').value,'');
  await bad.el('mfError').children[1].fire('click');assert.equal(bad.el('mfFile').focused,true);
});
test('stale upload responses cannot overwrite newer reference and old signal is aborted', async () => {
  const pending=[];const f=fixture({handler:(url,options,next) => url.endsWith('/references') ? new Promise(resolve => pending.push(resolve)) : next(url,options)});
  const first=f.upload('first.tif'), second=f.upload('second.tiff');
  assert.equal(f.requests[0].options.signal.aborted,true);
  pending[1](response(frame('new')));await second; pending[0](response(frame('old')));await first;
  await f.search();assert.equal(f.requests.at(-1).body.reference_id,'new');assert.match(f.el('mfUploadStatus').textContent,/second.tiff/);
});
test('geographic reference disables match_extent and sends exact search contract defaults', async () => {
  const f=fixture({handler:(url,options,next) => url.endsWith('/references') ? response({...frame('geo'),compatible_grid_modes:['match_grid']}) : next(url,options)});
  await f.upload();assert.equal(f.el('mfGridMode').value,'match_grid');assert.equal(f.el('mfGridMode').options[0].disabled,true);assert.equal(f.el('mfResolution').disabled,true);
  await f.search();assert.deepEqual(plain(f.requests.at(-1).body),{reference_id:'geo',selection_context:'reference',dataset:'sentinel2',start_date:'2026-10-01',end_date:'2026-10-22',grid_mode:'match_grid',target_resolution_m:10,band_preset:'full',polarization:'vvvh',sort:'least_cloud',reference_date:null,require_full_coverage:false,min_coverage_pct:99.9,merge_scenes:false,min_cloud_shadow_pct:null,max_cloud_shadow_pct:null,orbit_pass:null,relative_orbit:null,platform:null,prefer_reference_orbit:false,reference_orbit_pass:null,reference_relative_orbit:null});
});
test('dates, unsupported dataset, cloud threshold and confirmed orbit validation prevent requests', async () => {
  const f=fixture();await f.upload();
  for (const [id,value,pattern] of [['end_date','2025-01-01',/366/],['end_date','2026-99-99',/ngày hợp lệ/],['dataset','landsat',/chỉ hỗ trợ/],['mfCloud','101',/0–100/]]) {
    const old=f.el(id).value;f.el(id).value=value;await f.search();assert.match(f.el('mfError').textContent,pattern);f.el(id).value=old;
  }
  f.el('mfSort').value='nearest_date';await f.search();assert.match(f.el('mfError').textContent,/ngày hợp lệ/);
  f.el('mfReferenceDate').value='2026-10-12';f.el('dataset').value='s1gee';f.el('mfPreferOrbit').checked=true;await f.search();assert.match(f.el('mfError').textContent,/xác nhận cả/);
  f.el('mfReferencePass').value='ASCENDING';f.el('mfReferenceOrbit').value='176';await f.search();assert.match(f.el('mfError').textContent,/1 đến 175/);
  assert.equal(f.requests.length,1);
  f.el('mfReferenceOrbit').value='45';f.el('mfOrbitPass').value='DESCENDING';f.el('mfRelativeOrbit').value='12';f.el('mfPlatform').value='A';f.el('mfPolarization').value='hhhv';await f.search();
  const body=f.requests.at(-1).body;assert.equal(body.reference_relative_orbit,45);assert.equal(body.reference_orbit_pass,'ASCENDING');assert.equal(body.relative_orbit,12);assert.equal(body.prefer_reference_orbit,true);assert.equal(body.polarization,'hhhv');assert.equal(body.max_cloud_shadow_pct,null);
});
test('source/grid edits invalidate selection and preflight, stale search success and failure are ignored', async () => {
  let resolve;const f=fixture({handler:(url,options,next) => url.endsWith('/searches') ? new Promise(done => resolve=done) : next(url,options)});
  await f.upload();const pending=f.search();f.el('mfGridMode').value='match_grid';await f.el('mfGridMode').fire('change');
  resolve(response({search_id:'stale',total:1,scenes:[scene(0)]}));await pending;assert.equal(f.el('mfSearchResults').hidden,true);assert.equal(f.el('btnSearch').disabled,false);
  const failure=f.search();f.el('dataset').value='s1gee';await f.el('dataset').fire('change');resolve(response({detail:{code:'old',message:'stale failure'}},502));await failure;assert.equal(f.el('mfError').hidden,true);
});
test('max10 selection is enforced, nullable metrics show N/A, ineligible scenes cannot be selected', async () => {
  const f=fixture({handler:(url,options,next) => url.endsWith('/searches') ? response({search_id:'search-1',total:12,parameters:JSON.parse(options.body),scenes:Array.from({length:12},(_,i) => scene(i,i===11 ? {eligible:false,band_coverage_pct:null,roi_cloud_pct:null} : {}))}) : next(url,options)});
  await f.upload();await f.search();for(let i=0;i<10;i++) await f.select(i);
  assert.match(f.el('mfSelected').textContent,/10 \/ 10/);assert.equal(f.el('mfSceneRows').children[10].children[0].children[0].disabled,true);
  assert.match(f.el('mfSceneRows').children[11].textContent,/N\/A/);await f.select(10);assert.match(f.el('mfError').textContent,/tối đa 10/);
  await f.preflight();assert.equal(f.requests.at(-1).body.scene_tokens.length,10);
  await f.el('mfClearSelection').fire('click');assert.equal(f.el('mfConfirmation').hidden,true);assert.equal(f.el('mfPreflight').disabled,true);
});
test('pagination retains cross-page selection and always uses the search snapshot endpoint', async () => {
  const f=fixture({handler:(url,options,next) => url === `${API}/searches` ? response({search_id:'search-pages',total:51,offset:0,limit:50,parameters:JSON.parse(options.body),scenes:[scene(0)]}) : url.includes('search-pages?') ? response({search_id:'search-pages',total:51,offset:50,limit:50,scenes:[scene(50)]}) : next(url,options)});
  await f.upload();await f.search();await f.select(0);await f.el('mfNext').fire('click');assert.equal(f.requests.at(-1).url,`${API}/searches/search-pages?offset=50&limit=50`);
  await f.select(0);await f.preflight();assert.deepEqual(plain(f.requests.at(-1).body.scene_tokens),['scene-0','scene-50']);assert.match(f.el('mfPage').textContent,/51–51 \/ 51/);
});
test('preview is lazy, same-origin, accessible, and failures never erase search selection', async () => {
  const f=fixture();await f.upload();await f.search();await f.select(0);
  const card=f.el('mfSceneRows').children[0],button=card.children.at(-2),preview=card.children.at(-1);
  assert.equal(preview.children.length,0);await button.fire('click');const image=preview.children[0];assert.match(image.alt,/Sentinel-2A.*2026-10-01/);assert.match(image.src,/\/preview$/);assert.equal(button.attributes['aria-expanded'],'true');
  await image.fire('error');assert.match(preview.textContent,/không khả dụng/);assert.match(f.el('mfSelected').textContent,/1 \/ 10/);assert.equal(f.el('mfSearchResults').hidden,false);
});
test('preflight requires explicit acknowledgement and submits only the returned digest, then polls independently of edits', async () => {
  const f=fixture();await f.upload();await f.search();await f.select(0);await f.preflight();
  assert.equal(f.el('mfConfirm').disabled,true);assert.equal(f.el('mfConfirmTitle').focused,true);assert.match(f.el('mfPreflightSummary').textContent,/9.986754966887418/);assert.match(f.el('mfPreflightSummary').textContent,/512 MiB/);
  await f.el('mfConfirm').fire('click');assert.ok(!f.requests.some(item => item.url === `${API}/jobs`));
  await f.confirm();assert.deepEqual(plain(f.requests.find(item => item.url === `${API}/jobs`).body),{preflight_id:'preflight-1',digest:'exact-digest',acknowledged:true});assert.deepEqual(f.writes[0],{key:STORAGE,value:'job-1'});
  assert.equal(f.timers.size,1);assert.equal([...f.timers.values()][0].delay,2000);
  f.el('mfBands').value='rgb';await f.el('mfBands').fire('change');assert.equal(f.el('mfConfirmation').hidden,true);assert.equal(f.el('mfSearchResults').hidden,true);assert.match(f.el('mfJobStatus').textContent,/job-1/);assert.equal(f.timers.size,1);
  await f.timer();assert.equal(f.el('mfZip').href,`${API}/jobs/job-1/download`);assert.equal(f.timers.size,0);
});
test('stale preflight response is discarded after scene or form edits', async () => {
  let resolve;const f=fixture({handler:(url,options,next) => url.endsWith('/preflight') ? new Promise(done => resolve=done) : next(url,options)});
  await f.upload();await f.search();await f.select(0);const pending=f.preflight();f.el('mfResolution').value='20';await f.el('mfResolution').fire('change');resolve(response(preflightData));await pending;
  assert.equal(f.el('mfConfirmation').hidden,true);assert.equal(f.el('mfConfirm').disabled,true);
});
test('job creation retains acknowledged digest even if form is edited while POST is pending', async () => {
  let resolve;const f=fixture({handler:(url,options,next) => url === `${API}/jobs` ? new Promise(done => resolve=done) : next(url,options)});
  await f.upload();await f.search();await f.select(0);await f.preflight();const pending=f.confirm();await tick();await tick();f.el('mfBands').value='rgb';await f.el('mfBands').fire('change');resolve(response(jobData()));await pending;
  assert.match(f.el('mfJobStatus').textContent,/job-1/);assert.equal(f.timers.size,1);
});
test('partial reload restores verified TIFF/ZIP/report links and stops terminal polling', async () => {
  const f=fixture({saved:'job-1',handler:() => response(jobData('partial',{processed:2,succeeded:1,failed:1,download_url:`${API}/jobs/job-1/download`,scenes:[{status:'completed',filename:'good.tif',download_url:`${API}/jobs/job-1/files/file-1`,report:{actual_coverage_pct:100}},{status:'failed',filename:'bad.tif',error:{code:'coverage',message:'Thiếu coverage thực'},download_url:`${API}/jobs/job-1/files/bad`}]}))});
  await tick();assert.equal(f.requests[0].url,`${API}/jobs/job-1`);assert.match(f.el('mfJobStatus').textContent,/Hoàn tất một phần/);assert.equal(f.el('mfZip').hidden,false);assert.equal(f.el('mfJobScenes').children[0].children.at(-1).href,`${API}/jobs/job-1/files/file-1`);assert.ok(!f.el('mfJobScenes').children[1].children.some(value => value.href));assert.equal(f.timers.size,0);
  await f.doc.fire('visibilitychange');assert.equal(f.requests.length,1);
});
test('visibility pauses polling and network retries back off at most 15s; missing and terminal jobs stop', async () => {
  let status='running',fail=false;
  const f=fixture({saved:'job-1',handler:() => fail ? response({detail:{code:'network',message:'Tạm lỗi'}},502) : response(jobData(status))});await tick();assert.equal(f.timers.size,1);
  f.doc.hidden=true;await f.doc.fire('visibilitychange');assert.equal(f.timers.size,0);const count=f.requests.length;await f.win.fire('pageshow');assert.equal(f.requests.length,count);
  f.doc.hidden=false;fail=true;await f.doc.fire('visibilitychange');assert.equal([...f.timers.values()][0].delay,4000);
  await f.timer();assert.equal([...f.timers.values()][0].delay,8000);await f.timer();assert.equal([...f.timers.values()][0].delay,15000);await f.timer();assert.equal([...f.timers.values()][0].delay,15000);
  fail=false;status='interrupted';await f.timer();assert.equal(f.timers.size,0);assert.match(f.el('mfJobStatus').textContent,/Bị gián đoạn/);
  const missing=fixture({saved:'gone',handler:() => response({detail:{code:'not_found',message:'Không tìm thấy'}},404)});await tick();assert.equal(missing.timers.size,0);assert.equal(missing.el('mfRetryJob').hidden,false);assert.match(missing.el('mfJobStatus').textContent,/hết hạn/);
});
test('storage denied, malicious text and external result links cannot break the workflow', async () => {
  const f=fixture({blocked:true,handler:(url,options,next) => url === `${API}/jobs` ? response(jobData('completed',{processed:2,succeeded:1,failed:1,download_url:'https://outside.example/secret',scenes:[{status:'completed',filename:'<img onerror=bad>',download_url:'javascript:bad()'}]})) : next(url,options)});
  await f.upload();await f.search();await f.select(0);await f.preflight();await f.confirm();assert.match(f.el('mfStorageWarning').textContent,/chặn lưu/);assert.equal(f.el('mfZip').hidden,true);assert.match(f.el('mfJobScenes').textContent,/<img onerror=bad>/);assert.ok(!script.includes('innerHTML'));
});
test('copy feature refreshes both script URLs instead of reusing the cached pre-copy editor', () => {
  const editor = new URL(html.match(/src="(\/rectangle\.js[^\"]*)"/)[1],'http://localhost');
  const matched = new URL(html.match(/src="(\/matched-frame\.js[^\"]*)"/)[1],'http://localhost');
  assert.notEqual(editor.searchParams.get('v'),'rectangle-1');
  assert.notEqual(matched.searchParams.get('v'),'1');
  assert.ok(editor.searchParams.get('v'));
  assert.equal(editor.searchParams.get('v'),matched.searchParams.get('v'));
});

test('workspace integration observes matched results and keeps scripts, labels, scoped styles and warnings', () => {
  assert.match(workspace,/move\(region, \['mfRoiSource', 'referenceSelection'/);
  assert.match(workspace,/move\(source, \[[^\]]*'mfPlatformSelection'/);
  assert.match(workspace,/const resultIds = \[[^\]]*'matchedFrameResults'/);
  assert.match(workspace,/move\(get\('workspaceResultContent'\), resultIds\)/);
  assert.match(workspace,/const resultNodes = resultIds\.map\(get\)/);
  assert.match(workspace,/resultNodes\.forEach\(node => observer\.observe\(node/);
  assert.match(workspace,/window.SatelliteWorkspace = Object.freeze\(\{showView, hasResults\}\)/);
  assert.ok(html.indexOf('src="/matched-frame.js') > html.indexOf('src="/workspace.js'));
  for(const id of ['mfFile','mfGridMode','mfResolution','mfBands','mfPolarization','mfSort','mfReferenceDate','mfCloud','mfCloudMin','mfMinCoverage','mfMergeScenes','mfResultMode','mfOrbitPass','mfRelativeOrbit','mfPlatform','mfReferencePass','mfReferenceOrbit','mfAcknowledge']) assert.match(html,new RegExp(`<label for="${id}"`));
  assert.match(html,/N\/A không phải 0%/);assert.match(html,/SCL 0% không bảo đảm/);assert.match(css,/#matchedFrameResults/);assert.match(css,/var\(--danger\)/);assert.match(css,/@media\(max-width:760px\)/);
});

test('allow-partial changes quality policy while server eligibility remains authoritative', async () => {
  const f=fixture({handler:(url,options,next) => url === `${API}/searches` ? response({search_id:'partial-search',total:2,parameters:JSON.parse(options.body),scenes:[scene(0,{band_coverage_pct:72,eligible:true}),scene(1,{band_coverage_pct:100,eligible:false})]}) : next(url,options)});
  await f.upload();await f.search();assert.equal(f.el('mfSceneRows').children[0].children[0].children[0].disabled,true);
  f.el('mfAllowPartial').checked=true;await f.el('mfAllowPartial').fire('change');await f.search();assert.equal(f.requests.at(-1).body.require_full_coverage,false);assert.equal(f.requests.at(-1).body.min_coverage_pct,0);assert.equal(f.el('mfMinCoverage').disabled,true);
  assert.equal(f.el('mfSceneRows').children[0].children[0].children[0].disabled,false);assert.equal(f.el('mfSceneRows').children[1].children[0].children[0].disabled,true);
});
test('selection keeps existing preview nodes and keyboard focus instead of rebuilding cards', async () => {
  const f=fixture();await f.upload();await f.search();
  const card=f.el('mfSceneRows').children[0], checkbox=card.children[0].children[0], previewButton=card.children.at(-2);
  await previewButton.fire('click');const preview=card.children.at(-1).children[0];checkbox.focus();await f.select(0);
  assert.equal(f.el('mfSceneRows').children[0],card);assert.equal(card.children[0].children[0],checkbox);assert.equal(checkbox.focused,true);assert.equal(card.children.at(-1).children[0],preview);
});
test('mode changes abort pending uploads, hide footprint and ignore late completion', async () => {
  let resolve;const f=fixture({handler:() => new Promise(done => resolve=done)});
  const pending=f.upload();f.el('selection_mode').value='rectangle';await f.el('selection_mode').fire('change');
  assert.equal(f.requests[0].options.signal.aborted,true);resolve(response(frame('stale')));await pending;
  assert.equal(f.el('mfReferenceMeta').children.length,0);assert.equal(f.visible.size,0);assert.equal(f.el('mfFit').disabled,true);
});
test('empty searches and malformed preflight never enable job confirmation', async () => {
  const empty=fixture({handler:(url,options,next) => url === `${API}/searches` ? response({search_id:'empty',total:0,parameters:JSON.parse(options.body),scenes:[]}) : next(url,options)});
  await empty.upload();await empty.search();assert.match(empty.el('mfSceneRows').textContent,/Kh\u00f4ng c\u00f3 c\u1ea3nh/);assert.equal(empty.el('mfPreflight').disabled,true);
  const f=fixture({handler:(url,options,next) => url.endsWith('/preflight') ? response({...preflightData,output_grid:{width:1,height:1,crs:'EPSG:32648'}}) : next(url,options)});
  await f.upload();await f.search();await f.select(0);await f.preflight();assert.equal(f.el('mfConfirmation').hidden,true);assert.match(f.el('mfError').textContent,/h\u1ee3p l\u1ec7/);
});
test('WKT summaries use root EPSG authority and pixel_unit while preserving full CRS in disclosures', async () => {
  const wkt='PROJCS["UTM zone 48N",GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]],UNIT["metre",1],AUTHORITY["EPSG","32648"]]';
  const f=fixture({handler:(url,options,next) => {
    if(url.endsWith('/references')) return response({...frame('wkt-reference'),metadata:{...frame('x').metadata,crs:wkt,pixel_unit:'m'}});
    if(url.endsWith('/preflight')) return response({...preflightData,output_grid:{...preflightData.output_grid,crs:wkt,pixel_unit:'m'}});
    return next(url,options);
  }});
  await f.upload();
  assert.equal(f.el('mfReferenceMeta').children[0].textContent,'CRS: EPSG:32648 · 1508 × 1157 pixel');
  assert.equal(f.el('mfReferenceMeta').children[1].tagName,'DETAILS');
  assert.equal(f.el('mfReferenceMeta').children[1].children[1].textContent,wkt);
  assert.match(f.el('mfReferenceMeta').textContent,/Vector pixel mẫu: 3 × 3 m/);
  await f.search();await f.select(0);await f.preflight();
  const summary=f.el('mfPreflightSummary');
  assert.ok(summary.children.some(child => child.tagName === 'P' && child.textContent.startsWith('CRS đầu ra: EPSG:32648 ·')));
  assert.ok(summary.children.some(child => child.tagName === 'DETAILS' && child.children[1].textContent === wkt));
  assert.match(summary.textContent,/9\.974137931034482 m\./);
});
test('custom WKT without root EPSG never mistakes nested datum authority for the output CRS', async () => {
  const wkt='PROJCS["Custom grid",GEOGCS["WGS 84",AUTHORITY["EPSG","4326"]],UNIT["metre",1]]';
  const f=fixture({handler:(url,options,next) => url.endsWith('/references') ? response({...frame('custom'),metadata:{...frame('x').metadata,crs:wkt,pixel_unit:'m'}}) : next(url,options)});
  await f.upload();
  const summary=f.el('mfReferenceMeta');
  assert.ok(!summary.children[0].textContent.includes('EPSG:4326'));
  assert.equal(summary.children[1].children[1].textContent,wkt);
});
test('machine quality warnings are Vietnamese and preflight Unix expiry is formatted in UTC', async () => {
  const codes=['coverage_estimated_20m','coverage_estimated_100m','grid_alignment_is_not_coregistration','scl_does_not_guarantee_haze_free','native_resolution_10_20_60m'];
  const expires=1792700000;
  const f=fixture({handler:(url,options,next) => {
    if(url === `${API}/searches`) return response({search_id:'warnings',total:1,parameters:JSON.parse(options.body),scenes:[scene(0,{warnings:codes})]});
    if(url.endsWith('/preflight')) return response({...preflightData,expires_at:expires,warnings:codes.map(code => ({code}))});
    return next(url,options);
  }});
  await f.upload();await f.search();
  const warningList=f.el('mfSceneRows').children[0].children.find(child => child.tagName === 'UL');
  assert.equal(warningList.children.length,5);
  for(const code of codes) assert.ok(!warningList.textContent.includes(code));
  assert.match(warningList.textContent,/20 m/);assert.match(warningList.textContent,/100 m/);assert.match(warningList.textContent,/đồng đăng ký/);assert.match(warningList.textContent,/mây mỏng/);assert.match(warningList.textContent,/10\/20\/60 m/);
  await f.select(0);await f.preflight();
  const summary=f.el('mfPreflightSummary');
  const formatted=new Date(expires*1000).toLocaleString('vi-VN',{timeZone:'UTC',hour12:false});
  assert.ok(summary.textContent.includes(formatted+' UTC'));assert.ok(!summary.textContent.includes(String(expires)));
});
test('job rows prefer acquisition date and platform over opaque scene token and retain filenames', async () => {
  const f=fixture({saved:'job-1',handler:() => response(jobData('completed',{processed:2,succeeded:2,scenes:[
    {status:'completed',scene_token:'opaque-uuid-1',timestamp:'2026-10-01T02:00:00Z',platform:'Sentinel-2A',filename:'verified.tif'},
    {status:'completed',scene_token:'opaque-uuid-2',date:'2026-10-02',platform:'Sentinel-2B'}
  ]}))});
  await tick();
  const rows=f.el('mfJobScenes').children;
  assert.match(rows[0].children[0].textContent,/Sentinel-2A · 2026-10-01T02:00:00Z UTC/);
  assert.equal(rows[0].children[1].textContent,'verified.tif');
  assert.match(rows[1].children[0].textContent,/Sentinel-2B · 2026-10-02 UTC/);
  assert.ok(!f.el('mfJobScenes').textContent.includes('opaque-uuid'));
});
test('failed jobs and pagehide stop timers; a restored page resumes only an active job', async () => {
  const f=fixture({saved:'job-1',handler:() => response(jobData())});await tick();assert.equal(f.timers.size,1);
  await f.win.fire('pagehide');assert.equal(f.timers.size,0);await f.win.fire('pageshow');assert.equal(f.timers.size,1);
  const failed=fixture({saved:'job-1',handler:() => response(jobData('failed',{failed:2,processed:2}))});await tick();assert.equal(failed.timers.size,0);assert.equal(failed.el('mfZip').hidden,true);
});

test('reference workflow moves shared date fields after footprint and restores them on exit', async () => {
  const f=fixture();
  assert.equal(f.el('dateSelection').parentElement,f.el('mfDateSlot'));
  assert.ok(html.indexOf('id="mfDateSlot"') < html.indexOf('id="mfResultMode"'));
  assert.ok(html.indexOf('id="mfMinCoverage"') < html.indexOf('id="mfMergeScenes"'));
  f.el('selection_mode').value='point';await f.el('selection_mode').fire('change');
  const home=f.el('dateSelection').parentElement;
  assert.equal(home.id,'dateHome');assert.equal(home.children[0],f.el('dateSelection'));
  f.el('selection_mode').value='reference';await f.el('selection_mode').fire('change');
  assert.equal(f.el('dateSelection').parentElement,f.el('mfDateSlot'));assert.equal(f.el('start_date').value,'2026-10-01');
});
test('validation links disclose advanced fields before focusing them', async () => {
  const f=fixture();await f.upload();
  const advanced=new Element('', 'details');advanced.append(f.el('mfReferenceOrbit'));
  f.el('dataset').value='s1gee';f.el('mfPreferOrbit').checked=true;f.el('mfReferencePass').value='ASCENDING';f.el('mfReferenceOrbit').value='176';
  await f.search();await f.el('mfError').children[1].fire('click');
  assert.equal(advanced.open,true);assert.equal(f.el('mfReferenceOrbit').focused,true);
});
test('cloud range, coverage threshold and mosaic flag send the explicit search contract', async () => {
  const f=fixture();await f.upload();
  f.el('mfCloudMin').value='4';f.el('mfCloud').value='25.5';f.el('mfMinCoverage').value='83.2';f.el('mfMergeScenes').checked=true;
  await f.search();const body=f.requests.at(-1).body;
  assert.equal(body.min_cloud_shadow_pct,4);assert.equal(body.max_cloud_shadow_pct,25.5);
  assert.equal(body.min_coverage_pct,83.2);assert.equal(body.require_full_coverage,false);assert.equal(body.merge_scenes,true);
  f.el('dataset').value='s1gee';await f.el('dataset').fire('change');await f.search();
  assert.equal(f.requests.at(-1).body.min_cloud_shadow_pct,null);assert.equal(f.requests.at(-1).body.max_cloud_shadow_pct,null);
  assert.equal(f.el('mfS2Options').hidden,true);assert.equal(f.el('mfS2ExportOptions').hidden,true);
});
test('invalid cloud range and coverage prevent search; zero and 100 remain valid boundaries', async () => {
  const f=fixture();await f.upload();
  for (const [id,value] of [['mfCloudMin','-1'],['mfCloudMin','101'],['mfMinCoverage',''],['mfMinCoverage','bad'],['mfMinCoverage','100.1']]) {
    const original=f.el(id).value;f.el(id).value=value;await f.search();
    assert.equal(f.requests.length,1);assert.equal(f.el(id).attributes['aria-invalid'],'true');f.el(id).value=original;
  }
  f.el('mfCloudMin').value='40';f.el('mfCloud').value='20';await f.search();
  assert.match(f.el('mfError').textContent,/tối thiểu.*tối đa/);assert.equal(f.requests.length,1);
  f.el('mfCloudMin').value='0';f.el('mfCloud').value='100';f.el('mfMinCoverage').value='0';await f.search();
  assert.equal(f.requests.at(-1).body.min_cloud_shadow_pct,0);assert.equal(f.requests.at(-1).body.max_cloud_shadow_pct,100);assert.equal(f.requests.at(-1).body.min_coverage_pct,0);
});
test('best scene skips ineligible and below-threshold rows and permits exactly one replacement', async () => {
  const f=fixture({handler:(url,options,next) => url === `${API}/searches` ? response({search_id:'best',total:4,parameters:JSON.parse(options.body),scenes:[scene(0,{eligible:false}),scene(1,{band_coverage_pct:70}),scene(2),scene(3)]}) : next(url,options)});
  await f.upload();f.el('mfResultMode').value='best';await f.el('mfResultMode').fire('change');await f.search();
  const rows=f.el('mfSceneRows').children;
  assert.equal(rows[0].children[0].children[0].checked,false);assert.equal(rows[1].children[0].children[0].checked,false);
  assert.equal(rows[2].children[0].children[0].checked,true);assert.equal(rows[2].children[0].children[0].type,'radio');
  assert.match(rows[2].textContent,/48PXS/);assert.match(rows[2].textContent,/ROI mây/);assert.match(rows[2].textContent,/COPERNICUS/);
  await f.preflight();assert.deepEqual(plain(f.requests.at(-1).body.scene_tokens),['scene-2']);
  await f.select(3);assert.equal(rows[2].children[0].children[0].checked,false);assert.equal(f.el('mfConfirmation').hidden,true);
  await f.preflight();assert.deepEqual(plain(f.requests.at(-1).body.scene_tokens),['scene-3']);
});
test('best mode with no eligible scene does not silently select an ineligible result', async () => {
  const f=fixture({handler:(url,options,next) => url === `${API}/searches` ? response({search_id:'none',total:1,parameters:JSON.parse(options.body),scenes:[scene(0,{eligible:false})]}) : next(url,options)});
  await f.upload();f.el('mfResultMode').value='best';await f.search();
  assert.equal(f.el('mfPreflight').disabled,true);assert.equal(f.el('mfSceneRows').children[0].children[0].children[0].checked,false);
  assert.match(f.el('mfStatus').textContent,/Chưa có ảnh đủ điều kiện/);
});
test('mosaic accepts individually partial eligible sources and preserves explicit user priority', async () => {
  const f=fixture({handler:(url,options,next) => {
    if(url === `${API}/searches`) return response({search_id:'mosaic',total:3,parameters:JSON.parse(options.body),scenes:[scene(0,{band_coverage_pct:65}),scene(1,{band_coverage_pct:80}),scene(2,{eligible:false})]});
    if(url.endsWith('/preflight')) return response({...preflightData,selection_quality:mergeQuality(JSON.parse(options.body).scene_tokens.map(token=>scene(Number(token.split('-')[1])).asset_id)),estimate:{...preflightData.estimate,source_count:2,output_count:1}});
    return next(url,options);
  }});
  await f.upload();f.el('mfMergeScenes').checked=true;await f.search();
  assert.equal(f.el('mfSceneRows').children[0].children[0].children[0].disabled,false);
  assert.equal(f.el('mfSceneRows').children[2].children[0].children[0].disabled,true);
  await f.select(1);await f.select(0);await f.preflight();
  assert.deepEqual(plain(f.requests.at(-1).body.scene_tokens),['scene-1','scene-0']);
  assert.match(f.el('mfSelected').textContent,/1 ảnh ghép đầu ra/);
  const summary=f.el('mfPreflightSummary').textContent;
  assert.match(summary,/2 nguồn → 1 ảnh đầu ra/);assert.match(summary,/MỘT TIFF/);assert.match(summary,/99.9%.*ảnh ghép cuối cùng/);assert.match(summary,/kể cả mây/);
  const sourceList=f.el('mfSelectedSources').querySelector('ol');
  assert.match(sourceList.children[0].textContent,/Ảnh chính.*HARMONIZED\/1/);
  const buttons=f.el('mfSelectedSources').querySelectorAll('button');assert.equal(buttons[0].disabled,true);assert.equal(buttons[3].disabled,true);
  await buttons[2].fire('click');assert.equal(f.el('mfConfirmation').hidden,true);
  assert.equal(f.el('mfSelectedSources').querySelectorAll('button')[2].focused,true);
  await f.preflight();assert.deepEqual(plain(f.requests.at(-1).body.scene_tokens),['scene-0','scene-1']);
  const checkbox=f.el('mfSceneRows').children[0].children[0].children[0];checkbox.checked=false;await checkbox.fire('change');await f.select(0);
  await f.preflight();assert.deepEqual(plain(f.requests.at(-1).body.scene_tokens),['scene-1','scene-0']);
});
test('a single source is still one mosaic and non-mosaic coverage stays per scene', async () => {
  const f=fixture({handler:(url,options,next) => url === `${API}/searches` ? response({search_id:'coverage',total:2,parameters:JSON.parse(options.body),scenes:[scene(0,{band_coverage_pct:79}),scene(1,{band_coverage_pct:80})]}) : next(url,options)});
  await f.upload();f.el('mfMinCoverage').value='80';await f.search();
  assert.equal(f.el('mfSceneRows').children[0].children[0].children[0].disabled,true);await f.select(1);await f.preflight();
  assert.match(f.el('mfPreflightSummary').textContent,/1 nguồn → 1 ảnh đầu ra/);assert.match(f.el('mfPreflightSummary').textContent,/80% cho từng ảnh/);
  f.el('mfMergeScenes').checked=true;await f.el('mfMergeScenes').fire('change');await f.search();await f.select(0);await f.preflight();
  assert.match(f.el('mfPreflightSummary').textContent,/1 nguồn → 1 ảnh đầu ra/);assert.match(f.el('mfPreflightSummary').textContent,/Ghép fill_missing/);
});
test('every new search option invalidates acknowledged preflight and source selection', async () => {
  for (const [id,event] of [['mfCloudMin','input'],['mfMinCoverage','input'],['mfMergeScenes','change'],['mfResultMode','change']]) {
    const f=fixture();await f.upload();await f.search();await f.select(0);await f.preflight();
    f.el('mfAcknowledge').checked=true;await f.el(id).fire(event);
    assert.equal(f.el('mfSearchResults').hidden,true,id);assert.equal(f.el('mfConfirmation').hidden,true,id);
    assert.equal(f.el('mfAcknowledge').checked,false,id);assert.equal(f.el('mfPreflight').disabled,true,id);assert.equal(f.el('mfSelectedSources').children.length,0,id);
  }
});
test('mosaic restored job shows ordered source info and one verified grouped TIFF', async () => {
  const f=fixture({saved:'mosaic-job',handler:() => response(jobData('completed',{job_id:'mosaic-job',total:1,processed:1,succeeded:1,parameters:{merge_scenes:true},sources:[scene(2,{band_coverage_pct:65}),scene(0,{band_coverage_pct:80})],scenes:[{...scene(2),status:'completed',filename:'mosaic.tif',download_url:`${API}/jobs/mosaic-job/files/result`,report:{actual_coverage_pct:100}}],download_url:`${API}/jobs/mosaic-job/download`}))});
  await tick();const panel=f.el('mfJobScenes');
  assert.match(panel.textContent,/chỉ bù NoData.*giữ nguyên mây/);
  const sources=panel.querySelector('ol');assert.equal(sources.children.length,2);assert.match(sources.children[0].textContent,/Ảnh chính.*HARMONIZED\/2/);assert.match(sources.children[1].textContent,/Bù NoData ưu tiên 1.*HARMONIZED\/0/);
  const rows=panel.querySelectorAll('article');assert.equal(rows.length,1);assert.match(rows[0].textContent,/Ảnh ghép.*Sentinel-2A/);
  assert.equal(rows[0].children.at(-1).href,`${API}/jobs/mosaic-job/files/result`);assert.equal(f.el('mfZip').hidden,false);assert.equal(f.timers.size,0);
});
test('SAR incompatible orbit preflight errors leave sources intact without creating a job', async () => {
  const f=fixture({handler:(url,options,next) => url.endsWith('/preflight') ? response({detail:{code:'incompatible_orbits',message:'Các nguồn SAR phải cùng hướng bay và quỹ đạo tương đối.'}},422) : next(url,options)});
  await f.upload();f.el('dataset').value='s1gee';f.el('mfMergeScenes').checked=true;await f.search();await f.select(0);await f.select(1);await f.preflight();
  assert.match(f.el('mfError').textContent,/cùng hướng bay/);assert.equal(f.el('mfConfirmation').hidden,true);assert.match(f.el('mfSelected').textContent,/2 \/ 10/);assert.ok(!f.requests.some(item => item.url === `${API}/jobs`));
});
