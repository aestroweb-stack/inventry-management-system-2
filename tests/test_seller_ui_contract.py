"""Static UI contract checks for independent seller workspaces."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

FRONT = Path(__file__).resolve().parents[1]/'frontend'

class IDs(HTMLParser):
    def __init__(self):super().__init__();self.ids=[];self.scripts=[];self.views=[]
    def handle_starttag(self,tag,attrs):
        d=dict(attrs)
        if 'id' in d:self.ids.append(d['id'])
        if tag=='script' and d.get('src'):self.scripts.append(d['src'])
        if d.get('data-view'):self.views.append(d['data-view'])

class SellerUIContract(unittest.TestCase):
    def test_app_ui_and_sidebar_integration(self):
        p=IDs();p.feed((FRONT/'index.html').read_text())
        self.assertEqual(len(p.ids),len(set(p.ids)),'Duplicate DOM ids break callbacks')
        for key in ('view-challans','myraa-challan-studio','cs-rows','cs-push','cs-files','cs-add','cs-save','view-profile','workspace-profile-form','sound-toggle'):
            self.assertIn(key,p.ids)
        self.assertIn('profile',p.views)
        self.assertNotIn('team',p.views)
        self.assertTrue(any('myraa-challan-studio.js' in name for name in p.scripts))
        self.assertTrue(any('seller-sounds.js' in name for name in p.scripts))
        self.assertNotIn('assistant-panel',p.ids)
        self.assertNotIn('assistant-launcher',p.ids)
        self.assertNotIn('workspace-team-link',p.ids)

    def test_login_and_preview_are_present(self):
        for name in ('sign-in.html','preview.html','preview-login.html'):
            text=(FRONT/name).read_text()
            self.assertIn('<meta name="viewport"',text)
            self.assertNotIn('pending admin approval',text.lower())
        text=(FRONT/'sign-in.html').read_text()
        self.assertIn('Create account',text)
        self.assertIn('email + password',text)
        self.assertIn('email OTP',text)
        self.assertTrue((FRONT/'seller-workspace.css').exists())

    def test_ownership_footer_and_sound_are_optin(self):
        self.assertIn('Hacker Room',(FRONT/'index.html').read_text())
        self.assertIn('8630404031',(FRONT/'index.html').read_text())
        sound=(FRONT/'seller-sounds.js').read_text()
        self.assertIn("localStorage.getItem(KEY) === '1'",sound)
        self.assertIn('navigator.vibrate',sound)
        self.assertNotIn("document.addEventListener('click'", sound)
        self.assertIn('click() { /* compatibility: intentionally silent */ }',sound)

if __name__ == '__main__':unittest.main()
