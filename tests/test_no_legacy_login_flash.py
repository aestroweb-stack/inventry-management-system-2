"""Regression checks: there must be exactly one Clerk-backed login flow."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1] / 'frontend'


class LegacyLoginFlashRegression(unittest.TestCase):
    def test_legacy_overlay_not_in_dashboard_dom(self):
        html = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('id="myraa-login"', html)
        self.assertNotIn('Welcome to MYRAA</h2>', html)
        self.assertNotIn('Continue to secure login', html)

    def test_legacy_auth_status_poll_removed(self):
        js = (ROOT / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('checkMyraaAccess(', js)
        self.assertNotIn('/auth/status', js)

    def test_clerk_app_error_uses_current_login_page(self):
        js = (ROOT / 'clerk-auth.js').read_text(encoding='utf-8')
        self.assertNotIn("$('myraa-login')", js)
        self.assertIn("location.replace(localAuthUrl('signin'))", js)
        self.assertIn("'/api/clerk/exchange'", js)
        self.assertIn("clerk.signOut()", js)

    def test_bust_old_dashboard_script_cache(self):
        html = (ROOT / 'index.html').read_text(encoding='utf-8')
        self.assertIn('app.js?v=myraa-no-legacy-gate-1', html)
        self.assertIn('clerk-auth.js?v=myraa-401-gate-1', html)


if __name__ == '__main__':
    unittest.main()
