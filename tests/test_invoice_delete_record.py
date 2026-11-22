"""A saved draft can be deleted; posted stock and its evidence cannot."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from database import Base, get_db
import models, business_models  # noqa: F401
import business_api
from business_models import PurchaseChallan, PurchaseChallanLine, PurchaseChallanAttachment


class InvoiceDeleteTests(unittest.TestCase):
    def test_draft_invoice_deleted_with_lines_and_private_scan(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work)
            engine = create_engine('sqlite:///' + str(root / 'tenant.db'),
                                   connect_args={'check_same_thread': False})
            Base.metadata.create_all(bind=engine)
            Session = sessionmaker(bind=engine)
            with Session() as db:
                drafts = [
                    PurchaseChallan(party_name='Fashion Supplier', challan_no='INV-OPEN', invoice_no='',
                                    order_date='2026-11-23', source_filename='manual',
                                    file_hash='invoice-open-hash', status='READY'),
                    PurchaseChallan(party_name='Fashion Supplier', challan_no='INV-POSTED', invoice_no='',
                                    order_date='2026-11-23', source_filename='manual',
                                    file_hash='invoice-posted-hash', status='POSTED'),
                ]
                db.add_all(drafts)
                db.flush()
                open_id, posted_id = (b.id for b in drafts)
                db.add_all([
                    PurchaseChallanLine(challan_id=open_id, sku='KURTA', name='Kurta', size='M',
                                        qty_ordered=2, qty_received=2, unit_cost=150),
                    PurchaseChallanLine(challan_id=posted_id, sku='SHIRT', name='Shirt', size='L',
                                        qty_ordered=1, qty_received=1, unit_cost=150),
                    PurchaseChallanAttachment(challan_id=open_id, original_filename='scan.png',
                                              storage_filename='tenant-scan.png',content_type='image/png',size_bytes=8),
                ])
                db.commit()
            private_scan = root / 'tenant-scan.png'
            private_scan.write_bytes(b'private!')
            app = FastAPI()
            app.include_router(business_api.router)
            def provide_db():
                with Session() as db:
                    yield db
            app.dependency_overrides[get_db] = provide_db
            with patch.object(business_api, '_invoice_file_path', lambda name: root / name):
                with TestClient(app) as client:
                    self.assertEqual(client.delete('/api/purchase-challans/999999').status_code, 404)
                    response = client.delete(f'/api/purchase-challans/{posted_id}')
                    self.assertEqual(response.status_code, 409, response.text)
                    response = client.delete(f'/api/purchase-challans/{open_id}')
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), {'status':'deleted', 'id':open_id})
                    self.assertFalse(private_scan.exists())
                    self.assertEqual(client.get(f'/api/purchase-challans/{open_id}').status_code, 404)
                    self.assertEqual(client.delete(f'/api/purchase-challans/{open_id}').status_code, 404)
                    self.assertEqual(client.get(f'/api/purchase-challans/{posted_id}').status_code, 200)
            with Session() as db:
                self.assertEqual(db.query(PurchaseChallanLine).filter_by(challan_id=open_id).count(), 0)
                self.assertEqual(db.query(PurchaseChallanAttachment).filter_by(challan_id=open_id).count(), 0)
                self.assertEqual(db.query(PurchaseChallanLine).filter_by(challan_id=posted_id).count(), 1)
            engine.dispose()

    def test_view_search_and_uploader_are_wired(self):
        fs = ROOT / 'frontend'
        nav = (fs/'myraa-signature-v3.js').read_text()
        self.assertNotIn("globalSearch.addEventListener('input', () => {\n      clearTimeout", nav)
        self.assertIn("if (event.key === 'Enter')", nav)
        self.assertIn('Search products — press Enter', nav)
        rec = (fs/'business-workspace.js').read_text()
        self.assertIn('data-action="delete"',rec)
        self.assertIn("method:'DELETE'", rec)
        self.assertIn('newChallan?.()',rec)
        html = (fs/'index.html').read_text()
        self.assertIn('class="invoice-file-native"', html)
        self.assertIn('id="invoice-file-zone"', html)
        css = (fs/'myraa-ui-refresh.css').read_text()
        self.assertIn('background:transparent!important', css)
        self.assertIn('label.invoice-file-zone',css)

if __name__ == '__main__': unittest.main()
