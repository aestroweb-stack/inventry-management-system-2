"""Regressions for the marketing page and invoice workflow, no live Railway access."""
from pathlib import Path
from html.parser import HTMLParser
import unittest

ROOT=Path(__file__).resolve().parents[1]
FRONT=ROOT/'frontend'
BACK=ROOT/'backend'

class InvoiceUiUpgrade(unittest.TestCase):
    def test_public_landing_and_signup_links(self):
        landing=(FRONT/'landing.html').read_text()
        self.assertIn('Smart inventory.',landing)
        self.assertIn('/app/sign-in.html?mode=signup',landing)
        self.assertIn('/app/sign-in.html?mode=signin',landing)
        self.assertIn('MYRAA',landing)
        self.assertIn('Hacker Room',landing)
        self.assertIn('landing.html',(BACK/'main.py').read_text())
    def test_add_item_is_explicit(self):
        script=(FRONT/'myraa-challan-studio.js').read_text()
        self.assertIn("rows:[]",script)
        self.assertIn("$('cs-empty').hidden=hasRows",script)
        self.assertIn("$('cs-table-wrap').hidden=!hasRows",script)
        self.assertIn("state.rows.push(row());render()",script)
    def test_record_controls(self):
        js=(FRONT/'business-workspace.js').read_text()
        for s in ('data-action="view"','data-action="edit"','/api/purchase-challans/${b.id}/export','scrollIntoView'):
            self.assertIn(s,js)
        self.assertIn('/purchase-challans/export-records',(BACK/'business_api.py').read_text())
    def test_profile_single_photo_and_dark(self):
        css=(FRONT/'myraa-ui-refresh.css').read_text()
        self.assertIn('#workspace-profile-preview[hidden]',css)
        self.assertIn('#workspace-photo-empty[hidden]',css)
        self.assertIn(':root[data-theme="dark"] #view-challans .myraa-challan-studio',css)
        ui=(FRONT/'index.html').read_text()
        self.assertNotIn('RAILWAY CLOUD',ui)
        self.assertIn('Purchase Invoice',ui)
        self.assertIn('Supplier Invoice Records',ui)

if __name__=='__main__':unittest.main()
