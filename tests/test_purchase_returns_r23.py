"""Real SQLite / API regression tests for posted supplier returns."""
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from io import BytesIO
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from openpyxl import load_workbook
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'backend'))
from database import Base, get_db
import models, business_models, business_api
from services import inventory_service

@pytest.fixture
def setup(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'inventory.db'),connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine,autoflush=False)
    app=FastAPI();app.include_router(business_api.router)
    def provide():
        with Session() as db: yield db
    app.dependency_overrides[get_db]=provide
    with TestClient(app) as c:
        yield c,Session
    engine.dispose()


def purchase(c,number='PI-1',qty=10,rate=125,sku='KURTA'):
    payload={'party_name':'Supplier A','challan_no':number,'order_date':'2026-11-27','lines':[{'sku':sku,'name':'Kurta '+sku,'size':size,'qty_ordered':qty,'qty_received':qty,'unit_cost':rate} for size in ['M','L']]}
    r=c.post('/api/purchase-challans/manual',json=payload); assert r.status_code==201,r.text
    d=r.json();r=c.post(f"/api/purchase-challans/{d['id']}/push");assert r.status_code==200,r.text
    return d,payload


def payload(d,number='RI-1',qty=3,idx=0):
    return {'purchase_id':d['id'],'return_no':number,'return_date':'2026-11-27','reason':'Damaged','lines':[{'line_id':d['lines'][idx]['id'],'quantity':qty}]}


def stock(S,sku='KURTA'):
    with S() as db:
        return {v.size:v.current_stock for v in db.query(models.Variant).join(models.Product).filter(models.Product.sku==sku)}


def test_partial_return_rate_ledger_alert_and_history(setup):
    c,S=setup;d,_=purchase(c)
    r=c.post('/api/purchase-returns',json=payload(d,qty=6));assert r.status_code==201,r.text
    result=r.json();assert result['amount']==750 and result['quantity']==6
    assert stock(S)=={'M':4,'L':10}
    with S() as db:
        t=db.query(models.InventoryTransaction).filter_by(created_by='supplier_return').one();assert t.change_qty==-6 and t.balance_after==4
        assert db.query(models.Alert).filter_by(variant_id=t.variant_id,resolved=False).count()==1
        assert db.query(business_models.PurchaseChallanLine).filter_by(id=d['lines'][0]['id']).one().qty_received==10
    data=c.get(f"/api/purchase-returns/sources/{d['id']}").json();assert data['lines'][0]['available']==4 and data['lines'][0]['returned']==6
    history=c.get('/api/purchase-returns?date_filter=2026-11-27&q=supplier').json();assert history['quantity']==6 and history['amount']==750 and history['total']==1
    assert c.get('/api/purchase-returns?date_filter=2026-11-26').json()['total']==0
    assert c.get(f"/api/purchase-returns/{result['id']}").json()['lines'][0]['balance_after']==4
    wb=load_workbook(BytesIO(c.get(f"/api/purchase-returns/{result['id']}/export").content));assert wb.active['J2'].value==750


def test_duplicate_number_and_cumulative_cap(setup):
    c,S=setup;d,_=purchase(c);purchase(c,'PI-2',qty=20)
    assert c.post('/api/purchase-returns',json=payload(d,qty=7)).status_code==201
    assert c.post('/api/purchase-returns',json=payload(d,'ri-1',qty=1)).status_code==409
    assert c.post('/api/purchase-returns',json=payload(d,'RI-2',qty=4)).status_code==409
    assert c.post('/api/purchase-returns',json=payload(d,'RI-2',qty=3)).status_code==201
    assert stock(S)=={'M':20,'L':30}


def test_stock_cap_and_atomic_multiline_rejection(setup):
    c,S=setup;d,_=purchase(c)
    with S() as db:
        v=db.query(models.Variant).filter_by(size='L').one();inventory_service.apply_stock_change(db,v,-9,models.TransactionType.ORDER_DEDUCTION);db.commit()
    p=payload(d);p['lines'].append({'line_id':d['lines'][1]['id'],'quantity':2})
    assert c.post('/api/purchase-returns',json=p).status_code==409
    assert stock(S)=={'M':10,'L':1};assert c.get('/api/purchase-returns').json()['total']==0


def test_validation_and_wrong_invoice(setup):
    c,S=setup;d,_=purchase(c);d2,_=purchase(c,'PI-2',sku='OTHER')
    p=payload(d);p['lines'][0]['line_id']=d2['lines'][0]['id'];assert c.post('/api/purchase-returns',json=p).status_code==409
    for bad in [-1,0,1.5,True,'2']:
        assert c.post('/api/purchase-returns',json=payload(d,qty=bad)).status_code==422
    p=payload(d);p['lines']*=2;assert c.post('/api/purchase-returns',json=p).status_code==422
    p=payload(d);p['return_date']='2026-11-26';assert c.post('/api/purchase-returns',json=p).status_code==422
    assert stock(S)=={'M':10,'L':10}


def test_moving_average_not_overwritten_and_correction_blocked(setup):
    c,S=setup;d,original=purchase(c,rate=100);purchase(c,'PI-2',rate=200)
    r=c.post('/api/purchase-returns',json=payload(d,qty=2));assert r.status_code==201,r.text
    assert r.json()['amount']==200 and r.json()['lines'][0]['inventory_unit_cost']==150
    with S() as db:
        v=db.query(models.Variant).filter_by(size='M').one();assert v.cost_record.unit_cost==Decimal('150.00')
    original['lines'][0]['qty_received']=original['lines'][0]['qty_ordered']=9
    assert c.put(f"/api/purchase-challans/{d['id']}/manual",json=original).status_code==409
    assert stock(S)=={'M':18,'L':20}


def test_concurrent_returns_never_overreturn(setup):
    c,S=setup;d,_=purchase(c)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(lambda n:c.post('/api/purchase-returns',json=payload(d,n,qty=7)).status_code,['RI-A','RI-B']))
    assert sorted(codes)==[201,409];assert stock(S)=={'M':3,'L':10}


def test_missing_or_trashed_original_stock_is_blocked(setup):
    c,S=setup;d,_=purchase(c)
    with S() as db:
        v=db.query(models.Variant).filter_by(size='M').one();v.is_deleted=True;db.commit()
    assert c.post('/api/purchase-returns',json=payload(d)).status_code==409
    assert stock(S)=={'M':10,'L':10}


def test_receipt_not_present_and_unposted_invoice(setup):
    c,S=setup;d,_=purchase(c)
    with S() as db:
        db.query(models.InventoryTransaction).filter_by(created_by='challan_receipt').delete();db.commit()
    assert c.post('/api/purchase-returns',json=payload(d)).status_code==409
    with S() as db:
        db.query(business_models.PurchaseChallan).filter_by(id=d['id']).one().status='READY';db.commit()
    assert c.get(f"/api/purchase-returns/sources/{d['id']}").status_code==404
    assert c.post('/api/purchase-returns',json=payload(d)).status_code==404


def test_pagination_and_export(setup):
    c,S=setup;d,_=purchase(c,qty=20)
    for i in range(8): assert c.post('/api/purchase-returns',json=payload(d,f'RI-{i}',1)).status_code==201
    data=c.get('/api/purchase-returns').json();assert data['total']==8 and len(data['items'])==6 and data['quantity']==8
    assert len(c.get('/api/purchase-returns?offset=6').json()['items'])==2
    wb=load_workbook(BytesIO(c.get('/api/purchase-returns/export-records').content));assert wb.active.max_row==9


def test_cross_database_seller_isolation(setup,tmp_path):
    c,S=setup;d,_=purchase(c);r=c.post('/api/purchase-returns',json=payload(d));assert r.status_code==201
    engine=create_engine('sqlite:///'+str(tmp_path/'seller_b.db'),connect_args={'check_same_thread':False});Base.metadata.create_all(engine);Other=sessionmaker(bind=engine)
    app=FastAPI();app.include_router(business_api.router)
    def provide():
        with Other() as db:yield db
    app.dependency_overrides[get_db]=provide
    with TestClient(app) as b:
        assert b.get('/api/purchase-returns').json()['total']==0
        assert b.get(f"/api/purchase-returns/{r.json()['id']}").status_code==404
        assert b.post('/api/purchase-returns',json=payload(d)).status_code==404
    engine.dispose()


def test_failure_after_first_mutation_rolls_back_all(setup,monkeypatch):
    c,S=setup;d,_=purchase(c)
    real=inventory_service.apply_stock_change
    def fail_second(db,variant,delta,*args,**kwargs):
        if variant.size=='L':raise inventory_service.InsufficientStockError(variant.variant_code,0,-delta)
        return real(db,variant,delta,*args,**kwargs)
    monkeypatch.setattr(inventory_service,'apply_stock_change',fail_second)
    p=payload(d);p['lines'].append({'line_id':d['lines'][1]['id'],'quantity':2})
    assert c.post('/api/purchase-returns',json=p).status_code==409
    assert stock(S)=={'M':10,'L':10}
    with S() as db:
        assert db.query(models.InventoryTransaction).filter_by(created_by='supplier_return').count()==0
        assert db.query(business_models.PurchaseReturn).count()==0


def test_additive_tables_for_existing_database(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'existing.db'))
    old=[t for t in Base.metadata.sorted_tables if t.name not in ('myraa_purchase_returns','myraa_purchase_return_lines')]
    Base.metadata.create_all(engine,tables=old)
    S=sessionmaker(bind=engine)
    with S() as db:
        db.add(models.Product(sku='EXISTING',name='Existing'));db.commit()
    Base.metadata.create_all(engine)
    with S() as db:
        assert db.query(models.Product).filter_by(sku='EXISTING').count()==1
        assert db.query(business_models.PurchaseReturn).count()==0
    engine.dispose()
