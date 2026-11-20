// Native headless Chrome + disposable localhost fixture; no user browser/profile.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const http=require('node:http');
const path=require('node:path');
const {spawn}=require('node:child_process');
const {once}=require('node:events');
const root=path.resolve(__dirname,'../..');
const output=path.join(root,'downloads/download-performance/browser');fs.mkdirSync(output,{recursive:true});
const ID='a'.repeat(32), requests=[], errors=[];
let state='running',stage='assembling';
const job=()=>({id:ID,state,stage,tiles_total:10,tiles_verified:10,verified_bytes:10485760,received_bytes:10485760,
  expires_at:Date.now()/1000+86400,...(state==='completed'?{file_url:`/api/download-jobs/${ID}/file`}:{})});
const server=http.createServer(async(req,res)=>{
  if(req.url.startsWith('/api/')) {
    let body='';for await(const chunk of req)body+=chunk;
    requests.push({method:req.method,url:req.url,body:body?JSON.parse(body):null});
    if(req.url.endsWith('/pause')) {state='paused';stage='paused';}
    if(req.url.endsWith('/resume')) {state='running';stage='downloading';}
    res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(job()));return;
  }
  if(req.url==='/download-jobs.js') {res.setHeader('content-type','text/javascript');res.end(fs.readFileSync(path.join(root,'static/download-jobs.js')));return;}
  res.setHeader('content-type','text/html; charset=utf-8');
  res.end('<!doctype html><meta charset="utf-8"><title>Download jobs fixture acceptance</title><style>body{font:16px system-ui;max-width:700px;margin:40px}button,a{display:inline-block;margin:8px;padding:12px}progress{width:100%}</style><a id="dl" href="/legacy">Tải GeoTIFF</a><section id="downloadJobPanel" hidden></section><script src="/download-jobs.js"></script><script>DownloadJobs.bindSelection({selection_token:"frozen-fixture",download_job_available:true});</script>');
});
async function until(check){const end=Date.now()+20000;while(Date.now()<end){if(await check())return;await new Promise(r=>setTimeout(r,50));}throw Error('Browser check timeout');}
(async()=>{
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  const profile=fs.mkdtempSync(path.join(output,'chrome-'));
  const browser=spawn('C:/Program Files/Google/Chrome/Application/chrome.exe',[
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check','--remote-debugging-address=127.0.0.1',
    '--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{windowsHide:true,stdio:'ignore'});
  let socket;
  try {
    const active=path.join(profile,'DevToolsActivePort');await until(()=>fs.existsSync(active));
    const debugPort=fs.readFileSync(active,'utf8').split('\n')[0];
    const targets=await(await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json();
    socket=new WebSocket(targets.find(t=>t.type==='page').webSocketDebuggerUrl);await once(socket,'open');
    let seq=0;const pending=new Map();
    socket.addEventListener('message',event=>{const p=JSON.parse(event.data);if(p.id){const pair=pending.get(p.id);pending.delete(p.id);p.error?pair.reject(Error(JSON.stringify(p.error))):pair.resolve(p.result);}if(p.method==='Runtime.exceptionThrown')errors.push(p.params.exceptionDetails.text);});
    const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,{resolve,reject});socket.send(JSON.stringify({id,method,params}));});
    const run=async expression=>{const value=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(value.exceptionDetails)throw Error(value.exceptionDetails.text);return value.result.value;};
    await cdp('Runtime.enable');await cdp('Page.enable');
    await cdp('Page.navigate',{url:`http://127.0.0.1:${server.address().port}/`});
    await until(()=>run('typeof DownloadJobs!=="undefined"'));
    await run('document.getElementById("dl").click()');
    await until(()=>run('document.getElementById("downloadJobPanel").textContent.includes("Đang xử lý ảnh")'));
    const during=await run('document.getElementById("downloadJobPanel").textContent');
    assert.match(during,/10\/10/);assert.doesNotMatch(during,/Hoàn tất/);
    assert.equal(requests.filter(r=>r.method==='POST'&&r.url==='/api/download-jobs').length,1);
    assert.deepEqual(requests[0].body,{selection_token:'frozen-fixture'});
    await run('[...document.querySelectorAll("button")].find(b=>b.textContent==="Tạm dừng").click()');
    await until(()=>run('document.getElementById("downloadJobPanel").textContent.includes("Đã tạm dừng")'));
    await run('[...document.querySelectorAll("button")].find(b=>b.textContent==="Tiếp tục").click()');
    await until(()=>run('document.getElementById("downloadJobPanel").textContent.includes("Đang tải dữ liệu")'));
    state='completed';stage='completed';await cdp('Page.reload');
    await until(()=>run('document.getElementById("downloadJobPanel").textContent.includes("Hoàn tất")'));
    assert.equal(await run('document.querySelector("#downloadJobPanel a").getAttribute("href")'),`/api/download-jobs/${ID}/file`);
    const screenshot=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'completed.png'),Buffer.from(screenshot.data,'base64'));
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify({scope:'Chrome UI with fixture API; not live GEE',checks:{start:true,processingNotComplete:true,pause:true,resume:true,reloadRestore:true,safeArtifactLink:true},requests,errors},null,2));
    console.log('PASS: Chrome start, processing, pause/resume, reload restore, completed file link; no JS exceptions. Fixture API.');
  } finally {socket?.close();browser.kill();server.close();}
})().catch(error=>{console.error(error);server.close();process.exitCode=1;});
