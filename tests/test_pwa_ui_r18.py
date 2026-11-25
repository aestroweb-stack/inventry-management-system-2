"""Actual HTML/CSS and install JS; mock install prompt, no native OS installation."""
import base64, os, re
from pathlib import Path
from playwright.sync_api import sync_playwright
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]/'frontend'

def html(file):
    source=(ROOT/file).read_text()
    source=re.sub(r'<script\b[^>]*>.*?</script>','',source,flags=re.S)
    def css(m):
        href=re.search(r'href="([^"]+)"',m.group())
        if not href:return ''
        name=href.group(1).split('?')[0].removeprefix('/app/')
        if name.endswith('.css') and (ROOT/name).is_file():return '<style>'+(ROOT/name).read_text()+'</style>'
        return ''
    source=re.sub(r'<link\b[^>]*>',css,source)
    for asset in ['assets/myraa-mark.svg','assets/myraa-lady-full.webp']:
        f=ROOT/asset;kind='svg+xml' if f.suffix=='.svg' else 'webp'
        source=source.replace(f'src="{asset}"',f'src="data:image/{kind};base64,{base64.b64encode(f.read_bytes()).decode()}"')
    return source

def main():
    for name,size in [('icon-192',192),('icon-512',512),('icon-maskable',512),('apple-touch-icon',180)]:
        with Image.open(ROOT/'icons'/f'{name}.png') as im:assert im.size==(size,size)
    with sync_playwright() as pw:
        b=pw.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),args=['--no-sandbox'])
        count=0
        for w in (320,390,768,1440,3840):
            for theme in ('light','dark'):
                page=b.new_page(viewport={'width':w,'height':850});page.set_content(html('index.html'));page.evaluate('(t)=>document.documentElement.dataset.theme=t',theme)
                page.add_script_tag(content=(ROOT/'pwa-install.js').read_text())
                page.evaluate('document.querySelector("[data-myraa-install]").click()')
                dialog=page.locator('#myraa-install-dialog');assert dialog.is_visible()
                page.evaluate('(src)=>document.querySelector("#myraa-install-dialog img").src=src','data:image/png;base64,'+base64.b64encode((ROOT/'icons/icon-192.png').read_bytes()).decode())
                assert page.evaluate('(()=>{const r=document.querySelector("#myraa-install-dialog").getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight})()')
                if w==390 and theme=='dark' and os.getenv('PREVIEW_DIR'):page.screenshot(path=str(Path(os.environ['PREVIEW_DIR'])/'MYRAA_R18_Install_Mobile.png'))
                dialog.get_by_role('button',name='Close').click();assert not dialog.is_visible()
                page.evaluate('''()=>{let e=new Event('beforeinstallprompt',{cancelable:true});e.prompt=async()=>{window.promptCalls=(window.promptCalls||0)+1};e.userChoice=Promise.resolve({outcome:'accepted'});window.dispatchEvent(e);document.querySelector('[data-myraa-install]').click()}''')
                page.wait_for_function('window.promptCalls===1 && document.querySelector("[data-myraa-install]").hidden');page.close();count+=1
        page=b.new_page(viewport={'width':390,'height':850});page.set_content(html('sign-in.html'));page.add_script_tag(content=(ROOT/'pwa-install.js').read_text());assert page.locator('[data-myraa-install]').is_visible();page.close()
        page=b.new_page(viewport={'width':390,'height':850});page.set_content((ROOT/'offline.html').read_text());assert page.get_by_role('heading',name='You are offline').is_visible();assert page.get_by_role('button',name='Try again').is_visible()
        if os.getenv('PREVIEW_DIR'):page.screenshot(path=str(Path(os.environ['PREVIEW_DIR'])/'MYRAA_R18_Offline_Mobile.png'))
        page.close()
        # Actual dashboard shell and router, with synthetic API data, standalone mode simulated.
        from test_navigation_r17 import BOOT
        page=b.new_page(viewport={'width':390,'height':850});page.set_content(html('index.html'));page.add_script_tag(content=BOOT);page.add_script_tag(content=(ROOT/'business-workspace.js').read_text());page.add_script_tag(content=(ROOT/'app.js').read_text());page.evaluate('Object.defineProperty(navigator,"standalone",{value:true,configurable:true})');page.add_script_tag(content=(ROOT/'pwa-install.js').read_text());page.wait_for_function('document.querySelector(".view.is-active")?.id==="view-dashboard"');assert page.locator('[data-myraa-install]').get_attribute('hidden') is not None
        if os.getenv('PREVIEW_DIR'):page.screenshot(path=str(Path(os.environ['PREVIEW_DIR'])/'MYRAA_R18_App_Mobile.png'))
        page.close();b.close();print(f'PASS: {count} dialog viewport/theme combinations; login entry; simulated native prompt; offline page; standalone dashboard; icon sizes')
if __name__=='__main__':main()
