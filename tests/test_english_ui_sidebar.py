"""Regression checks: production UI labels stay English, sidebar toggle stays independent."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "frontend"

class TestEnglishUISidebar(unittest.TestCase):
    def test_sidebar_toggle_is_not_overlaying_the_brand(self):
        html = (FRONT / "index.html").read_text(encoding="utf-8")
        css = (FRONT / "myraa-accessible-v2.css").read_text(encoding="utf-8")
        self.assertIn('id="sidebar-collapse"', html)
        self.assertIn('myraa-accessible-v2.css?v=3', html)
        self.assertIn('grid-template-areas:"brand-icon brand-name" ". brand-toggle"', css)
        self.assertIn('position:static!important', css)
        self.assertIn('.sidebar-collapsed #sidebar .rail-identity', css)

    def test_visible_frontend_contains_no_hindi_or_hinglish(self):
        for suffix in ("*.html", "*.js"):
            for path in FRONT.glob(suffix):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"[\u0900-\u097f]", text), path.name)
                self.assertIsNone(re.search(r"\b(?:aur|nahi|honge|hoga|rahega|rahenge|karo|mein)\b", text, flags=re.I), path.name)

    def test_invoice_and_packing_labels_are_english(self):
        app = (FRONT / 'index.html').read_text(encoding='utf-8')
        self.assertIn('Import Supplier Invoice', app)
        self.assertIn('Supplier Reference No.', app)
        self.assertIn('Include Review and Source Audit sheets', app)
        self.assertIn('Product- and size-level stock alerts.', app)
        self.assertNotIn('Loading challans', app)
        server = (ROOT / 'backend' / 'business_api.py').read_text(encoding='utf-8')
        self.assertIn('Invoice not found', server)

if __name__ == '__main__':
    unittest.main()
