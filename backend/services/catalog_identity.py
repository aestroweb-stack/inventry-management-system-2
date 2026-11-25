"""Shared duplicate checks for seller catalog writes (no destructive migration)."""
import unicodedata
from sqlalchemy import text, or_
from models import Product, Variant
from business_models import MarketplaceSkuAlias, ProductMarketplaceAlias


def design_key(name):
    # Delimiter, case and Unicode presentation differences must not create a
    # second design. Keep letters/digits (including color in the design name).
    return ''.join(c for c in unicodedata.normalize('NFKC', str(name or '')).casefold() if c.isalnum())


def lock_catalog(db):
    # SQLite seller databases: acquire the writer lock before checking identity.
    # An empty UPDATE takes the write lock without modifying catalog records.
    dialect = db.get_bind().dialect.name
    if dialect == 'sqlite':
        db.execute(text('UPDATE products SET sku = sku WHERE 1 = 0'))
    elif dialect == 'postgresql':
        db.execute(text('LOCK TABLE products IN SHARE ROW EXCLUSIVE MODE'))


def duplicate_design(db, name, exclude_id=None):
    key = design_key(name)
    if not key:
        return None
    return next((p for p in db.query(Product).all()
                 if p.id != exclude_id and design_key(p.name) == key), None)


def duplicate_message(product):
    return (f"Duplicate design: '{product.name}' already exists (Master SKU {product.sku}). "
            + ('Restore it from Trash before adding sizes.' if product.is_deleted else 'Open the existing product and add sizes or linked SKUs there.'))


def canonical_conflict(db, code, product_id=None, variant_id=None):
    """A new canonical code cannot shadow a registered external code."""
    code = str(code).strip().upper()
    p = db.query(Product).filter(Product.sku == code).first()
    if p and p.id != product_id:
        return True
    rows = db.query(Variant).filter(or_(Variant.variant_code == code, Variant.seller_sku_code == code, Variant.marketplace_sku_code == code)).all()
    if any(v.id != variant_id for v in rows):
        return True
    p_alias = db.query(ProductMarketplaceAlias).filter_by(normalized_code=code).first()
    if p_alias and p_alias.product_id != product_id:
        return True
    v_alias = db.query(MarketplaceSkuAlias).filter_by(normalized_code=code).first()
    return bool(v_alias and v_alias.variant_id != variant_id)
