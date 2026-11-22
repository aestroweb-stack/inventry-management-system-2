const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '..', 'frontend', 'myraa-auth-gate.js'),'utf8');
function env(handler) {
  const calls=[];
  const location = {href:'https://myraa.example/app/',origin:'https://myraa.example',replace(url){this.redirect=url;}};
  const window = {location,fetch: async (request) => {
    const url = new URL(typeof request === 'string' ? request : request.url,location.href);
    calls.push(url.pathname);
    return handler(url.pathname,calls.length);
  }};
  vm.runInNewContext(source,{window,location,URL,Request,Response,Error,Promise});
  return {window,calls,location};
}
(async () => {
  const a=env(async()=>new Response('{}',{status:200}));
  const pending=a.window.fetch('/api/summary');
  await Promise.resolve();
  assert.equal(a.calls.length,0,'private API must await Clerk exchange');
  await a.window.fetch('/api/clerk/config');
  await a.window.fetch('/api/clerk/exchange',{method:'POST'});
  assert.deepEqual(a.calls,['/api/clerk/config','/api/clerk/exchange']);
  a.window.myraaAuthGate.ready();
  const res=await pending;
  assert.equal(res.status,200);
  assert.equal(a.calls[2],'/api/summary');
  console.log('PASS queue private requests and bypass Clerk handshake');

  let refreshCalls=0,variantsCalls=0;
  const b=env(async(path)=>{
    if(path==='/api/variants') {
      variantsCalls++;
      return new Response('{}',{status:variantsCalls===1?401:200});
    }
    return new Response('{}',{status:200});
  });
  b.window.myraaAuthGate.setRefresh(async()=>{refreshCalls++;});
  b.window.myraaAuthGate.ready();
  const second=await b.window.fetch('/api/variants?limit=100&offset=0');
  assert.equal(second.status,200);
  assert.equal(variantsCalls,2);
  assert.equal(refreshCalls,1);
  console.log('PASS 401 -> refresh Clerk-bound cookie -> retry once');

  const c=env(async()=>new Response('{}',{status:200}));
  const p=c.window.fetch('/api/costing/summary').then(()=>{throw Error('should not resolve')},e=>e);
  c.window.myraaAuthGate.fail(new Error('No Clerk session'));
  const error=await p;
  assert.match(error.message,/No Clerk session/);
  assert.equal(c.calls.length,0);
  console.log('PASS auth failure rejects queued API without network request');

  let refreshCount=0;
  const counts={};
  const d=env(async(path)=>{
    counts[path]=(counts[path]||0)+1;
    return new Response('{}',{status:counts[path]===1?401:200});
  });
  d.window.myraaAuthGate.ready();
  d.window.myraaAuthGate.setRefresh(async()=>{refreshCount++;await new Promise(resolve=>setImmediate(resolve));});
  const r=await Promise.all(['/api/summary','/api/variants','/api/analytics/movers'].map(x=>d.window.fetch(x)));
  assert.ok(r.every(x=>x.status===200));
  assert.equal(refreshCount,1,'concurrent 401s must trigger a single refresh');
  console.log('PASS concurrent 401s deduplicated to one exchange');

  let bad=0;
  const e=env(async()=>{bad++;return new Response('{}',{status:401});});
  e.window.myraaAuthGate.ready();
  e.window.myraaAuthGate.setRefresh(async()=>{});
  const r5=await e.window.fetch('/api/summary');
  assert.equal(r5.status,401);
  assert.equal(bad,2);
  assert.equal(e.location.redirect,'/app/sign-in.html');
  console.log('PASS persistent 401 fails closed without infinite loop');
})();
