"""Verify uncapped catalogs, cached vectors and gallery saves with synthetic data."""
from time import perf_counter
import json
import numpy as np
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from fastapi import BackgroundTasks
from test_myraa_r13 import db, photo_fixture
from test_myraa_r16 import upload
from models import Product, ProductImage, LocalVisualVector
from database import Base
from services import visual_search_service as search


def vec(n):
    v=np.zeros(search.VECTOR_DIMENSIONS,dtype='<f4');v[n]=1;return v


def test_gallery_over_eight(db,tmp_path,monkeypatch):
    main=photo_fixture(db,tmp_path,monkeypatch)
    monkeypatch.setattr(main,'tenant_id',lambda:1)
    original=main.get_product('SAMPLE_A',db=db)['images']
    result=main.save_product_editor('SAMPLE_A',BackgroundTasks(),'{}',json.dumps(original),json.dumps(original+list(range(8))),[upload() for _ in range(8)],db)
    assert len(result['images'])==11 and result['total_stock']==7
    result=main.save_product_editor('SAMPLE_A',BackgroundTasks(),'{}',json.dumps(result['images']),json.dumps(result['images']),[],db)
    assert len(result['images'])==11 and result['total_stock']==7


def populate(db, root, count):
    root.mkdir(exist_ok=True)
    db.bulk_insert_mappings(Product,[dict(id=i,sku=f'P{i}',name=f'P{i}',is_deleted=False) for i in range(1,count+1)])
    refs=[]; vectors=[]
    for i in range(1,count+1):
        f=root/f'{i}.png';f.write_bytes(b'fixture'); stat=f.stat(); path=f'/static/product_images/{f.name}'
        refs.append(dict(id=i,product_id=i,image_path=path,is_primary=True))
        vectors.append(dict(image_id=i,model_name='local-color-layout:v1',image_path=path,mtime_ns=stat.st_mtime_ns,file_size=stat.st_size,embedding=vec(0 if i==count else 1).tobytes()))
    db.bulk_insert_mappings(ProductImage,refs);db.bulk_insert_mappings(LocalVisualVector,vectors);db.commit()


def test_above_old_maximum_and_seller_isolation(db,tmp_path,monkeypatch):
    monkeypatch.delenv('VISUAL_AI_URL',raising=False)
    root=tmp_path/'a';populate(db,root,3101)
    monkeypatch.setattr(search.visual_match_service,'local_visual_embedding',lambda _: (_ for _ in ()).throw(AssertionError('cached vector unexpectedly re-encoded')))
    start=perf_counter(); hits=search._development_search(db,vec(0),root,5); elapsed=perf_counter()-start
    assert hits[0].product_id==3101 and hits[0].similarity==1
    assert len({h.product_id for h in hits})==5
    print(f'Warm 3101-reference exact retrieval: {elapsed:.3f}s (synthetic precomputed vectors, excludes query inference)')
    other=create_engine(f'sqlite:///{tmp_path}/other.sqlite');Base.metadata.create_all(other)
    with Session(other) as b:
        populate(b,tmp_path/'b',2)
        assert search._development_search(b,vec(0),tmp_path/'b',5)[0].product_id==2
    other.dispose()
    db.query(Product).filter_by(id=3101).update({'is_deleted':True});db.commit()
    assert all(x.product_id!=3101 for x in search._development_search(db,vec(0),root,5))


def test_cold_rebuild_then_cache_and_replace(db,tmp_path,monkeypatch):
    monkeypatch.delenv('VISUAL_AI_URL',raising=False)
    monkeypatch.setattr(search,'LOCAL_SEARCH_BATCH',2)
    root=tmp_path/'refs';root.mkdir()
    db.add_all([Product(id=1,sku='one',name='one'),Product(id=2,sku='two',name='two')]);db.flush()
    for i in range(1,7):
        f=root/f'{i}.png';f.write_bytes(b'good' if i==5 else b'bad')
        db.add(ProductImage(id=i,product_id=1 if i<=5 else 2,image_path=f'/static/product_images/{f.name}'))
    db.commit();calls=[]
    def embed(content):
        calls.append(content);return vec(0 if content==b'good' else 1)
    monkeypatch.setattr(search.visual_match_service,'local_visual_embedding',embed)
    hits=search._development_search(db,vec(0),root,5)
    assert [h.product_id for h in hits]==[1,2] and hits[0].reference_image_path.endswith('5.png')
    assert len(calls)==6 and db.query(LocalVisualVector).count()==6
    db.expire_all();calls.clear()
    assert search._development_search(db,vec(0),root,5)==hits and calls==[]
    (root/'5.png').write_bytes(b'bad');calls.clear()
    assert search._development_search(db,vec(0),root,5)[0].similarity==0 and calls==[b'bad']
    assert search._decide_local(search._development_search(db,vec(0),root,5)).status=='no_match'
