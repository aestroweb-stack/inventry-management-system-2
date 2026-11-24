"""Ephemeral seller-level regression checks for posted corrections and invoices."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import database
import models
from business_api import ChallanDraft, create_manual_challan, edit_manual_challan, push_challan_to_inventory, purchase_challan_summary
from fastapi import HTTPException
from services import inventory_service


class SafePostedCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='myraa-correct-')
        self.root = database.SELLER_DB_ROOT
        database.SELLER_DB_ROOT = Path(self.temp.name)
        database._tenant_engines.clear()
        self.scope = database.tenant_scope(1221)
        self.scope.__enter__()
        self.db = database.SessionLocal()
        self.payload = lambda qty: ChallanDraft(party_name='Factory', challan_no='SAFE1',order_date='2026-11-24',lines=[dict(name='Tee',sku='TEE',size='S',qty_ordered=qty,qty_received=qty,unit_cost=100)])
        self.challan = create_manual_challan(self.payload(4),db=self.db)
        push_challan_to_inventory(self.challan['id'],db=self.db)

    def tearDown(self):
        self.db.close()
        self.scope.__exit__(None,None,None)
        for e in database._tenant_engines.values():e.dispose()
        database._tenant_engines.clear()
        database.SELLER_DB_ROOT=self.root
        self.temp.cleanup()

    def test_edit_four_to_three_creates_ledger_delta(self):
        result=edit_manual_challan(self.challan['id'],self.payload(3),db=self.db)
        self.assertEqual(result['status'],'POSTED')
        variant=self.db.query(models.Variant).one()
        self.db.refresh(variant)
        self.assertEqual(variant.current_stock,3)
        changes=self.db.query(models.InventoryTransaction).order_by(models.InventoryTransaction.id).all()
        self.assertEqual([t.change_qty for t in changes],[4,-1])
        day=purchase_challan_summary(date_filter='2026-11-24',db=self.db)['today']
        self.assertEqual(day['ordered_pieces'],3)
        self.assertEqual(day['pending_pieces'],0)
        self.assertEqual(day['ordered_value'],300.0)

    def test_dispatch_after_post_prevents_reversal(self):
        variant=self.db.query(models.Variant).one()
        inventory_service.apply_stock_change(self.db, variant,  -1,models.TransactionType.ORDER_DEDUCTION,reference='orders:123')
        self.db.commit()
        with self.assertRaises(HTTPException) as raised:
            edit_manual_challan(self.challan['id'],self.payload(3),db=self.db)
        self.assertEqual(raised.exception.status_code,409)
        self.db.refresh(variant)
        self.assertEqual(variant.current_stock,3)
        self.assertEqual(self.db.query(models.InventoryTransaction).count(),2)

if __name__=='__main__':unittest.main()
