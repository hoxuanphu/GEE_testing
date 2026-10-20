const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ROI = require('../static/rectangle.js');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const inline = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match => match[1]).find(code => code.includes('const $ ='));
const esri = fs.readFileSync(path.join(__dirname, '..', 'static', 'esri.js'), 'utf8');
const base = {center_lon:105, center_lat:12, width_m:8000, height_m:2000, rotation_deg:30};
const plain = value => JSON.parse(JSON.stringify(value));
function close(a,b,tolerance=1e-8) { assert.ok(Math.abs(a-b) < tolerance, `${a} != ${b}`); }

class Element {
  constructor(id = '') {
    this.id = id; this.value = ''; this.checked = false; this.hidden = false; this.disabled = false;
    this.style = {}; this.dataset = {}; this.children = []; this.listeners = {}; this.textContent = '';
    this.classList = {toggle() {}};
  }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  removeEventListener(type, callback) { this.listeners[type] = (this.listeners[type] || []).filter(fn => fn !== callback); }
  async fire(type, extra = {}) {
    const event = {target:this, type, preventDefault() {}, stopImmediatePropagation() {}, ...extra};
    for (const fn of this.listeners[type] || []) await fn(event);
    await new Promise(setImmediate);
  }
  dispatchEvent(event) { this.fire(event.type); return true; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  add(child) { this.children.push(child); }
  closest() { return new Element(); }
  removeAttribute(key) { delete this[key]; }
  querySelectorAll() { return []; }
  set innerHTML(value) { this.children = []; this._html = value; }
  get innerHTML() { return this._html || ''; }
}
function fixture(fetchHandler = async () => response({})) {
  const elements = new Map(), requests = [], fits = [], visible = new Set();
  const el = id => { if (!elements.has(id)) elements.set(id,new Element(id)); return elements.get(id); };
  for (const match of html.matchAll(/id="([^"]+)"[^>]*>/g)) {
    const element = el(match[1]);
    element.value = (match[0].match(/\bvalue="([^"]*)"/) || [,''])[1];
    element.checked = /\bchecked\b/.test(match[0]);
  }
  el('selection_mode').value = 'point'; el('dataset').value = 'sentinel2';
  el('esriMode').value = 'point'; el('s2_source').value = 'cdse'; el('slc_mode').value = 'scenes';
  const inputIds = [...html.matchAll(/<(?:input|select)\b[^>]*id="([^"]+)"/g)].map(match => match[1]);
  const doc = Object.assign(new Element(), {getElementById:el, querySelectorAll:selector => inputIds.filter(id => !selector.includes(':not(#time_series_enabled)') || id !== 'time_series_enabled').map(el), createElement:() => new Element(), activeElement:null});
  const container = new Element(); container.ownerDocument = doc;
  container.setPointerCapture = id => { container.captured = id; };
  container.hasPointerCapture = id => container.captured === id;
  container.releasePointerCapture = () => { container.captured = null; };
  const map = {getContainer:() => container, setView() { return this; }, on() {}, off() {},
    fitBounds:bounds => fits.push(bounds), hasLayer:layer => visible.has(layer), removeLayer:layer => visible.delete(layer),
    mouseEventToLatLng:event => event.latlng, getCenter:() => ({lat:12,lng:105}),
    latLngToContainerPoint:([lat,lng]) => ({x:lng*100,y:-lat*100}),
    containerPointToLatLng:([x,y]) => ({lat:-y/100,lng:x/100})};
  for (const key of ['dragging','doubleClickZoom','scrollWheelZoom','boxZoom','keyboard','touchZoom']) {
    map[key] = {state:key !== 'scrollWheelZoom', enabled() { return this.state; }, enable() { this.state = true; }, disable() { this.state = false; }};
  }
  function layer() { return {children:[], addTo(target) { if (target.children) target.children.push(this); else visible.add(this); return this; }, clearLayers() { this.children = []; }, getElement:() => ({dataset:{},style:{}}), getBounds:() => [], setLatLng() {}, bindTooltip() {}, on() {}, bringToFront() {}}; }
  const L = {map:() => map, tileLayer:layer, layerGroup:layer, polygon:layer, polyline:layer, marker:layer, divIcon:x => x,
    geoJSON:layer, control:{layers:() => ({addTo() { return this; }, addOverlay() {}})}, DomEvent:{stopPropagation() {}}};
  const context = vm.createContext({document:doc, window:{addEventListener() {}}, L, RectangleROI:ROI, console,
    fetch:async (url, options = {}) => {
      if (String(url).startsWith('/api/admin/')) return response(url.includes('geojson') ? {type:'FeatureCollection',features:[]} : {provinces:[],communes:[]});
      requests.push({url,options,body:options.body ? JSON.parse(options.body) : undefined});
      return fetchHandler(url,options);
    }, localStorage:{getItem:() => null,setItem() {},removeItem() {}}, Event:class {constructor(type) {this.type = type;}},
    AbortController, URL, URLSearchParams, encodeURIComponent, Option:class {},
    performance:{now:() => 100}, location:{origin:'http://localhost'},
    setTimeout:() => 1, clearTimeout() {}, requestAnimationFrame:fn => { fn(); return 1; }, cancelAnimationFrame() {}});
  vm.runInContext(inline,context);
  const run = code => vm.runInContext(code,context);
  return {context,run,el,map,container,requests,fits,visible,
    editor:run('rectangleEditor'), async rectangle(rect=base) {
      el('selection_mode').value = 'rectangle'; await el('selection_mode').fire('change');
      run('rectangleEditor').cancel(); run('rectangleEditor').set(rect);
    }, loadEsri() { vm.runInContext(esri,context); }};
}
function response(data,ok=true) { return {ok, headers:{get:() => ''}, json:async () => data}; }
function scene() { return {scene_id:'scene',dataset:'sentinel2',date:'2026-10-01',cloud_pct:1,scale_m:10,bands:['B02'],selection_mode:'rectangle',roi_geometry:ROI.geometry(base),download_url:'/download?snapshot=1'}; }
function pointer(f,type,id,latlng,handle) {
  return f.container.fire(type,{pointerId:id,button:0,latlng,target:{closest:selector => selector === '[data-roi-handle]' && handle ? {dataset:{roiHandle:handle}} : null}});
}

test('geometry matches backend clockwise non-square local-metre formula', () => {
  const geometry = ROI.geometry(base), [lon,lat] = geometry.coordinates[0][0];
  const a = Math.PI/6, x=-4000,y=-1000;
  close(lon,105+(x*Math.cos(a)+y*Math.sin(a))/(111320*Math.cos(12*Math.PI/180)));
  close(lat,12+(-x*Math.sin(a)+y*Math.cos(a))/111320);
  assert.deepEqual(geometry.coordinates[0][0],geometry.coordinates[0][4]);
  const [lx,ly] = ROI.local(base,lon,lat); close(lx,x); close(ly,y);
  const box = ROI.bbox(base);
  assert.ok(geometry.coordinates[0].every(([x,y]) => x >= box[0] && x <= box[2] && y >= box[1] && y <= box[3]));
});
test('0 and 360 agree; 90 degree rotation does not square the ROI', () => {
  assert.deepEqual(ROI.geometry({...base,rotation_deg:0}),ROI.geometry({...base,rotation_deg:360}));
  const box = ROI.bbox({...base,rotation_deg:90});
  close((box[2]-box[0])*111320*Math.cos(base.center_lat*Math.PI/180),base.height_m);
  close((box[3]-box[1])*111320,base.width_m);
});
test('reject invalid, blank, excessive, dateline and polar rectangles', () => {
  for (const changes of [{width_m:0},{height_m:-1},{width_m:90001},{rotation_deg:361},{rotation_deg:-1},
    {center_lon:NaN},{center_lat:Infinity},{center_lon:''},{center_lon:181},{center_lat:86},{center_lon:179.999},{center_lat:85.051}]) {
    assert.ok(ROI.validate({...base,...changes}),JSON.stringify(changes));
    assert.throws(() => ROI.snapshot({...base,...changes}));
  }
  assert.equal(ROI.validate({...base,width_m:90000,height_m:90000}), '');
});
test('draw, translate, symmetric corner/edge resize and rotate are deterministic', () => {
  const drawn = ROI.draw({lat:12,lng:105},{lat:12.01,lng:105.02});
  close(drawn.height_m,1113.2); assert.notEqual(drawn.width_m,drawn.height_m);
  const moved = ROI.edit(base,'move',{lng:105,lat:12},{lng:106,lat:13});
  assert.equal(moved.center_lon,106); assert.equal(moved.center_lat,13); assert.equal(moved.width_m,8000);
  const [lng,lat] = ROI.point(base,5000,1800);
  const corner = ROI.edit(base,'ne',{}, {lng,lat}); close(corner.width_m,10000); close(corner.height_m,3600);
  const edge = ROI.edit(base,'e',{}, {lng,lat}); close(edge.width_m,10000); assert.equal(edge.height_m,2000);
  const rotated = ROI.edit(base,'rotate',{}, {lng:105.1,lat:12}); close(rotated.rotation_deg,90);
});
test('flat request body/query preserve dimensions and reject mixed point/admin selections', () => {
  const input = {...base}, body = ROI.body({dataset:'sentinel2',lon:1,lat:2,radius_m:4,province_id:'a',commune_id:'b'},input);
  assert.deepEqual(body,{dataset:'sentinel2',selection_mode:'rectangle',...base});
  input.width_m=1; assert.equal(body.width_m,8000);
  const query = new URLSearchParams(ROI.query(base));
  for (const key of ROI.fields) assert.equal(Number(query.get(key)),base[key]);
  assert.ok(!query.has('radius_m'));
});
test('controller draws, restores only previously enabled map interactions, fits and clears', async () => {
  const f = fixture(); await f.rectangle();
  f.editor.arm(); assert.equal(f.map.dragging.enabled(),false);
  await pointer(f,'pointerdown',1,{lat:12,lng:105});
  await pointer(f,'pointermove',1,{lat:12.01,lng:105.02});
  await pointer(f,'pointerup',1,{lat:12.01,lng:105.02});
  close(f.editor.get().height_m,1113.2);
  assert.equal(f.map.dragging.enabled(),true); assert.equal(f.map.scrollWheelZoom.enabled(),false);
  await f.el('rectangleFit').fire('click'); assert.equal(f.fits.length,1);
  await f.el('rectangleClear').fire('click'); assert.throws(() => f.editor.get());
  assert.equal(f.editor.group.children.length,0);
});
test('pointer cancel rolls back editing, ignores other pointers and restores interaction state', async () => {
  const f = fixture(); await f.rectangle();
  await pointer(f,'pointerdown',7,{lat:12,lng:105},'move');
  await pointer(f,'pointermove',8,{lat:13,lng:106},'move'); assert.equal(f.editor.get().center_lon,105);
  await pointer(f,'pointermove',7,{lat:13,lng:106},'move'); assert.equal(f.editor.get().center_lon,106);
  await pointer(f,'pointercancel',7,{lat:13,lng:106},'move');
  assert.deepEqual(f.editor.get(),base); assert.equal(f.map.dragging.enabled(),true); assert.equal(f.map.scrollWheelZoom.enabled(),false);
});
test('mode switching cancels drawing/editing without losing rectangle and preserves admin layers', async () => {
  const f = fixture(); await f.rectangle(); const boundary = {}; f.visible.add(boundary);
  await pointer(f,'pointerdown',1,{lat:12,lng:105},'move');
  await pointer(f,'pointermove',1,{lat:12.1,lng:105.1},'move');
  f.el('selection_mode').value = 'point'; await f.el('selection_mode').fire('change');
  assert.equal(f.editor.active,false); assert.equal(f.map.dragging.enabled(),true); assert.deepEqual(f.editor.get(),base);
  assert.ok(f.visible.has(boundary));
  f.el('selection_mode').value='rectangle'; await f.el('selection_mode').fire('change');
  assert.equal(f.editor.active,true); assert.ok(f.editor.group.children.length >= 11);
});
test('invalid numeric edits suppress geometry and requests; 360 remains a valid input', async () => {
  const f = fixture(); await f.rectangle();
  f.el('rect_center_lon').value='181'; await f.el('rect_center_lon').fire('input');
  assert.throws(() => f.editor.get()); assert.equal(f.editor.group.children.length,0);
  await f.el('btnSearch').fire('click'); assert.equal(f.requests.length,0);
  f.editor.set({...base,rotation_deg:360}); assert.equal(f.editor.get().rotation_deg,360);
});
test('scene and S1 GEE requests contain rectangle only; response cannot replace handles', async () => {
  const f = fixture(async () => response(scene())); await f.rectangle();
  await f.el('btnSearch').fire('click');
  assert.equal(f.requests[0].url,'/api/scene');
  for (const key of ROI.fields) assert.equal(f.requests[0].body[key],base[key]);
  for (const key of ['lon','lat','radius_m','province_id','commune_id']) assert.ok(!(key in f.requests[0].body));
  assert.equal(f.el('result').style.display,'block'); assert.ok(f.editor.group.children.length >= 11);
  f.el('dataset').value='s1gee'; await f.el('dataset').fire('change'); await f.el('btnSearch').fire('click');
  assert.equal(f.requests[1].url,'/api/s1gee/scene'); assert.equal(f.requests[1].body.height_m,2000);
  assert.ok(!('radius_m' in f.requests[1].body));
});
test('old in-flight scene response cannot restore results after a rectangle edit', async () => {
  let resolve; const f = fixture(() => new Promise(done => resolve=done)); await f.rectangle();
  const pending = f.el('btnSearch').fire('click');
  f.editor.set({...base,width_m:1000}); resolve(response(scene())); await pending;
  assert.equal(f.el('result').style.display,'none'); assert.equal(f.el('dl').href,undefined);
  assert.equal(f.el('searchTiming').textContent,'');
});
test('rectangle catalogue searches use envelope endpoint, retain cloud controls and immutable crop links', async () => {
  let saved;
  const f = fixture(async (_url,options) => {
    saved = JSON.parse(options.body);
    return response({search_id:'catalog1',total:1,offset:0,parameters:{...saved,rectangle:{...base}},scenes:[{id:'product',name:'name',cloud_pct:2,download_url:'/snapshot/download'}]});
  });
  await f.rectangle(); f.el('dataset').value='s2aws'; await f.el('dataset').fire('change');
  await f.el('btnSearch').fire('click');
  assert.equal(f.requests[0].url,'/api/scene-catalog'); assert.equal(saved.source,'aws'); assert.equal(saved.cloud_min,0); assert.equal(saved.cloud_max,100);
  assert.equal(saved.width_m,8000); assert.ok(!('radius_m' in saved));
  const row = f.el('sceneCatalogRows').children[0];
  const crop = row.children.find(child => String(child.href).startsWith('/api/s2/aws/download?'));
  const original = crop.href; assert.equal(new URL(crop.href,'http://localhost').searchParams.get('height_m'),'2000');
  f.editor.set({...base,height_m:5000}); assert.equal(crop.href,original); assert.equal(f.el('sceneCatalogResults').hidden,true);
  f.el('least_cloud_enabled').checked=false; f.el('cloud_min').value='12'; f.el('cloud_max').value='48';
  await f.el('btnSearch').fire('click'); assert.equal(saved.cloud_min,12); assert.equal(saved.cloud_max,48);
});
test('time series rectangle body omits radius and survives page/download snapshot flow', async () => {
  const f = fixture(async url => response(url === '/api/timeseries/jobs'
    ? {status:'completed',job_id:'job',total:1,processed:1,succeeded:1,failed:0}
    : {search_id:'series',total:1,scenes:[{asset_id:'asset',date:'2026-10-01'}],parameters:{rectangle:base}}));
  await f.rectangle(); f.el('time_series_enabled').checked=true; await f.el('time_series_enabled').fire('change');
  await f.el('btnSearch').fire('click');
  assert.equal(f.requests[0].url,'/api/timeseries/search'); assert.equal(f.requests[0].body.width_m,8000); assert.ok(!('radius_m' in f.requests[0].body));
  await f.el('timeSeriesDownload').fire('click'); assert.equal(f.el('timeSeriesZip').href,'/api/timeseries/jobs/job/download');
  f.editor.set({...base,rotation_deg:90}); assert.equal(f.el('timeSeriesZip').hidden,true);
});
test('unsupported rectangle S1/SLC pairs/S3 searches never silently send point requests', async () => {
  const f = fixture(); await f.rectangle();
  for (const dataset of ['s1','s1slc','s3']) {
    f.el('dataset').value=dataset; f.el('slc_mode').value='pairs'; await f.el('dataset').fire('change');
    await f.el('btnSearch').fire('click'); assert.match(f.el('status').textContent,/chưa hỗ trợ/);
  }
  assert.equal(f.requests.length,0);
});
test('known GRD IDs create rectangle crop/mosaic snapshot links and reject invalid IDs', async () => {
  const f = fixture(); await f.rectangle(); f.el('dataset').value='s1'; await f.el('dataset').fire('change');
  const id='01234567-89ab-cdef-0123-456789abcdef'; f.el('rectangleGrdIds').value=id;
  await f.el('rectangleGrdCrop').fire('click');
  const link=f.el('rectangleGrdDownload'); assert.match(link.href,/^\/api\/s1\/crop\?/); assert.equal(new URL(link.href,'http://localhost').searchParams.get('width_m'),'8000');
  f.el('rectangleGrdIds').value=id+','+id; await f.el('rectangleGrdCrop').fire('click'); assert.match(link.href,/crop-mosaic/);
  const saved=link.href; f.editor.set({...base,width_m:1000}); assert.equal(link.href,saved); assert.equal(link.hidden,true);
  f.el('rectangleGrdIds').value='bad'; await f.el('rectangleGrdCrop').fire('click'); assert.match(f.el('status').textContent,/UUID/);
});
test('Esri submits rectangle snapshot, switches modes and labels persisted job dimensions', async () => {
  const f = fixture(async (_url,options) => response({status:'completed',job_id:'esri1',request:JSON.parse(options.body)}));
  f.loadEsri(); f.el('esriMode').value='rectangle'; await f.el('esriMode').fire('change'); f.editor.set(base);
  await f.el('esriStart').fire('click');
  assert.equal(f.requests[0].url,'/api/esri/jobs'); assert.deepEqual(f.requests[0].body,{zoom:18,workers:4,max_tiles:4000,selection_mode:'rectangle',...base});
  assert.match(f.el('esriStatus').textContent,/8000 × 2000/);
  const href=f.el('esriDownload').href; f.editor.set({...base,width_m:9000}); assert.equal(f.el('esriDownload').href,href);
  f.el('selection_mode').value='point'; await f.el('selection_mode').fire('change'); assert.equal(f.el('esriMode').value,'point');
});
test('CDSE rectangle catalogue offers band crop and explicitly uncropped ZIP without point fields', async () => {
  const f = fixture(async (_url,options) => {
    const body=JSON.parse(options.body);
    return response({search_id:'cdse',total:1,offset:0,parameters:{...body,rectangle:base},scenes:[{id:'id',name:'scene',cloud_pct:12,download_url:'/snapshot/cdse'}]});
  });
  await f.rectangle(); f.el('dataset').value='s2'; f.el('s2_source').value='cdse'; await f.el('dataset').fire('change');
  await f.el('btnSearch').fire('click');
  const children=f.el('sceneCatalogRows').children[0].children;
  const bands=children.find(child => String(child.href).startsWith('/api/s2/bands?'));
  const query=new URL(bands.href,'http://localhost').searchParams;
  assert.equal(query.get('selection_mode'),'rectangle'); assert.equal(query.get('action'),'download');
  assert.equal(query.get('width_m'),'8000'); assert.ok(!query.has('lon')); assert.ok(!query.has('radius_m'));
  const raw=children.find(child => String(child.href).startsWith('/api/s2/download?'));
  assert.match(raw.textContent,/không cắt ROI/); assert.ok(!raw.href.includes('selection_mode'));
});
test('stale failures and catalogue responses do not overwrite edited ROI UI', async () => {
  let reject,resolve;
  const f=fixture(() => new Promise((_resolve,_reject) => {reject=_reject;resolve=_resolve;}));
  await f.rectangle(); const pending=f.el('btnSearch').fire('click');
  f.editor.set({...base,rotation_deg:45}); f.el('status').textContent='current'; reject(new Error('old request failed')); await pending;
  assert.equal(f.el('status').textContent,'current'); assert.equal(f.el('result').style.display,'none');
  f.el('dataset').value='s2aws'; await f.el('dataset').fire('change');
  const listing=f.el('btnSearch').fire('click'); f.editor.set({...base,height_m:4000});
  resolve(response({total:1,offset:0,parameters:{cloud_min:0,cloud_max:100},scenes:[]})); await listing;
  assert.equal(f.el('sceneCatalogResults').hidden,true);
});
test('blank initial numeric form can be completed without drawing, with default zero rotation', async () => {
  const f=fixture(); f.el('selection_mode').value='rectangle'; await f.el('selection_mode').fire('change');
  for (const [key,value] of Object.entries({...base,rotation_deg:0})) {
    f.el('rect_'+key).value=String(value); await f.el('rect_'+key).fire('input');
  }
  assert.deepEqual(f.editor.get(),{...base,rotation_deg:0}); assert.equal(f.map.dragging.enabled(),true);
});
test('resize and rotate gestures support cancellation, pointer capture loss and keyboard Escape', async () => {
  const f=fixture(); await f.rectangle();
  const [lng,lat]=ROI.point(base,7000,3000);
  await pointer(f,'pointerdown',2,{lat:12,lng:105},'ne'); await pointer(f,'pointermove',2,{lat,lng},'ne');
  close(f.editor.get().width_m,14000); close(f.editor.get().height_m,6000);
  await pointer(f,'pointerup',2,{lat,lng},'ne');
  await pointer(f,'pointerdown',3,{lat:12,lng:105},'rotate');
  await pointer(f,'pointermove',3,{lat:12,lng:105.1},'rotate'); close(f.editor.get().rotation_deg,90);
  await pointer(f,'lostpointercapture',3,{lat:12,lng:105.1},'rotate'); close(f.editor.get().rotation_deg,30);
  assert.equal(f.map.dragging.enabled(),true);
  f.editor.arm(); await f.container.ownerDocument.fire('keydown',{key:'Escape'});
  assert.equal(f.editor.armed,false); assert.equal(f.map.dragging.enabled(),true);
  f.editor.arm(); f.container.ownerDocument.hidden=true; await f.container.ownerDocument.fire('visibilitychange');
  assert.equal(f.editor.armed,false); assert.equal(f.map.scrollWheelZoom.enabled(),false);
});
test('point and administrative request shapes remain unchanged when switching away from rectangle', async () => {
  const f=fixture(); await f.rectangle();
  f.el('selection_mode').value='point'; f.el('lon').value='105'; f.el('lat').value='12'; await f.el('selection_mode').fire('change');
  let body=f.run("opticalSceneBody('point', '', '', 105, 12)");
  assert.equal(body.lon,105); assert.equal(body.radius_m,2000); assert.ok(!('width_m' in body));
  f.el('rotation_deg').value='30'; f.el('selection_mode').value='province'; await f.el('selection_mode').fire('change');
  body=f.run("opticalSceneBody('province', 'VN', '', null, null)");
  assert.equal(body.province_id,'VN'); assert.equal(body.rotation_deg,0); assert.ok(!('width_m' in body));
  f.el('selection_mode').value='commune'; f.el('commune_id').value=''; await f.el('selection_mode').fire('change');
  body=f.run("opticalSceneBody('commune', '', '001', null, null)");
  assert.equal(body.commune_id,'001'); assert.equal(body.rotation_deg,0); assert.ok(!('width_m' in body));
});
test('Esri invalid rectangle cannot create a job and explicit bbox remains a separate mode', async () => {
  const f=fixture(); f.loadEsri(); await f.rectangle({...base,center_lon:180});
  await f.el('esriStart').fire('click'); assert.equal(f.requests.length,0); assert.match(f.el('esriStatus').textContent,/ROI/);
  f.el('esriMode').value='bbox'; await f.el('esriMode').fire('change');
  for (const [key,value] of Object.entries({esriWest:104,esriSouth:11,esriEast:106,esriNorth:13})) f.el(key).value=String(value);
  await f.el('esriStart').fire('click');
  assert.deepEqual(f.requests[0].body.bbox,[104,11,106,13]); assert.ok(!('width_m' in f.requests[0].body));
});
test('destroy cancels pending gesture, restores map and removes all controller listeners', async () => {
  const f=fixture(); await f.rectangle(); f.editor.arm();
  await pointer(f,'pointerdown',1,{lat:12,lng:105}); await pointer(f,'pointermove',1,{lat:12.01,lng:105.01});
  f.editor.destroy(); assert.equal(f.map.dragging.enabled(),true); assert.equal(f.map.scrollWheelZoom.enabled(),false);
  assert.equal(f.visible.has(f.editor.group),false); assert.equal(f.container.listeners.pointerdown.length,0);
  assert.deepEqual(f.editor.get(),base);
});
test('drawing leaves Leaflet controls usable and ignores non-primary/right pointer events', async () => {
  const f=fixture(); await f.rectangle(); f.editor.arm();
  await f.container.fire('pointerdown',{pointerId:2,button:0,latlng:{lat:12,lng:105},target:{closest:selector => selector === '.leaflet-control' ? {} : null}});
  assert.equal(f.editor.gesture,null);
  await f.container.fire('pointerdown',{pointerId:3,button:2,latlng:{lat:12,lng:105}}); assert.equal(f.editor.gesture,null);
  await f.container.fire('pointerdown',{pointerId:4,button:0,isPrimary:false,latlng:{lat:12,lng:105}}); assert.equal(f.editor.gesture,null);
  f.editor.cancel();
});
test('whole ZIP links explicitly disclose no ROI crop and no new CDN/package dependency is added', () => {
  assert.ok(!/textContent = '(?:Tải ZIP|S3|Tải SLC ZIP)'/.test(html));
  assert.match(html,/ZIP nguyên sản phẩm \(không cắt ROI\)/);
  assert.match(html,/src="\/rectangle.js/);
  assert.ok(!fs.readFileSync(path.join(__dirname,'..','static','rectangle.js'),'utf8').includes('https://'));
});
