const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const ID='a'.repeat(32);
const job=(state='running',extra={})=>({id:ID,state,stage:'downloading',tiles_total:10,tiles_verified:10,
  received_bytes:1024,verified_bytes:1024,...extra});
function fixture(saved=null) {
  class Element {
    constructor(tag='div'){this.tagName=tag;this.children=[];this.listeners={};this.attrs={};this.textContent='';this.hidden=false;this.disabled=false;}
    append(...nodes){this.children.push(...nodes);}
    replaceChildren(...nodes){this.children=[...nodes];}
    setAttribute(key,value){this.attrs[key]=value;}
    removeAttribute(key){delete this.attrs[key];}
    addEventListener(name,fn){this.listeners[name]=fn;}
    set innerHTML(value){throw Error('unsafe HTML');}
  }
  const panel=new Element(),link=new Element('a'),calls=[],timers=new Map(),stored=new Map();
  if(saved)stored.set('gee.download.job_id',saved);
  const document={hidden:false,createElement:tag=>new Element(tag),getElementById:id=>id==='downloadJobPanel'?panel:link};
  const window={document,localStorage:{getItem:k=>stored.get(k),setItem:(k,v)=>stored.set(k,v),removeItem:k=>stored.delete(k)},
    fetch:(url,options={})=>new Promise((resolve,reject)=>calls.push({url,options,resolve,reject})),
    setTimeout:(fn,delay)=>{timers.set(1,{fn,delay});return 1;},clearTimeout:id=>timers.delete(id),
    SatelliteWorkspace:{showView(){} }};
  const source=fs.existsSync('static/download-jobs.js')?fs.readFileSync('static/download-jobs.js','utf8'):'';
  vm.runInNewContext(source,{window,document,console});
  const walk=node=>[node,...node.children.flatMap(walk)];
  const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  const reply=async(call,data,status=200)=>{call.resolve({ok:status<400,status,json:async()=>data});await flush();};
  return {window,document,panel,link,calls,timers,stored,flush,reply,api:window.DownloadJobs,
    text:()=>walk(panel).map(node=>node.textContent).join(' '),all:tag=>walk(panel).filter(n=>n.tagName===tag)};
}

test('start uses only frozen token, prevents double POST, and never falls back to legacy',async()=>{
  const f=fixture();assert.ok(f.api,'job controller exists');
  const result={selection_token:'frozen',download_job_available:true};
  f.api.bindSelection(result);result.selection_token='changed';
  const first=f.api.start();f.api.start();
  assert.equal(f.calls.length,1);
  assert.deepEqual(JSON.parse(f.calls[0].options.body),{selection_token:'frozen'});
  await f.reply(f.calls[0],job());await first;
  assert.equal(f.stored.get('gee.download.job_id'),ID);
  assert.equal(f.timers.get(1).delay,1000);
  assert.match(f.text(),/10\/10/);
  assert.doesNotMatch(f.text(),/Hoàn tất/);
  const progress=f.all('progress')[0];
  assert.equal(progress.attrs['aria-label'],'Dữ liệu đầu vào');
  f.api.bindSelection({download_job_available:false});
  assert.equal(f.calls.length,1,'selection change does not create another job');
});

test('completed file link is constrained, restores ID and polling retries transient errors',async()=>{
  const f=fixture(ID);assert.equal(f.calls[0].url,`/api/download-jobs/${ID}`);
  f.calls[0].reject(Error('offline'));await f.flush();
  assert.equal(f.timers.size,1);
  f.document.hidden=true;const timer=f.timers.get(1);f.timers.delete(1);timer.fn();await f.flush();
  await f.reply(f.calls.at(-1),job('completed',{stage:'completed',file_url:'https://malicious.test/file'}));
  assert.equal(f.all('a').length,0);
  assert.equal(f.timers.size,0);
  const clean=fixture(ID);
  await clean.reply(clean.calls[0],job('completed',{stage:'completed',file_url:`/api/download-jobs/${ID}/file`}));
  assert.equal(clean.all('a')[0].href,`/api/download-jobs/${ID}/file`);
});

test('storage failures and expired jobs are visible without duplicate heavy requests',async()=>{
  const f=fixture();assert.ok(f.api);
  f.window.localStorage.setItem=()=>{throw Error('blocked');};
  f.api.bindSelection({selection_token:'frozen',download_job_available:true});
  const start=f.api.start();await f.reply(f.calls[0],{detail:'expired'},410);await start;
  assert.equal(f.calls.length,1);
  assert.match(f.text(),/expired/);
  assert.equal(f.timers.size,0);
});
