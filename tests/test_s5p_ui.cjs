const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'static', 's5p.js'), 'utf8');
function fixture(fetch) {
  const elements = new Map();
  function node() { return {hidden:false, disabled:false, value:'', textContent:'', style:{}, children:[], listeners:{},
    setAttribute(k,v) {this[k]=v;}, replaceChildren(...v) {this.children=v;}, append(...v) {this.children.push(...v);},
    addEventListener(k,v) {this.listeners[k]=v;}, querySelector() {return null;}, click() {this.clicked=true;}}; }
  function get(id) {if (!elements.has(id)) elements.set(id,node()); return elements.get(id);}
  get('dataset').value='s5p';
  const context = {document:{getElementById:get, createElement:node}, window:{}, fetch, AbortController, URL, setTimeout};
  vm.runInNewContext(source, context);
  return {get, api:context.window.S5P};
}
const data = {search_id:'snapshot',total:1,offset:0,limit:50,parameters:{product:'NO2',unit:'mol/m²'},
  scenes:[{timestamp:'2026-01-01T01:02:03Z',scene_id:'individual',download_url:'/download',preview_url:'/preview'}]};
test('renders individual timestamp, actions and empty state; invalidates on product change', async () => {
  const f=fixture(async () => ({ok:true,json:async()=>data}));
  await f.api.search({product:'NO2'});
  assert.equal(f.get('s5pResults').hidden,false);
  const row=f.get('s5pScenes').children[0];
  assert.match(row.children[0].textContent,/2026-01-01T01:02:03Z/);
  assert.equal(row.children[2].textContent,'Tải ảnh GeoTIFF');
  assert.equal(f.get('s5pNext').disabled,true);
  f.get('s5p_product').listeners.change();
  assert.equal(f.get('s5pResults').hidden,true);
  const empty=fixture(async()=>({ok:true,json:async()=>({...data,total:0,scenes:[]})}));
  await empty.api.search({});
  assert.match(empty.get('s5pMeta').textContent,/Không có ảnh/);
});
test('discards stale search responses after selection changes', async () => {
  let finish;
  const f=fixture(()=>new Promise(resolve=>finish=resolve));
  const pending=f.api.search({});
  f.get('selection_mode').listeners.change();
  finish({ok:true,json:async()=>data});
  await pending;
  assert.equal(f.get('s5pResults').hidden,true);
  assert.equal(f.get('s5pScenes').children.length,0);
});
test('request errors remain visible with busy state cleared', async () => {
  const f=fixture(async()=>({ok:false,json:async()=>({detail:'EE unavailable'})}));
  await f.api.search({});
  assert.equal(f.get('s5pMeta').textContent,'EE unavailable');
  assert.equal(f.get('s5pResults')['aria-busy'],'false');
});
test('workspace moves and observes Sentinel-5P controls and result panel', () => {
  const workspace=fs.readFileSync(path.join(__dirname,'..','static','workspace.js'),'utf8');
  assert.match(workspace,/move\(source, \[[^\]]*'s5pSelection'/);
  assert.match(workspace,/const resultIds = \[[^\]]*'s5pResults'/);
  assert.match(workspace,/move\(get\('workspaceResultContent'\), resultIds\)/);
  assert.match(workspace,/const resultNodes = resultIds\.map\(get\)/);
  assert.match(workspace,/resultNodes\.forEach\(node => observer\.observe\(node/);
});
