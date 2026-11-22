"""
Database connection setup.

Global DATABASE_URL stores Clerk identities and the untouched legacy catalog.
Authenticated sellers use separate SQLite files beneath the persistent Railway
volume. PostgreSQL tenancy requires a separate implementation; fail closed.
"""
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./inventory.db")

# MYRAA_DB_PREFLIGHT_V1: fail safely if Railway SQLite storage is not mounted.
# Railway supplies RAILWAY_VOLUME_MOUNT_PATH automatically when a volume is attached.
# Never silently fall back to an ephemeral database for seller inventory.
def _prepare_sqlite_database_file(url):
    from sqlalchemy.engine import make_url
    db_name = make_url(url).database
    if not db_name or db_name == ":memory:":
        return

    db_path = Path(db_name).expanduser().resolve()
    on_railway = bool(
        os.getenv("RAILWAY_PROJECT_ID")
        or os.getenv("RAILWAY_SERVICE_ID")
        or os.getenv("RAILWAY_ENVIRONMENT")
    )
    if on_railway:
        mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        if not mount:
            raise RuntimeError(
                "MYRAA storage: no Railway persistent volume attached. "
                "Attach the existing volume to this service at /data; "
                "set DATABASE_URL=sqlite:////data/inventory.db. "
                "Do not create or use an ephemeral database."
            )
        volume_path = Path(mount).resolve()
        if not volume_path.is_dir():
            raise RuntimeError(
                f"MYRAA storage: volume mount {volume_path} is not a directory "
                "at runtime. Check Railway volume attachment/mount path."
            )
        if not db_path.is_relative_to(volume_path) or db_path == volume_path:
            raise RuntimeError(
                f"MYRAA storage: SQLite path {db_path} is outside volume "
                f"{volume_path}. Set DATABASE_URL to a path inside that volume."
            )

    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"MYRAA storage: cannot create database directory {db_path.parent}. "
            "Check Railway volume mount path and write permissions."
        ) from exc
    if not os.access(db_path.parent, os.W_OK | os.X_OK):
        raise RuntimeError(
            f"MYRAA storage: database directory {db_path.parent} is not writable. "
            "Check volume permissions; never replace the database to fix permissions."
        )
    if db_path.exists() and not os.access(db_path, os.W_OK):
        raise RuntimeError(
            f"MYRAA storage: existing database {db_path} is not writable. "
            "Check Railway volume permissions and ownership."
        )


if DATABASE_URL.startswith("sqlite"):
    _prepare_sqlite_database_file(DATABASE_URL)


connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
if DATABASE_URL.startswith("sqlite"):
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
else:
    # Bound each web process to a modest number of DB connections. Worker
    # count multiplied by this pool should stay within the PostgreSQL plan.
    engine = create_engine(
        DATABASE_URL,
        pool_size=max(1, int(os.getenv("DB_POOL_SIZE", "5"))),
        max_overflow=max(0, int(os.getenv("DB_MAX_OVERFLOW", "5"))),
        pool_timeout=max(1, int(os.getenv("DB_POOL_TIMEOUT", "30"))),
        pool_recycle=max(60, int(os.getenv("DB_POOL_RECYCLE", "1800"))),
        pool_pre_ping=True,
    )

AuthSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Every seller has a separate physical database. The root DATABASE_URL remains
# the Clerk identity / profile registry and existing legacy database. Do not
# silently assign or rewrite legacy inventory records.
_tenant_id = ContextVar('myraa_tenant_id', default=None)
_tenant_engines = {}
_engine_lock = threading.RLock()
SELLER_DB_ROOT = Path(os.getenv('MYRAA_SELLER_DB_ROOT') or
    (str(Path(os.environ['RAILWAY_VOLUME_MOUNT_PATH']) / 'seller_workspaces')
     if os.getenv('RAILWAY_VOLUME_MOUNT_PATH') else '/tmp/myraa_seller_workspaces')).resolve()


def tenant_id():
    return _tenant_id.get()


def tenant_root(account_id=None):
    account_id = account_id if account_id is not None else tenant_id()
    if account_id is None or not re.fullmatch(r'[1-9][0-9]{0,16}', str(account_id)):
        raise RuntimeError('Authenticated seller identity is required')
    return SELLER_DB_ROOT / ('seller_' + str(account_id))


@contextmanager
def tenant_scope(account_id):
    token = _tenant_id.set(str(account_id))
    try:
        yield
    finally:
        _tenant_id.reset(token)


def tenant_engine(account_id):
    if os.getenv('RAILWAY_ENVIRONMENT') and not os.getenv('RAILWAY_VOLUME_MOUNT_PATH') and not os.getenv('MYRAA_SELLER_DB_ROOT'):
        raise RuntimeError('Railway persistent volume required; configure RAILWAY_VOLUME_MOUNT_PATH=/data')
    if not DATABASE_URL.startswith('sqlite:'):
        raise RuntimeError('Per-seller SQLite isolation requires a SQLite auth DATABASE_URL; configure a separate tenant architecture for Postgres')
    directory = tenant_root(account_id)
    with _engine_lock:
        key = str(directory)
        if key not in _tenant_engines:
            directory.mkdir(parents=True, exist_ok=True)
            dbfile = directory / 'inventory.db'
            local = create_engine('sqlite:///' + str(dbfile), connect_args={'check_same_thread':False})
            # All inventory/business tables are registered at startup. The
            # workspace identity tables in this DB are unused, but harmless.
            Base.metadata.create_all(bind=local)
            _tenant_engines[key] = local
        return _tenant_engines[key]


def SessionLocal():
    current = tenant_id()
    if current is None:
        return AuthSessionLocal()
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=tenant_engine(current))
    return session_factory()


def auth_get_db():
    db = AuthSessionLocal()
    try:
        yield db
    finally:
        db.close()



def get_db():
    """FastAPI dependency - one DB session per request, always closed after."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
