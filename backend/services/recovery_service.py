"""Private, on-volume SQLite recovery points for each authenticated seller.

Snapshots are created using SQLite's online backup API, never by copying a live
WAL database file. Downloads are seller-scoped and require authenticated routes.
Restoring a whole live database requires maintenance and an explicit review;
this module deliberately does not offer unsafe hot-swap restoration.
"""
from __future__ import annotations

import json
import os
import shutil
import zipfile
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from database import DATABASE_URL, tenant_root, tenant_engine

SNAPSHOT_RE = re.compile(r"^inventory-(\d{8}T\d{6}Z)-([0-9a-f]{12})\.sqlite3$")


def _directory(account_id: int) -> Path:
    return tenant_root(account_id) / "recovery_snapshots"


def _snapshot_media_path(database_snapshot: Path) -> Path:
    return database_snapshot.parent / (database_snapshot.stem + "-files")


def _protect_private_media(account_id: int, database_snapshot: Path) -> int:
    """Hard-link immutable files so deleted scans remain recoverable in a ZIP.

    Backups and originals live on the same seller-owned volume; hard links do
    not multiply storage for unchanged media. When linking is unsupported, copy
    the file. Never follow symlinks or traverse outside a seller directory.
    """
    root = tenant_root(account_id)
    archive = _snapshot_media_path(database_snapshot)
    files = 0
    for category in ("product_images", "challan_scans"):
        directory = root / category
        if not directory.is_dir():
            continue
        for source in directory.iterdir():
            if source.is_symlink() or not source.is_file():
                continue
            if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,250}", source.name) or source.name.startswith("."):
                continue
            files += 1
            if files > 5000:
                raise RuntimeError("Too many media files for a local recovery point; configure external backup storage")
            target = archive / category / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
    return files


def create_snapshot(account_id: int, reason: str = "Manual recovery point") -> dict:
    """Fail closed on SQLite backup failure rather than allowing destructive writes."""
    if not DATABASE_URL.startswith("sqlite:"):
        raise RuntimeError("Tenant database snapshots require the SQLite seller architecture")
    # tenant_engine enforces Railway's persistent volume and creates a new empty
    # seller DB safely on first use. Never fall back to /tmp in production.
    tenant_engine(account_id)
    source = tenant_root(account_id) / "inventory.db"
    destination_dir = _directory(account_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = f"inventory-{timestamp}-{uuid.uuid4().hex[:12]}.sqlite3"
    dest = destination_dir / name
    fd, tmp = tempfile.mkstemp(dir=destination_dir, prefix=".snapshot-", suffix=".tmp")
    os.close(fd)
    try:
        # backup() works even if live DB is in WAL mode; a raw file copy doesn't.
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10) as live:
            with sqlite3.connect(tmp) as saved:
                live.backup(saved, pages=256, sleep=0.05)
                check = saved.execute("PRAGMA quick_check").fetchone()
                if check != ("ok",):
                    raise RuntimeError("SQLite backup consistency check failed")
        os.replace(tmp, dest)
        media_files = _protect_private_media(account_id, dest)
        label = str(reason).strip()[:160] or "Recovery point"
        meta_path = dest.with_suffix(".json")
        # Metadata is supplementary: snapshot data is already durable even if
        # writing its label fails. Never expose full filesystem paths.
        try:
            meta_path.write_text(json.dumps({"reason": label, "created_at": timestamp, "media_files": media_files}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return {"name": name, "size_bytes": dest.stat().st_size, "created_at": timestamp, "reason": label, "media_files": media_files}
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        dest.unlink(missing_ok=True)
        shutil.rmtree(_snapshot_media_path(dest), ignore_errors=True)
        raise


def snapshot_path(account_id: int, name: str) -> Path:
    if not SNAPSHOT_RE.fullmatch(str(name)):
        raise FileNotFoundError("Recovery point not found")
    path = _directory(account_id) / name
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError("Recovery point not found")
    return path


def list_snapshots(account_id: int, limit: int = 40) -> list[dict]:
    directory = _directory(account_id)
    if not directory.is_dir():
        return []
    results = []
    for path in sorted(directory.glob("inventory-*.sqlite3"), reverse=True):
        if not SNAPSHOT_RE.fullmatch(path.name) or path.is_symlink():
            continue
        try:
            reason = "Recovery point"
            media_files = 0
            sidecar = path.with_suffix(".json")
            if sidecar.is_file():
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
                reason = str(meta.get("reason") or reason)[:160]
                media_files = int(meta.get("media_files") or 0)
            results.append({"name": path.name, "size_bytes": path.stat().st_size, "created_at": path.name[10:26], "reason": reason, "media_files": media_files})
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if len(results) >= limit:
            break
    return results


def snapshot_summary(account_id: int, name: str) -> dict:
    path = snapshot_path(account_id, name)
    result = {"name": path.name, "products": 0, "deleted_products": 0, "variants": 0, "supplier_invoices": None}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as backup:
        backup.execute("PRAGMA query_only=ON")
        tables = {row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "products" in tables:
            result["products"] = backup.execute("SELECT COUNT(*) FROM products WHERE is_deleted=0").fetchone()[0]
            result["deleted_products"] = backup.execute("SELECT COUNT(*) FROM products WHERE is_deleted=1").fetchone()[0]
        if "variants" in tables:
            result["variants"] = backup.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
        if "purchase_challans" in tables:
            result["supplier_invoices"] = backup.execute("SELECT COUNT(*) FROM purchase_challans").fetchone()[0]
    return result


def prepare_download_archive(account_id: int, name: str) -> Path:
    """Make a temporary ZIP of the consistent DB plus versioned media files.

    Caller MUST arrange deletion after the response is sent. Only the caller's
    snapshot directory is ever accessed; names are validated in snapshot_path.
    """
    source_db = snapshot_path(account_id, name)
    fd, temporary = tempfile.mkstemp(dir=source_db.parent, prefix=".download-", suffix=".zip")
    os.close(fd)
    result = Path(temporary)
    try:
        with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(source_db, "inventory.db")
            media = _snapshot_media_path(source_db)
            for category in ("product_images", "challan_scans"):
                folder = media / category
                if not folder.is_dir():
                    continue
                for file in folder.iterdir():
                    if file.is_file() and not file.is_symlink() and re.fullmatch(r"[a-zA-Z0-9_.-]{1,250}", file.name):
                        archive.write(file, f"{category}/{file.name}")
        return result
    except Exception:
        result.unlink(missing_ok=True)
        raise
