"""Run directly: python tests/test_navigation_r17.py.
Chromium + Playwright; real DOM/router, mock APIs; fresh initialization models refresh.
"""
import json, os, re
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]/'frontend'
HTML=re.sub(r'<script\b[^>]*>.*?</script>','',(ROOT/'index.html').read_text(),flags=re.S)
HTML=re.sub(r'<link\b[^>]*>','',HTML)
SKU='SAMPLE_H&N_D01_BLACK'
VIEWS='dashboard products scan visual bulk orders challans packing reorder alerts history labels trash profile'.split()
BOOT='''
Object.defineProperty(window,"localStorage",{value:{getItem:()=>null,setItem:()=>{},removeItem:()=>{}}});
let writes=[], failures={TEMP:503};
let catalog=Object.fromEntries(["SAMPLE_H&N_D01_BLACK","OTHER","SLOW"].map((sku,i)=>[sku,{id:i+1,sku,name:`Design ${i+1}`,category:'Sample',image_path:null,images:[],variants:[],unit_cost:300,stock_cost_value:0,product_aliases:[]} ]));
window.fetch=async (input,init={})=>{let u=String(input),method=init.method||'GET',data={},status=200;
 if(u.startsWith('/api/products/')){const sku=decodeURIComponent(u.split('/api/products/')[1]);if(method==='PATCH'&&catalog[sku]){writes.push(sku);data={...catalog[sku],...JSON.parse(init.body)};delete catalog[sku];catalog[data.sku]=data;}else if(failures[sku]){status=failures[sku];data={detail:'Unavailable'};}else if(catalog[sku])data=catalog[sku];else{status=404;data={detail:'Not found'};}if(sku==='SLOW')await new Promise(r=>setTimeout(r,350));}
 else if(u.startsWith('/api/products?'))data={items:Object.values(catalog),total:Object.keys(catalog).length,offset:0,limit:12};
 else if(u.includes('/analytics/movers'))data=[];
 else if(u.includes('/summary'))data={products:3,stock_units:0,low:0,out:0,open_alerts:0};
 else data={items:[],total:0,offset:0,limit:12,page:1,pages:1,count:0};
 return {ok:status===200,status,json:async()=>JSON.parse(JSON.stringify(data))};};
window.MYRAAUI={notify:()=>{},error:()=>{},alert:async()=>{},confirm:async()=>true};
'''

def main():
    with sync_playwright() as pw:
        browser=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH') or ('/usr/bin/chromium' if Path('/usr/bin/chromium').exists() else None),args=['--no-sandbox'])
        def start(fragment,width=1440,catalog=None):
            p=browser.new_page(viewport={'width':width,'height':900});p.set_default_timeout(5000)
            p.on("pageerror",lambda e:print("PAGE ERROR",e,flush=True));p.set_content(HTML);p.evaluate('(h)=>window.history.replaceState({},"","#"+h)',fragment)
            p.add_script_tag(content=BOOT+(f'\ncatalog={json.dumps(catalog)};' if catalog else ''))
            p.add_script_tag(content=(ROOT/'business-workspace.js').read_text())
            p.add_script_tag(content=(ROOT/'app.js').read_text())
            return p
        def view(p,n):p.wait_for_function('(n)=>{const v=document.querySelectorAll(".view.is-active");return v.length===1&&v[0].id==="view-"+n}',arg=n)
        def product(p,s):
            view(p,'detail');p.wait_for_function('(s)=>document.querySelector("#detail-content #detail-sku")?.value===s',arg=s)
            assert p.evaluate('document.querySelector(".rail-link.is-active")?.dataset.view')=='products'
        p=start('products');p.locator('.pcard').filter(has_text=SKU).click();product(p,SKU);h=p.evaluate('location.hash.slice(1)');assert 'H%26N' in h
        p.locator('#detail-name').fill('Unsaved draft');assert p.evaluate('writes.length')==0
        fresh=start(h);product(fresh,SKU);assert fresh.locator('#detail-name').input_value()=='Design 1';fresh.close()
        p.locator('#detail-back').click();view(p,'products');p.evaluate('window.history.back()');product(p,SKU);p.evaluate('window.history.forward()');view(p,'products')
        p.evaluate('()=>openProductDetail("OTHER")');product(p,'OTHER');p.locator('#detail-sku').fill('RENAMED');p.locator('#detail-save').click();p.wait_for_function('location.hash.includes("RENAMED")');product(p,'RENAMED')
        fresh=start(p.evaluate('location.hash.slice(1)'),catalog=p.evaluate('catalog'));product(fresh,'RENAMED');fresh.close()
        p.evaluate('location.hash="detail?sku=MISSING"');p.wait_for_selector('[data-detail-retry]');view(p,'detail');assert 'Product not available' in p.locator('#detail-content').inner_text()
        p.evaluate('location.hash="detail?sku=TEMP"');p.wait_for_function('document.querySelector("#detail-content h2")?.textContent==="Could not open product"');p.evaluate('delete failures.TEMP;catalog.TEMP={...catalog.OTHER,...catalog.RENAMED,sku:"TEMP",id:99}');p.locator('[data-detail-retry]').click();product(p,'TEMP')
        p.evaluate('location.hash="detail"');view(p,'products');p.evaluate('location.hash="not-a-page"');view(p,'dashboard')
        p.evaluate('()=>{void openProductDetail("SLOW");switchView("history")}');view(p,'history');p.wait_for_timeout(500);view(p,'history');assert p.locator('#detail-content #detail-sku').count()==0
        p.evaluate('(s)=>{void openProductDetail("SLOW");void openProductDetail(s)}',SKU);product(p,SKU);p.wait_for_timeout(500);product(p,SKU);p.close()
        print('PASS: product refresh initialization, rename, drafts, history, missing/retry and stale response',flush=True)
        count=0
        for n in VIEWS:
            p=start(n);view(p,n);assert p.evaluate('location.hash')=='#'+n;p.close();count+=1
        for w in [320,390,768,1440,3840]:
            p=start(h,w);product(p,SKU);p.close();count+=1
        print('PASS:',count,'fresh route initializations; all 14 sidebar views and product detail at five widths',flush=True)
        browser.close()
if __name__=='__main__':main()
