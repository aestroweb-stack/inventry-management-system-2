"""R12 offline product import and duplicate identity regressions."""
from io import BytesIO
from pathlib import Path
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from openpyxl import Workbook, load_workbook
from fastapi import HTTPException
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import models, business_models
from database import Base
from services.bulk_product_service import HEADERS, build_bulk_product_template, import_bulk_products
from services.catalog_identity import design_key
from services import bulk_stock_service

@pytest.fixture
def db(tmp_path):
    engine=create_engine(f'sqlite:///{tmp_path}/catalog.db')
    Base.metadata.create_all(engine)
    with Session(engine,autoflush=False) as session:
        yield session
    engine.dispose()


LEGACY_HEADERS = HEADERS + ["Size Unit Cost", "Product SKU Aliases", "Size SKU Aliases", "Photo Filenames"]

def workbook(rows, headers=LEGACY_HEADERS):
    wb=Workbook(); ws=wb.active; ws.append(headers)
    for r in rows: ws.append(r)
    b=BytesIO(); wb.save(b); return b.getvalue()


def row(sku='SAMPLE_V_D01_BLACK',name='D01_BLACK',size='S',stock=5,**kwargs):
    return [sku,name,'Kurta set',size,stock,2,20,kwargs.get('cost'),kwargs.get('size_cost'),kwargs.get('pa'),kwargs.get('va'),kwargs.get('photos')]


def test_template():
    wb=load_workbook(BytesIO(build_bulk_product_template()))
    assert [c.value for c in wb.active[1]]==HEADERS
    assert wb.active['A2'].number_format=='@'
    assert wb.active['H2'].number_format=='0.00'
    assert len(wb.sheetnames)==3


def test_full_product_fields_and_alias_scopes(db):
    r=import_bulk_products(db,workbook([row(cost=300,size_cost=320,pa='Platform A | EXT-D01\nPlatform C | EXT-2',va='Platform A | EXT-D01-S',photos='front.jpg\nback.png'),row(size='M',stock=0,cost=300,va='Platform B | EXT-D01-M')]),'bulk.xlsx')
    assert r['products_created']==1 and r['variants_created']==2 and not r['errors']
    assert r['photo_assignments']==[{'sku':'SAMPLE_V_D01_BLACK','files':['front.jpg','back.png']}]
    p=db.query(models.Product).one(); assert p.cost_record.unit_cost==300
    v={v.size:v for v in p.variants}; assert v['S'].cost_record.unit_cost==320 and not v['M'].cost_record
    assert v['S'].current_stock==5 and v['M'].current_stock==0
    assert v['S'].reorder_threshold==2 and v['S'].target_stock_level==20
    assert {a.alias_code for a in p.product_aliases}=={'EXT-D01','EXT-2'}
    assert {a.alias_code for a in v['S'].marketplace_aliases}=={'EXT-D01-S'}
    assert db.query(models.InventoryTransaction).one().change_qty==5


def test_duplicate_design_and_existing_sizes_protected(db):
    import_bulk_products(db,workbook([row(cost=300)]),'bulk.xlsx')
    r=import_bulk_products(db,workbook([row(sku='SAMPLE2_V_D01_BLACK',name=' d01-black ',stock=100),row(stock=99,cost=999),row(size='M',stock=2,cost=999)]),'bulk.xlsx')
    assert r['products_created']==0 and r['variants_created']==1 and r['duplicate_warnings']==2
    p=db.query(models.Product).one(); assert p.cost_record.unit_cost==300
    assert {v.size:v.current_stock for v in p.variants}=={'S':5,'M':2}
    assert any('Duplicate design' in x['message'] for x in r['errors'])
    assert any('Duplicate existing size' in x['message'] for x in r['errors'])


def test_duplicate_in_one_upload(db):
    r=import_bulk_products(db,workbook([row(),row(sku='OTHER_D01',name='d01 black'),row(sku='OTHER_RED',name='D01_RED')]),'bulk.xlsx')
    assert r['products_created']==2 and r['duplicate_warnings']==1
    assert db.query(models.Product).count()==2


def test_alias_collision_rolls_back_entire_design(db):
    import_bulk_products(db,workbook([row(va='Platform A | SHARED')]),'bulk.xlsx')
    r=import_bulk_products(db,workbook([row(sku='SAMPLE_D02',name='D02',va='Platform A | UNIQUE'),row(sku='SAMPLE_D02',name='D02',size='M',va='Platform A | SHARED'),row(sku='SAMPLE_D03',name='D03')]),'bulk.xlsx')
    assert r['products_created']==1 and r['variants_created']==1
    assert not db.query(models.Product).filter_by(sku='SAMPLE_D02').first()
    assert not db.query(business_models.MarketplaceSkuAlias).filter_by(normalized_code='UNIQUE').first()
    assert db.query(models.InventoryTransaction).count()==2


def test_legacy_header_preserves_variant_sku(db):
    r=import_bulk_products(db,workbook([['SAMPLE_V_D01_BLACK-S','D01_BLACK-S','Kurta set','S',0,5,20],['SAMPLE_V_D01_BLACK-M','D01_BLACK-M','Kurta set','M',1,5,20]],['SKU','Product Name','Category','Size','Stock','Alert Below','Target Stock']),'bulk.xlsx')
    assert r['products_created']==1 and r['variants_created']==2
    p=db.query(models.Product).one(); assert p.sku=='SAMPLE_V_D01_BLACK'
    assert {v.variant_code for v in p.variants}=={'SAMPLE_V_D01_BLACK-S','SAMPLE_V_D01_BLACK-M'}


def test_conflicting_repeated_size_and_invalid_photo(db):
    r=import_bulk_products(db,workbook([row(),row(stock=99),row(sku='OTHER',name='Other',photos='../bad.jpg')]),'bulk.xlsx')
    assert r['products_created']==0 and r['rows_skipped']==3 and r['errors']


def test_repeat_rows_can_add_aliases_without_double_stock(db):
    r=import_bulk_products(db,workbook([row(va='Platform A | EXT-A'),row(va='Platform B | EXT-B')]),'bulk.xlsx')
    assert r['products_created']==1 and r['variants_created']==1 and r['duplicate_rows_merged']==1
    v=db.query(models.Variant).one(); assert v.current_stock==5 and len(v.marketplace_aliases)==2


def test_bulk_count_does_not_create_duplicate(db):
    import_bulk_products(db,workbook([row()]),'bulk.xlsx')
    with pytest.raises(ValueError,match='Duplicate design'):
        bulk_stock_service.bulk_set_stock(db,workbook([['OTHER','M','D01 Black',10]],['SKU','Size','Product','Count']),'stock.xlsx')
    assert db.query(models.Product).count()==1


def test_single_create_duplicate_and_rename(db):
    import main
    payload=main.schemas.ProductCreate(sku='SAMPLE_A',name='Design 01 Black',variants=[{'size':'S'}])
    main.create_product(payload,db=db)
    with pytest.raises(HTTPException,match='Duplicate design') as e:
        main.create_product(main.schemas.ProductCreate(sku='SAMPLE_B',name='design-01_black',variants=[{'size':'M'}]),db=db)
    assert e.value.status_code==409
    db.rollback()
    main.create_product(main.schemas.ProductCreate(sku='SAMPLE_C',name='Design 01 Red',variants=[{'size':'S'}]),db=db)
    with pytest.raises(HTTPException,match='Duplicate design'):
        main.update_product('SAMPLE_C',main.schemas.ProductUpdate(name='Design 01 Black'),db=db)
    db.rollback()
    assert db.query(models.Product).count()==2


def test_qr_only_markup_and_duplicate_dialog():
    root=Path(__file__).resolve().parents[1]/'frontend'; h=(root/'index.html').read_text(); js=(root/'app.js').read_text()
    assert 'product-barcode-download' not in h+js and 'product-combo-download' not in h+js
    assert 'product-qr-download' in h and 'Open product barcode label' not in js
    assert 'Duplicate product or SKU' in js and 'Duplicate catalog entries' in js


def test_bulk_aliases_work_in_packing_and_qr(db):
    from services import packing_sync_service as packing
    from services.barcode_service import generate_product_qr_label
    from PIL import Image
    import_bulk_products(db,workbook([row(pa='Platform A | EXT-D01',va='Platform A | EXT-D01-S'),row(size='M',stock=2)]),'bulk.xlsx')
    result=packing.consolidate(db,[('orders.csv',b'SKU,Qty,Size,Marketplace\nEXT-D01-S,2,S,Platform A\nEXT-D01,3,M,Platform A\n')],{'name':'Sample Apparel','children':[]},'2026-11-25')
    assert result['matched_qty']==5 and result['review_qty']==0
    assert db.query(models.Variant).filter_by(size='S').one().current_stock==5
    assert Image.open(BytesIO(generate_product_qr_label('SAMPLE_V_D01_BLACK','D01_BLACK'))).format=='PNG'
