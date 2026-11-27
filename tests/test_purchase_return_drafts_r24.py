"""R24 save/post, period reporting and optimistic draft revision checks."""
from test_purchase_returns_r23 import setup, purchase, payload, stock
from concurrent.futures import ThreadPoolExecutor


def save(c,d,number='RI-D',qty=3):
    r=c.post('/api/purchase-returns/drafts',json=payload(d,number,qty));assert r.status_code==201,r.text
    return r.json()


def test_save_edit_post_and_period_totals(setup):
    c,S=setup;d,_=purchase(c);draft=save(c,d)
    assert stock(S)=={'M':10,'L':10}
    assert c.get('/api/purchase-returns').json()['total']==0
    periods=c.get('/api/purchase-returns/periods?report_date=2026-11-27').json()
    assert periods['day']['pending_quantity']==3 and periods['day']['quantity']==0
    assert periods['month']['draft_count']==1 and periods['all']['pending_amount']==375
    url=f"/api/purchase-returns/drafts/{draft['id']}"
    assert c.get(url).json()['payload']==draft['payload']
    new=c.put(url,json={**payload(d,qty=4,number='RI-D'),'revision':draft['revision']});assert new.status_code==200,new.text
    new=new.json();assert new['revision']!=draft['revision']
    assert c.post(url+'/post',json={'revision':draft['revision']}).status_code==409
    assert stock(S)=={'M':10,'L':10}
    r=c.post(url+'/post',json={'revision':new['revision']});assert r.status_code==201,r.text
    assert r.json()['quantity']==4 and stock(S)=={'M':6,'L':10}
    assert c.get(url).status_code==404
    assert c.post(url+'/post',json={'revision':new['revision']}).status_code==404
    periods=c.get('/api/purchase-returns/periods?report_date=2026-11-28').json()
    assert periods['day']['count']==0 and periods['month']['amount']==500 and periods['all']['pending_quantity']==0


def test_duplicate_drafts_and_delete_no_stock_change(setup):
    c,S=setup;d,_=purchase(c);draft=save(c,d)
    assert c.post('/api/purchase-returns/drafts',json=payload(d,'RI-D')).status_code==409
    assert c.post('/api/purchase-returns',json=payload(d,'RI-D')).status_code==409
    url=f"/api/purchase-returns/drafts/{draft['id']}"
    assert c.delete(url+'?revision='+'a'*32).status_code==409
    assert c.delete(url+'?revision='+draft['revision']).status_code==200
    assert stock(S)=={'M':10,'L':10}
    assert c.get('/api/purchase-returns/drafts').json()['total']==0


def test_draft_stock_revalidation_and_preservation(setup):
    c,S=setup;d,_=purchase(c);draft=save(c,d,qty=8)
    assert c.post('/api/purchase-returns',json=payload(d,'RI-OTHER',5)).status_code==201
    url=f"/api/purchase-returns/drafts/{draft['id']}"
    assert c.post(url+'/post',json={'revision':draft['revision']}).status_code==409
    assert c.get(url).status_code==200 and stock(S)=={'M':5,'L':10}


def test_concurrent_post_same_draft(setup):
    c,S=setup;d,_=purchase(c);draft=save(c,d)
    url=f"/api/purchase-returns/drafts/{draft['id']}/post"
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes=list(pool.map(lambda _:c.post(url,json={'revision':draft['revision']}).status_code,range(2)))
    assert sorted(codes)==[201,404] and stock(S)=={'M':7,'L':10}


def test_draft_date_filter_pagination_and_report_separate(setup):
    c,S=setup;d,_=purchase(c)
    for i in range(8):save(c,d,f'RI-{i}',1)
    assert len(c.get('/api/purchase-returns/drafts').json()['items'])==6
    assert len(c.get('/api/purchase-returns/drafts?offset=6').json()['items'])==2
    assert c.get('/api/purchase-returns/drafts?date_filter=2026-11-28').json()['total']==0
    assert c.get('/api/purchase-returns/periods?report_date=2026-11-27').json()['all']['draft_count']==8
    assert stock(S)=={'M':10,'L':10}


def test_import_preview_and_invalid_quantity(setup):
    c,S=setup;d,_=purchase(c);url=f"/api/purchase-returns/import-preview/{d['id']}"
    def preview(text):return c.post(url,files={'file':('return.csv',text.encode(),'text/csv')})
    assert c.get('/api/purchase-returns/import-template').status_code==200
    r=preview('Master SKU,Size,Return Qty\nKURTA,M,2\nKURTA,M,3\nKURTA,L,1\n');assert r.status_code==200,r.text
    assert r.json()['lines']==[{'line_id':d['lines'][0]['id'],'quantity':5},{'line_id':d['lines'][1]['id'],'quantity':1}]
    for text,code in [('KURTA,M,1.5',422),('OTHER,M,1',422),('KURTA,M,11',409)]:
        assert preview('Master SKU,Size,Return Qty\n'+text+'\n').status_code==code
    assert stock(S)=={'M':10,'L':10}


def test_attachments_follow_atomic_post_and_stale_revision(setup):
    from PIL import Image
    from io import BytesIO
    c,S=setup;d,_=purchase(c);draft=save(c,d)
    url=f"/api/purchase-returns/drafts/{draft['id']}"
    b=BytesIO();Image.new('RGB',(12,12),'red').save(b,format='PNG');raw=b.getvalue()
    r=c.post(url+'/documents',data={'revision':draft['revision']},files={'files':('photo.png',raw,'image/png')});assert r.status_code==200,r.text
    doc=r.json();ident=doc['items'][0]['id'];assert doc['revision']!=draft['revision']
    assert c.get(url+f'/documents/{ident}').content==raw
    assert c.get(url.replace(str(draft['id']),str(draft['id']+99))+f'/documents/{ident}').status_code==404
    assert c.post(url+'/post',json={'revision':draft['revision']}).status_code==409
    r=c.post(url+'/post',json={'revision':doc['revision']});assert r.status_code==201,r.text
    posted=f"/api/purchase-returns/{r.json()['id']}/documents"
    assert c.get(posted).json()['items'][0]['filename']=='photo.png'
    assert c.get(posted+f'/{ident}').content==raw
    assert c.get(url+f'/documents/{ident}').status_code==404
    assert stock(S)=={'M':7,'L':10}


def test_document_remove_and_draft_delete(setup):
    c,S=setup;d,_=purchase(c);draft=save(c,d);url=f"/api/purchase-returns/drafts/{draft['id']}"
    bad=c.post(url+'/documents',data={'revision':draft['revision']},files={'files':('bad.png',b'<html>', 'image/png')});assert bad.status_code==422
    r=c.post(url+'/documents',data={'revision':draft['revision']},files={'files':('receipt.pdf',b'%PDF-1.4\n%%EOF','application/pdf')});assert r.status_code==200,r.text
    data=r.json();ident=data['items'][0]['id'];r=c.delete(url+f'/documents/{ident}?revision='+data['revision']);assert r.status_code==200 and not r.json()['items']
    assert c.delete(url+'?revision='+r.json()['revision']).status_code==200
    assert stock(S)=={'M':10,'L':10}


def test_draft_source_locked_and_export(setup):
    from openpyxl import load_workbook
    from io import BytesIO
    c,S=setup;d,_=purchase(c);other,_=purchase(c,'PI-2',sku='OTHER');draft=save(c,d)
    url=f"/api/purchase-returns/drafts/{draft['id']}"
    assert c.put(url,json={**payload(other,number='RI-D'),'revision':draft['revision']}).status_code==409
    r=c.get('/api/purchase-returns/drafts/export');assert r.status_code==200,r.text
    wb=load_workbook(BytesIO(r.content));assert wb.active['E2'].value==3 and wb.active['G2'].value=='DRAFT'
    assert stock(S)=={'M':10,'L':10}
