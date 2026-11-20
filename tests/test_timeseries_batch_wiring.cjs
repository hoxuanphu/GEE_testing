const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(yearly) {
  const html = fs.readFileSync('static/index.html','utf8');
  const source = html.slice(html.indexOf('async function searchTimeSeries(body)'),html.indexOf('function renderTimeSeriesResults(data)'));
  const calls = [], nodes = {};
  const context = {window:{TimeSeriesBatches:{enabled:()=>yearly,start:async body=>{calls.push(['batch',body]);return {batch_id:'new'};}}},
    $:id=>nodes[id]??=( {disabled:false,textContent:''}), timeSeriesVersion:1, AbortController,
    invalidateTimeSeries:()=>calls.push(['invalidate']),
    timeSeriesRequest:async(path,options)=>{calls.push(['legacy',path,JSON.parse(options.body)]);return {total:1};},
    renderTimeSeriesResults:data=>calls.push(['render',data])};
  vm.createContext(context);vm.runInContext(source,context);
  return {run:body=>context.searchTimeSeries(body),calls,nodes};
}
test('explicit yearly selection sends immutable selection to batch without enumerating the legacy search',async()=>{
  const f=fixture(true),body={selection_mode:'vector',vector_roi_id:'saved',dataset:'sentinel2',start_date:'2020-12-31',end_date:'2021-01-01'};
  await f.run(body);
  assert.deepEqual(f.calls.filter(c=>c[0]==='batch'),[['batch',body]]);
  assert.equal(f.calls.some(c=>c[0]==='legacy'),false);
  assert.equal(f.nodes.btnSearch.disabled,false);
});
test('legacy time series keeps its existing search and rendering when yearly mode is off',async()=>{
  const f=fixture(false),body={dataset:'sentinel2'};await f.run(body);
  assert.equal(f.calls.some(c=>c[0]==='batch'),false);
  assert.deepEqual(f.calls.find(c=>c[0]==='legacy').slice(1),['/api/timeseries/search',body]);
  assert.equal(f.calls.some(c=>c[0]==='render'),true);
});
