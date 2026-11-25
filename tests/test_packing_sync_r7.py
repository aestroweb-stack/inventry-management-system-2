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

def test_fallback_and_legacy_prefix_ignored(session):
    files=[('file.csv',b'SKU,Qty\nMAIN_V_DRESS_S,2\n')]
    assert s.consolidate(session,files,GROUP,'2026-11-25','BETA')['matched_qty']==2
    assert s.consolidate(session,files,GROUP,'2026-11-25')['matched_qty']==2  # R10: main name abbreviation included automatically
    assert s.consolidate(session,files,{**GROUP,'master_prefix':'OTHER'},'2026-11-25','BETA')['matched_qty']==2

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
    assert 'A1:E1' in {str(r) for r in ws.merged_cells.ranges}
    assert all(ws.cell(i,1).value!='TOTAL' for i in range(1,41))
    assert ws['A40'].value=='MAIN_V_038_S' and ws['D2'].value=='MAIN_V_039_S' and ws['D8'].value is None and ws['D9'].value=='MAIN_LP_000_S'
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
        assert wb.sheetnames==['G'] and wb.active['B2'].value==2
        assert c.get(base+f'/history/{r["id"]}',params={'status':'review'}).json()['rows'][0]['qty']==3

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


def test_r8_multiple_groups_single_brand_alias_and_explicit_edit(session):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:session
    with TestClient(app) as c:
        b='/api/packing-sync/groups'
        a=c.post(b,json={'name':'MODA RAPIDO','children':['MODA RAPIDO | MODA','HERE&NOW | H&N','SZN']}).json()
        d=c.post(b,json={'name':'TWINS LADY'}).json()
        e=c.post(b,json={'name':'Another','children':['SNGR']}).json()
        assert len({a['id'],d['id'],e['id']})==3
        assert d['children']==['TWINS LADY']
        assert len(c.get(b).json())==3
        assert c.put(b+'/'+str(d['id']),json={'name':'TWINS LADY','children':['TWINS LADY | TL']}).status_code==200
        assert next(x for x in c.get(b).json() if x['id']==a['id'])==a
        assert c.post(b,json={'name':'MODA RAPIDO'}).status_code==409
        assert c.post(b,json={'name':'Conflict','children':['A | X','B | X']}).status_code==422
        assert c.delete(b+'/'+str(e['id'])).status_code==200
        assert {x['id'] for x in c.get(b).json()}=={a['id'],d['id']}
        f=c.post(b,json={'name':'Fourth'}).json();assert f['id'] not in (a['id'],d['id'],e['id'])


def test_r8_marketplace_partition_same_sku_and_arbitrary_platform(session):
    group={**GROUP,'children':['Alpha full name | ALPHA','Beta full name | BETA']}
    files=[('mixed.csv',b'SKU,Qty,Brand,Marketplace,Size\nALPHA_DRESS_S,2,ALPHA,Myntra,S\nBETA_STYLE_S,3,BETA,MYNTRA,S\nALPHA_DRESS_S,4,ALPHA,Flipkart,S\nALPHA_DRESS_S,7,ALPHA,New Shop,S\nTOTAL,99,,,\n')]
    r=s.consolidate(session,files,group,'2026-11-25')
    assert r['matched_qty']==16 and r['skipped_rows']==1
    assert {x['marketplace']:x['qty'] for x in r['items']}=={'MYNTRA':5,'FLIPKART':4,'NEW SHOP':7}
    ws=load_workbook(BytesIO(s.workbook_bytes(r))).active
    assert [ws.cell(1,col).value for col in (1,4,7)]==['Main Group - 25/11/2026 · FLIPKART','Main Group - 25/11/2026 · MYNTRA','Main Group - 25/11/2026 · NEW SHOP']
    assert all(ws.cell(row,col).value is None for row in range(1,3) for col in (3,6))
    assert [ws.cell(2,col).value for col in (2,5,8)]==[4,5,7]
    assert ws['A2'].font.sz==22 and ws['A2'].font.bold and ws.row_dimensions[2].height==38


def test_r8_marketplace_alias_inference_and_file_fallback(session):
    alias=session.query(business_models.MarketplaceSkuAlias).one();alias.marketplace='Novel platform';session.commit()
    r=s.consolidate(session,[('a.csv',b'SKU,Qty,Brand\nBETA_STYLE_S,2,BETA\n')],GROUP,'2026-11-25')
    assert r['items'][0]['marketplace']=='NOVEL PLATFORM'
    files=[('a.csv',b'SKU,Qty,Brand\nALPHA_DRESS_S,2,ALPHA\n'),('b.csv',b'SKU,Qty,Brand\nALPHA_DRESS_S,3,ALPHA\n')]
    r=s.consolidate(session,files,GROUP,'2026-11-25',file_marketplaces=['Myntra','Flipkart'])
    assert len(r['items'])==2 and r['matched_qty']==5
    r=s.consolidate(session,files,GROUP,'2026-11-25');assert len(r['items'])==2 and all(x['marketplace'].startswith('UNSPECIFIED FILE') for x in r['items'])
    r=s.consolidate(session,[('a.csv',b'SKU,Qty,Brand,Marketplace\nALPHA_DRESS_S,2,ALPHA,Myntra\n')],GROUP,'2026-11-25',file_marketplaces=['Flipkart'])
    assert r['review_qty']==2 and 'conflicts' in r['audit'][0]['issue']


def test_r8_marketplace_block_40_rows_and_party_number():
    assert s.master_party('MODA_123_DESIGN_BLUE')==('123','')
    assert s.master_party('H&N_V_STYLE_BLACK')==('V','')
    items=[{**x,'marketplace':'MYNTRA'} for x in make_items('V',40)]+[{**x,'marketplace':'FLIPKART'} for x in make_items('LP',2)]
    ws=load_workbook(BytesIO(s.workbook_bytes({'group':GROUP,'items':items,'audit':[],'include_audit':False}))).active
    assert ws.max_row==40
    assert [ws.cell(1,col).value for col in (1,4,7)]==['Main Group · FLIPKART','Main Group · MYNTRA','Main Group · MYNTRA']
    assert sum(c.value for row in ws for c in row if isinstance(c.value,int))==42


def test_r9_sidecar_summary_and_six_rows(session):
    # Real export structure: left SKU table + unrelated right-side summary.
    file=b'Master SKU,Category,Size,Color,Qty,,Marketplace,Count\nALPHA_DRESS_S,Top,S,Black,2,,Myntra PPMP 2(MY_Twinslady),9\nBETA_STYLE_S,Top,S,Black,3,,Total,9\nBETA_TOP,Top,M,Black,4,,Courier,Count\nTotal,,,,9,,Total,9\n'
    r=s.consolidate(session,[('orders.csv',file)],GROUP,'2026-11-25')
    assert r['total_qty']==r['matched_qty']==9 and r['skipped_rows']==1 and r['marketplaces']==['MYNTRA']
    assert [x['qty'] for x in r['items']]==[5,4]
    mixed=file.replace(b'Myntra PPMP 2(MY_Twinslady),9',b'Myntra PPMP 2(MY_Twinslady),5').replace(b',,Total,9\nBETA_TOP',b',,Flipkart,4\nBETA_TOP')
    r=s.consolidate(session,[('orders.csv',mixed)],GROUP,'2026-11-25')
    assert r['total_qty']==r['review_qty']==9 and not r['items'] and all('summary cannot assign' in x['issue'] for x in r['audit'])
    same_platform=mixed.replace(b'Flipkart,4',b'Myntra PPMP(MY_MFB MODA),4')
    r=s.consolidate(session,[('orders.csv',same_platform)],GROUP,'2026-11-25')
    assert r['matched_qty']==9 and r['marketplaces']==['MYNTRA']


def test_r9_exact_suffix_across_synced_brands(session):
    p=models.Product(sku='TWINS_V_HELLY_PURPLE',name='Helly');session.add(p);session.flush()
    session.add_all([models.Variant(product_id=p.id,size=size,variant_code=f'TWINS_V_HELLY_PURPLE-{size}',current_stock=0) for size in ('S','M')]);session.commit()
    group={'id':42,'name':'Twins Lady','children':['TWINS LADY | TWINS','ANOUK','FUSION']}
    payload=b'Master SKU,Category,Size,Color,Qty,,Marketplace,Count\nANOUK_HELLY_PURPLE_S,A,S,PURPLE,2,,Myntra PPMP 2(MY_Twinslady),10\nFUSION_HELLY_PURPLE_S,B,S,PURPLE,3,,Total,10\nTWINS_HELLY_PURPLE_S,A,S,PURPLE,4,,Courier,Count\nTWINS_HELLY_PURPLE_M,A,M,PURPLE,1,,Total,10\nTotal,,,,10,,,'
    r=s.consolidate(session,[('orders.csv',payload)],group,'2026-11-25')
    assert r['matched_qty']==10 and {x['pick_sku']:x['qty'] for x in r['items']}=={'TWINS_V_HELLY_PURPLE_S':9,'TWINS_V_HELLY_PURPLE_M':1}
    # A second party with the same design/colour/size must not be guessed.
    p=models.Product(sku='TWINS_LP_HELLY_PURPLE',name='Helly');session.add(p);session.flush();session.add(models.Variant(product_id=p.id,size='S',variant_code='TWINS_LP_HELLY_PURPLE-S',current_stock=0));session.commit()
    r=s.consolidate(session,[('orders.csv',payload)],group,'2026-11-25')
    assert r['review_qty']==9 and r['matched_qty']==1


def test_r9_page_limit_and_zero_match_export(session):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:session
    with TestClient(app) as c:
        b='/api/packing-sync';g=c.post(b+'/groups',json={'name':'G','children':['ALPHA']}).json()
        for i in range(7):
            payload=b'SKU,Qty\n'+b'ALPHA_DRESS_S,1\n'*13
            run=c.post(b+'/process',files=[('files',('a.csv',payload,'text/csv'))],data={'group_id':g['id'],'picklist_date':f'2026-11-{i+1:02d}'}).json()
        assert len(c.get(b+'/history').json()['items'])==6 and len(c.get(b+'/history',params={'page':2}).json()['items'])==1
        url=b+f'/history/{run["id"]}'
        assert [len(c.get(url,params={'page':i}).json()['rows']) for i in (1,2,3)]==[6,6,1]
        assert c.get(url,params={'page_size':7}).status_code==422
        miss=c.post(b+'/process',files=[('files',('a.csv',b'SKU,Qty\nMISSING,3\n','text/csv'))],data={'group_id':g['id'],'picklist_date':'2026-11-25'}).json()
        assert c.get(b+f'/history/{miss["id"]}/excel',params={'allow_partial':True}).status_code==409


def test_r10_independent_main_brands_and_shared_catalog(session):
    for sku in ('TWINS_V_HELLY_PURPLE','MODA_LP_JUGNI_PINK','TWINS_V_SHARED_BLACK'):
        p=models.Product(sku=sku,name=sku);session.add(p);session.flush()
        session.add(models.Variant(product_id=p.id,size='S',variant_code=sku+'-S',current_stock=10))
    session.commit()
    twins={'id':1,'name':'TWINS LADY','children':['ANOUK','FUSION']}
    moda={'id':2,'name':'MODA RAPIDO','children':['H&N','SZN']}
    def run(g, rows):
        return s.consolidate(session,[('file.csv',('SKU,Qty\n'+rows).encode())],g,'2026-11-25')
    a=run(twins,'ANOUK_HELLY_PURPLE_S,3\nTWINS_HELLY_PURPLE_S,2\n')
    assert a['matched_qty']==5 and a['items'][0]['pick_sku']=='TWINS_V_HELLY_PURPLE_S'
    b=run(moda,'MODA_JUGNI_PINK_S,3\nSZN_JUGNI_PINK_S,2\nH&N_SHARED_BLACK_S,4\n')
    assert b['matched_qty']==9 and {x['pick_sku']:x['qty'] for x in b['items']}=={'MODA_LP_JUGNI_PINK_S':5,'TWINS_V_SHARED_BLACK_S':4}
    assert 'TWINS' not in s.group_brand_lookup(moda)[0]
    assert run(moda,'UNRELATED_SHARED_BLACK_S,3\n')['review_qty']==3
    # A second physical master with the same exact suffix is never guessed.
    p=models.Product(sku='OTHER_LP_SHARED_BLACK',name='other');session.add(p);session.flush();session.add(models.Variant(product_id=p.id,size='S',variant_code=p.sku+'-S',current_stock=1));session.commit()
    assert run(moda,'H&N_SHARED_BLACK_S,4\n')['review_qty']==4
    # Group-specific master takes priority when other groups have a similar design.
    p=models.Product(sku='TWINS_V_JUGNI_PINK',name='other');session.add(p);session.flush();session.add(models.Variant(product_id=p.id,size='S',variant_code=p.sku+'-S',current_stock=1));session.commit()
    assert run(moda,'SZN_JUGNI_PINK_S,1\n')['items'][0]['pick_sku']=='MODA_LP_JUGNI_PINK_S'
    assert session.query(models.InventoryTransaction).count()==0


def test_r10_party_items_paginate_six(session):
    app=FastAPI();app.include_router(router);app.dependency_overrides[get_db]=lambda:session
    for n in range(13):
        p=models.Product(sku=f'MODA_{n}_DESIGN_BLACK',name=f'design {n}');session.add(p);session.flush();session.add(models.Variant(product_id=p.id,size='S',variant_code=p.sku+'-S',current_stock=0))
    session.commit()
    with TestClient(app) as c:
        b='/api/packing-sync';g=c.post(b+'/groups',json={'name':'MODA RAPIDO','children':['SZN']}).json()
        payload=('SKU,Qty\n'+''.join(f'MODA_{n}_DESIGN_BLACK_S,1\n' for n in range(13))).encode()
        r=c.post(b+'/process',files=[('files',('a.csv',payload,'text/csv'))],data={'group_id':g['id'],'picklist_date':'2026-11-25'}).json()
        assert r['matched_qty']==13 and len(r['party_totals'])==13
        url=b+f'/history/{r["id"]}'
        assert [len(c.get(url,params={'page':i,'view':'items'}).json()['rows']) for i in (1,2,3)]==[6,6,1]
