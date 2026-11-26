"""Optional Qdrant ANN candidate retrieval. SQLite is the source of truth.

Immutable generations, mandatory seller filter, and live reference validation.
No inventory writes and no remote image upload (only derived vectors).
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import numpy as np
from sqlalchemy import text
from database import tenant_id, tenant_root
from services import visual_search_service as visual

log = logging.getLogger(__name__)
STATE = 'visual_ann_state_r21'
BATCH = 128


def configured():
    return bool(os.getenv('VISUAL_QDRANT_URL', '').strip())


def _identity(db):
    account = tenant_id()
    bind = db.get_bind()
    if not account or bind.dialect.name != 'sqlite' or Path(bind.url.database or '').resolve() != (tenant_root() / 'inventory.db').resolve():
        raise ValueError('Authenticated seller database required')
    namespace = os.getenv('VISUAL_QDRANT_NAMESPACE', '').strip()
    if not namespace:
        raise ValueError('VISUAL_QDRANT_NAMESPACE required')
    seller = hashlib.sha256(f'{namespace}\0{account}'.encode()).hexdigest()
    mode = visual._local_mode()
    collection = 'myraa_r21_' + hashlib.sha256(f'{namespace}\0{mode}\0{visual.VECTOR_DIMENSIONS}'.encode()).hexdigest()[:32]
    return seller, mode, collection


class Qdrant:
    def __init__(self, collection):
        url = os.getenv('VISUAL_QDRANT_URL', '').strip().rstrip('/')
        key = os.getenv('VISUAL_QDRANT_API_KEY', '').strip()
        parsed = urlsplit(url)
        if not key or parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path:
            raise ValueError('Private Qdrant URL and API key required')
        if parsed.scheme == 'http' and os.getenv('VISUAL_QDRANT_ALLOW_PRIVATE_HTTP') != '1':
            raise ValueError('HTTPS required (private network HTTP needs explicit opt-in)')
        self.client = httpx.Client(base_url=url, headers={'api-key': key}, timeout=10, follow_redirects=False, trust_env=False)
        self.path = f'/collections/{collection}'

    def __enter__(self): return self
    def __exit__(self, *_): self.client.close()

    def request(self, method, suffix='', body=None):
        response = self.client.request(method, self.path + suffix, json=body)
        response.raise_for_status()
        data = response.json()
        if data.get('status') != 'ok':
            raise RuntimeError('Qdrant did not acknowledge operation')
        return data['result']

    def ensure(self):
        result = self.request('GET', '/exists')
        if not result['exists']:
            try:
                self.request('PUT', body={'vectors': {'size': visual.VECTOR_DIMENSIONS, 'distance': 'Cosine', 'on_disk': True}, 'hnsw_config': {'m': 16, 'ef_construct': 128, 'on_disk': True}, 'on_disk_payload': True})
            except httpx.HTTPStatusError:
                if not self.request('GET', '/exists')['exists']:
                    raise
        info = self.request('GET')
        params = info['config']['params']['vectors']
        if params['size'] != visual.VECTOR_DIMENSIONS or params['distance'].lower() != 'cosine':
            raise ValueError('Incompatible Qdrant collection')
        for field in ('seller', 'generation'):
            if field not in info.get('payload_schema', {}):
                schema = {'type': 'keyword', 'is_tenant': True} if field == 'seller' else 'keyword'
                self.request('PUT', '/index?wait=true', {'field_name': field, 'field_schema': schema})

    @staticmethod
    def filter(seller, generation):
        return {'must': [{'key': 'seller', 'match': {'value': seller}}, {'key': 'generation', 'match': {'value': generation}}]}

    def upsert(self, points):
        if points:
            self.request('PUT', '/points?wait=true', {'points': points})

    def count(self, seller, generation):
        return int(self.request('POST', '/points/count', {'filter': self.filter(seller, generation), 'exact': True})['count'])

    def delete(self, seller, generation):
        self.request('POST', '/points/delete?wait=true', {'filter': self.filter(seller, generation)})

    def query(self, seller, generation, vector, limit):
        return self.request('POST', '/points/query', {'query': vector.tolist(), 'filter': self.filter(seller, generation), 'limit': max(100, limit * 20), 'with_payload': True, 'with_vector': False, 'params': {'hnsw_ef': 128}})['points']


def _install(db):
    # Triggers participate in the catalog transaction. Stock-only edits do not
    # invalidate an image index. Random revisions avoid counter reuse on restore.
    db.execute(text(f'''CREATE TABLE IF NOT EXISTS {STATE} (
      id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL,
      ready_revision TEXT, generation TEXT, collection TEXT, seller TEXT,
      photo_count INTEGER NOT NULL DEFAULT 0)'''))
    db.execute(text(f"INSERT OR IGNORE INTO {STATE}(id, revision) VALUES (1,lower(hex(randomblob(16))) )"))
    for table, operation, suffix in [('product_images','INSERT','ai'), ('product_images','UPDATE OF image_path, product_id','au'), ('product_images','DELETE','ad'), ('products','INSERT','pi'), ('products','UPDATE OF is_deleted','pu'), ('products','DELETE','pd'), ('local_visual_vectors_v1','DELETE','vd')]:
        db.execute(text(f'''CREATE TRIGGER IF NOT EXISTS visual_ann_r21_{suffix} AFTER {operation} ON {table}
        BEGIN UPDATE {STATE} SET revision=lower(hex(randomblob(16))) WHERE id=1; END'''))
    # Cache changes detected by exact fallback also invalidate a published index.
    db.execute(text(f'''CREATE TRIGGER IF NOT EXISTS visual_ann_r21_vu AFTER UPDATE ON local_visual_vectors_v1
        WHEN OLD.model_name != NEW.model_name OR OLD.image_path != NEW.image_path OR OLD.mtime_ns != NEW.mtime_ns OR OLD.file_size != NEW.file_size OR OLD.embedding != NEW.embedding
        BEGIN UPDATE {STATE} SET revision=lower(hex(randomblob(16))) WHERE id=1 AND ready_revision=revision; END'''))
    db.commit()


def _state(db):
    exists = db.execute(text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"), {'name': STATE}).scalar()
    if not exists:
        return None
    return db.execute(text(f'SELECT * FROM {STATE} WHERE id=1')).mappings().first()


def _ready(state, seller, collection):
    return bool(state and state['ready_revision'] == state['revision'] and state['generation'] and state['seller'] == seller and state['collection'] == collection)


def status(db):
    if not configured(): return {'state': 'disabled'}
    try:
        seller, _, collection = _identity(db)
        state = _state(db)
        if _ready(state, seller, collection):
            return {'state': 'indexed', 'indexed_photos': state['photo_count']}
        return {'state': 'pending'}
    except Exception:
        return {'state': 'unavailable'}


def _cached_vector(row, root, mode):
    path = (root / Path(row.image_path).name).resolve()
    path.relative_to(root)
    stat = path.stat()
    if row.model_name == mode and row.cached_path == row.image_path and row.mtime_ns == stat.st_mtime_ns and row.file_size == stat.st_size and row.embedding is not None and len(row.embedding) == 4 * visual.VECTOR_DIMENSIONS:
        vector = np.frombuffer(row.embedding, dtype='<f4')
        if np.isfinite(vector).all() and abs(float(np.linalg.norm(vector)) - 1) <= .01:
            return path, stat, vector
    return path, stat, None


def _rows(db):
    from models import Product, ProductImage, LocalVisualVector as V
    return db.query(ProductImage.id, ProductImage.product_id, ProductImage.image_path,
                    V.model_name, V.image_path.label('cached_path'), V.mtime_ns, V.file_size, V.embedding).select_from(ProductImage).join(Product, Product.id == ProductImage.product_id).outerjoin(V, V.image_id == ProductImage.id).filter(Product.is_deleted == False)  # noqa: E712


def build(db, image_dir: Path, stop=None, force=False):
    """Publish complete generation only if catalog revision remains unchanged.

    Called by a single worker per shared seller volume, never on request path.
    Incomplete/invalid reference photos leave exact search available.
    """
    from models import ProductImage
    seller, mode, collection = _identity(db)
    root = image_dir.resolve()
    _install(db)
    state = _state(db)
    if _ready(state, seller, collection) and not force:
        # A restored or wiped remote index must be rebuilt, not marked ready forever.
        with Qdrant(collection) as q:
            q.ensure()
            if q.count(seller, state['generation']) == state['photo_count']:
                return 'already_indexed'
        db.execute(text(f'UPDATE {STATE} SET ready_revision=NULL WHERE id=1 AND generation=:generation'), {'generation':state['generation']})
        db.commit()
        state = _state(db)
    revision = state['revision']
    db.execute(text(f'UPDATE {STATE} SET ready_revision=NULL WHERE id=1 AND revision=:revision'), {'revision':revision})
    generation = uuid.uuid4().hex
    old = state['generation'] if state['seller'] == seller and state['collection'] == collection else None
    db.commit()
    last = count = 0
    published = False
    with Qdrant(collection) as q:
        q.ensure()
        try:
            while True:
                if stop is not None and stop.is_set(): raise RuntimeError('Worker stopping')
                rows = _rows(db).filter(ProductImage.id > last).order_by(ProductImage.id).limit(BATCH).all()
                db.commit()  # release read transaction before inference/network
                if not rows: break
                points = []
                for row in rows:
                    if stop is not None and stop.is_set(): raise RuntimeError('Worker stopping')
                    path, stat, vector = _cached_vector(row, root, mode)
                    if vector is None:
                        with path.open('rb') as source:
                            content = source.read(visual.visual_match_service.MAX_SCAN_BYTES + 1)
                        visual.visual_match_service.validate_image(content)
                        vector = visual._float_vector(visual._image_embedding(content) if visual._semantic_local_mode() else visual.visual_match_service.local_visual_embedding(content))
                        current = path.stat()
                        if (stat.st_mtime_ns,stat.st_size) != (current.st_mtime_ns,current.st_size):
                            raise RuntimeError('Reference changed while encoding')
                        visual._store_local_vector(db,row.id,row.image_path,stat,vector)
                    points.append({'id': str(uuid.uuid5(uuid.NAMESPACE_URL, f'{seller}:{generation}:{row.id}')), 'vector': vector.tolist(), 'payload': {'seller': seller, 'generation': generation, 'image_id': row.id, 'product_id': row.product_id}})
                current_state = _state(db)
                if current_state['revision'] != revision: raise RuntimeError('Catalog changed during indexing')
                db.commit()
                q.upsert(points)
                count += len(points)
                last = rows[-1].id
            if q.count(seller, generation) != count:
                raise RuntimeError('Remote index count mismatch')
            result = db.execute(text(f'''UPDATE {STATE} SET ready_revision=:revision,generation=:generation,collection=:collection,seller=:seller,photo_count=:count WHERE id=1 AND revision=:revision'''), dict(revision=revision,generation=generation,collection=collection,seller=seller,count=count))
            if result.rowcount != 1: raise RuntimeError('Catalog changed during indexing')
            db.commit()
            published = True
            if old and old != generation:
                try: q.delete(seller, old)
                except Exception: log.warning('Old visual generation cleanup pending')
            return 'indexed'
        finally:
            if not published:
                db.rollback()
                try: q.delete(seller, generation)
                except Exception: log.warning('Incomplete visual generation cleanup pending')


def search(db, embedding, image_dir, limit):
    """None means use exact R20 retrieval. ANN never auto-confirms a design."""
    if not configured(): return None
    from models import ProductImage
    try:
        seller, mode, collection = _identity(db)
        state = _state(db)
        if not _ready(state, seller, collection) or not state['photo_count']:
            return None
        generation = state['generation']
        vector = visual._float_vector(embedding)
        with Qdrant(collection) as q:
            points = q.query(seller, generation, vector, limit)
        if not points or len(points) > max(100, limit * 20): return None
        refs = {}
        for point in points:
            p = point['payload']
            if p.get('seller') != seller or p.get('generation') != generation: return None
            iid, pid = p['image_id'], p['product_id']
            if type(iid) is not int or type(pid) is not int: return None
            refs[iid] = pid
        rows = _rows(db).filter(ProductImage.id.in_(refs)).all()
        if len(rows) != len(refs): return None
        best = {}
        for row in rows:
            if row.product_id != refs[row.id]: return None
            _, _, candidate = _cached_vector(row, image_dir.resolve(), mode)
            if candidate is None: return None
            score = float(np.clip(np.dot(candidate,vector),-1,1))
            hit = visual.SearchHit(row.product_id, score, row.image_path)
            if row.product_id not in best or score > best[row.product_id].similarity: best[row.product_id] = hit
        fresh = _state(db)
        if not _ready(fresh,seller,collection) or fresh['generation'] != generation: return None
        hits = sorted(best.values(),key=lambda h:(-h.similarity,h.product_id))[:limit]
        decision = visual._decide_semantic_local(hits) if visual._semantic_local_mode() else visual._decide_local(hits)
        if decision.status == 'no_match': return None  # exact fallback on uncertain shortlist
        return visual.SearchResult('ambiguous',tuple(hits[:5]),'Indexed visual candidates found. Compare the saved photos and confirm the exact design; stock is read from your own catalog.')
    except Exception as exc:
        log.warning('Visual ANN unavailable; exact search fallback (%s)',type(exc).__name__)
        return None
