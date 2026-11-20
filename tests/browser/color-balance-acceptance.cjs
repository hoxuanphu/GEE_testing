// Disposable local fixture + native Chrome CDP; no npm dependencies or user profile.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {once} = require('node:events');

const root = path.resolve(__dirname, '../..');
const requests = [], errors = [];
const report = {method:'clear_overlap_affine_v1',spectral_values_modified:true,
  scenes:[{status:'reference'},{status:'applied'},{status:'skipped',reason:'insufficient_overlap'}]};
const server = http.createServer(async (req,res) => {
  if (req.url.startsWith('/api/')) {
    let body = ''; for await (const chunk of req) body += chunk;
    let data = {provinces:[],communes:[],features:[],platforms:[]};
    if (req.url === '/api/scene') {
      requests.push(JSON.parse(body));
      const requested = requests.at(-1);
      data = {dataset:'sentinel2',scene_id:'fixture-raw-balanced',date:'2026-01-01',cloud_pct:10,
        selection_mode:'point',composite:true,composite_method:'raw',scene_count:3,scale_m:10,bands:['B2','B3','B4'],
        bbox:{west:106,south:9,east:108,north:11},color_balance:report,
        download_url:'/api/download/sentinel2?color_balance=true&color_snapshot=fixture'};
      if (requested.composite_method === 'mosaic') {
        data.composite_method='mosaic'; data.color_balance=null;
        data.mosaic_mask_clouds=requested.mosaic_mask_clouds;
        data.scene_grouping={mode:requested.scene_grouping,max_scene_span_days:requested.max_scene_span_days,
          span_days:.5,data_coverage_pct:100,output_coverage_pct:requested.mosaic_mask_clouds ? 75.15 : 100};
      }
    }
    res.writeHead(200,{'Content-Type':'application/json'}); res.end(JSON.stringify(data)); return;
  }
  const pathname = decodeURIComponent(req.url.split('?')[0]);
  const file = path.resolve(root, 'static', pathname === '/' ? 'index.html' : '.' + pathname);
  if (!file.startsWith(path.join(root,'static') + path.sep) || !fs.existsSync(file)) {
    res.writeHead(404); res.end(); return;
  }
  res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'});
  res.end(fs.readFileSync(file));
});

async function until(check) {
  const deadline = Date.now() + 30000;
  while (Date.now() < deadline) { if (await check()) return; await new Promise(resolve => setTimeout(resolve,100)); }
  throw new Error('Browser check timed out');
}

(async () => {
  await new Promise(resolve => server.listen(0,'127.0.0.1',resolve));
  const port = server.address().port, debugPort = 19227;
  const profile = fs.mkdtempSync(path.join(root,'downloads','color-balance-chrome-'));
  const browser = spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--remote-debugging-address=127.0.0.1',`--remote-debugging-port=${debugPort}`,`--user-data-dir=${profile}`,'about:blank'
  ],{windowsHide:true,stdio:'ignore'});
  let socket;
  try {
    let targets;
    await until(async () => { try { targets = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json(); return targets.some(t=>t.type==='page'); } catch { return false; } });
    const target = targets.find(t=>t.type==='page');
    socket = new WebSocket(target.webSocketDebuggerUrl); await once(socket,'open');
    let id = 0; const pending = new Map();
    socket.addEventListener('message', event => {
      const packet = JSON.parse(event.data);
      if (packet.id) { const pair = pending.get(packet.id); pending.delete(packet.id); if(packet.error) pair.reject(new Error(JSON.stringify(packet.error))); else pair.resolve(packet.result); }
      if (packet.method === 'Runtime.exceptionThrown') errors.push(packet.params.exceptionDetails.text + ': ' + (packet.params.exceptionDetails.exception?.description || ''));
    });
    const cdp = (method,params={}) => new Promise((resolve,reject)=>{ const seq=++id; pending.set(seq,{resolve,reject}); socket.send(JSON.stringify({id:seq,method,params})); });
    const run = async expression => {
      const value = await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
      if (value.exceptionDetails) throw new Error(value.exceptionDetails.exception?.description || value.exceptionDetails.text);
      return value.result.value;
    };
    await cdp('Runtime.enable'); await cdp('Page.enable');
    await cdp('Emulation.setDeviceMetricsOverride',{width:1360,height:1000,deviceScaleFactor:1,mobile:false});
    await cdp('Page.navigate',{url:`http://127.0.0.1:${port}/`});
    await until(()=>run("typeof opticalSceneBody === 'function' && typeof rectangleEditor !== 'undefined'"));
    const initial = await run(`(() => {
      document.getElementById('merge_scenes').checked=true;
      document.getElementById('merge_scenes').dispatchEvent(new Event('change'));
      const details = document.getElementById('color_balance').closest('details');
      if (details) details.open = true;
      return {checked:document.getElementById('color_balance').checked,visible:!document.getElementById('colorBalanceSelection').hidden};
    })()`);
    assert.deepEqual(initial,{checked:true,visible:true});
    await run(`(() => {
      for (const [id,value] of Object.entries({lon:107,lat:10,radius_m:2000,start_date:'2026-01-01',end_date:'2026-01-31'})) document.getElementById(id).value=value;
      document.getElementById('least_cloud_enabled').checked=true;
      document.getElementById('btnSearch').click();
    })()`);
    await until(()=>run("document.getElementById('meta').textContent.includes('RAW + cân bằng màu')"));
    assert.equal(requests[0].color_balance,true);
    const meta = await run("document.getElementById('meta').textContent");
    assert.match(meta,/chồng lấp/);
    await run("document.getElementById('color_balance').scrollIntoView({block:'center'})");
    const shot = await cdp('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(root,'downloads','color-balance-browser.png'),Buffer.from(shot.data,'base64'));
    await run("document.getElementById('color_balance').click()");
    assert.equal(await run("document.getElementById('result').style.display"),'none');
    assert.equal(await run("opticalSceneBody('point',null,null,107,10).color_balance === undefined"),true);
    await run(`(() => {
      for (const [id,value] of Object.entries({composite_method:'mosaic',scene_grouping:'near_date',max_scene_span_days:'3',mosaic_mask_clouds:'false'})) {
        const node=document.getElementById(id); node.value=value; node.dispatchEvent(new Event('change'));
      }
      document.getElementById('btnSearch').click();
    })()`);
    await until(()=>run("document.getElementById('meta').textContent.includes('không mask mây')"));
    assert.equal(requests[1].composite_method,'mosaic'); assert.equal(requests[1].scene_grouping,'near_date');
    assert.equal(requests[1].max_scene_span_days,3); assert.equal(requests[1].mosaic_mask_clouds,false);
    assert.equal(requests[1].color_balance,undefined);
    assert.equal(await run("document.getElementById('mosaicMaskSelection').hidden"),false);
    await run("document.getElementById('mosaic_mask_clouds').value='true'; document.getElementById('mosaic_mask_clouds').dispatchEvent(new Event('change'))");
    assert.equal(await run("document.getElementById('result').style.display"),'none');
    await run("document.getElementById('btnSearch').click()");
    await until(()=>run("document.getElementById('meta').textContent.includes('mask mây/bóng mây')"));
    assert.equal(requests[2].mosaic_mask_clouds,true);
    const mosaicMeta=await run("document.getElementById('meta').textContent");
    assert.match(mosaicMeta,/75.15%/);
    await run("document.getElementById('scene_grouping').scrollIntoView({block:'center'})");
    const mosaicShot=await cdp('Page.captureScreenshot',{format:'png'});
    fs.writeFileSync(path.join(root,'downloads','coherent-mosaic-browser.png'),Buffer.from(mosaicShot.data,'base64'));
    fs.writeFileSync(path.join(root,'downloads','coherent-mosaic-browser.json'),JSON.stringify({requests,mosaicMeta,errors},null,2));
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(root,'downloads','color-balance-browser.json'),JSON.stringify({initial,request:requests[0],meta,errors},null,2));
    console.log('PASS: Chrome RAW color balance; coherent grouping/day controls; stable masked/unmasked request/result/invalidation; no JS exceptions. Fixture API, not live imagery.');
  } finally {
    socket?.close(); browser.kill(); server.close();
    // Keep the disposable profile as a diagnostic artifact; never touch a user's profile.
  }
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
