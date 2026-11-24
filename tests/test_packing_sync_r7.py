from pathlib import Path
import sys
from io import BytesIO
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from openpyxl import load_workbook
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import models, business_models, workspace_models
from database import Base
from packing_sync_api import BrandGroup, SavedPicklist, router
from services import packing_sync_service as s
from fastapi import FastAPI
from fastapi.testclient import TestClient
from database import get_db

@pytest.fixture
def session(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/test.db',connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        a=models.Product(sku='MAIN_V_DRESS',name='Dress'); b=models.Product(sku='MAIN_LP_TOP',name='Top'); db.add_all([a,b]); db.flush()
        v=models.Variant(product_id=a.id,size='S',variant_code='MAIN_V_DRESS-S',current_stock=10,seller_sku_code='ALPHA_DRESS_S')
        w=models.Variant(product_id=b.id,size='M',variant_code='MAIN_LP_TOP-M',current_stock=5); db.add_all([v,w]); db.flush()
        db.add_all([business_models.MarketplaceSkuAlias(variant_id=v.id,alias_code='BETA_STYLE_S',normalized_code='BETA_STYLE_S'),business_models.ProductMarketplaceAlias(product_id=b.id,alias_code='BETA_TOP',normalized_code='BETA_TOP')]);db.commit();yield db
    engine.dispose()

GROUP={'id':1,'name':'Main Group','master_prefix':'MAIN','children':['ALPHA','BETA']}
UPLOAD=[('orders.csv',b'SKU,Qty,Brand,Size\nALPHA_DRESS_S,2,ALPHA,S\nBETA_STYLE_S,3,BETA,S\nBETA_TOP,4,BETA,M\nTOTAL,999,,\n')]

def test_exact_merge_total_ignored_and_stock_unchanged(session):
    r=s.consolidate(session,UPLOAD,GROUP,'2026-11-25')
    assert r['total_qty']==r['matched_qty']==9 and r['skipped_rows']==1
    assert [(x['party'],x['pick_sku'],x['qty']) for x in r['items']]==[('V','MAIN_V_DRESS_S',5),('LP','MAIN_LP_TOP_M',4)]
    assert session.query(models.Variant).order_by(models.Variant.id).all()[0].current_stock==10
    assert session.query(models.InventoryTransaction).count()==0

def test_unknown_conflicting_size_and_brand_review(session):
    files=[('file.csv',b'SKU,Qty,Brand,Size\nMISSING,2,ALPHA,S\nALPHA_DRESS_S,3,ALPHA,M\nBETA_TOP,4,OTHER,M\n')]
    r=s.consolidate(session,files,GROUP,'2026-11-25')
    assert r['total_qty']==r['review_qty']==9 and r['matched_qty']==0 and r['review_count']==3
    assert len(r['items'])==0

def test_fallback_and_explicit_prefix(session):
    files=[('file.csv',b'SKU,Qty\nMAIN_V_DRESS_S,2\n')]
    assert s.consolidate(session,files,GROUP,'2026-11-25','BETA')['matched_qty']==2
    assert s.consolidate(session,files,GROUP,'2026-11-25')['review_qty']==2
    assert s.consolidate(session,files,{**GROUP,'master_prefix':'OTHER'},'2026-11-25','BETA')['review_qty']==2

@pytest.mark.parametrize('qty',['2.5','0','-1','NaN','Infinity'])
def test_invalid_qty(session,qty):
    with pytest.raises(s.PicklistError): s.consolidate(session,[('file.csv',f'SKU,Qty\nALPHA_DRESS_S,{qty}\n'.encode())],GROUP,'2026-11-25')

def test_duplicate_file(session):
    with pytest.raises(s.PicklistError,match='Duplicate'): s.consolidate(session,UPLOAD*2,GROUP,'2026-11-25')

def make_items(party,n):
    return [dict(party=party,master_sku=f'MAIN_{party}_{i:03d}',size='S',pick_sku=f'MAIN_{party}_{i:03d}_S',qty=1) for i in range(n)]

@pytest.mark.parametrize('v,lp,sizes',[(20,10,[31]),(40,2,[40,2]),(45,10,[40,16]),(39,1,[39,1]),(41,40,[40,1,40]),(90,5,[40,40,16])])
def test_lane_boundaries(v,lp,sizes):
    lanes=s.pack_lanes(make_items('V',v)+make_items('LP',lp))
    assert [len(x) for x in lanes]==sizes
    assert sum(x['qty'] for lane in lanes for x in lane if x)==v+lp
    assert all(len(l)<=40 for l in lanes)

def test_excel_format():
    items=make_items('V',45)+make_items('LP',10)
    result={'group':GROUP,'items':items,'audit':[],'include_audit':False}
    wb=load_workbook(BytesIO(s.workbook_bytes(result))); ws=wb.active
    assert wb.sheetnames==['Main Group'];assert ws.max_row==40 and ws.max_column==5
    assert ws['A40'].value=='MAIN_V_039_S' and ws['D1'].value=='MAIN_V_040_S' and ws['D6'].value is None and ws['D7'].value=='MAIN_LP_000_S'
    assert all(ws.cell(r,3).value is None for r in range(1,41))
    assert ws['A1'].font.sz==22 and ws['A1'].font.bold and ws.row_dimensions[1].height==38
    assert all(getattr(ws['A1'].border,k).color.rgb=='FF000000' for k in ('top','bottom','left','right'))
    assert sum(c.value for row in ws for c in row if isinstance(c.value,int))==55
    assert all(c.value!='TOTAL' for row in ws for c in row)

def test_api_persistence_download_delete_and_group_isolation(session):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:session
    with TestClient(app) as client:
        base='/api/packing-sync'
        g=client.post(base+'/groups',json={'name':'Main Group','master_prefix':'MAIN','children':['ALPHA','BETA']});assert g.status_code==201,g.text
        gid=g.json()['id']; assert client.post(base+'/groups',json={'name':'Main  Group','children':['X']}).status_code==409
        other=client.post(base+'/groups',json={'name':'Other Group','children':['X']});assert other.status_code==201
        req=dict(files=[('files',('orders.csv',UPLOAD[0][1],'text/csv'))],data={'group_id':gid,'picklist_date':'2026-11-25'})
        response=client.post(base+'/process',**req);assert response.status_code==200,response.text
        run=response.json();id=run['id'];assert client.post(base+'/process',**req).json()['id']==id
        assert client.get(base+'/history',params={'day':'2026-11-25'}).json()['count']==1
        assert client.get(base+'/history',params={'group_id':other.json()['id']}).json()['count']==0
        assert client.get(base+f'/history/{id}',params={'view':'items'}).json()['count']==2
        binary=client.get(base+f'/history/{id}/excel').content
        assert client.delete(base+f'/groups/{gid}').status_code==200
        assert client.get(base+f'/history/{id}/excel').content==binary
        assert client.delete(base+f'/history/{id}').status_code==200
        assert client.get(base+'/history').json()['count']==0 and client.get(base+f'/history/{id}/excel').status_code==404
        assert session.get(SavedPicklist,id).hidden and session.get(SavedPicklist,id).workbook==binary

def test_api_partial_review_requires_confirmation(session):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:session
    with TestClient(app) as c:
        base='/api/packing-sync';g=c.post(base+'/groups',json={'name':'G','children':['ALPHA']}).json()
        r=c.post(base+'/process',files=[('files',('a.csv',b'SKU,Qty\nALPHA_DRESS_S,2\nMISSING,3\n','text/csv'))],data={'group_id':g['id'],'picklist_date':'2026-11-25'}).json()
        url=base+f'/history/{r["id"]}/excel'; assert c.get(url).status_code==409
        wb=load_workbook(BytesIO(c.get(url,params={'allow_partial':'true'}).content))
        assert wb.sheetnames==['G','Review'] and wb['Review']['H2'].value==3

def test_seller_database_isolation_and_restart(monkeypatch,tmp_path):
    import database
    monkeypatch.setattr(database,'SELLER_DB_ROOT',tmp_path/'workspaces')
    monkeypatch.setattr(database,'DATABASE_URL','sqlite:///test-root.db')
    monkeypatch.setattr(database,'_tenant_engines',{})
    with database.tenant_scope(1001):
        with database.SessionLocal() as db:
            db.add(BrandGroup(name='Seller A',name_key='SELLER_A',children_json='["ALPHA"]',master_prefix='A'));db.commit()
            assert db.query(BrandGroup).count()==1
    with database.tenant_scope(1002):
        with database.SessionLocal() as db: assert db.query(BrandGroup).count()==0
    for engine in database._tenant_engines.values(): engine.dispose()
    database._tenant_engines.clear()
    with database.tenant_scope(1001):
        with database.SessionLocal() as db: assert db.query(BrandGroup).one().name=='Seller A'
    for engine in database._tenant_engines.values(): engine.dispose()
