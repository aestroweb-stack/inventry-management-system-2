from test_purchase_returns_r23 import setup, purchase, stock
from test_purchase_returns_manual_r25 import line, manual, save, post
import models


def test_manual_existing_sku_is_never_deducted(setup):
    c,S=setup;purchase(c)
    x=dict(sku='KURTA',size='M',name='Manual product',quantity=25,unit_cost=700,record_only=True)
    d=save(c,manual([x]));assert stock(S)=={'M':10,'L':10}
    r=post(c,d);assert r.status_code==201,r.text
    assert r.json()['lines'][0]['record_only'] and stock(S)=={'M':10,'L':10}
    assert c.get('/api/purchase-returns/periods?report_date=2026-11-27').json()['day']['record_only_quantity']==25
    assert c.post('/api/purchase-returns/drafts',json=manual([{**x,'variant_id':line(c)['variant_id']}],'BAD')).status_code==409


def test_autocomplete_draft_posted_photos_and_export(setup):
    c,S=setup;purchase(c)
    with S() as db:
        p=db.query(models.Product).one();p.image_path='/static/product_images/kurta.jpg';db.add(models.ProductImage(product_id=p.id,image_path='/static/product_images/second.jpg',is_primary=False));db.commit()
    search=c.get('/api/purchase-returns/catalog-search?q=kur').json()['items'];assert search[0]['sku']=='KURTA' and search[0]['image_path'].endswith('kurta.jpg')
    assert c.get('/api/purchase-returns/catalog-search?q=%25').json()['items']==[]
    d=save(c,manual([line(c)]));detail=c.get(f"/api/purchase-returns/drafts/{d['id']}/view").json();assert len(detail['lines'][0]['images'])==2
    assert c.get(f"/api/purchase-returns/drafts/{d['id']}/export").status_code==200
    r=post(c,d);assert r.status_code==201,r.text
    posted=c.get(f"/api/purchase-returns/{r.json()['id']}").json();assert len(posted['lines'][0]['images'])==2 and posted['lines'][0]['balance_after']==7
    assert c.get('/api/purchase-returns/periods?report_date=2026-11-27').json()['day']['adjusted_quantity']==3
    with S() as db:
        p=db.query(models.Product).one();p.sku='RENAMED';db.commit()
    assert c.get(f"/api/purchase-returns/{r.json()['id']}").json()['lines'][0]['images']==[]
