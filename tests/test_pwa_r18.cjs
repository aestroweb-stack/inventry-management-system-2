// Node test harness: actual service-worker handlers, mock network and CacheStorage.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname,'../frontend');
const listeners = {}, stores = new Map([['unrelated',new Map()],['myraa-pwa-offline-old',new Map()]]);
let online=true, calls=[], status=200;
const context={URL,Response,Set,fetch:async (req,init)=>{calls.push([req,init]);if(!online)throw Error('offline');return new Response('network',{status,headers:{'content-type':'text/html'}})},caches:{open:async name=>{if(!stores.has(name))stores.set(name,new Map());const m=stores.get(name);return {put:async(k,v)=>m.set(k,v),match:async k=>m.get(k)?.clone()}},keys:async()=>[...stores.keys()],delete:async k=>stores.delete(k)},self:{location:{origin:'https://sample.invalid'},addEventListener:(name,fn)=>listeners[name]=fn,skipWaiting:async()=>{},clients:{claim:async()=>{}}}};
vm.runInNewContext(fs.readFileSync(path.join(root,'sw.js'),'utf8'),context);
function lifecycle(name){let result;listeners[name]({waitUntil:p=>result=p});return result}
function route(url,mode='navigate',method='GET'){let result;listeners.fetch({request:{url,mode,method},respondWith:p=>result=p});return result}
(async()=>{
 await lifecycle('install');await lifecycle('activate');
 assert.deepEqual([...stores.keys()].sort(),['myraa-pwa-offline-nov25-r18','unrelated']);
 assert.deepEqual([...stores.get('myraa-pwa-offline-nov25-r18').keys()],['/app/offline.html']);assert.equal(calls[0][1].credentials,'omit');
 for(const p of ['/app/','/app/sign-in.html?mode=signin','/app/index.html'])assert.equal((await route('https://sample.invalid'+p)).status,200);
 status=401;assert.equal((await route('https://sample.invalid/app/')).status,401);status=503;assert.equal((await route('https://sample.invalid/app/')).status,503);
 online=false;assert.equal(await (await route('https://sample.invalid/app/')).text(),'network');
 for(const [url,mode,method] of [['/api/products','cors','GET'],['/api/products','cors','POST'],['/api/health','navigate','GET'],['/app/app.js','script','GET'],['/app/','navigate','POST'],['/static/product_images/example.jpg','no-cors','GET'],['/api/products/bulk-template','navigate','GET']])assert.equal(route('https://sample.invalid'+url,mode,method),undefined);
 assert.equal(route('https://other.invalid/app/'),undefined);
 assert.equal(stores.get('myraa-pwa-offline-nov25-r18').size,1);
 const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.webmanifest')));assert.equal(manifest.scope,'/app/');assert.equal(manifest.start_url,'/app/');assert.equal(manifest.display,'standalone');
 console.log('PASS: public-only offline cache, lifecycle, online navigation, auth/server errors, offline fallback, API/POST/photo/download bypass, manifest scope');
})().catch(e=>{console.error(e);process.exit(1)});
