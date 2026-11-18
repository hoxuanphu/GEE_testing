const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const ID = '12345678-1234-4234-8234-123456789abc';
const OTHER = '87654321-4321-4321-8321-cba987654321';
const ROOT = `/api/timeseries/batches/${ID}`;
const plain = value => JSON.parse(JSON.stringify(value));
const batch = (status = 'running', extra = {}) => ({batch_id:ID, status,
  parameters:{dataset:'sentinel2', start_date:'2020-02-29', end_date:'2021-01-01', bands:['B4']},
  current_year:2020, error:null, years:[
    {year:2020, start_date:'2020-02-29', end_date:'2020-12-31', status:'searching', total:null, processed:0, succeeded:0, failed:0, error:null, zip_url:null, csv_url:null},
    {year:2021, start_date:'2021-01-01', end_date:'2021-01-01', status:'pending', total:null, processed:0, succeeded:0, failed:0, error:null, zip_url:null, csv_url:null}
  ], ...extra});

function fixture({saved = null, storageFails = false} = {}) {
  class Element {
    constructor(tag = 'div') { this.tagName = tag; this.children = []; this.listeners = {}; this.attrs = {}; this.textContent = ''; this.value = ''; this.checked = false; this.hidden = false; this.disabled = false; }
    append(...nodes) { for (const node of nodes) { node.parentElement = this; this.children.push(node); } }
    appendChild(node) { this.append(node); return node; }
    replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
    setAttribute(name, value) { this.attrs[name] = String(value); }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    dispatchEvent(event) { return Promise.all((this.listeners[event.type] || []).map(fn => fn(event))); }
    set innerHTML(value) { throw new Error('Untrusted HTML must never be parsed'); }
    get innerHTML() { throw new Error('Use safe text DOM'); }
  }
  const controls = new Element(), panel = new Element(), start = new Element('input'), end = new Element('input');
  controls.id = 'timeSeriesBatchControls'; panel.id = 'timeSeriesBatchPanel'; start.id = 'start_date'; end.id = 'end_date';
  start.value = '2020-02-29'; end.value = '2021-01-01';
  const walk = root => [root, ...root.children.flatMap(walk)];
  const get = id => [controls, panel, start, end].flatMap(walk).find(node => node.id === id) || null;
  const document = {createElement:tag => new Element(tag), getElementById:get};
  const calls = [], timers = new Map(), stored = new Map(saved ? [['gee.timeseries.batch_id', saved]] : []), views = [];
  let timerId = 0;
  const fetch = (url, options = {}) => new Promise((resolve, reject) => calls.push({url, options, resolve, reject}));
  const window = {document, fetch, SatelliteWorkspace:{showView:value => views.push(value)},
    localStorage:{getItem(key) { if (storageFails) throw Error('blocked'); return stored.get(key) || null; }, setItem(key, value) { if (storageFails) throw Error('blocked'); stored.set(key, value); }, removeItem(key) { stored.delete(key); }},
    setTimeout(fn, delay) { const id = ++timerId; timers.set(id, {fn, delay}); return id; }, clearTimeout(id) { timers.delete(id); }};
  const source = fs.existsSync('static/timeseries-batches.js') ? fs.readFileSync('static/timeseries-batches.js', 'utf8') : '';
  vm.runInNewContext(source, {window, document, fetch, setTimeout:window.setTimeout, clearTimeout:window.clearTimeout, console});
  const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
  const succeed = async (call, data) => { call.resolve({ok:true, json:async () => data}); await flush(); };
  return {api:window.TimeSeriesBatches, calls, timers, stored, views, controls, panel, get, walk, flush, succeed,
    all:tag => walk(panel).filter(node => node.tagName === tag),
    text:() => walk(panel).map(node => node.textContent).join(' '),
    click:async id => { const button = get(id); assert.ok(button, id); if (!button.disabled) { const pending = button.dispatchEvent({type:'click'}); await flush(); return pending; } },
    tick:async () => { const first = timers.entries().next().value; assert.ok(first, 'poll must be scheduled'); timers.delete(first[0]); const pending = first[1].fn(); await flush(); return pending; },
    enable() { assert.ok(this.api, 'annual batch controller should be exposed'); this.api.setAvailable(true); get('time_series_yearly').checked = true; },
    async start(data = batch()) { this.enable(); const pending = this.api.start({dataset:'sentinel2', bands:['B4']}); await succeed(calls.at(-1), data); await pending; }
  };
}

test('annual mode is opt-in and only explicit start submits an immutable request', async () => {
  const f = fixture(); assert.ok(f.api, 'annual batch controller should be exposed');
  assert.equal(f.api.enabled(), false); f.api.setAvailable(true);
  assert.equal(f.api.enabled(), false); f.get('time_series_yearly').checked = true;
  await f.get('time_series_yearly').dispatchEvent({type:'change'}); assert.equal(f.calls.length, 0);
  assert.equal(f.api.enabled(), true);
  const body = {dataset:'sentinel2', bands:['B4'], vector_roi_id:'fixed'};
  const pending = f.api.start(body); body.bands.push('B8'); body.vector_roi_id = 'changed';
  assert.equal(f.calls[0].url, '/api/timeseries/batches'); assert.equal(f.calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(f.calls[0].options.body), {dataset:'sentinel2', bands:['B4'], vector_roi_id:'fixed'});
  await f.succeed(f.calls[0], batch()); await pending;
  assert.equal(f.stored.get('gee.timeseries.batch_id'), ID); assert.deepEqual(f.views, ['jobs']);
  assert.match(f.text(), /2020-02-29/); assert.match(f.text(), /2021-01-01/);
  f.api.setAvailable(false); assert.equal(f.api.enabled(), false); assert.equal(f.get('time_series_yearly').disabled, true);
  assert.equal(f.timers.size, 1, 'selection changes do not stop existing batch');
});

test('unknown year totals remain unknown and server links are restricted to matching annual endpoints', async () => {
  const f = fixture(); const data = batch('partial');
  data.years[0] = {...data.years[0], status:'partial', total:3, processed:3, succeeded:2, failed:1,
    zip_url:`${ROOT}/years/2020/download`, csv_url:`${ROOT}/years/2020/csv`, error:'<img src=x onerror=bad()>'};
  data.years[1].zip_url = 'javascript:alert(1)'; data.years[1].csv_url = `/api/timeseries/batches/${OTHER}/years/2021/csv`;
  await f.start(data); assert.match(f.text(), /chưa biết/); assert.match(f.text(), /<img src=x/);
  assert.deepEqual(f.all('a').map(node => node.href), [`${ROOT}/years/2020/download`, `${ROOT}/years/2020/csv`]);
  assert.equal(f.timers.size, 0);
});

test('polling continues despite selection changes and exposes transient errors safely', async () => {
  const f = fixture(); await f.start(); f.api.setAvailable(false);
  const timer = f.timers.values().next().value; assert.equal(timer.delay, 2000);
  const polling = f.tick(); await f.flush(); assert.equal(f.calls.at(-1).url, ROOT);
  f.calls.at(-1).reject(new Error('<script>offline</script>')); await polling;
  assert.match(f.text(), /<script>offline/); assert.equal(f.timers.size, 1);
  const next = f.tick(); await f.flush(); await f.succeed(f.calls.at(-1), batch('completed')); await next;
  assert.equal(f.timers.size, 0); assert.equal(f.get('timeSeriesBatchError').textContent, '');
});

test('pause and resume reject an older in-flight poll that finishes after the command', async () => {
  const f = fixture(); await f.start();
  const polling = f.tick(); await f.flush(); const old = f.calls.at(-1);
  const pausing = f.click('timeSeriesBatchPause'); await f.flush();
  assert.equal(f.calls.at(-1).url, `${ROOT}/pause`); assert.equal(f.calls.at(-1).options.method, 'POST');
  await f.succeed(f.calls.at(-1), batch('paused')); await pausing;
  const resuming = f.click('timeSeriesBatchResume'); await f.flush();
  assert.equal(f.calls.at(-1).url, `${ROOT}/resume`);
  await f.succeed(f.calls.at(-1), batch('running')); await resuming;
  await f.succeed(old, batch('paused')); await polling;
  assert.match(f.get('timeSeriesBatchStatus').textContent, /Đang chạy/);
  assert.equal(f.get('timeSeriesBatchPause').hidden, false); assert.equal(f.get('timeSeriesBatchResume').hidden, true);
  assert.equal(f.timers.size, 1);
});

test('restore reads server state without starting or navigating and retry is explicit', async () => {
  const f = fixture({saved:ID}); assert.equal(f.calls.length, 1); assert.equal(f.calls[0].url, ROOT);
  assert.equal(f.calls[0].options.method || 'GET', 'GET');
  await f.succeed(f.calls[0], batch('interrupted')); assert.deepEqual(f.views, []); assert.equal(f.timers.size, 0);
  assert.equal(f.get('timeSeriesBatchResume').hidden, false);
  const resuming = f.click('timeSeriesBatchResume'); await f.flush(); await f.succeed(f.calls.at(-1), batch('partial')); await resuming;
  const retrying = f.click('timeSeriesBatchRetry'); await f.flush(); assert.equal(f.calls.at(-1).url, `${ROOT}/retry`);
  await f.succeed(f.calls.at(-1), batch('running')); await retrying; assert.equal(f.timers.size, 1);
});

test('a later explicit start ignores a stale restored response', async () => {
  const f = fixture({saved:OTHER}), restore = f.calls[0];
  await f.start(batch('completed')); await f.succeed(restore, batch('paused', {batch_id:OTHER}));
  assert.match(f.get('timeSeriesBatchStatus').textContent, /Hoàn tất/); assert.equal(f.stored.get('gee.timeseries.batch_id'), ID);
});

test('blocked local storage and structured server errors retain usable controls with safe text', async () => {
  const f = fixture({storageFails:true}); f.enable();
  let pending = f.api.start({dataset:'sentinel2'});
  f.calls[0].resolve({ok:false, json:async () => ({detail:{code:'busy', message:'<b>Worker busy</b>'}})}); await pending;
  assert.match(f.get('timeSeriesBatchError').textContent, /<b>Worker busy<\/b>/);
  pending = f.api.start({dataset:'sentinel2'}); await f.succeed(f.calls.at(-1), batch()); await pending;
  assert.equal(f.timers.size, 1); assert.equal(f.get('timeSeriesBatchError').textContent, '');
});

test('duplicate explicit submits cannot create two active batches', async () => {
  const f = fixture(); f.enable(); const first = f.api.start({dataset:'sentinel2'});
  await f.api.start({dataset:'sentinel2'}); assert.equal(f.calls.length, 1);
  await f.succeed(f.calls[0], batch()); await first;
});

test('restore reconnects after network and server failures before any batch state is known', async () => {
  const f = fixture({saved:ID});
  f.calls[0].reject(new Error('Temporary network failure')); await f.flush();
  assert.equal(f.timers.size, 1, 'a failed initial restore must retry its saved ID');
  assert.match(f.get('timeSeriesBatchError').textContent, /Temporary network failure/);
  let polling = f.tick(); await f.flush(); assert.equal(f.calls.at(-1).url, ROOT);
  f.calls.at(-1).resolve({ok:false, status:503, json:async () => ({detail:{message:'<b>Restarting</b>'}})});
  await polling; assert.equal(f.timers.size, 1); assert.match(f.text(), /<b>Restarting<\/b>/);
  polling = f.tick(); await f.flush(); await f.succeed(f.calls.at(-1), batch('running')); await polling;
  assert.equal(f.timers.size, 1); assert.equal(f.get('timeSeriesBatchError').textContent, '');
  assert.deepEqual(f.views, []); assert.equal(f.calls.every(call => (call.options.method || 'GET') === 'GET'), true);
  polling = f.tick(); await f.flush(); await f.succeed(f.calls.at(-1), batch('paused')); await polling;
  assert.equal(f.timers.size, 0, 'a restored paused batch must stay paused');
});

test('a missing saved batch clears its ID and stops retry with a safe visible error', async () => {
  const f = fixture({saved:ID});
  f.calls[0].resolve({ok:false, status:404, json:async () => ({detail:{message:'<script>Expired batch</script>'}})});
  await f.flush();
  assert.equal(f.stored.has('gee.timeseries.batch_id'), false);
  assert.equal(f.timers.size, 0); assert.equal(f.panel.hidden, false);
  assert.match(f.get('timeSeriesBatchError').textContent, /<script>Expired batch<\/script>/);
  assert.deepEqual(f.views, []); assert.equal(f.calls.length, 1);
});

test('a running batch that expires stops polling and hides obsolete actions and downloads', async () => {
  const f = fixture(); const data = batch();
  data.years[0].zip_url = `${ROOT}/years/2020/download`; await f.start(data);
  const polling = f.tick(); await f.flush();
  f.calls.at(-1).resolve({ok:false, status:404, json:async () => ({detail:'Batch expired'})}); await polling;
  assert.equal(f.timers.size, 0); assert.equal(f.stored.has('gee.timeseries.batch_id'), false);
  assert.equal(f.get('timeSeriesBatchPause').hidden, true); assert.equal(f.all('a').length, 0);
  assert.match(f.text(), /Batch expired/);
});

test('late restore errors cannot clear a newly started batch ID or start another retry', async () => {
  const f = fixture({saved:OTHER}), restore = f.calls[0];
  await f.start(batch('completed'));
  restore.resolve({ok:false, status:404, json:async () => ({detail:'Old batch missing'})}); await f.flush();
  assert.equal(f.stored.get('gee.timeseries.batch_id'), ID); assert.equal(f.timers.size, 0);
  assert.equal(f.get('timeSeriesBatchError').textContent, '');
});
