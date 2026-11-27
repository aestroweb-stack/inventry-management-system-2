"""Optional Chromium regression test with the real return API on a temporary SQLite DB.
Run with playwright, beautifulsoup4 and Chromium installed; never against production.
"""
import shutil
import re
from pathlib import Path
from urllib.parse import urlsplit
from io import BytesIO
import pytest
from PIL import Image
from test_purchase_returns_r23 import setup, purchase, stock


def test_save_clears_only_after_success_and_reopen_post(setup):
    bs = pytest.importorskip('bs4')
    pw = pytest.importorskip('playwright.sync_api')
    chromium = shutil.which('chromium') or shutil.which('chromium-browser')
    if not chromium:
        pytest.skip('System Chromium is not installed')
    c, S = setup
    purchase(c)
    root = Path(__file__).resolve().parents[1] / 'frontend'
    soup = bs.BeautifulSoup((root/'index.html').read_text(), 'html.parser')
    for tag in soup.find_all(['script', 'link']):
        tag.decompose()
    for v in soup.select('.view.is-active,.rail-link.is-active'):
        v['class'] = [x for x in v['class'] if x != 'is-active']
    soup.select_one('#view-returns')['class'].append('is-active')
    soup.head.insert(0,soup.new_tag('base',href='https://myraa.example/'))
    photo = BytesIO(); Image.new('RGB',(12,12),'blue').save(photo,'PNG')
    fail = {'save': False, 'docs': False}
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=chromium,headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=browser.new_page(viewport={'width':1440,'height':1050})
        errors=[];page.on('pageerror',lambda e: errors.append(str(e)))
        def bridge(route):
            req=route.request;u=urlsplit(req.url);path=u.path+('?' + u.query if u.query else '')
            cors={'access-control-allow-origin':req.headers.get('origin','null'),'access-control-allow-credentials':'true','access-control-allow-methods':'GET, POST, PUT, DELETE, OPTIONS','access-control-allow-headers':'content-type'}
            if req.method=='OPTIONS':
                route.fulfill(status=204,headers=cors);return
            key='docs' if u.path.endswith('/documents') else 'save' if u.path.endswith('/drafts') else None
            if req.method=='POST' and key and fail[key]:
                fail[key]=False;route.fulfill(status=503,headers={**cors,'content-type':'application/json'},body='{"detail":"Simulated failure"}');return
            hdr={k:v for k,v in req.headers.items() if k in ('content-type','accept')}
            r=c.request(req.method,path,headers=hdr,content=req.post_data_buffer)
            route.fulfill(status=r.status_code,body=r.content,headers={**cors,'content-type':r.headers.get('content-type','application/json')})
        page.route('https://myraa.example/api/**',bridge)
        page.set_content(str(soup));page.evaluate('window.MYRAAUI={confirm:async()=>true};')
        # Supply only the minimal visibility rules needed by this behavioral test.
        page.add_style_tag(content='[hidden],.view:not(.is-active){display:none!important}.ri-viewing #return-studio{display:none!important}')
        page.add_script_tag(content=(root/'invoice-attachment-preview.js').read_text())
        page.add_script_tag(content=(root/'purchase-returns.js').read_text())
        def fill(number):
            page.fill('#ri-party','VIENNA');page.fill('#ri-number',number);page.fill('#ri-date','2026-11-27')
            page.click('#ri-add');page.fill('[data-manual-field="name"]','Outside catalog');page.fill('[data-manual-field="size"]','S');page.fill('[data-line="1"]','7');page.fill('[data-rate="1"]','450');page.fill('#ri-reason','Damaged')
        def cleared():
            pw.expect(page.locator('#ri-message')).to_contain_text('Invoice saved.')
            pw.expect(page.locator('#ri-detail')).to_be_hidden()
            pw.expect(page.locator('#return-studio')).to_be_visible()
            pw.expect(page.locator('#view-returns')).not_to_have_class(re.compile(r'\bri-viewing\b'))
            for selector in ['#ri-party','#ri-number','#ri-reason','#ri-sku']:
                pw.expect(page.locator(selector)).to_have_value('')
            pw.expect(page.locator('#ri-rows tr')).to_have_count(0)
            pw.expect(page.locator('#ri-documents [data-preview-doc]')).to_have_count(0)
            pw.expect(page.locator('#ri-documents [data-pending-preview]')).to_have_count(0)
            pw.expect(page.locator('#ri-tab-drafts')).to_have_attribute('aria-pressed','true')
            pw.expect(page.locator('#ri-save')).to_be_disabled();pw.expect(page.locator('#ri-post')).to_be_disabled()
        fill('RI-RESET-1');page.locator('#ri-files').set_input_files({'name':'invoice.png','mimeType':'image/png','buffer':photo.getvalue()})
        fail['save']=True;page.click('#ri-save');pw.expect(page.locator('#ri-message')).to_contain_text('Simulated failure');pw.expect(page.locator('#ri-number')).to_have_value('RI-RESET-1');assert c.get('/api/purchase-returns/drafts').json()['total']==0
        fail['docs']=True;page.click('#ri-save');pw.expect(page.locator('#ri-message')).to_contain_text('attachments failed');pw.expect(page.locator('#ri-number')).to_have_value('RI-RESET-1');pw.expect(page.locator('#ri-documents [data-pending-preview]')).to_have_count(1)
        assert c.get('/api/purchase-returns/drafts').json()['total']==1
        page.click('#ri-save');cleared()
        drafts=c.get('/api/purchase-returns/drafts').json();assert drafts['total']==1;d=drafts['items'][0];assert d['amount']==3150
        assert len(c.get(f'/api/purchase-returns/drafts/{d["id"]}/documents').json()['items'])==1
        assert stock(S)=={'M':10,'L':10}
        pw.expect(page.locator('#ri-record-list')).to_contain_text('RI-RESET-1')
        page.locator(f'[data-draft-view="{d["id"]}"]').click()
        pw.expect(page.locator('#ri-detail')).to_be_visible()
        pw.expect(page.locator('#ri-detail')).to_contain_text('SAVED DRAFT')
        page.click('#ri-close-detail')
        page.locator(f'[data-edit="{d["id"]}"]').click()
        pw.expect(page.locator('#ri-post')).to_be_enabled();pw.expect(page.locator('#ri-number')).to_have_value('RI-RESET-1');pw.expect(page.locator('#ri-documents [data-preview-doc]')).to_have_count(1)
        page.fill('[data-rate="1"]','451');page.click('#ri-save');cleared()
        pw.expect(page.locator(f'[data-edit="{d["id"]}"]')).to_be_visible();page.locator(f'[data-edit="{d["id"]}"]').click();pw.expect(page.locator('#ri-post')).to_be_enabled();page.click('#ri-post')
        pw.expect(page.locator('#ri-message')).to_contain_text('posted.')
        pw.expect(page.locator('#ri-detail')).to_be_hidden()
        pw.expect(page.locator('#return-studio')).to_be_visible()
        pw.expect(page.locator('#ri-number')).to_have_value('')
        pw.expect(page.locator('#ri-rows tr')).to_have_count(0)
        pw.expect(page.locator('#ri-tab-posted')).to_have_attribute('aria-pressed','true')
        pw.expect(page.locator('#ri-post')).to_be_disabled()
        posted=c.get('/api/purchase-returns').json()['items'][0]
        page.locator(f'[data-return-id="{posted["id"]}"]').click()
        pw.expect(page.locator('#ri-detail')).to_be_visible();pw.expect(page.locator('#ri-detail')).to_contain_text('POSTED');page.click('#ri-close-detail')
        assert c.get('/api/purchase-returns').json()['items'][0]['amount']==3157;assert stock(S)=={'M':10,'L':10}
        # New invoice after save must create a separate draft and not overwrite the first record.
        fill('RI-RESET-2');page.click('#ri-save');cleared();assert c.get('/api/purchase-returns/drafts').json()['total']==1;assert c.get('/api/purchase-returns').json()['total']==1
        assert not errors, errors
        browser.close()
