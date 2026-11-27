"""Regression: exact photos, distractors, duplicate and missing designs."""
from io import BytesIO
from PIL import Image
from sqlalchemy.orm import Session
import pytest
from test_visual_ann_r21 import setup
from database import tenant_scope
from models import Product, ProductImage
from services import visual_search_service as visual, visual_ann_service as ann


def add(db, root, pid, color):
    path = f'{pid}.png'
    db.add(Product(id=pid, sku=f'P{pid}', name=f'Product {pid}'))
    db.flush()
    db.add(ProductImage(id=pid, product_id=pid, image_path='/static/product_images/'+path))
    Image.new('RGB', (128,128), color).save(root/path)
    db.commit()


@pytest.mark.parametrize('indexed', [True, False])
def test_exact_only_and_unrelated_no_match(setup, indexed):
    fake, seller = setup
    engine, root = seller(1, 'red')
    with tenant_scope(1), Session(engine) as db:
        add(db, root, 2, 'blue'); add(db, root, 3, 'green'); add(db, root, 4, 'white')
        if indexed: ann.build(db, root)
        else: fake.down=True
        result=visual.search(db,(root/'ref.png').read_bytes(),root,5)
        assert result.status=='matched' and [x.product_id for x in result.matches]==[1]
        assert result.retrieval_engine==('qdrant_ann' if indexed else 'exact_batched')
        out=BytesIO(); Image.new('RGB',(128,128),'purple').save(out,format='PNG')
        result=visual.search(db,out.getvalue(),root,5)
        assert result.status=='no_match' and not result.matches


def test_duplicate_photo_and_same_color_collision(setup):
    fake,seller=setup;engine,root=seller(1,'red')
    with tenant_scope(1),Session(engine) as db:
        add(db,root,2,'red');add(db,root,3,'blue');ann.build(db,root)
        result=visual.search(db,(root/'ref.png').read_bytes(),root,5)
        assert result.status=='ambiguous' and {x.product_id for x in result.matches}=={1,2}
        # Force a descriptor collision: distinct pixels must not be confirmed.
        query=BytesIO();Image.new('RGB',(128,128),(254,0,0)).save(query,format='PNG')
        result=visual.search(db,query.getvalue(),root,5)
        assert result.status=='ambiguous' and {x.product_id for x in result.matches}=={1,2}


def test_filter_each_candidate_and_no_unrelated_tail():
    h=visual.SearchHit
    assert [x.product_id for x in visual._decide_local([h(1,.94),h(2,.90),h(3,.80),h(4,.3)]).matches]==[1,2]
    assert [x.product_id for x in visual._decide_semantic_local([h(1,.85),h(2,.81),h(3,.60),h(4,.2)]).matches]==[1,2]
    assert visual._decide_local([h(1,.65)]).status=='no_match'
    assert visual._decide_local([h(1,1)]).status=='ambiguous'

@pytest.mark.parametrize('indexed', [True, False])
def test_exact_photo_wins_color_collision_and_gallery_tie(setup, indexed):
    fake,seller=setup;engine,root=seller(1,'red')
    with tenant_scope(1),Session(engine) as db:
        add(db,root,2,(254,0,0))
        # Both reference vectors collide. Exact pixels must win over the first ID.
        if indexed: ann.build(db,root)
        result=visual.search(db,(root/'2.png').read_bytes(),root,5)
        assert result.status=='matched' and [x.product_id for x in result.matches]==[2]
        db.add(ProductImage(id=3,product_id=1,image_path='/static/product_images/3.png'))
        Image.new('RGB',(128,128),(253,0,0)).save(root/'3.png');db.commit()
        if indexed: ann.build(db,root)
        result=visual.search(db,(root/'3.png').read_bytes(),root,5)
        assert result.status=='matched' and result.matches[0].reference_image_path.endswith('/3.png')
