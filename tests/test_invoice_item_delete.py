"""POSTED inventory is immutable; saved invoice items can be deleted server-side."""
from pathlib import Path
from io import BytesIO
import sys, tempfile, unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE/'backend'))
from database import Base, get_db
import models, business_models  # noqa: F401 registers tables
from business_api import router
from business_models import PurchaseChallan, PurchaseChallanLine

class SavedInvoiceLineDeleteTests(unittest.TestCase):
    def test_delete_saved_items_and_reject_posted_or_wrong_invoice(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = create_engine('sqlite:///' + str(Path(temp)/'test.db'), connect_args={'check_same_thread':False})
            Base.metadata.create_all(bind=engine)
            sessions=sessionmaker(bind=engine)
            with sessions() as db:
                a=PurchaseChallan(party_name='Store One', challan_no='A-100', invoice_no='', order_date='2026-11-22',source_filename='manual',file_hash='hash-one',status='PARTIAL')
                b=PurchaseChallan(party_name='Store One', challan_no='A-101', invoice_no='', order_date='2026-11-22',source_filename='manual',file_hash='hash-two',status='POSTED')
                db.add_all([a,b]);db.flush()
                line_a=PurchaseChallanLine(challan_id=a.id,sku='K01',name='Kurta',size='M',qty_ordered=3,qty_received=1,unit_cost=199)
                line_b=PurchaseChallanLine(challan_id=b.id,sku='K02',name='Shirt',size='L',qty_ordered=2,qty_received=2,unit_cost=175)
                db.add_all([line_a,line_b]);db.flush()
                a_id,b_id,l_id,posted_line=a.id,b.id,line_a.id,line_b.id
                db.commit()
            app=FastAPI();app.include_router(router)
            def provide():
                with sessions() as db: yield db
            app.dependency_overrides[get_db]=provide
            with TestClient(app) as client:
                self.assertEqual(client.delete(f'/api/purchase-challans/{b_id}/lines/{posted_line}').status_code,409)
                self.assertEqual(client.delete(f'/api/purchase-challans/{a_id}/lines/{posted_line}').status_code,404)
                response=client.delete(f'/api/purchase-challans/{a_id}/lines/{l_id}')
                self.assertEqual(response.status_code,200,response.text)
                self.assertEqual(response.json()['lines'],[])
                self.assertEqual(response.json()['status'],'OPEN')
                self.assertEqual(client.get(f'/api/purchase-challans/{a_id}').json()['lines'],[])
                self.assertEqual(client.delete(f'/api/purchase-challans/{a_id}/lines/{l_id}').status_code,404)
            with sessions() as db:
                self.assertEqual(db.query(PurchaseChallanLine).filter_by(challan_id=b_id).count(),1)
            engine.dispose()

if __name__=='__main__': unittest.main()
