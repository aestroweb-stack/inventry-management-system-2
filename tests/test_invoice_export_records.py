"""Verify all-records xlsx is generated for the current tenant DB only."""
import tempfile
from pathlib import Path
import sys
from io import BytesIO
import unittest
BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/'backend'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from database import Base,get_db
import models,business_models
from business_api import router
from business_models import PurchaseChallan, PurchaseChallanLine
from openpyxl import load_workbook

class InvoiceExportTests(unittest.TestCase):
 def test_records_excel_is_tenant_scoped(self):
  with tempfile.TemporaryDirectory() as tmp:
   engine=create_engine('sqlite:///'+str(Path(tmp)/'tenant.db'),connect_args={'check_same_thread':False})
   Base.metadata.create_all(bind=engine)
   factory=sessionmaker(bind=engine)
   with factory() as db:
    b=PurchaseChallan(party_name='Only My Store',challan_no='PI-001',invoice_no='',order_date='2026-11-22',source_filename='manual',file_hash='fake-'+tmp[-4:],status='OPEN')
    db.add(b);db.flush();db.add(PurchaseChallanLine(challan_id=b.id,name='Kurta',sku='K01',size='M',qty_ordered=3,qty_received=2,unit_cost=120));db.commit()
   app=FastAPI();app.include_router(router)
   def use_tenant():
    with factory() as db:yield db
   app.dependency_overrides[get_db]=use_tenant
   with TestClient(app) as client:
    r=client.get('/api/purchase-challans/export-records')
    self.assertEqual(r.status_code,200)
    wb=load_workbook(BytesIO(r.content),read_only=True)
    rows=list(wb.active.values)
    self.assertIn('Invoice No',rows[0]);self.assertEqual(len(rows),2)
    self.assertEqual(rows[1][0],'PI-001')
    self.assertEqual(rows[1][1],'Only My Store')
   engine.dispose()
if __name__=='__main__':unittest.main()
