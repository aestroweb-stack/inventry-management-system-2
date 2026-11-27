from io import BytesIO
from PIL import Image
from test_purchase_returns_r23 import setup,purchase,stock
from test_purchase_returns_manual_r25 import line,manual,save,post
from test_purchase_returns_r28 import record
import models

def test_manual_user_example_and_attachment_reopen_delete(setup):
    c,S=setup
    purchase(c)
    payload=dict(party_name='VIENNA',return_no='7000',return_date='2026-11-27',reason='GGGG',lines=[record(name='GULIKA_PINK',sku='TWINS_V_GULIKA_PINK',size='S',quantity=7)])
    payload['lines'][0]['unit_cost']=450
    d=save(c,payload);assert d['amount']==3150
    img=BytesIO();Image.new('RGB',(12,12),'blue').save(img,'PNG');image=img.getvalue()
    r=c.post(f'/api/purchase-returns/drafts/{d["id"]}/documents',data={'revision':d['revision']},files={'files':('invoice.png',image,'image/png')});assert r.status_code==200,r.text
    doc=r.json()['items'][0];d['revision']=r.json()['revision']
    p=post(c,d);assert p.status_code==201,p.text
    b=p.json();assert b['can_correct'] and stock(S)=={'M':10,'L':10}
    assert c.get(f'/api/purchase-returns/{b["id"]}/documents/{doc["id"]}').content==image
    r=c.post(f'/api/purchase-returns/{b["id"]}/reopen',json={'return_no':'7000','revision':b['correction_revision']});assert r.status_code==200,r.text
    d=r.json();assert d['amount']==3150 and c.get('/api/purchase-returns').json()['total']==0
    assert c.get(f'/api/purchase-returns/drafts/{d["id"]}/documents/{doc["id"]}').content==image
    p=post(c,d);assert p.status_code==201,p.text
    old=b['correction_revision'];b=p.json();assert b['correction_revision']!=old;assert c.delete(f'/api/purchase-returns/{b["id"]}',params={'return_no':'7000','revision':old}).status_code==409;r=c.delete(f'/api/purchase-returns/{b["id"]}',params={'return_no':'7000','revision':b['correction_revision']});assert r.status_code==200,r.text
    assert c.get('/api/purchase-returns').json()['total']==0 and stock(S)=={'M':10,'L':10}
    with S() as db:assert db.query(models.InventoryTransaction).filter_by(created_by='supplier_return').count()==0

def test_adjusted_return_locked(setup):
    c,S=setup;purchase(c)
    b=post(c,save(c,manual([line(c),record(name='Outside catalog')]))).json()
    assert not b['can_correct'];before=stock(S)
    assert c.post(f'/api/purchase-returns/{b["id"]}/reopen',json={'return_no':b['return_no'],'revision':b['correction_revision']}).status_code==409
    assert c.delete(f'/api/purchase-returns/{b["id"]}',params={'return_no':b['return_no'],'revision':b['correction_revision']}).status_code==409
    assert stock(S)==before
