"""SQLite + HTTP mock of Qdrant; real small image fixtures, no live service."""
import json, sys
from pathlib import Path
import httpx
import numpy as np
import pytest
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))
import models, business_models
from database import Base, tenant_scope
from services import visual_ann_service as ann, visual_search_service as visual

class FakeServer:
    def __init__(self):
        self.points={}; self.created=False; self.indexes={}; self.down=False; self.cross=False; self.on_upsert=None
    def handler(self,r):
        assert r.headers['api-key']=='test-key'
        p=r.url.path; body=json.loads(r.content) if r.content else {}
        if self.down: return httpx.Response(503,json={})
        def reply(result): return httpx.Response(200,json={'status':'ok','result':result})
        if p.endswith('/exists'): return reply({'exists':self.created})
        if '/points' not in p and not p.endswith('/index'):
            if r.method=='PUT':
                assert body['hnsw_config']['m']==16
                self.created=True;return reply(True)
            return reply({'config':{'params':{'vectors':{'size':768,'distance':'Cosine'}}},'payload_schema':self.indexes})
        if p.endswith('/index'):
            self.indexes[body['field_name']]={};return reply(True)
        if r.method=='PUT' and p.endswith('/points'):
            self.points.update({x['id']:x for x in body['points']})
            if self.on_upsert: self.on_upsert()
            return reply({'status':'completed'})
        conditions={c['key']:c['match']['value'] for c in body['filter']['must']}
        assert set(conditions)=={'seller','generation'}
        matches=[x for x in self.points.values() if all(x['payload'][k]==v for k,v in conditions.items())]
        if p.endswith('/count'):return reply({'count':len(matches)})
        if p.endswith('/delete'):
            for x in matches:self.points.pop(x['id'])
            return reply({'status':'completed'})
        if p.endswith('/query'):
            assert body['params']['hnsw_ef']==128 and body['with_vector'] is False
            if self.cross: matches=list(self.points.values())
            return reply({'points':sorted(matches,key=lambda x:-np.dot(x['vector'],body['query']))[:body['limit']]})
        raise AssertionError(p)

@pytest.fixture
def setup(tmp_path,monkeypatch):
    import database
    monkeypatch.setattr(database,'SELLER_DB_ROOT',tmp_path)
    monkeypatch.setenv('VISUAL_QDRANT_URL','https://qdrant.example')
    monkeypatch.setenv('VISUAL_QDRANT_API_KEY','test-key')
    monkeypatch.setenv('VISUAL_QDRANT_NAMESPACE','test-env')
    monkeypatch.delenv('VISUAL_AI_URL',raising=False)
    fake=FakeServer(); actual=httpx.Client
    monkeypatch.setattr(ann.httpx,'Client',lambda **kw: actual(**kw,transport=httpx.MockTransport(fake.handler)))
    def seller(account, color):
        folder=tmp_path/f'seller_{account}';folder.mkdir();root=folder/'product_images';root.mkdir()
        engine=create_engine(f'sqlite:///{folder}/inventory.db');Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(models.Product(id=1,sku=f'PRIVATE-{account}',name='Private design'));db.flush()
            db.add(models.ProductImage(id=1,product_id=1,image_path='/static/product_images/ref.png'))
            Image.new('RGB',(128,128),color).save(root/'ref.png');db.commit()
        return engine,root
    return fake,seller

def test_real_photo_build_search_and_isolation(setup):
    fake,seller=setup; a,ra=seller(1,'red');b,rb=seller(2,'blue')
    with tenant_scope(1),Session(a) as db:
        assert ann.status(db)['state']=='pending'
        assert ann.build(db,ra)=='indexed'
        assert ann.status(db)=={'state':'indexed','indexed_photos':1}
        assert ann.build(db,ra)=='already_indexed'
        result=visual.search(db,(ra/'ref.png').read_bytes(),ra,5)
        assert result.status=='matched' and result.matches[0].product_id==1
    with tenant_scope(2),Session(b) as db:
        assert ann.build(db,rb)=='indexed'
        assert visual.search(db,(rb/'ref.png').read_bytes(),rb,5).status=='matched'
        fake.cross=True
        assert ann.search(db,visual.visual_match_service.local_visual_embedding((rb/'ref.png').read_bytes()),rb,5) is None
        fake.cross=False
    with tenant_scope(2),Session(a) as db:
        assert ann.status(db)['state']=='unavailable'
        assert ann.search(db,np.ones(768),ra,5) is None
    assert len(fake.points)==2

def test_delete_restore_invalidation_and_outage(setup):
    fake,seller=setup; a,ra=seller(1,'red')
    with tenant_scope(1),Session(a) as db:
        ann.build(db,ra);content=(ra/'ref.png').read_bytes()
        fake.down=True
        assert visual.search(db,content,ra,5).status=='matched'
        fake.down=False
        db.query(models.Product).filter_by(id=1).update({'is_deleted':True});db.commit()
        assert ann.status(db)['state']=='pending'
        assert visual.search(db,content,ra,5).status=='no_match'
        assert ann.build(db,ra)=='indexed' and not fake.points
        db.query(models.Product).filter_by(id=1).update({'is_deleted':False});db.commit()
        assert ann.build(db,ra)=='indexed' and len(fake.points)==1
        fake.points.clear()
        assert ann.build(db,ra)=='indexed' and len(fake.points)==1
        db.query(models.ProductImage).filter_by(id=1).delete();db.commit()
        assert ann.status(db)['state']=='pending'
        assert ann.build(db,ra)=='indexed' and not fake.points

def test_mid_build_change_cannot_publish(setup):
    fake,seller=setup;a,ra=seller(1,'red')
    def change():
        with Session(a) as db:
            db.query(models.Product).filter_by(id=1).update({'is_deleted':True});db.commit()
    fake.on_upsert=change
    with tenant_scope(1),Session(a) as db:
        with pytest.raises(RuntimeError,match='Catalog changed'):
            ann.build(db,ra)
        assert ann.status(db)['state']=='pending' and not fake.points

def test_disabled_and_stale_file(setup,monkeypatch):
    fake,seller=setup;a,ra=seller(1,'red')
    with tenant_scope(1),Session(a) as db:
        ann.build(db,ra)
        query=visual.visual_match_service.local_visual_embedding((ra/'ref.png').read_bytes())
        Image.new('RGB',(128,128),'blue').save(ra/'ref.png')
        assert ann.search(db,query,ra,5) is None
        monkeypatch.delenv('VISUAL_QDRANT_URL')
        assert ann.status(db)['state']=='disabled' and ann.search(db,query,ra,5) is None
