"""Independent supplier returns, migration, and stock safety regression."""
from test_purchase_returns_r23 import setup, purchase, stock, payload
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import models, business_models as bm
from database import Base, upgrade_return_schema
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from openpyxl import load_workbook


def line(c,size='M',quantity=3,rate=111.25):
    r=c.get('/api/purchase-returns/catalog?sku=kurta');assert r.status_code==200,r.text
    v=next(x for x in r.json()['items'] if x['size']==size)
    return dict(sku=v['sku'],size=size,variant_id=v['variant_id'],quantity=quantity,unit_cost=rate)


def manual(lines,number='RI-M'):
    return dict(party_name='Supplier outside purchase records',return_no=number,return_date='2026-11-27',lines=lines)


def save(c,p):
    r=c.post('/api/purchase-returns/drafts',json=p);assert r.status_code==201,r.text
    return r.json()


def post(c,d):
    return c.post(f"/api/purchase-returns/drafts/{d['id']}/post",json={'revision':d['revision']})


def test_independent_without_any_purchase_invoice(setup):
    c,S=setup
    from datetime import datetime, timezone
    with S() as db:
        p=models.Product(sku='KURTA',name='Manual stock');db.add(p);db.flush()
        for size in ('M','L'):db.add(models.Variant(product_id=p.id,size=size,variant_code='KURTA-'+size,current_stock=10,last_counted_at=datetime.now(timezone.utc)))
        db.commit()
    assert c.get('/api/purchase-returns/sources').json()['total']==0
    d=save(c,manual([line(c,quantity=10),line(c,'L',quantity=2,rate=100)]))
    assert d['purchase_id'] is None and d['amount']==1312.5 and stock(S)=={'M':10,'L':10}
    r=post(c,d);assert r.status_code==201,r.text
    data=r.json();assert data['quantity']==12 and data['amount']==1312.5 and data['lines'][0]['balance_after']==0
    assert stock(S)=={'M':0,'L':8}
    with S() as db:
        assert db.query(bm.PurchaseChallan).count()==0
        assert db.query(models.InventoryTransaction).filter_by(created_by='supplier_return').count()==2
        assert db.query(models.Alert).filter_by(resolved=False).count()>=1
    assert post(c,d).status_code==404
    wb=load_workbook(BytesIO(c.get(f"/api/purchase-returns/{data['id']}/export").content));assert wb.active['J2'].value==1112.5


def test_unmatched_explicit_record_only_and_no_fake_product(setup):
    c,S=setup;purchase(c)
    x=dict(sku='NOT-IN-CATALOG',size='XL',name='Outside catalog',quantity=2,unit_cost=50)
    assert c.post('/api/purchase-returns/drafts',json=manual([x])).status_code==409
    d=save(c,manual([{**x,'record_only':True},line(c)]));r=post(c,d);assert r.status_code==201,r.text
    assert r.json()['lines'][0]['record_only'] and r.json()['lines'][0]['balance_after'] is None
    assert stock(S)=={'M':7,'L':10}
    with S() as db:assert db.query(models.Product).count()==1


def test_reject_wrong_identity_duplicate_and_overstock(setup):
    c,S=setup;purchase(c);x=line(c)
    for rows in ([{**x,'quantity':11}],[{**x,'variant_id':999}],[{**x,'record_only':True}],[{**x,'size':'XXL'}]):
        assert c.post('/api/purchase-returns/drafts',json=manual(rows)).status_code==409
    assert c.post('/api/purchase-returns/drafts',json=manual([x,x])).status_code==422
    assert stock(S)=={'M':10,'L':10}


def test_edit_and_revalidation(setup):
    c,S=setup;purchase(c);d=save(c,manual([line(c,quantity=8)]));url=f"/api/purchase-returns/drafts/{d['id']}"
    r=c.put(url,json={**manual([line(c,quantity=7,rate=99)]),'revision':d['revision']});assert r.status_code==200,r.text
    d=r.json();assert d['amount']==693
    other=save(c,manual([line(c,quantity=5)],'RI-OTHER'));assert post(c,other).status_code==201
    assert post(c,d).status_code==409 and stock(S)=={'M':5,'L':10}
    assert c.get(url).status_code==200


def test_two_simultaneous_manual_returns(setup):
    c,S=setup;purchase(c)
    drafts=[save(c,manual([line(c,quantity=8)],f'RI-{i}')) for i in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:codes=list(pool.map(lambda d:post(c,d).status_code,drafts))
    assert sorted(codes)==[201,409] and stock(S)=={'M':2,'L':10}


def test_manual_import_without_source(setup):
    c,S=setup;purchase(c)
    raw=b'Master SKU,Size,Return Qty,Return Rate,Product Name\nKURTA,M,2,50,\nKURTA,M,3,50,\nNEW,XL,1,100,Outside\n'
    r=c.post('/api/purchase-returns/import-preview',files={'file':('return.csv',raw,'text/csv')});assert r.status_code==200,r.text
    lines=r.json()['lines'];assert lines[0]['quantity']==5 and lines[1]['record_only']
    assert stock(S)=={'M':10,'L':10}
    d=save(c,manual(lines));assert post(c,d).status_code==201
    assert stock(S)=={'M':5,'L':10}
    assert c.get('/api/purchase-returns/periods?report_date=2026-11-27').json()['day']['amount']==350


def test_legacy_database_upgrade_with_return_and_attachment(setup,tmp_path):
    # Construct the exact previous NOT NULL table schema and populate it with
    # legacy linked records; migrate with SQLite FK enforcement enabled.
    c,S=setup;d,_=purchase(c);old=c.post('/api/purchase-returns',json=payload(d));assert old.status_code==201,old.text
    old_id=old.json()['id'];draft=save(c,payload(d,'RI-OLD-D',2))
    path=tmp_path/'old.db';e=create_engine('sqlite:///'+str(path))
    fields=[bm.PurchaseReturn.purchase_id,bm.PurchaseReturnLine.purchase_line_id,bm.PurchaseReturnLine.variant_id,bm.PurchaseReturnLine.inventory_unit_cost,bm.PurchaseReturnLine.balance_after,bm.PurchaseReturnDraft.purchase_id]
    for f in fields:f.property.columns[0].nullable=False
    try:Base.metadata.create_all(e)
    finally:
        for f in fields:f.property.columns[0].nullable=True
    with S() as db:
        src=db.connection().connection.driver_connection
        with e.connect() as conn:
            dst=conn.connection.driver_connection
            # Copy all existing rows using SQLite backup then recreate the three
            # old tables separately with old DDL from schema just created.
            ddl={n:dst.execute('SELECT sql FROM sqlite_master WHERE name=?',(n,)).fetchone()[0] for n in ('myraa_purchase_returns','myraa_purchase_return_lines','myraa_purchase_return_drafts')}
            src.backup(dst)
            dst.execute('PRAGMA foreign_keys=OFF')
            dst.execute('PRAGMA legacy_alter_table=ON')
            for n,sql in ddl.items():
                dst.execute(f'ALTER TABLE {n} RENAME TO {n}_old')
                # legacy_alter_table makes rename behavior explicit in this fixture below
                dst.execute(sql);dst.execute(f'INSERT INTO {n} SELECT * FROM {n}_old');dst.execute(f'DROP TABLE {n}_old')
            dst.commit()
    # Preserve original foreign key targets in the fixture.
    with e.begin() as conn:
        conn.exec_driver_sql("INSERT INTO myraa_purchase_return_documents (return_id,filename,content_type,size_bytes,content) VALUES (?, 'a.pdf','application/pdf',1,X'41')",(old_id,))
    with e.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all()==[]
    upgrade_return_schema(e);upgrade_return_schema(e)
    with e.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar()==1
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").all()==[]
        assert conn.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name LIKE '%_r25_upgrade'").scalar()==0
    with sessionmaker(bind=e)() as db:
        assert db.query(bm.PurchaseReturn).one().id==old_id
        assert db.query(bm.PurchaseReturnLine).one().quantity==3
        assert db.query(bm.PurchaseReturnDraft).one().id==draft['id']
        assert db.query(bm.PurchaseReturnDocument).one().content==b'A'
    assert all(next(x for x in inspect(e).get_columns(f.property.columns[0].table.name) if x['name']==f.key)['nullable'] for f in fields)
    e.dispose()
