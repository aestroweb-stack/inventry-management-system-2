"""Isolation and online backup tests; never touch a live Railway volume."""
import sqlite3
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import database
import models
import business_models, workspace_models, clerk_auth  # register relationship tables
from services import recovery_service


@pytest.fixture()
def private_store():
    original_root = database.SELLER_DB_ROOT
    original_engines = dict(database._tenant_engines)
    with TemporaryDirectory(prefix='myraa-recovery-test-') as temp:
        database.SELLER_DB_ROOT = Path(temp) / 'tenants'
        database._tenant_engines = {}
        yield
        for eng in database._tenant_engines.values():
            eng.dispose()
        database.SELLER_DB_ROOT = original_root
        database._tenant_engines = original_engines


def test_tenant_backups_preserve_deleted_and_live_products(private_store):
    with database.tenant_scope(1001):
        db = database.SessionLocal()
        try:
            db.add(models.Product(sku='SELLER-A', name='Seller A only', is_deleted=False))
            db.commit()
        finally:
            db.close()
    with database.tenant_scope(1002):
        db = database.SessionLocal()
        try:
            db.add(models.Product(sku='SELLER-B', name='Seller B only', is_deleted=False))
            db.commit()
        finally:
            db.close()
    a = recovery_service.create_snapshot(1001, 'Before delete')
    b = recovery_service.create_snapshot(1002, 'Manual')
    assert a['name'] != b['name']
    assert len(recovery_service.list_snapshots(1001)) == 1
    assert len(recovery_service.list_snapshots(1002)) == 1
    with pytest.raises(FileNotFoundError):
        recovery_service.snapshot_path(1002, a['name'])
    summary = recovery_service.snapshot_summary(1001, a['name'])
    assert summary['products'] == 1
    assert summary['deleted_products'] == 0
    with sqlite3.connect(recovery_service.snapshot_path(1001, a['name'])) as saved:
        assert saved.execute('select sku from products').fetchone()[0] == 'SELLER-A'
    with database.tenant_scope(1001):
        db = database.SessionLocal()
        try:
            p = db.query(models.Product).filter_by(sku='SELLER-A').one()
            p.is_deleted = True
            db.commit()
        finally:
            db.close()
    assert recovery_service.snapshot_summary(1001, a['name'])['products'] == 1


def test_path_traversal_denied_and_metadata_private(private_store):
    recovery_service.create_snapshot(55, 'Safe manual backup')
    for name in ('../../inventory.db', '..%2Finventory.db', 'inventory-20260101T000000Z-zzzzzzzzzzzz.sqlite3', '.hidden'):
        with pytest.raises(FileNotFoundError):
            recovery_service.snapshot_path(55, name)
    assert all(item['reason'] == 'Safe manual backup' for item in recovery_service.list_snapshots(55))


def test_sqlite_does_not_delete_original_on_backup_error(private_store, monkeypatch):
    import os
    recovery_service.create_snapshot(77)
    db_path = database.tenant_root(77) / 'inventory.db'
    assert db_path.is_file()
    monkeypatch.setattr(recovery_service, 'DATABASE_URL', 'postgresql://fake')
    with pytest.raises(RuntimeError):
        recovery_service.create_snapshot(77)
    assert db_path.is_file()


def test_private_media_survives_delete_and_archive_includes_original(private_store):
    import zipfile
    with database.tenant_scope(888):
        root = database.tenant_root()
        (root / 'challan_scans').mkdir(parents=True, exist_ok=True)
        (root / 'product_images').mkdir(parents=True, exist_ok=True)
        (root / 'challan_scans' / 'private-bill.pdf').write_bytes(b'%PDF-private-content')
        (root / 'product_images' / 'photo.webp').write_bytes(b'photo-private')
    saved = recovery_service.create_snapshot(888, 'Before invoice deletion')
    assert saved['media_files'] == 2
    (root / 'challan_scans' / 'private-bill.pdf').unlink()
    archive = recovery_service.prepare_download_archive(888, saved['name'])
    try:
        with zipfile.ZipFile(archive) as package:
            assert package.read('challan_scans/private-bill.pdf') == b'%PDF-private-content'
            assert package.read('product_images/photo.webp') == b'photo-private'
            assert package.testzip() is None
    finally:
        archive.unlink(missing_ok=True)
    with pytest.raises(FileNotFoundError):
        recovery_service.prepare_download_archive(889, saved['name'])
