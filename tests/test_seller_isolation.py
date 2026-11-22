"""Seller isolation tests: disposable databases, never production files."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import inspect
import database
import models, business_models, workspace_models, clerk_auth
from business_api import ChallanDraft, create_manual_challan, edit_manual_challan


class SellerIsolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='myraa-private-sellers-')
        self.old_root = database.SELLER_DB_ROOT
        database.SELLER_DB_ROOT = Path(self.tmp.name) / 'private'
        database._tenant_engines.clear()

    def tearDown(self):
        for eng in database._tenant_engines.values():
            eng.dispose()
        database._tenant_engines.clear()
        database.SELLER_DB_ROOT = self.old_root
        self.tmp.cleanup()

    def test_products_stock_orders_and_challans_do_not_cross_sellers(self):
        with database.tenant_scope(1001):
            with database.SessionLocal() as db:
                p = models.Product(sku='SHARED-SKU',name='Seller A Dress')
                db.add(p); db.flush()
                db.add(models.Variant(product_id=p.id,size='S',variant_code='SHARED-SKU-S',current_stock=11))
                db.commit()
                draft = ChallanDraft(party_name='Factory A',challan_no='A100',order_date='2026-11-21',lines=[dict(name='Dress A',sku='DRESS-A',size='S',qty_ordered=4,qty_received=0,unit_cost=100)])
                result=create_manual_challan(draft,db=db)
                self.assertEqual(result['ordered'],4)
                path_a = database.tenant_root()
                (path_a/'product_images').mkdir()
                (path_a/'product_images'/'private.webp').write_bytes(b'A-private')
                (path_a/'challan_scans').mkdir()
                (path_a/'challan_scans'/'a.pdf').write_bytes(b'%PDF-A')
        with database.tenant_scope(1002):
            with database.SessionLocal() as db:
                self.assertEqual(db.query(models.Product).count(),0)
                self.assertEqual(db.query(models.Variant).count(),0)
                self.assertEqual(db.query(business_models.PurchaseChallan).count(),0)
                p=models.Product(sku='SHARED-SKU',name='Seller B Dress')
                db.add(p);db.flush()
                db.add(models.Variant(product_id=p.id,size='S',variant_code='SHARED-SKU-S',current_stock=2))
                db.commit()
                self.assertEqual(db.query(models.Product).one().name,'Seller B Dress')
                self.assertFalse((database.tenant_root()/'product_images'/'private.webp').exists())
                self.assertFalse((database.tenant_root()/'challan_scans'/'a.pdf').exists())
        with database.tenant_scope(1001):
            with database.SessionLocal() as db:
                self.assertEqual(db.query(models.Variant).one().current_stock,11)
                self.assertEqual(db.query(business_models.PurchaseChallan).count(),1)
        self.assertTrue((path_a/'inventory.db').exists())
        self.assertNotEqual((path_a/'inventory.db').resolve(),(database.tenant_root(1002)/'inventory.db').resolve())

    def test_no_anonymous_tenant_root_and_root_session_remains_legacy(self):
        with self.assertRaises(RuntimeError):database.tenant_root()
        with self.assertRaises(RuntimeError):database.tenant_root('../../etc')
        with database.SessionLocal() as root:
            self.assertIs(root.bind, database.engine)
        self.assertFalse((database.SELLER_DB_ROOT/'seller_42').exists())

    def test_same_challan_ids_are_private_and_edit_only_own_database(self):
        for seller in (2001,2002):
            with database.tenant_scope(seller):
                with database.SessionLocal() as db:
                    draft=ChallanDraft(party_name='Supplier',challan_no='ONE',order_date='2026-11-21',lines=[dict(name='Dress',sku='SKU',size='M',qty_ordered=3,qty_received=0,unit_cost=50)])
                    result=create_manual_challan(draft,db=db)
                    self.assertEqual(result['id'],1)
        with database.tenant_scope(2001):
            with database.SessionLocal() as db:
                draft=ChallanDraft(party_name='Seller A Only',challan_no='ONE',order_date='2026-11-21',lines=[dict(name='Dress',sku='SKU',size='M',qty_ordered=3,qty_received=2,unit_cost=50)])
                edit_manual_challan(1,draft,db=db)
        with database.tenant_scope(2002):
            with database.SessionLocal() as db:
                record=db.query(business_models.PurchaseChallan).one()
                self.assertEqual(record.party_name,'Supplier')
                self.assertEqual(record.lines[0].qty_received,0)

    def test_railway_requires_real_volume_for_new_accounts(self):
        with patch.dict(os.environ,{'RAILWAY_ENVIRONMENT':'production'}, clear=False):
            with patch.dict(os.environ,{'RAILWAY_VOLUME_MOUNT_PATH':''}, clear=False):
                with patch.dict(os.environ,{'MYRAA_SELLER_DB_ROOT':''}, clear=False):
                    with self.assertRaises(RuntimeError):
                        database.tenant_engine(3001)

if __name__=='__main__':unittest.main()
