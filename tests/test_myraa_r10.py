"""R10 regressions: product lifetime, chart/export parity, and markup contract."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
import sys
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from openpyxl import load_workbook
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import models, business_models, workspace_models
from database import Base, get_db
from services.movers_export import build_movers_workbook


@pytest.fixture
def db(tmp_path):
    import main  # production dependencies required, including python-barcode
    engine = create_engine(f'sqlite:///{tmp_path}/r10.db', connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield main, session
    engine.dispose()


def test_delete_all_sizes_keeps_active_product_and_allows_new_size(db):
    main, session = db
    product = models.Product(sku='MODA_V_STYLE_BLACK', name='Style')
    session.add(product); session.flush()
    for size in ('S', 'M'):
        session.add(models.Variant(product_id=product.id, size=size, variant_code=product.sku+'_'+size, current_stock=0))
    session.commit()
    for size in ('S', 'M'):
        assert main.delete_variant(product.sku+'_'+size, db=session)['product_also_deleted'] is False
    session.expire_all()
    assert product.is_deleted is False and product.deleted_at is None
    assert main.get_product(product.sku, db=session)['variants'] == []
    assert main.get_product(product.sku, db=session)['total_stock'] == 0
    assert main.list_products(q=None, limit=None, offset=0, paged=False, db=session)[0]['sku'] == product.sku
    assert main.list_trash(limit=None, offset=0, db=session) == []
    assert session.query(models.Variant).filter_by(is_deleted=True).count() == 2
    updated = main.add_size(product.sku, main.schemas.VariantCreate(size='L', initial_stock=0), db=session)
    assert [v['size'] for v in updated['variants']] == ['L']
    main.delete_variant(product.sku+'_L', db=session)
    assert main.delete_product(product.sku, db=session)['trashed'] is True
    assert main.list_trash(limit=None, offset=0, db=session)[0]['sku'] == product.sku


def test_movers_chart_export_same_ranking_and_period(db):
    main, session = db
    now = datetime.now(timezone.utc)
    for i in range(10):
        p = models.Product(sku=f'BRAND_V_{i}', name=f'Design {i}')
        session.add(p); session.flush()
        v = models.Variant(product_id=p.id, size='S', variant_code=p.sku+'_S', current_stock=0)
        session.add(v); session.flush()
        order = models.OrderUpload(filename=f'{i}.csv', file_hash=f'hash-{i}', uploaded_at=now-timedelta(days=1 if i<9 else 20))
        session.add(order); session.flush()
        session.add(models.OrderLine(order_upload_id=order.id, variant_id=v.id, sku=v.variant_code, size='S', qty_ordered=i+1, qty_fulfilled=0, status=models.OrderLineStatus.PENDING))
    session.commit()
    app = FastAPI()
    app.add_api_route('/movers', main.movers, methods=['GET'])
    app.add_api_route('/movers/export', main.export_movers, methods=['GET'])
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app) as client:
        for days in (7,30,90):
            rows = client.get('/movers', params={'days':days,'limit':8}).json()
            r = client.get('/movers/export',params={'days':days,'limit':8})
            assert r.status_code == 200 and r.headers['cache-control'] == 'private, no-store'
            ws = load_workbook(BytesIO(r.content)).active
            assert list(ws.values)[2:] == [(i+1,x['sku'],x['name'],x['units_moved']) for i,x in enumerate(rows)]
            assert len(rows)==8 and rows[0]['units_moved']==(9 if days==7 else 10)
            assert ws['A1'].value == f'Top Movers - last {days} days'
        assert client.get('/movers/export',params={'days':0}).status_code==422


def test_excel_escapes_formula_and_empty_report():
    ws=load_workbook(BytesIO(build_movers_workbook([{'sku':'=1+1','name':'=2+2','units_moved':3}],7))).active
    assert ws['B3'].data_type==ws['C3'].data_type=='s'
    assert ws['B3'].value=='=1+1'
    assert load_workbook(BytesIO(build_movers_workbook([],30))).active.max_row==2
