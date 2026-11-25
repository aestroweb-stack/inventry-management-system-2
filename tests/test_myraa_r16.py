"""Staged photo saves: metadata and gallery are committed together."""
import json
from io import BytesIO
from pathlib import Path
import sys
import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from test_myraa_r13 import db, photo_fixture


def upload(valid=True):
    data = BytesIO()
    if valid:
        Image.new('RGB', (128,128), 'green').save(data, format='PNG')
    else:
        data.write(b'not a photo')
    data.seek(0)
    return UploadFile(file=data,filename='new.png')


def save(main, db, baseline, entries, files=(), **meta):
    return main.save_product_editor('SAMPLE_A',BackgroundTasks(),json.dumps(meta),json.dumps(baseline),json.dumps(entries),list(files),db)


def test_commit_rename_remove_add_and_stock(db,tmp_path,monkeypatch):
    main=photo_fixture(db,tmp_path,monkeypatch)
    monkeypatch.setattr(main,'tenant_id',lambda:1)
    old=main.get_product('SAMPLE_A',db=db)['images']
    result=save(main,db,old,[0,old[1]], [upload()],name='Updated design',sku='SAMPLE_NEW',category='Kurta')
    assert result['sku']=='SAMPLE_NEW' and result['name']=='Updated design' and result['total_stock']==7
    assert result['variants'][0]['variant_code']=='SAMPLE_NEW_S'
    assert len(result['images'])==2 and result['image_path']==result['images'][0] and result['images'][1]==old[1]
    assert (tmp_path/Path(result['image_path']).name).exists()
    assert not (tmp_path/'first.jpg').exists() and not (tmp_path/'third.jpg').exists() and (tmp_path/'other.jpg').exists()


def test_remove_all_photos_only(db,tmp_path,monkeypatch):
    main=photo_fixture(db,tmp_path,monkeypatch)
    old=main.get_product('SAMPLE_A',db=db)['images']
    result=save(main,db,old,[])
    assert not result['images'] and result['image_path'] is None and result['total_stock']==7
    assert result['name']=='Design A'


@pytest.mark.parametrize('case,status',[('bad_file',400),('duplicate',409),('foreign',422),('stale',409),('duplicate_upload',422)])
def test_failed_save_leaves_catalog_untouched(db,tmp_path,monkeypatch,case,status):
    main=photo_fixture(db,tmp_path,monkeypatch)
    old=main.get_product('SAMPLE_A',db=db)['images']
    args={'baseline':old,'entries':[0],'files':[upload()], 'name':'Changed design'}
    if case=='bad_file':args['files']=[upload(False)]
    elif case=='duplicate':args['name']='Design B'
    elif case=='foreign':args.update(entries=['/static/product_images/other.jpg'],files=[])
    elif case=='stale':args['baseline']=old[:-1]
    else:args['entries']=[0,0]
    before=set(tmp_path.iterdir())
    with pytest.raises(HTTPException) as error:save(main,db,**args)
    assert error.value.status_code==status
    db.rollback()
    result=main.get_product('SAMPLE_A',db=db)
    assert result['images']==old and result['name']=='Design A' and result['total_stock']==7
    assert set(tmp_path.iterdir())==before


def test_legacy_primary(db,tmp_path,monkeypatch):
    main=photo_fixture(db,tmp_path,monkeypatch)
    from models import ProductImage
    db.query(ProductImage).filter_by(image_path='/static/product_images/first.jpg').delete();db.commit()
    old=main.get_product('SAMPLE_A',db=db)['images']
    result=save(main,db,old,[old[0]])
    assert result['images']==[old[0]]


def test_multipart_remove_only_route(db,tmp_path,monkeypatch):
    """No uploaded files is a normal browser multipart submission."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from database import get_db
    main=photo_fixture(db,tmp_path,monkeypatch)
    old=main.get_product('SAMPLE_A',db=db)['images']
    local=FastAPI()
    local.post('/api/products/{sku}/editor-save')(main.save_product_editor)
    local.dependency_overrides[get_db]=lambda:db
    with TestClient(local) as client:
        r=client.post('/api/products/SAMPLE_A/editor-save',files={'metadata':(None,json.dumps({'name':'New design'})),'expected':(None,json.dumps(old)),'gallery':(None,json.dumps([old[0]]))})
    assert r.status_code==200,r.text
    assert r.json()['images']==[old[0]] and r.json()['name']=='New design'
