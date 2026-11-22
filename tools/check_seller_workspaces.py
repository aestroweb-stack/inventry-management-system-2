#!/usr/bin/env python3
"""Read-only Railway audit: private MYRAA seller SQLite workspace files."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from sqlalchemy import inspect
from database import engine, SELLER_DB_ROOT, AuthSessionLocal, tenant_scope, SessionLocal
import workspace_models, models, business_models, clerk_auth

print('Root database driver:', engine.dialect.name)
print('Persistent seller root:', SELLER_DB_ROOT)
print('Seller root exists:', SELLER_DB_ROOT.is_dir())
with AuthSessionLocal() as db:
    print('Clerk-linked identities:', db.query(clerk_auth.ClerkIdentity).count())
print('Private workspace database count:', len(list(SELLER_DB_ROOT.glob('seller_*/inventory.db'))) if SELLER_DB_ROOT.exists() else 0)
for path in sorted(SELLER_DB_ROOT.glob('seller_*/inventory.db')) if SELLER_DB_ROOT.exists() else []:
    seller_id = path.parent.name.removeprefix('seller_')
    if not seller_id.isdigit():continue
    with tenant_scope(int(seller_id)):
        with SessionLocal() as db:
            print('Seller ID:', seller_id,
                  'products:',db.query(models.Product).count(),
                  'variants:',db.query(models.Variant).count(),
                  'challans:',db.query(business_models.PurchaseChallan).count())
print('READ-ONLY CHECK COMPLETE — no records created or modified.')
