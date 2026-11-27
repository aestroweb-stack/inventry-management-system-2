"""R28 manual rows without a catalog SKU, and inventory safety."""
from test_purchase_returns_r23 import setup, purchase, stock
from test_purchase_returns_manual_r25 import line, manual, save, post
import models


def record(name='Manual product', sku='', size='M', quantity=2):
    return dict(sku=sku, name=name, size=size, quantity=quantity, unit_cost=50, record_only=True)


def test_manual_without_sku_save_reopen_post_export(setup):
    c,S=setup; purchase(c)
    d=save(c,manual([record('Manual kurta'),record('Manual shirt')]))
    assert stock(S)=={'M':10,'L':10}
    assert c.get(f"/api/purchase-returns/drafts/{d['id']}/view").json()['lines'][0]['sku']==''
    assert c.get(f"/api/purchase-returns/drafts/{d['id']}/export").status_code==200
    r=post(c,d); assert r.status_code==201,r.text
    assert r.json()['amount']==200 and all(x['record_only'] for x in r.json()['lines'])
    assert stock(S)=={'M':10,'L':10}
    assert c.get(f"/api/purchase-returns/{r.json()['id']}/export").status_code==200
    assert post(c,d).status_code==404


def test_manual_and_linked_same_sku_size(setup):
    c,S=setup;purchase(c)
    d=save(c,manual([record(sku='KURTA',quantity=25),line(c)]))
    r=post(c,d); assert r.status_code==201,r.text
    assert stock(S)=={'M':7,'L':10}
    assert r.json()['lines'][0]['balance_after'] is None
    assert r.json()['lines'][1]['balance_after']==7
    with S() as db:assert db.query(models.InventoryTransaction).filter_by(created_by='supplier_return').count()==1


def test_blank_sku_only_allowed_for_named_manual_rows(setup):
    c,S=setup;purchase(c)
    for x in [record(name=''),{**record(),'record_only':False},record(size=''),{**record(),'variant_id':line(c)['variant_id']}]:
        assert c.post('/api/purchase-returns/drafts',json=manual([x])).status_code in (409,422)
    assert c.post('/api/purchase-returns/drafts',json=manual([record(),record()])).status_code==422
    assert stock(S)=={'M':10,'L':10}
