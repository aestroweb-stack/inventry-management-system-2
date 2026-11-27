"""Private service contract and encoder-space isolation; no actual model download."""
from io import BytesIO
from unittest.mock import patch
import numpy as np
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
from test_visual_ann_r21 import setup
from database import tenant_scope
from models import LocalVisualVector
from services import visual_search_service as visual, visual_ann_service as ann
import visual_model_service as service


def test_service_lifespan_and_auth(monkeypatch):
    monkeypatch.setenv('VISUAL_AI_API_KEY','test-only')
    vector = tuple([1.0]+[0.0]*767)
    monkeypatch.setattr(service.visual_match_service,'image_embedding',lambda _: vector)
    with TestClient(service.app) as client:
        assert client.get('/health').json()['dimensions']==768
        assert client.get('/health',headers={'Authorization':'Bearer wrong'}).status_code==401
        assert client.get('/health',headers={'Authorization':'Bearer test-only'}).status_code==200
        assert client.get('/openapi.json').status_code==404
        assert client.post('/v1/embed',files={'file':('x.png',b'fake')}).status_code==401
        assert client.post('/v1/embed',headers={'Authorization':'test-only'},files={'file':('x.png',b'fake')}).status_code==401
        response=client.post('/v1/embed',headers={'Authorization':'Bearer test-only'},files={'file':('x.png',b'fake')})
        assert response.status_code==200 and response.json()['embedding']==list(vector)
    monkeypatch.delenv('VISUAL_AI_API_KEY')
    with pytest.raises(RuntimeError,match='VISUAL_AI_API_KEY'):
        with TestClient(service.app): pass


def test_model_version_and_norm(monkeypatch):
    monkeypatch.setenv('VISUAL_AI_URL','http://visual-model.railway.internal:8080')
    monkeypatch.setenv('VISUAL_AI_API_KEY','test-only')
    import httpx
    for payload in [{'model':'wrong','embedding':[1.0]*768},{'model':visual.MODEL_NAME,'embedding':[0.0]*768},{'model':visual.MODEL_NAME,'embedding':[float('nan')]*768}]:
        response=type('Response',(),{'json':lambda self:payload,'raise_for_status':lambda self:None})()
        with patch('httpx.post',return_value=response), pytest.raises(visual.visual_match_service.ImageModelUnavailable):
            visual._image_embedding(b'x')


def test_semantic_reindex_and_outage_keeps_cache(setup,monkeypatch):
    fake,seller=setup; a,root=seller(1,'red')
    content=(root/'ref.png').read_bytes()
    with tenant_scope(1),Session(a) as db:
        assert ann.build(db,root)=='indexed'
        old=ann._identity(db)[2]
        monkeypatch.setenv('VISUAL_AI_URL','http://visual-model.railway.internal:8080')
        monkeypatch.setattr(visual,'_image_embedding',lambda _:np.eye(1,768,2)[0])
        assert old!=ann._identity(db)[2] and ann.status(db)['state']=='pending'
        assert ann.build(db,root)=='indexed'
        result=visual.search(db,content,root,5)
        assert result.retrieval_engine=='qdrant_ann' and result.matching_mode.startswith('SigLIP')
        row=db.query(LocalVisualVector).one(); previous=row.embedding
        assert row.model_name==visual.MODEL_NAME
        def unavailable(_): raise visual.visual_match_service.ImageModelUnavailable('offline')
        monkeypatch.setattr(visual,'_image_embedding',unavailable)
        result=visual.search(db,content,root,5)
        assert result.retrieval_engine=='exact_color_fallback' and result.status=='matched'
        db.expire_all(); row=db.query(LocalVisualVector).one()
        assert row.model_name==visual.MODEL_NAME and row.embedding==previous
        assert ann.status(db)['state']=='indexed'
