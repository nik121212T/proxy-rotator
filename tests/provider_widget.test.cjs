const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {join} = require('node:path');

class Element {
  constructor() { this.listeners = {}; this.dataset = {}; this.value = ''; this.children = []; this.textContent = ''; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async trigger(type) { await Promise.all((this.listeners[type] || []).map(fn => fn({target:this}))); }
  dispatchEvent(event) { this.trigger(event.type); return true; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...children) { this.children = children; }
}
async function widget(enabled, responder, timers={}) {
  const names = ['section','token-card','token','token-hint','save-token','clear-token','balance','form',
                 'country','limit','preview','import','hint','region','city','refresh'];
  const nodes = Object.fromEntries(names.map(name => ['dichvusocks-'+name, new Element()]));
  nodes['dichvusocks-section'].dataset.enabled = String(enabled);
  nodes['dichvusocks-limit'].value = '5';
  const balance = new Element();
  const document = Object.assign(new Element(), {body: new Element(),
    getElementById: id => nodes[id] || null,
    querySelector: selector => selector.includes('data-field="balance"') ? balance : null,
    querySelectorAll: () => [], createElement: () => new Element(),
  });
  const requests=[];
  const context = {document, window: Object.assign(new Element(), {confirm:()=>true}),
    htmx:{defineExtension(){}}, Event, URLSearchParams, AbortController, setTimeout, clearTimeout, ...timers,
    fetch:async (url, options={}) => { requests.push([url, options]); return responder(url,options); },
  };
  await runInNewContext(readFileSync(join(__dirname,'../app/ui/static/app.js'),'utf8'),context);
  return {nodes,balance,requests,document};
}
const ok = body => ({ok:true,status:200,json:async()=>body});
const normal = url => ok(url.endsWith('/countries') ? ['FR','US'] : url.endsWith('/user-info') ?
  {user:{username:'demo',point:12}} : {imported:1,deduped:0,skipped:[]});

test('disabled provider shows disconnected state without making requests',async()=>{
  const w=await widget(false, normal);
  assert.equal(w.requests.length,0);
  assert.equal(w.nodes['dichvusocks-balance'].hidden,true);
  assert.match(w.nodes['dichvusocks-token-hint'].textContent,/Aucun token/);
});
test('successful import is not reported as a JavaScript failure',async()=>{
  const w=await widget(true,normal);
  await w.nodes['dichvusocks-import'].trigger('click');
  assert.match(w.nodes['dichvusocks-hint'].textContent,/^Importés : 1/);
  assert.equal(w.requests.filter(([url])=>url.includes('/import?')).length,1);
});
test('saving a token repeatedly does not duplicate countries',async()=>{
  const w=await widget(true,normal);
  for(let i=0;i<2;i++) { w.nodes['dichvusocks-token'].value='synthetic'; await w.nodes['dichvusocks-save-token'].trigger('click'); }
  const countries=w.nodes['dichvusocks-country'].children.map(c=>c.value);
  assert.equal(countries.filter(c=>c==='FR').length,1);
});
test('invalid account token produces an actionable message',async()=>{
  const w=await widget(true,url=>url.endsWith('/user-info') ?
    {ok:false,status:502,json:async()=>({detail:'Invalid or missing API token (401)'})} : normal(url));
  assert.match(w.balance.textContent,/Token refusé/);
});


test('stalled account request times out and can be retried',async()=>{
  let stalled=true;
  const w=await widget(true,(url,options)=> {
    if(stalled && url.endsWith('/user-info')) return new Promise((resolve,reject)=> {
      options.signal.addEventListener('abort',()=>reject(new Error('aborted')));
    });
    return normal(url);
  }, {setTimeout:fn=>setTimeout(fn,5)});
  assert.match(w.balance.textContent,/Délai dépassé/);
  stalled=false;
  await w.nodes['dichvusocks-refresh'].trigger('click');
  assert.match(w.balance.textContent,/demo · 12 pts/);
  assert.equal(w.nodes['dichvusocks-refresh'].disabled,false);
});
test('import outside allowed range sends no paid request',async()=>{
  const w=await widget(true,normal);
  for(const value of ['0','51','1.5','abc']) {
    w.nodes['dichvusocks-limit'].value=value;
    await w.nodes['dichvusocks-import'].trigger('click');
  }
  assert.equal(w.requests.filter(([url])=>url.includes('/import?')).length,0);
});
test('forgetting token hides account and importing controls',async()=>{
  const w=await widget(true,normal);
  await w.nodes['dichvusocks-clear-token'].trigger('click');
  assert.equal(w.nodes['dichvusocks-balance'].hidden,true);
  assert.equal(w.nodes['dichvusocks-form'].hidden,true);
  assert.equal(w.nodes['dichvusocks-clear-token'].hidden,true);
  assert.equal(w.nodes['dichvusocks-section'].dataset.enabled,'false');
});
