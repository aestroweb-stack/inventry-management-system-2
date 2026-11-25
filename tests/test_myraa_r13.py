"""R13: simple workbook, inherited costs, safe selected-photo removal."""
from io import BytesIO
from pathlib import Path
import sys
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from openpyxl import Workbook, load_workbook
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import models, business_models
from database import Base
from services.bulk_product_service import HEADERS, build_bulk_product_template, import_bulk_products

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f'sqlite:///{tmp_path}/catalog.sqlite')
    Base.metadata.create_all(engine)
    with Session(engine, autoflush=False) as session:
        yield session
    engine.dispose()

def test_eight_column_workbook():
    wb=load_workbook(BytesIO(build_bulk_product_template()))
    expected=['Master SKU','Product Name','Category','Size','Stock','Alert Below','Target Stock','Default Unit Cost']
    assert HEADERS==expected
    assert [c.value for c in wb['Products'][1]]==expected
    assert wb['Products'].max_column==8
    assert wb['Example - do not import'].max_column==8
    assert wb['Products'].auto_filter.ref.startswith('A1:H')

def test_default_cost_inherited(db):
    import main
    wb=Workbook(); ws=wb.active; ws.append(HEADERS)
    ws.append(['SAMPLE_V_D01_BLACK','D01_BLACK','Kurta set','S',2,5,20,300])
    ws.append(['SAMPLE_V_D01_BLACK','D01_BLACK','Kurta set','M',3,5,20,None])
    ws.append(['SAMPLE_V_D01_BLACK','D01_BLACK','Kurta set','L',0,5,20,None])
    b=BytesIO(); wb.save(b)
    result=import_bulk_products(db,b.getvalue(),'sample.xlsx')
    assert result['products_created']==1 and result['variants_created']==3 and not result['errors']
    p=main.get_product('SAMPLE_V_D01_BLACK',db=db)
    assert p['unit_cost']==300 and p['stock_cost_value']==1500
    assert {v['size']:v['unit_cost'] for v in p['variants']}=={'S':300,'M':300,'L':300}
    assert all(v['cost_override'] is None for v in p['variants'])
    duplicate=import_bulk_products(db,b.getvalue(),'sample.xlsx')
    assert duplicate['products_created']==0 and duplicate['variants_created']==0
    assert main.get_product('SAMPLE_V_D01_BLACK',db=db)['total_stock']==5

def photo_fixture(db,tmp_path,monkeypatch):
    import main
    monkeypatch.setattr(main,'seller_image_dir',lambda:tmp_path)
    p=models.Product(sku='SAMPLE_A',name='Design A',image_path='/static/product_images/first.jpg')
    q=models.Product(sku='SAMPLE_B',name='Design B',image_path='/static/product_images/other.jpg')
    db.add_all([p,q]); db.flush()
    db.add(models.Variant(product_id=p.id,size='S',variant_code='SAMPLE_A_S',current_stock=7))
    for product,name,primary in [(p,'first.jpg',True),(p,'second.jpg',False),(p,'third.jpg',False),(q,'other.jpg',True)]:
        db.add(models.ProductImage(product_id=product.id,image_path='/static/product_images/'+name,is_primary=primary))
        (tmp_path/name).write_bytes(b'photo test')
    db.commit()
    return main

def test_remove_extra_primary_last_preserves_product(db,tmp_path,monkeypatch):
    main=photo_fixture(db,tmp_path,monkeypatch)
    r=main.delete_product_photo('SAMPLE_A','third.jpg',db=db)
    assert r['image_path'].endswith('first.jpg') and len(r['images'])==2
    assert not (tmp_path/'third.jpg').exists()
    r=main.delete_product_photo('SAMPLE_A','first.jpg',db=db)
    assert r['image_path'].endswith('second.jpg') and len(r['images'])==1
    assert db.query(models.ProductImage).filter_by(image_path=r['image_path']).one().is_primary
    r=main.delete_product_photo('SAMPLE_A','second.jpg',db=db)
    assert r['image_path'] is None and r['images']==[] and r['total_stock']==7
    assert not db.query(models.Product).filter_by(sku='SAMPLE_A').one().is_deleted
    assert (tmp_path/'other.jpg').exists()

def test_wrong_product_and_path_rejected(db,tmp_path,monkeypatch):
    main=photo_fixture(db,tmp_path,monkeypatch)
    for name in ['other.jpg','../other.jpg','missing.jpg']:
        with pytest.raises(HTTPException) as e: main.delete_product_photo('SAMPLE_A',name,db=db)
        assert e.value.status_code==404
        db.rollback()
    assert db.query(models.ProductImage).count()==4 and (tmp_path/'other.jpg').exists()

def test_legacy_primary_without_reference(db,tmp_path,monkeypatch):
    import main
    monkeypatch.setattr(main,'seller_image_dir',lambda:tmp_path)
    db.add(models.Product(sku='LEGACY',name='Legacy',image_path='/static/product_images/legacy.jpg'));db.commit()
    (tmp_path/'legacy.jpg').write_bytes(b'photo')
    r=main.delete_product_photo('LEGACY','legacy.jpg',db=db)
    assert r['image_path'] is None and not r['images'] and not (tmp_path/'legacy.jpg').exists()

def test_bulk_markup_is_excel_only():
    frontend=Path(__file__).resolve().parents[1]/'frontend'
    html=(frontend/'index.html').read_text(); js=(frontend/'app.js').read_text()
    assert 'bulk-product-photos' not in html+js
    assert 'detail-photo-remove' in js and 'Remove product photo?' in js
    assert 'renderDetailPhotos' in js and 'product-qr-download' in html

@pytest.mark.asyncio
async def test_add_photos_to_empty_gallery(db,tmp_path,monkeypatch):
    from PIL import Image
    from fastapi import UploadFile, BackgroundTasks
    import main
    monkeypatch.setattr(main,'seller_image_dir',lambda:tmp_path)
    monkeypatch.setattr(main,'tenant_id',lambda:1)
    db.add(models.Product(sku='NEW_A',name='New Design')); db.commit()
    b=BytesIO(); Image.new('RGB',(128,128),'green').save(b,format='PNG'); b.seek(0)
    out=await main.upload_product_reference_images('NEW_A',BackgroundTasks(),[UploadFile(file=b,filename='front.png')],db=db)
    assert out['saved']==1
    p=main.get_product('NEW_A',db=db)
    assert p['image_path'] and p['image_path']==p['images'][0]
    assert db.query(models.ProductImage).one().is_primary
