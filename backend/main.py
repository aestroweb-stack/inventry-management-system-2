import asyncio
import json
import hmac
import os
import shutil
import uuid
import re
import logging
from contextlib import asynccontextmanager
from importlib.util import find_spec
from difflib import SequenceMatcher
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, RedirectResponse, Response, JSONResponse
from starlette.background import BackgroundTask
from sqlalchemy import case, func, or_, delete as sa_delete, inspect as sa_inspect, text
from sqlalchemy.orm import Session, joinedload, selectinload, contains_eager, aliased

from database import Base, engine, get_db, SessionLocal, tenant_scope, tenant_root, tenant_id
import models
import business_models
import workspace_models  # Register account table before create_all
from workspace_auth import router as workspace_auth_router, account_from_request
from clerk_auth import router as clerk_auth_router, ClerkIdentity  # register link table before create_all
import schemas
from business_api import router as business_router, upsert_product_cost, upsert_variant_cost, add_alias, add_product_alias
from services import (
    inventory_service, alert_service, excel_service, reorder_service,
    barcode_service, bulk_stock_service, bulk_product_service,
    visual_match_service, visual_search_service, recovery_service,
)
from services import app_auth
from services.catalog_identity import design_key, lock_catalog, duplicate_design, duplicate_message, canonical_conflict
from services import packing_picklist_service
from packing_sync_api import router as packing_sync_router  # register persistent picklist tables before create_all

Base.metadata.create_all(bind=engine)
logger = logging.getLogger(__name__)
ACTIVE_ORDER_STATUSES = (
    models.OrderLineStatus.PENDING,
    models.OrderLineStatus.INSUFFICIENT_STOCK,
)


def _index_product_photo_background(product_id: int, image_id: int, content: bytes, image_path: str, seller_account_id: int) -> None:
    """Index a saved catalog photo after the upload response has been returned.

    SigLIP's first model load can take several seconds on a small Railway
    service. Photo saving and inventory creation must not wait for that work.
    """
    # BackgroundTasks executes after middleware response; pass identity explicitly.
    with tenant_scope(seller_account_id):
        return _run_product_photo_background(product_id,image_id,content,image_path)


def _run_product_photo_background(product_id, image_id, content, image_path):
    db = SessionLocal()
    path = Path(image_path)
    try:
        visual_search_service.index_product_image(db, product_id, image_id, content, path)
        db.commit()
        visual_search_service.clear_visual_cache()
    except Exception as exc:
        db.rollback()
        try:
            visual_search_service.remove_product_embedding(db, product_id, image_id)
            db.commit()
        except Exception:
            db.rollback()
        logger.warning("Product photo saved; visual indexing queued for later rebuild (%s).", type(exc).__name__)
    finally:
        db.close()


def migrate_legacy_brand_data_explicitly(target_engine=engine):
    """Optional legacy cleanup; never invoke automatically during app startup.

    This changes saved product names and drops a database column. Run it only
    as a separately reviewed migration after taking a restorable backup.
    """
    columns = {column["name"] for column in sa_inspect(target_engine).get_columns("products")}
    if "brand" not in columns:
        return
    # Preserve product descriptions while removing an identical leading brand
    # prefix (for example, "CATALOG LABEL Women..." becomes "Women...").
    with target_engine.begin() as connection:
        connection.execute(text("""
            UPDATE products
            SET name = LTRIM(SUBSTR(name, LENGTH(brand) + 1))
            WHERE brand IS NOT NULL AND TRIM(brand) != ''
              AND LOWER(SUBSTR(name, 1, LENGTH(brand))) = LOWER(brand)
        """))
        connection.execute(text("ALTER TABLE products DROP COLUMN brand"))

app = FastAPI(title="MYRAA Inventory — Hacker Room", version="2.0")
app.include_router(business_router)
app.include_router(packing_sync_router)
app.include_router(workspace_auth_router)
app.include_router(clerk_auth_router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("APP_ALLOWED_ORIGINS", "").split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
)


@app.middleware("http")
async def authenticated_seller_workspace(request: Request, call_next):
    """Fail-closed tenant boundary. Auth lives in root DB; all inventory
    queries and filesystem assets are scoped to the Clerk-verified account.
    """
    path = request.url.path
    # Never serve stale application code or conditional 304 responses after deploy.
    if path == "/app" or path.startswith("/app/"):
        if path.endswith((".js", ".css", ".html")) or path in ("/app", "/app/"):
            request.scope["headers"] = [(k, v) for k, v in request.scope["headers"] if k not in (b"if-none-match", b"if-modified-since")]
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["X-MYRAA-Release"] = "nov25-r13"
            return response
    # The public StaticFiles mount must never serve a legacy/global product
    # photo from a nested path that bypasses the seller-specific image route.
    if path.startswith('/static/product_images/'):
        suffix = path[len('/static/product_images/'):]
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,250}', suffix) or suffix.startswith('.'):
            return JSONResponse({'detail':'Image not found'}, status_code=404)
    if path.startswith(('/api/mcp', '/api/assistant', '/api/workspace-admin')):
        return JSONResponse({'detail':'Feature not available'}, status_code=404)
    if path.startswith('/api/workspace-auth/') and path not in (
        '/api/workspace-auth/status', '/api/workspace-auth/me',
        '/api/workspace-auth/me/photo', '/api/workspace-auth/logout'):
        return JSONResponse({'detail':'Use Clerk Google/email/password/OTP login'},status_code=410)
    account = account_from_request(request)
    if path.startswith("/api/recovery/"):
        return JSONResponse({"detail": "Recovery is managed on the server"}, status_code=404)
    restricted = (path.startswith('/api/') and not path.startswith(('/api/clerk/', '/api/workspace-auth/'))) or path.startswith(('/api/workspace-auth/me', '/api/workspace-auth/logout'))
    if (restricted and path not in ('/api/auth/status','/api/auth/login','/api/auth/logout','/api/health')) or path.startswith('/static/product_images/'):
        if account is None:
            return JSONResponse({'detail':'Sign in with Clerk'},status_code=401)
        if not account.active or not account.approved:
            return JSONResponse({'detail':'Account not active'}, status_code=403)
        if request.method not in ('GET','HEAD','OPTIONS') and not app_auth.origin_allowed(request):
            return JSONResponse({'detail':'Invalid request origin'},status_code=403)
    if account is None:
        return await call_next(request)
    # Set context before entering any downstream FastAPI route/dependency.
    with tenant_scope(account.id):
        # A consistent on-volume snapshot is captured BEFORE destructive API
        # methods. If the volume is full or the backup cannot be validated,
        # reject the delete instead of losing the only recoverable copy.
        if (request.method == "DELETE" and path.startswith("/api/")) or (request.method == "POST" and path.startswith("/api/products/") and path.endswith("/editor-save")):
            try:
                await asyncio.to_thread(recovery_service.create_snapshot, account.id, f"Before {request.method} {path[:110]}")
            except Exception:
                logger.exception("Unable to create pre-delete recovery point")
                return JSONResponse({"detail": "Unable to create recovery point. Delete was cancelled; check persistent volume storage."}, status_code=503)
        response = await call_next(request)
        if path.startswith("/api/"):
            response.headers["Cache-Control"] = "private, no-store"
        return response

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)
PRODUCT_IMAGE_DIR = Path(os.getenv("PRODUCT_IMAGE_DIR") or str(STATIC_DIR / "product_images")).resolve()
PRODUCT_IMAGE_DIR.mkdir(parents=True, exist_ok=True)
# Mount the persistent Railway image directory before the general static mount.
from fastapi.responses import FileResponse

def seller_image_dir():
    directory = tenant_root() / 'product_images'
    directory.mkdir(parents=True, exist_ok=True)
    return directory

@app.get('/static/product_images/{filename}', include_in_schema=False)
def serve_private_product_image(filename: str):
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,250}', filename) or filename.startswith('.'):
        raise HTTPException(404, 'No image')
    path = seller_image_dir() / filename
    if not path.is_file():
        raise HTTPException(404, 'No image')
    return FileResponse(path, headers={'Cache-Control':'private, no-store'})

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

FRONTEND_DIR = BASE_DIR.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")



@app.get("/api/auth/status")
def auth_status(request: Request):
    account = account_from_request(request)
    return {"login_required": True,
            "authenticated": account is not None,
            "owner": bool(account and account.role == "admin"),
            "user_role": account.role if account else None,
            "configuration_required": len(os.getenv("MYRAA_USER_SESSION_SECRET", "")) < 32,
            "signup_enabled": bool(os.getenv("CLERK_PUBLISHABLE_KEY") and os.getenv("CLERK_SECRET_KEY"))}


@app.post("/api/auth/login")
async def auth_login_deprecated():
    # Do not accidentally re-enable the old shared-password sign-in when an
    # operator leaves APP_PASSWORD in Railway environment variables.
    raise HTTPException(410, "Shared password login retired. Use Clerk Google or email code.")


@app.post("/api/auth/logout")
def auth_logout_deprecated():
    # Old link compatibility: real logout is /api/workspace-auth/logout.
    response = JSONResponse({"ok": True, "message": "Use /api/workspace-auth/logout"})
    response.delete_cookie("myraa_session", path="/")
    return response


@app.get("/api/health")
def health(response: Response):
    response.headers["X-MYRAA-Release"] = "nov25-r13"
    response.headers["Cache-Control"] = "no-store"
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def app_home():
    """Public marketing home; Clerk-protected seller application remains under /app/."""
    landing = FRONTEND_DIR / "landing.html"
    if not landing.is_file():
        return RedirectResponse(url="/app/sign-in.html", status_code=307)
    return FileResponse(landing)

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ------------------------------------------------------------------ #
# helpers
# ------------------------------------------------------------------ #
def _variant_status(v: models.Variant) -> str:
    if v.last_counted_at is None:
        return "UNCOUNTED"
    if v.current_stock <= 0:
        return "OUT"
    if v.current_stock <= v.reorder_threshold:
        return "LOW"
    return "OK"


def _variant_out(v: models.Variant) -> dict:
    return {
        "variant_code": v.variant_code, "size": v.size, "current_stock": v.current_stock,
        "reorder_threshold": v.reorder_threshold, "target_stock_level": v.target_stock_level,
        "status": _variant_status(v),
        "unit_cost": float(v.cost_record.unit_cost if v.cost_record else v.product.cost_record.unit_cost if v.product.cost_record else 0) if (v.cost_record or v.product.cost_record) else None,
        "cost_value": float((v.cost_record.unit_cost if v.cost_record else v.product.cost_record.unit_cost if v.product.cost_record else 0) * max(v.current_stock, 0)) if (v.cost_record or v.product.cost_record) and v.last_counted_at else None,
        "cost_override": float(v.cost_record.unit_cost) if v.cost_record else None,
        "marketplace_aliases": [{"id": a.id, "marketplace": a.marketplace, "sku": a.alias_code} for a in v.marketplace_aliases],
    }


def _order_line_dispatchability(db: Session, target_lines) -> dict[int, int]:
    """Allocate counted physical stock to pending order lines in FIFO order.

    The returned quantity is informational for the UI and also limits a
    dispatch request to the quantity currently available for each selected
    line. Looking across batches prevents the same physical pieces from making
    every pending batch appear ready at once.
    """
    target_ids = {line.id for line in target_lines if line.status in ACTIVE_ORDER_STATUSES}
    variant_ids = {line.variant_id for line in target_lines if line.variant_id is not None}
    if not target_ids or not variant_ids:
        return {}

    pending_lines = (
        db.query(models.OrderLine)
        .join(models.OrderUpload, models.OrderLine.order_upload_id == models.OrderUpload.id)
        .options(joinedload(models.OrderLine.variant))
        .filter(
            models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES),
            models.OrderLine.variant_id.in_(variant_ids),
        )
        .order_by(models.OrderUpload.uploaded_at.asc(), models.OrderUpload.id.asc(), models.OrderLine.id.asc())
        .all()
    )
    stock_left: dict[int, int] = {}
    dispatchable: dict[int, int] = {}
    for line in pending_lines:
        variant = line.variant
        if variant is None:
            continue
        if variant.id not in stock_left:
            stock_left[variant.id] = max(int(variant.current_stock or 0), 0) if variant.last_counted_at else 0
        remaining = max(int(line.qty_ordered or 0) - int(line.qty_fulfilled or 0), 0)
        quantity = min(remaining, stock_left[variant.id])
        if line.id in target_ids:
            dispatchable[line.id] = quantity
        stock_left[variant.id] -= quantity
    return dispatchable


def _product_out(p: models.Product) -> dict:
    live_variants = [v for v in p.variants if not v.is_deleted]
    image_paths = []
    if p.image_path:
        image_paths.append(p.image_path)
    image_paths.extend(
        image.image_path for image in sorted(
            p.reference_images,
            key=lambda image: (not image.is_primary, image.id),
        )
        if image.image_path and image.image_path not in image_paths
    )
    return {
        "id": p.id, "sku": p.sku, "name": p.name, "category": p.category,
        "image_path": p.image_path,
        "images": image_paths,
        "vendor": {"id": p.vendor.id, "name": p.vendor.name, "contact": p.vendor.contact} if p.vendor else None,
        "variants": [_variant_out(v) for v in sorted(live_variants, key=lambda v: (bulk_stock_service.SIZE_ORDER.get(v.size, 99), v.size))],
        "total_stock": sum(v.current_stock for v in live_variants),
        "product_aliases": [{"id": a.id, "marketplace": a.marketplace, "sku": a.alias_code} for a in sorted(p.product_aliases, key=lambda x: x.id)],
        "unit_cost": float(p.cost_record.unit_cost) if p.cost_record else None,
        "stock_cost_value": float(sum((v.cost_record.unit_cost if v.cost_record else p.cost_record.unit_cost if p.cost_record else 0) * max(v.current_stock, 0) for v in live_variants if v.last_counted_at)),
    }


def _variant_row(v: models.Variant) -> dict:
    p = v.product
    return {
        "variant_code": v.variant_code, "sku": p.sku, "size": v.size, "product_name": p.name,
        "category": p.category, "vendor": p.vendor.name if p.vendor else None,
        "image_path": p.image_path, "current_stock": v.current_stock,
        "reorder_threshold": v.reorder_threshold, "target_stock_level": v.target_stock_level,
        "status": _variant_status(v), "last_counted_at": v.last_counted_at,
    }


def _get_product_by_sku(db: Session, sku: str, include_deleted: bool = False) -> models.Product:
    q = db.query(models.Product).options(
        selectinload(models.Product.variants).selectinload(models.Variant.cost_record),
        selectinload(models.Product.variants).selectinload(models.Variant.marketplace_aliases),
        selectinload(models.Product.cost_record),
        selectinload(models.Product.reference_images),
        joinedload(models.Product.vendor),
    ) \
        .filter(models.Product.sku == sku.strip().upper())
    if not include_deleted:
        q = q.filter(models.Product.is_deleted == False)  # noqa: E712
    product = q.first()
    if not product:
        raise HTTPException(404, f"Product '{sku}' not found")
    return product


def _get_variant_by_code(db: Session, variant_code: str, include_deleted: bool = False) -> models.Variant:
    code = variant_code.strip().upper()
    # Accept both legacy `SKU-SIZE` labels and the canonical `SKU_SIZE`
    # format. New variants are always created with the underscore format.
    candidates = [code]
    if "-" in code:
        candidates.append(code.replace("-", "_"))
    if "_" in code:
        candidates.append(code.rsplit("_", 1)[0] + "-" + code.rsplit("_", 1)[1])
    q = db.query(models.Variant).options(joinedload(models.Variant.product)) \
        .filter(models.Variant.variant_code.in_(list(dict.fromkeys(candidates))))
    if not include_deleted:
        q = q.filter(models.Variant.is_deleted == False)  # noqa: E712
    variant = q.first()
    if not variant:
        raise HTTPException(404, f"Variant '{variant_code}' not found")
    return variant


def _make_variant_code(sku: str, size: str) -> str:
    # Canonical stockable SKU: BASE_SKU_SIZE. Size is always the final token.
    return f"{sku.strip().upper()}_{size.strip().upper()}"


def _apply_variant_filters(query, q, status):
    V = models.Variant
    query = query.filter(V.is_deleted == False, models.Product.is_deleted == False)  # noqa: E712
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(models.Product.sku.ilike(like), models.Product.name.ilike(like), V.variant_code.ilike(like)))
    if status:
        s = status.strip().upper()
        counted = V.last_counted_at.isnot(None)
        if s == "UNCOUNTED":
            query = query.filter(V.last_counted_at.is_(None))
        elif s == "OUT":
            query = query.filter(counted, V.current_stock <= 0)
        elif s == "LOW":
            query = query.filter(counted, V.current_stock > 0, V.current_stock <= V.reorder_threshold)
        elif s == "OK":
            query = query.filter(counted, V.current_stock > V.reorder_threshold)
        elif s == "ATTENTION":
            query = query.filter(counted, V.current_stock <= V.reorder_threshold)
        else:
            raise HTTPException(400, "status must be one of UNCOUNTED, OUT, LOW, OK, ATTENTION")
    return query


# ------------------------------------------------------------------ #
# Live inventory (flat, filterable, paginated)
# ------------------------------------------------------------------ #
@app.get("/api/variants", response_model=schemas.VariantPage)
def list_variants(
    q: str | None = None, status: str | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    V = models.Variant
    filtered = _apply_variant_filters(db.query(V).join(V.product), q, status)
    total = filtered.with_entities(func.count(V.id)).scalar()
    rows = (
        filtered.options(contains_eager(V.product).joinedload(models.Product.vendor))
        .order_by(models.Product.sku, case(bulk_stock_service.SIZE_ORDER, value=func.upper(V.size), else_=99), V.size)
        .offset(offset).limit(limit).all()
    )
    return {"total": total, "limit": limit, "offset": offset, "items": [_variant_row(v) for v in rows]}


def _open_alert_count(db: Session) -> int:
    physical_codes = {
        code for (code,) in db.query(models.Variant.variant_code)
        .join(models.Alert, models.Alert.variant_id == models.Variant.id)
        .join(models.Product, models.Variant.product_id == models.Product.id)
        .filter(
            models.Alert.resolved == False,
            models.Variant.is_deleted == False,
            models.Product.is_deleted == False,
        ).all()
    }
    shortage_codes = {
        item["variant_code"] for item in reorder_service.generate_reorder_list(db, save_batch=False)
        # Unmapped order lines cannot be tied to a stock size, so they are
        # shown in Process Orders as SKU NOT FOUND rather than as Alerts.
        # Keep the badge consistent with _collect_alerts(), which omits them.
        if item["required_qty"] > 0 and not item.get("is_new")
    }
    return len(physical_codes | shortage_codes)


@app.get("/api/summary", response_model=schemas.SummaryOut)
def summary(db: Session = Depends(get_db)):
    V, P = models.Variant, models.Product
    live = (V.is_deleted == False, P.is_deleted == False)  # noqa: E712
    counted = V.last_counted_at.isnot(None)

    def count(*conds):
        return db.query(func.count(V.id)).join(P).filter(*live, *conds).scalar()

    return {
        "products": db.query(func.count(P.id)).filter(P.is_deleted == False).scalar(),  # noqa: E712
        "stock_units": int(db.query(func.coalesce(func.sum(V.current_stock), 0)).join(P).filter(*live, counted).scalar() or 0),
        "variants": count(),
        "uncounted": count(V.last_counted_at.is_(None)),
        "out": count(counted, V.current_stock <= 0),
        "low": count(counted, V.current_stock > 0, V.current_stock <= V.reorder_threshold),
        "ok": count(counted, V.current_stock > V.reorder_threshold),
        # One visible alert per affected variant. Physical low-stock alerts
        # and pending-order shortages can refer to the same variant, so do not
        # double-count them in the badge.
        "open_alerts": _open_alert_count(db),
    }


# ------------------------------------------------------------------ #
# Products (+ their size variants)
# ------------------------------------------------------------------ #
def _smart_search_tokens(value: str | None) -> list[str]:
    return list(dict.fromkeys(token for token in re.split(r"[^\w]+", (value or "").strip().casefold()) if len(token) > 1))


def _apply_smart_search(query, columns, value: str | None):
    """Match each search word against any identity/name field."""
    for token in _smart_search_tokens(value):
        like = f"%{token}%"
        query = query.filter(or_(*(column.ilike(like) for column in columns)))
    return query


def _fuzzy_product_score(value: str, product) -> float:
    """Score typo-tolerant product-name/SKU matches after exact SQL search misses."""
    wanted = _smart_search_tokens(value)
    candidates = _smart_search_tokens(" ".join((product.sku or "", product.name or "", product.category or "")))
    if not wanted or not candidates:
        return 0.0
    scores = []
    for token in wanted:
        best = 1.0 if any(token in candidate for candidate in candidates) else max(
            (SequenceMatcher(None, token, candidate).ratio() for candidate in candidates),
            default=0.0,
        )
        minimum = 1.0 if len(token) <= 2 else (0.82 if len(token) <= 4 else 0.70)
        if best < minimum:
            return 0.0
        scores.append(best)
    return sum(scores) / len(scores)


@app.get("/api/products")
def list_products(q: str | None = None, limit: int | None = Query(None, ge=1, le=500), offset: int = Query(0, ge=0), paged: bool = False, db: Session = Depends(get_db)):
    base = db.query(models.Product).options(
        selectinload(models.Product.variants).selectinload(models.Variant.cost_record),
        selectinload(models.Product.variants).selectinload(models.Variant.marketplace_aliases),
        selectinload(models.Product.cost_record),
        selectinload(models.Product.reference_images),
        joinedload(models.Product.vendor),
    ) \
        .filter(models.Product.is_deleted == False)  # noqa: E712
    query = _apply_smart_search(base, (models.Product.sku, models.Product.name, models.Product.category), q)
    # Paged clients need an exact count even when the search box is empty.
    # Without this, Products renders "null designs" and "1–0 of null" while
    # the first page itself contains valid products.
    exact_total = query.count() if paged or (q and q.strip()) else None
    total = exact_total if paged else None
    if q and q.strip() and exact_total == 0:
        fuzzy = [(score, product) for product in base.all() if (score := _fuzzy_product_score(q, product)) > 0]
        fuzzy.sort(key=lambda item: (item[0], item[1].created_at.isoformat() if item[1].created_at else "", item[1].id), reverse=True)
        total = len(fuzzy)
        if limit:
            fuzzy = fuzzy[offset:offset + limit]
        items = [_product_out(product) for _, product in fuzzy]
        return {"items": items, "total": total, "offset": offset, "limit": limit} if paged else items
    query = query.order_by(models.Product.created_at.desc(), models.Product.id.desc())
    if limit:
        query = query.offset(offset).limit(limit)
    items = [_product_out(p) for p in query.all()]
    return {"items": items, "total": total, "offset": offset, "limit": limit} if paged else items


def _daily_sales_summary(db: Session) -> dict:
    """Summarize units actually dispatched today in India Standard Time."""
    ist = timezone(timedelta(hours=5, minutes=30))
    today = datetime.now(ist).date()
    start_utc = datetime.combine(today, datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    end_utc = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=ist).astimezone(timezone.utc)
    T, V, P = models.InventoryTransaction, models.Variant, models.Product
    void_marker = aliased(models.InventoryTransaction)
    not_voided = ~db.query(void_marker.id).filter(
        void_marker.reference == func.replace(T.reference, ":dispatch", ":delete-reversal")
    ).exists()
    base = db.query(T).filter(
        T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
        T.created_at >= start_utc,
        T.created_at < end_utc,
        T.change_qty < 0,
        T.reference.like("order_upload:%:dispatch"),
        not_voided,
    )
    units = base.with_entities(func.coalesce(-func.sum(T.change_qty), 0)).scalar()
    line_count = base.with_entities(func.count(T.id)).scalar()
    design_count = (
        db.query(func.count(func.distinct(P.id)))
        .join(V, V.product_id == P.id).join(T, T.variant_id == V.id)
        .filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc, T.created_at < end_utc, T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"), not_voided,
        ).scalar()
    )
    rows = (
        db.query(P.sku, P.name, func.sum(-T.change_qty).label("units"))
        .join(V, V.product_id == P.id)
        .join(T, T.variant_id == V.id)
        .filter(
            T.transaction_type == models.TransactionType.ORDER_DEDUCTION,
            T.created_at >= start_utc,
            T.created_at < end_utc,
            T.change_qty < 0,
            T.reference.like("order_upload:%:dispatch"),
            not_voided,
        )
        .group_by(P.id, P.sku, P.name)
        .order_by(func.sum(-T.change_qty).desc(), P.sku.asc())
        .limit(5).all()
    )
    return {
        "date": today.isoformat(),
        "timezone": "Asia/Kolkata",
        "dispatched_units": int(units or 0),
        "dispatch_lines": int(line_count or 0),
        "designs_sold": int(design_count or 0),
        "top_products": [{"sku": row.sku, "name": row.name, "units": int(row.units or 0)} for row in rows],
    }


@app.get("/api/analytics/daily-sales")
def daily_sales_summary(db: Session = Depends(get_db)):
    return _daily_sales_summary(db)


@app.get("/api/products/bulk-template")
def bulk_product_template():
    content = bulk_product_service.build_bulk_product_template()
    return StreamingResponse(
        BytesIO(content), media_type=XLSX_MEDIA,
        headers={"Content-Disposition": "attachment; filename=bulk_product_template.xlsx", "Cache-Control":"private, no-store"},
    )


@app.post("/api/products/bulk", response_model=schemas.BulkProductImportResult)
async def bulk_create_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read(bulk_product_service.MAX_FILE_BYTES + 1)
    try:
        result = bulk_product_service.import_bulk_products(db, content, file.filename or "")
        if result.get("products_created") or result.get("variants_created"):
            excel_service.reconcile_unmapped_order_lines(db)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return result


@app.get("/api/products/{sku}", response_model=schemas.ProductOut)
def get_product(sku: str, db: Session = Depends(get_db)):
    return _product_out(_get_product_by_sku(db, sku))


@app.post("/api/products", response_model=schemas.ProductOut)
def create_product(payload: schemas.ProductCreate, db: Session = Depends(get_db)):
    sku = payload.sku.strip().upper()
    lock_catalog(db)
    if not sku or len(sku) > 64 or not design_key(payload.name) or len(payload.name) > 300:
        raise HTTPException(422, "Enter a valid Master SKU and product name.")
    existing = db.query(models.Product).filter(models.Product.sku == sku).first()
    if existing and not existing.is_deleted:
        raise HTTPException(400, f"SKU '{sku}' already exists.")
    if existing and existing.is_deleted:
        raise HTTPException(409, f"SKU '{sku}' exists in Trash. Restore it instead of creating a new one.")

    duplicate = duplicate_design(db, payload.name)
    if duplicate:
        raise HTTPException(409, duplicate_message(duplicate))
    if canonical_conflict(db, sku):
        raise HTTPException(409, f"Master SKU '{sku}' already identifies a linked SKU or size.")
    sizes_seen = set()
    for v in payload.variants:
        code = _make_variant_code(sku, v.size)
        if not v.size.strip() or len(v.size.strip()) > 20 or len(code) > 80:
            raise HTTPException(422, "Invalid size or size SKU length.")
        if canonical_conflict(db, code):
            raise HTTPException(409, f"Size SKU '{code}' already exists as another SKU or mapping.")
        if v.size.strip().upper() in sizes_seen:
            raise HTTPException(400, f"Duplicate size '{v.size}' in request.")
        sizes_seen.add(v.size.strip().upper())

    # Reject repeated external SKUs across product-wide / size-specific scopes
    # before opening the create transaction, so there is no ambiguous mapping.
    alias_codes = [a.sku.strip().upper() for a in payload.marketplace_aliases]
    if len(alias_codes) != len(set(alias_codes)):
        raise HTTPException(422, "Duplicate marketplace SKU in this product. Use one mapping per external SKU.")

    product = models.Product(sku=sku, name=payload.name, category=payload.category, vendor_id=payload.vendor_id)
    db.add(product)
    db.flush()

    now = datetime.now(timezone.utc)
    for v in payload.variants:
        variant = models.Variant(
            product_id=product.id, size=v.size.strip().upper(), variant_code=_make_variant_code(sku, v.size),
            current_stock=0, reorder_threshold=v.reorder_threshold, target_stock_level=v.target_stock_level,
            last_counted_at=now,
        )
        db.add(variant)
        db.flush()
        if v.initial_stock:
            inventory_service.apply_stock_change(db, variant, v.initial_stock, transaction_type=models.TransactionType.INITIAL_STOCK, reference="product created")
        alert_service.check_variant_alert(db, variant)

    # Costs and marketplace aliases share the same transaction as product creation.
    if payload.unit_cost is not None:
        upsert_product_cost(db, product.id, payload.unit_cost)
    for input_variant in payload.variants:
        saved = next(v for v in product.variants if v.size == input_variant.size.strip().upper())
        if input_variant.unit_cost is not None:
            upsert_variant_cost(db, saved.id, input_variant.unit_cost)
    for alias in payload.marketplace_aliases:
        if not alias.size:
            add_product_alias(db, product, alias.marketplace, alias.sku)
            continue
        saved = next((v for v in product.variants if v.size == alias.size.strip().upper()), None)
        if not saved:
            raise HTTPException(422, f"Alias size {alias.size} is not part of this product")
        add_alias(db, saved, alias.marketplace, alias.sku)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(product)
    return _product_out(product)


@app.patch("/api/products/{sku}", response_model=schemas.ProductOut)
def update_product(sku: str, payload: schemas.ProductUpdate, db: Session = Depends(get_db)):
    lock_catalog(db)
    product = _get_product_by_sku(db, sku)
    _apply_product_update(db, product, payload)
    db.commit()
    db.refresh(product)
    return _product_out(product)


def _apply_product_update(db, product, payload):
    """Apply normal duplicate/SKU validation without committing."""
    data = payload.model_dump(exclude_unset=True)
    if "name" in data:
        if not design_key(data["name"]) or len(data["name"]) > 300:
            raise HTTPException(422, "Enter a valid product name.")
        if design_key(data["name"]) != design_key(product.name):
            duplicate = duplicate_design(db, data["name"], exclude_id=product.id)
            if duplicate:
                raise HTTPException(409, duplicate_message(duplicate))
    cost = data.pop("unit_cost", None)
    if cost is not None:
        upsert_product_cost(db, product.id, cost)

    # SKU is the single canonical identity used by Products, Bulk Stock,
    # Reorder and new marketplace-order matching. If the user edits it,
    # regenerate every size's stockable Variant code from the new SKU too.
    if "sku" in data:
        new_sku = str(data.pop("sku") or "").strip().upper()
        if not new_sku:
            raise HTTPException(400, "SKU cannot be empty.")
        if new_sku != product.sku:
            conflict = db.query(models.Product).filter(
                models.Product.sku == new_sku, models.Product.id != product.id
            ).first()
            if conflict:
                raise HTTPException(409, f"SKU '{new_sku}' already exists.")
            if canonical_conflict(db, new_sku, product_id=product.id):
                raise HTTPException(409, f"SKU '{new_sku}' already identifies a linked SKU or size.")
            for variant in product.variants:
                if variant.is_deleted:
                    continue
                new_code = _make_variant_code(new_sku, variant.size)
                if len(new_code) > 80 or canonical_conflict(db, new_code, product_id=product.id, variant_id=variant.id):
                    raise HTTPException(409, f"Size SKU '{new_code}' conflicts with an existing SKU or mapping.")
                variant.variant_code = new_code
            product.sku = new_sku

    for field, value in data.items():
        setattr(product, field, value)


@app.post("/api/products/{sku}/editor-save", response_model=schemas.ProductOut)
def save_product_editor(
    sku: str, background_tasks: BackgroundTasks,
    metadata: str = Form(...), expected: str = Form(...), gallery: str = Form(...),
    files: list[UploadFile] = File(default=[]), db: Session = Depends(get_db),
):
    """Commit the editor fields and complete staged photo list in one transaction."""
    try:
        payload = schemas.ProductUpdate.model_validate_json(metadata)
        baseline, entries = json.loads(expected), json.loads(gallery)
        if not isinstance(baseline, list) or not all(isinstance(x, str) for x in baseline):
            raise ValueError('Invalid original photos')
        if not isinstance(entries, list) or len(entries) > 8 or len(files) > 8:
            raise ValueError('Choose up to 8 photos per product')
        if not all(type(x) is int or isinstance(x, str) for x in entries):
            raise ValueError('Invalid photo selection')
        if len(set(entries)) != len(entries) or sorted(x for x in entries if type(x) is int) != list(range(len(files))):
            raise ValueError('Invalid photo selection')
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, 'Invalid editor data. Check product details and the 8-photo limit.') from exc
    lock_catalog(db)
    product = _get_product_by_sku(db, sku)
    current = _product_out(product)['images']
    if baseline != current:
        raise HTTPException(409, 'Photos changed in another session. Reopen the product before saving.')
    if any(isinstance(x, str) and x not in current for x in entries):
        raise HTTPException(422, 'Photo does not belong to this product.')
    prepared = []
    try:
        for file in files:
            content = file.file.read(visual_match_service.MAX_SCAN_BYTES + 1)
            visual_match_service.validate_image(content)
            prepared.append((content, visual_match_service.image_extension(content)))
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    created = []
    removed_ids = []
    removed_paths = set(current) - {x for x in entries if isinstance(x, str)}
    removable_files = []
    try:
        _apply_product_update(db, product, payload)
        refs = {ref.image_path: ref for ref in product.reference_images}
        paths = []
        for entry in entries:
            if isinstance(entry, str):
                ref = refs.get(entry)
                if ref is None:  # legacy main photo without a reference record
                    ref = models.ProductImage(product_id=product.id, image_path=entry, is_primary=False)
                    db.add(ref)
                    refs[entry] = ref
                paths.append(entry)
            else:
                content, ext = prepared[entry]
                fname = f"{re.sub(r'[^A-Z0-9_-]', '_', product.sku.upper())[:55]}_{uuid.uuid4().hex}{ext}"
                dest = seller_image_dir() / fname
                path = f'/static/product_images/{fname}'
                ref = models.ProductImage(product_id=product.id, image_path=path, is_primary=False)
                created.append((ref, content, dest))
                dest.write_bytes(content)
                db.add(ref)
                refs[path] = ref
                paths.append(path)
        product.image_path = paths[0] if paths else None
        for path, ref in refs.items():
            if path not in paths:
                removed_ids.append(ref.id)
                product.reference_images.remove(ref)
            else:
                ref.is_primary = path == product.image_path
        db.flush()
        for path in removed_paths:
            if not (db.query(models.Product.id).filter(models.Product.image_path == path).first()
                    or db.query(models.ProductImage.id).filter(models.ProductImage.image_path == path).first()):
                if path.startswith('/static/product_images/') and re.fullmatch(r'[a-zA-Z0-9_.-]{1,250}', Path(path).name) and not Path(path).name.startswith('.'):
                    removable_files.append(seller_image_dir() / Path(path).name)
        product_id = product.id
        pending_index = [(ref.id, content, dest) for ref, content, dest in created]
        db.commit()
    except Exception:
        db.rollback()
        for _, _, dest in created:
            dest.unlink(missing_ok=True)
        raise
    if db.get_bind().dialect.name == 'sqlite':
        for image_id in removed_ids:
            visual_search_service.remove_product_embedding(db, product_id, image_id)
    visual_search_service.clear_visual_cache()
    for dest in removable_files:
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            logger.warning('Editor photo reference removed; file cleanup failed')
    for image_id, content, dest in pending_index:
        background_tasks.add_task(_index_product_photo_background, product_id, image_id, content, str(dest), int(tenant_id()))
    db.expire_all()
    return _product_out(_get_product_by_sku(db, product.sku))


@app.post("/api/products/{sku}/variants", response_model=schemas.ProductOut)
def add_size(sku: str, payload: schemas.VariantCreate, db: Session = Depends(get_db)):
    """Adds one more size to a design that already exists - for a size that
    was missed when the product was first added."""
    product = _get_product_by_sku(db, sku)
    size = payload.size.strip().upper()
    code = _make_variant_code(sku, size)

    existing = db.query(models.Variant).filter(models.Variant.product_id == product.id, models.Variant.size == size).first()
    if existing and not existing.is_deleted:
        raise HTTPException(400, f"Size '{size}' already exists on {sku}.")
    if existing and existing.is_deleted:
        existing.is_deleted = False
        existing.deleted_at = None
        variant = existing
    else:
        variant = models.Variant(
            product_id=product.id, size=size, variant_code=code, current_stock=0,
            reorder_threshold=payload.reorder_threshold, target_stock_level=payload.target_stock_level,
        )
        db.add(variant)
    db.flush()
    if payload.initial_stock:
        inventory_service.apply_stock_change(db, variant, payload.initial_stock, transaction_type=models.TransactionType.INITIAL_STOCK, reference="size added")
        variant.last_counted_at = datetime.now(timezone.utc)
    alert_service.check_variant_alert(db, variant)
    if payload.unit_cost is not None:
        upsert_variant_cost(db, variant.id, payload.unit_cost)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(product)
    return _product_out(product)


@app.patch("/api/variants/{variant_code}", response_model=schemas.VariantOut)
def update_variant(variant_code: str, payload: schemas.VariantUpdate, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code)
    data = payload.model_dump(exclude_unset=True)
    cost = data.pop("unit_cost", None)
    if cost is not None:
        upsert_variant_cost(db, variant.id, cost)
    for field, value in data.items():
        setattr(variant, field, value)
    db.flush()
    alert_service.check_variant_alert(db, variant)
    db.commit()
    # Variant metadata may add a seller-SKU alias that lets old batches resolve.
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(variant)
    return _variant_out(variant)


@app.post("/api/products/{sku}/image")
def upload_product_image(
    sku: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    lock_catalog(db)
    product = _get_product_by_sku(db, sku)
    content = file.file.read(visual_match_service.MAX_SCAN_BYTES + 1)
    try:
        visual_match_service.validate_image(content)
        ext = visual_match_service.image_extension(content)
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    fname = f"{re.sub(r'[^A-Z0-9_-]', '_', product.sku.upper())[:55]}_{uuid.uuid4().hex[:8]}{ext}"
    dest = seller_image_dir() / fname
    dest.write_bytes(content)
    old_path = product.image_path
    primary_ref = db.query(models.ProductImage).filter(
        models.ProductImage.product_id == product.id, models.ProductImage.is_primary == True  # noqa: E712
    ).first()
    product.image_path = f"/static/product_images/{fname}"
    if primary_ref:
        primary_ref.image_path = product.image_path
    else:
        primary_ref = models.ProductImage(product_id=product.id, image_path=product.image_path, is_primary=True)
        db.add(primary_ref)
    try:
        db.flush()
        db.commit()
    except Exception:
        db.rollback()
        dest.unlink(missing_ok=True)
        raise
    background_tasks.add_task(_index_product_photo_background, product.id, primary_ref.id, content, str(dest), int(tenant_id()))
    if old_path:
        try:
            old_name = Path(old_path).name
            (seller_image_dir() / old_name).unlink(missing_ok=True)
        except Exception:
            pass
    return {"image_path": product.image_path}


@app.post("/api/products/{sku}/images")
async def upload_product_reference_images(
    sku: str,
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Attach up to eight genuine extra views to this product, never to stock rows."""
    lock_catalog(db)
    product = _get_product_by_sku(db, sku)
    if not files or len(files) > 8:
        raise HTTPException(400, "Choose between 1 and 8 reference photos.")
    saved: list[tuple[models.ProductImage, bytes, Path]] = []
    try:
        for file in files:
            content = await file.read(visual_match_service.MAX_SCAN_BYTES + 1)
            visual_match_service.validate_image(content)
            ext = visual_match_service.image_extension(content)
            fname = f"{re.sub(r'[^A-Z0-9_-]', '_', product.sku.upper())[:55]}_{uuid.uuid4().hex[:8]}{ext}"
            dest = seller_image_dir() / fname
            dest.write_bytes(content)
            image = models.ProductImage(
                product_id=product.id,
                image_path=f"/static/product_images/{fname}",
                is_primary=False,
            )
            db.add(image)
            saved.append((image, content, dest))
        # Adding photos to an empty gallery also sets the first as its main photo.
        if not product.image_path and saved:
            product.image_path = saved[0][0].image_path
            saved[0][0].is_primary = True
        db.flush()
        db.commit()
    except visual_match_service.InvalidProductImage as exc:
        db.rollback()
        for _, _, dest in saved:
            dest.unlink(missing_ok=True)
        raise HTTPException(400, str(exc)) from exc
    except Exception:
        db.rollback()
        for _, _, dest in saved:
            dest.unlink(missing_ok=True)
        raise

    for image, content, dest in saved:
        background_tasks.add_task(_index_product_photo_background, product.id, image.id, content, str(dest), int(tenant_id()))
    return {"saved": len(saved), "queued_for_indexing": len(saved), "message": "Photos saved. Visual indexing will finish in the background."}


@app.delete("/api/products/{sku}/images/{filename}")
def delete_product_photo(sku: str, filename: str, db: Session = Depends(get_db)):
    """Remove one catalog photo, not the product or its inventory.

    The authenticated middleware takes a recovery snapshot before DELETE.
    Never use a caller-supplied filesystem path or another product's photo.
    """
    if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,250}', filename) or filename.startswith('.'):
        raise HTTPException(404, "Photo not found")
    path = f"/static/product_images/{filename}"
    lock_catalog(db)
    product = _get_product_by_sku(db, sku)
    removed = [ref for ref in product.reference_images if ref.image_path == path]
    if product.image_path != path and not removed:
        raise HTTPException(404, "Photo not found on this product")
    removed_ids = [ref.id for ref in removed]
    remaining = sorted((ref for ref in product.reference_images if ref.image_path != path),
                       key=lambda ref: (not ref.is_primary, ref.id))
    if product.image_path == path or not product.image_path:
        product.image_path = remaining[0].image_path if remaining else None
    for ref in remaining:
        ref.is_primary = ref.image_path == product.image_path
    for ref in removed:
        product.reference_images.remove(ref)
    db.flush()
    # Legacy data may reference the same file from another product; don't
    # remove its physical file while it is still referenced.
    still_used = (db.query(models.Product.id).filter(models.Product.image_path == path).first()
                  or db.query(models.ProductImage.id).filter(models.ProductImage.image_path == path).first())
    db.commit()
    # PostgreSQL's index has an ON DELETE CASCADE FK; SQLite caches are local.
    if db.get_bind().dialect.name == 'sqlite':
        for image_id in removed_ids:
            visual_search_service.remove_product_embedding(db, product.id, image_id)
    visual_search_service.clear_visual_cache()
    if not still_used:
        try:
            (seller_image_dir() / filename).unlink(missing_ok=True)
        except OSError:
            logger.warning("Photo reference removed, but physical file cleanup failed")
    return _product_out(_get_product_by_sku(db, sku))


@app.post("/api/products/visual-match")
async def visual_product_match(
    request: Request,
    file: UploadFile = File(...),
    limit: int = Query(5, ge=1, le=10),
    db: Session = Depends(get_db),
):
    """Return a verified, ambiguous, or no-match result; this route never changes stock."""
    client_key = request.client.host if request.client else "unknown"
    if not visual_search_service.allow_scan(client_key):
        raise HTTPException(429, "Too many image searches. Please wait a minute and try again.")
    content = await file.read(visual_match_service.MAX_SCAN_BYTES + 1)
    try:
        visual_match_service.validate_image(content)
        result = await asyncio.to_thread(visual_search_service.search, db, content, seller_image_dir(), limit)
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    except (visual_search_service.VisualSearchUnavailable, visual_match_service.ImageModelUnavailable) as exc:
        raise HTTPException(503, f'Visual matching model unavailable: {exc}') from exc
    product_ids = [hit.product_id for hit in result.matches]
    products = db.query(models.Product).options(selectinload(models.Product.variants)).filter(
        models.Product.id.in_(product_ids), models.Product.is_deleted == False  # noqa: E712
    ).all() if product_ids else []
    by_id = {product.id: product for product in products}
    matches = []
    for hit in result.matches:
        product = by_id.get(hit.product_id)
        if product:
            matches.append({
                "similarity": round(hit.similarity, 4),
                "reference_image_path": hit.reference_image_path,
                "product": _product_out(product),
            })
    status = result.status if matches else "no_match"
    message = result.message if matches else "No active catalog match is available. Check the product photos or try another photo."
    return {
        "ranked_designs": len(product_ids),
        "status": status,
        "matches": matches,
        "message": message,
        "score_type": "cosine_similarity_not_probability",
        "matching_mode": (
            "SigLIP 2 semantic image embeddings" if engine.dialect.name == "postgresql"
            or os.getenv("VISUAL_AI_URL", "").strip()
            else "local color/layout suggestions"
        ),
        "notice": "Only a matched result is a strong visual match. Ambiguous results are suggestions; photo search never changes stock.",
    }


@app.get("/api/visual-search/status")
def visual_search_status(db: Session = Depends(get_db)):
    """Report safe setup diagnostics for photo search without exposing secrets."""
    dialect = engine.dialect.name
    catalog_product_count = 0
    try:
        photo_count = db.query(models.ProductImage).join(models.Product).filter(
            models.Product.is_deleted == False  # noqa: E712
        ).count()
        catalog_product_count = db.query(models.Product).filter(
            models.Product.is_deleted == False  # noqa: E712
        ).count()
    except Exception:
        db.rollback()
        photo_count = 0

    pgvector_index_exists = False
    pgvector_extension_installed = False
    indexed_photo_count = 0
    postgres_connected = False
    if dialect == "postgresql":
        try:
            db.execute(text("SELECT 1"))
            postgres_connected = True
        except Exception:
            db.rollback()
    if postgres_connected:
        try:
            pgvector_extension_installed = bool(db.execute(text(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')"
            )).scalar())
            pgvector_index_exists = bool(db.execute(
                text("SELECT to_regclass(:table_name) IS NOT NULL"),
                {"table_name": visual_search_service.TABLE_NAME},
            ).scalar())
            if pgvector_index_exists:
                indexed_photo_count = int(db.execute(
                    text(f"SELECT count(*) FROM {visual_search_service.TABLE_NAME} WHERE model_name = :model"),
                    {"model": visual_search_service.MODEL_NAME},
                ).scalar() or 0)
        except Exception:
            db.rollback()
            pgvector_index_exists = False

    runtime_installed = bool(find_spec("torch") and find_spec("transformers"))
    model_weights_loaded = visual_match_service._encoder is not None
    external_model_service = bool(os.getenv("VISUAL_AI_URL", "").strip())
    external_model_service_ready = False
    if external_model_service and os.getenv("VISUAL_AI_API_KEY", "").strip():
        try:
            import httpx
            service_url = os.getenv("VISUAL_AI_URL", "").strip().rstrip("/")
            response = httpx.get(f"{service_url}/health", timeout=1.5)
            response.raise_for_status()
            health = response.json()
            external_model_service_ready = (
                health.get("status") == "ready"
                and health.get("model") == visual_search_service.MODEL_NAME
            )
        except Exception:
            external_model_service_ready = False
    needs_rebuild = dialect == "postgresql" and (
        not pgvector_index_exists or indexed_photo_count < photo_count
    )
    local_matcher_available = dialect != "postgresql"
    model_available = (
        local_matcher_available and not external_model_service
        or runtime_installed and not external_model_service
        or external_model_service_ready
    )
    if dialect == "postgresql":
        index_ready = (
            postgres_connected
            and pgvector_extension_installed
            and pgvector_index_exists
            and photo_count > 0
            and indexed_photo_count >= photo_count
        )
    else:
        # SQLite uses the Pillow fallback unless the private SigLIP service is
        # configured. With the service, query and catalog photos share semantic
        # embeddings, allowing packaged and model-view photos to match.
        index_ready = photo_count > 0 and catalog_product_count <= visual_search_service.DEV_MAX_CATALOG
    if dialect != "postgresql":
        if not os.getenv("DATABASE_URL", "").strip():
            if external_model_service_ready:
                message = f"Database: SQLite local mode. SigLIP 2 semantic matching via the private model service is configured for up to {visual_search_service.DEV_MAX_CATALOG} designs. Catalog photos: {photo_count}."
            elif external_model_service:
                message = "SQLite is available, but the private SigLIP service is not ready or its key is missing. Check VISUAL_AI_URL, VISUAL_AI_API_KEY, and the model service /health endpoint."
            else:
                message = f"Database: SQLite local mode. Lightweight color/layout suggestions are available for up to {visual_search_service.DEV_MAX_CATALOG} designs. For packaged-photo to model-photo matching, configure the private SigLIP model service. Catalog photos: {photo_count}."
            if os.getenv("RAILWAY_ENVIRONMENT", "").strip():
                message += " Railway is not connected to PostgreSQL (DATABASE_URL is missing); connect the PostgreSQL service before relying on durable production inventory."
        else:
            mode_text = "SigLIP 2 semantic matching via the private model service" if external_model_service_ready else "lightweight color/layout suggestions; configure a ready private SigLIP model service for packaged-photo matching"
            message = f"Database: {dialect}. {mode_text} are available for up to {visual_search_service.DEV_MAX_CATALOG} designs; use PostgreSQL with pgvector for larger catalogs. Catalog photos: {photo_count}."
    elif not postgres_connected:
        message = "Database URL is PostgreSQL, but the app cannot connect to it. Check the Railway PostgreSQL service and DATABASE_URL reference."
    elif not pgvector_extension_installed:
        message = "Database: PostgreSQL connected; pgvector extension is missing. Enable vector on this database, then rebuild the image index."
    elif photo_count == 0:
        message = "Database: PostgreSQL with pgvector connected; no catalog reference photos are saved yet. Add clear product photos first."
    elif needs_rebuild:
        message = f"Database: PostgreSQL with pgvector connected; {indexed_photo_count} of {photo_count} photos are indexed. Rebuild the image index from this app service."
    elif not model_weights_loaded and not external_model_service:
        message = "Catalog photo index is ready. SigLIP weights will load the first time you search a photo."
    else:
        message = "Visual model runtime and catalog photo index are ready."
    cloud_vision_fallback = False
    if not model_available:
        if external_model_service:
            message += " The configured private SigLIP service is not ready or its key is missing."
        else:
            message += " SigLIP runtime is not installed and no private model service is configured."

    return {
        "status": "ready" if (index_ready and model_available) or cloud_vision_fallback else "unavailable",
        "database": dialect,
        "database_url_configured": bool(os.getenv("DATABASE_URL", "").strip()),
        "postgres_connected": postgres_connected,
        "pgvector_extension_installed": pgvector_extension_installed,
        "index_ready": index_ready or cloud_vision_fallback,
        "cloud_vision_fallback": cloud_vision_fallback,
        "claude_vision_fallback": False,
        "runtime_installed": runtime_installed,
        "model_weights_loaded": model_weights_loaded,
        "local_matcher_available": local_matcher_available,
        "external_model_service_configured": external_model_service,
        "external_model_service_ready": external_model_service_ready,
        "semantic_matching_available": (dialect == "postgresql" and model_available) or external_model_service_ready,
        "pgvector_index_exists": pgvector_index_exists,
        "catalog_photos": photo_count,
        "indexed_photos": indexed_photo_count,
        "needs_rebuild": needs_rebuild,
        "message": message,
    }


# ------------------------------------------------------------------ #
# Seller recovery center. File download is authenticated and tenant-scoped.
# ------------------------------------------------------------------ #
@app.get("/api/recovery/snapshots")
def recovery_snapshot_list():
    return {"items": recovery_service.list_snapshots(int(tenant_id())),
            "message": "Private restore points are stored on this seller's Railway volume. Download regularly for off-server backups."}


@app.post("/api/recovery/snapshots", status_code=201)
def recovery_snapshot_create():
    try:
        return recovery_service.create_snapshot(int(tenant_id()), "Manual recovery point")
    except Exception as exc:
        logger.exception("Manual recovery snapshot failed")
        raise HTTPException(503, "Recovery snapshot could not be saved. Check volume space and permissions.") from exc


@app.get("/api/recovery/snapshots/{name}/summary")
def recovery_snapshot_summary(name: str):
    try:
        return recovery_service.snapshot_summary(int(tenant_id()), name)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Recovery point not found") from exc


@app.get("/api/recovery/snapshots/{name}/archive")
def recovery_snapshot_archive(name: str):
    try:
        archive = recovery_service.prepare_download_archive(int(tenant_id()), name)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Recovery point not found") from exc
    except Exception as exc:
        logger.exception("Unable to create recovery download archive")
        raise HTTPException(503, "Recovery archive unavailable; check persistent volume space") from exc
    return FileResponse(
        archive, media_type="application/zip", filename=name.replace(".sqlite3", "-backup.zip"),
        background=BackgroundTask(lambda: archive.unlink(missing_ok=True)),
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.get("/api/recovery/snapshots/{name}/download")
def recovery_snapshot_download(name: str):
    try:
        path = recovery_service.snapshot_path(int(tenant_id()), name)
    except FileNotFoundError as exc:
        raise HTTPException(404, "Recovery point not found") from exc
    return FileResponse(path, media_type="application/octet-stream", filename=path.name,
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


# ------------------------------------------------------------------ #
# Trash: soft delete + restore
# ------------------------------------------------------------------ #
@app.delete("/api/products/{sku}", response_model=schemas.DeleteResult)
def delete_product(sku: str, db: Session = Depends(get_db)):
    """Moves a design and all its sizes to Trash. Stock, alerts and reorder
    lines stop counting it immediately; full history is kept and it can be
    restored later from the Trash view."""
    product = _get_product_by_sku(db, sku)
    now = datetime.now(timezone.utc)
    count = 0
    for v in product.variants:
        if not v.is_deleted:
            v.is_deleted = True
            v.deleted_at = now
            open_alert = db.query(models.Alert).filter(models.Alert.variant_id == v.id, models.Alert.resolved == False).first()  # noqa: E712
            if open_alert:
                open_alert.resolved = True
                open_alert.resolved_at = now
            count += 1
    product.is_deleted = True
    product.deleted_at = now
    db.commit()
    return {"deleted": product.sku, "trashed": True, "sizes_removed": count}


@app.post("/api/products/{sku}/restore", response_model=schemas.RestoreResult)
def restore_product(sku: str, db: Session = Depends(get_db)):
    product = _get_product_by_sku(db, sku, include_deleted=True)
    if not product.is_deleted or db.query(business_models.HiddenTrashProduct).filter_by(product_id=product.id).first():
        raise HTTPException(404, "Product is not in seller Trash.")
    product.is_deleted = False
    product.deleted_at = None
    count = 0
    for v in product.variants:
        if v.is_deleted:
            v.is_deleted = False
            v.deleted_at = None
            count += 1
        alert_service.check_variant_alert(db, v)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"restored": product.sku, "variants_restored": count}


@app.delete("/api/variants/{variant_code}", response_model=schemas.DeleteResult)
def delete_variant(variant_code: str, db: Session = Depends(get_db)):
    """Archive only the selected size. The product remains active even with zero sizes."""
    variant = _get_variant_by_code(db, variant_code)
    now = datetime.now(timezone.utc)
    variant.is_deleted = True
    variant.deleted_at = now
    open_alert = db.query(models.Alert).filter(models.Alert.variant_id == variant.id, models.Alert.resolved == False).first()  # noqa: E712
    if open_alert:
        open_alert.resolved = True
        open_alert.resolved_at = now
    db.commit()
    return {"deleted": variant.variant_code, "trashed": True, "product_also_deleted": False}


@app.post("/api/variants/{variant_code}/restore", response_model=schemas.RestoreResult)
def restore_variant(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code, include_deleted=True)
    if db.query(business_models.HiddenTrashProduct).filter_by(product_id=variant.product_id).first():
        raise HTTPException(404, "Product is not in seller Trash.")
    if not variant.is_deleted:
        raise HTTPException(400, f"'{variant_code}' is not in Trash.")
    variant.is_deleted = False
    variant.deleted_at = None
    if variant.product.is_deleted:
        variant.product.is_deleted = False
        variant.product.deleted_at = None
    alert_service.check_variant_alert(db, variant)
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"restored": variant.variant_code}


@app.get("/api/trash")
def list_trash(limit: int | None = Query(None, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = (
        db.query(models.Product).options(selectinload(models.Product.variants))
        .filter(models.Product.is_deleted == True)  # noqa: E712
        .filter(~models.Product.id.in_(db.query(business_models.HiddenTrashProduct.product_id)))
        .order_by(models.Product.deleted_at.desc())
    )
    total = query.count()
    products = query.offset(offset).limit(limit).all() if limit else query.all()
    items = [{
        "sku": p.sku, "name": p.name, "image_path": p.image_path,
        "variant_count": len(p.variants), "deleted_at": p.deleted_at,
    } for p in products]
    return {"total": total, "limit": limit, "offset": offset, "items": items} if limit is not None else items


@app.delete("/api/trash/{sku}")
def permanently_delete(sku: str, db: Session = Depends(get_db)):
    """Do not destroy stock history or related orders from seller Trash."""
    product = _get_product_by_sku(db, sku, include_deleted=True)
    if not product.is_deleted:
        raise HTTPException(400, "Move the product to Trash first.")
    if not db.query(business_models.HiddenTrashProduct).filter_by(product_id=product.id).first():
        db.add(business_models.HiddenTrashProduct(product_id=product.id))
        db.commit()
    return {"hidden": True, "retained_on_server": True, "sku": sku}


# ------------------------------------------------------------------ #
# Variant lookup (scan screen) + counting / adjustment
# ------------------------------------------------------------------ #
@app.get("/api/variants/{variant_code}", response_model=schemas.VariantLookupOut)
def lookup_variant(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code)
    return {
        "variant_code": variant.variant_code, "sku": variant.product.sku, "size": variant.size,
        "product_name": variant.product.name, "current_stock": variant.current_stock,
        "counted": variant.last_counted_at is not None, "image_path": variant.product.image_path,
    }


@app.get("/api/variants/{variant_code}/product", response_model=schemas.ProductOut)
def product_for_variant(variant_code: str, db: Session = Depends(get_db)):
    """Resolve a scanned size to its parent design; the client limits barcode
    editing to that size while product QR/manual search show the full design."""
    variant = _get_variant_by_code(db, variant_code)
    return _product_out(_get_product_by_sku(db, variant.product.sku))


@app.post("/api/inventory/adjust", response_model=schemas.VariantOut)
def adjust_stock(payload: schemas.StockAdjustment, db: Session = Depends(get_db)):
    if payload.operation not in {"set", "delta"}:
        raise HTTPException(400, "Operation must be 'set' or 'delta'.")
    if payload.operation == "set":
        if payload.new_count is None:
            raise HTTPException(400, "Enter the final physical stock count.")
        if payload.new_count < 0:
            raise HTTPException(400, "Count cannot be negative.")
    else:
        if payload.delta is None or payload.delta == 0:
            raise HTTPException(400, "Enter a non-zero quantity to add or remove.")
        if payload.expected_current_stock is None:
            raise HTTPException(400, "Current stock is required for a safe add/remove update.")
    variant = _get_variant_by_code(db, payload.variant_code)
    variant = db.query(models.Variant).filter(models.Variant.id == variant.id).populate_existing().with_for_update().one()
    if payload.expected_current_stock is not None and variant.current_stock != payload.expected_current_stock:
        raise HTTPException(409, f"Stock changed since this update was prepared. Current stock is {variant.current_stock}; refresh and try again.")
    try:
        if payload.operation == "set":
            inventory_service.set_absolute_stock(
                db, variant, payload.new_count, note=payload.note,
                expected_current_stock=payload.expected_current_stock,
            )
        else:
            new_count = variant.current_stock + payload.delta
            if new_count < 0:
                raise HTTPException(400, f"Cannot remove {abs(payload.delta)} units; only {variant.current_stock} are in stock.")
            inventory_service.apply_stock_change(
                db,
                variant,
                payload.delta,
                transaction_type=(models.TransactionType.RESTOCK if payload.delta > 0 else models.TransactionType.MANUAL_ADJUSTMENT),
                reference=payload.note,
                created_by="stock adjustment",
                expected_current_stock=payload.expected_current_stock,
            )
            variant.last_counted_at = datetime.now(timezone.utc)
            db.flush()
    except inventory_service.StaleStockError as exc:
        raise HTTPException(409, f"Stock changed since this update was prepared. Current stock is {exc.current}; refresh and try again.") from exc
    alert_service.check_variant_alert(db, variant)
    db.commit()
    # Map older marketplace lines now that stock has been counted or adjusted;
    # the next history refresh can show precisely which pieces are dispatchable.
    excel_service.reconcile_unmapped_order_lines(db)
    db.refresh(variant)
    return _variant_out(variant)


@app.get("/api/inventory/count-sheet")
def count_sheet(only_uncounted: bool = False, db: Session = Depends(get_db)):
    xlsx_bytes = bulk_stock_service.build_count_sheet(db, only_uncounted=only_uncounted)
    return StreamingResponse(BytesIO(xlsx_bytes), media_type=XLSX_MEDIA, headers={"Content-Disposition": "attachment; filename=count_sheet.xlsx"})


@app.post("/api/inventory/bulk-set", response_model=schemas.BulkSetResult)
async def bulk_set(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        result = bulk_stock_service.bulk_set_stock(db, content, file.filename or "")
        if result.get("updated_count") or result.get("created_variants_count"):
            excel_service.reconcile_unmapped_order_lines(db)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/variants/{variant_code}/transactions", response_model=list[schemas.TransactionOut])
def variant_transactions(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code, include_deleted=True)
    return (
        db.query(models.InventoryTransaction).filter(models.InventoryTransaction.variant_id == variant.id)
        .order_by(models.InventoryTransaction.created_at.desc(), models.InventoryTransaction.id.desc()).limit(200).all()
    )


@app.get("/api/transactions")
def global_transactions(
    q: str | None = None, transaction_type: str | None = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    paged: bool = False,
    db: Session = Depends(get_db),
):
    """Every stock change across the whole catalog, newest first - the
    'who/what/when/how much changed' activity log."""
    T, V, P = models.InventoryTransaction, models.Variant, models.Product
    query = db.query(T).join(V, T.variant_id == V.id).join(P, V.product_id == P.id)
    if q and q.strip():
        like = f"%{q.strip()}%"
        query = query.filter(or_(P.sku.ilike(like), P.name.ilike(like), V.variant_code.ilike(like)))
    if transaction_type:
        query = query.filter(T.transaction_type == transaction_type)
    total = query.order_by(None).count() if paged else None
    rows = query.options(contains_eager(T.variant).contains_eager(V.product)) \
        .order_by(T.created_at.desc(), T.id.desc()).offset(offset).limit(limit).all()
    items = [{
        "id": t.id, "variant_code": t.variant.variant_code, "sku": t.variant.product.sku, "size": t.variant.size,
        "product_name": t.variant.product.name, "change_qty": t.change_qty, "transaction_type": t.transaction_type.value,
        "reference": t.reference, "balance_after": t.balance_after, "created_by": t.created_by, "created_at": t.created_at,
    } for t in rows]
    return {"items": items, "total": total, "offset": offset, "limit": limit} if paged else items



# ------------------------------------------------------------------ #
# Packing picklist: independent, read-only Excel preparation workflow.
# This endpoint never imports/dispatches orders or writes inventory records.
# Operator authentication + same-origin checks are handled by /api/ middleware.
# ------------------------------------------------------------------ #
@app.post("/api/packing-picklist/preview")
async def packing_picklist_preview(
    file: UploadFile = File(...),
    brand_override: str = Form("AUTO"),
    picklist_date: str = Form(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    db: Session = Depends(get_db),
):
    data = await file.read(packing_picklist_service.MAX_UPLOAD_BYTES + 1)
    try:
        result = packing_picklist_service.parse_upload(
            data, file.filename or "", brand_override=brand_override,
            picklist_date=picklist_date or None,
        )
        result = packing_picklist_service.apply_registered_master_skus(result, db)
    except packing_picklist_service.PicklistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Page over brand-aware merged rows, not source or cross-brand rows.
    items = sorted(result["brand_items"], key=lambda r: (
        r["brand"], packing_picklist_service.party_rank(r["party"]),
        r["design"], packing_picklist_service._size_order(r["size"]),
    ))
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    return {
        "source_rows": result["source_rows"], "total_qty": result["total_qty"],
        "merged_rows": result["merged_rows"], "review_count": result["review_count"],
        "master_sku_mapped_rows": result["master_sku_mapped_rows"],
        "master_sku_mapped_qty": result["master_sku_mapped_qty"],
        "picklist_date": result["picklist_date"],
        "party_totals": result["party_totals"], "brand_totals": result["brand_totals"],
        "sample_items": items[(page - 1) * page_size : page * page_size],
        "preview_page": page, "preview_total_pages": total_pages,
        "preview_total_rows": total,
        "quantity_reconciled": True,
    }


@app.post("/api/packing-picklist/export")
async def packing_picklist_export(
    file: UploadFile = File(...),
    brand_override: str = Form("AUTO"),
    picklist_date: str = Form(""),
    include_audit: bool = Form(False),
    db: Session = Depends(get_db),
):
    data = await file.read(packing_picklist_service.MAX_UPLOAD_BYTES + 1)
    try:
        result = packing_picklist_service.parse_upload(
            data, file.filename or "", brand_override=brand_override,
            picklist_date=picklist_date or None,
        )
        result = packing_picklist_service.apply_registered_master_skus(result, db)
        output = packing_picklist_service.to_xlsx(result, include_audit=include_audit)
    except packing_picklist_service.PicklistError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Packing picklist generation failed")
        raise HTTPException(status_code=500, detail="Excel generation failed") from exc
    datepart = result["picklist_date"].replace("/", "-")
    detected_brands = list(result["brand_totals"])
    brandpart = (re.sub(r"[^A-Za-z0-9_-]", "", detected_brands[0])[:24] or "BRAND") if len(detected_brands) == 1 else "MULTI-BRAND"
    filename = f'MYRAA_Packing_{brandpart}_{datepart}.xlsx'
    return StreamingResponse(
        BytesIO(output),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Picklist-Total-Qty": str(result["total_qty"])},
    )


# ------------------------------------------------------------------ #
# Order history / batches
# ------------------------------------------------------------------ #
@app.get("/api/orders/history")
def order_history(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), paged: bool = False, db: Session = Depends(get_db)):
    total = db.query(func.count(models.OrderUpload.id)).scalar() if paged else None
    uploads = (
        db.query(models.OrderUpload)
        .options(
            selectinload(models.OrderUpload.lines).joinedload(models.OrderLine.variant).joinedload(models.Variant.product)
        )
        .order_by(models.OrderUpload.uploaded_at.desc(), models.OrderUpload.id.desc())
        .offset(offset).limit(limit).all()
    )
    page_lines = [line for upload in uploads for line in upload.lines]
    dispatchable_by_line = _order_line_dispatchability(db, page_lines)
    result = []
    for u in uploads:
        pending_lines = [line for line in u.lines if line.status in ACTIVE_ORDER_STATUSES]
        pending = len(pending_lines)
        fulfilled = sum(1 for line in u.lines if line.qty_fulfilled > 0)
        insufficient = sum(1 for line in u.lines if line.status == models.OrderLineStatus.INSUFFICIENT_STOCK)
        not_found = sum(1 for line in u.lines if line.status == models.OrderLineStatus.SKU_NOT_FOUND)
        pending_units = sum(max(line.qty_ordered - line.qty_fulfilled, 0) for line in pending_lines)
        dispatched_units = sum(line.qty_fulfilled for line in u.lines)
        dispatchable_units = sum(dispatchable_by_line.get(line.id, 0) for line in pending_lines)
        if pending:
            status = "PENDING"
        elif fulfilled:
            status = "DISPATCHED"
        elif insufficient:
            status = "INSUFFICIENT STOCK"
        else:
            status = "PROCESSED"
        lines = []
        for line in sorted(u.lines, key=lambda x: x.id):
            product_name = line.variant.product.name if line.variant and line.variant.product else None
            variant_code = line.variant.variant_code if line.variant else None
            pending_qty = max(line.qty_ordered - line.qty_fulfilled, 0) if line.status in ACTIVE_ORDER_STATUSES else 0
            dispatchable_qty = dispatchable_by_line.get(line.id, 0)
            line_status = (
                "PARTIALLY_DISPATCHED" if pending_qty and line.qty_fulfilled
                else "PENDING" if pending_qty and dispatchable_qty > 0
                else line.status.value
            )
            lines.append({
                "id": line.id, "sku": line.sku, "size": line.size, "variant_code": variant_code,
                "product_name": product_name, "qty_ordered": line.qty_ordered,
                "qty_fulfilled": line.qty_fulfilled, "pending_qty": pending_qty,
                "dispatchable_qty": dispatchable_qty, "status": line_status,
            })
        dispatch_state = "WAITING"
        if dispatchable_units and dispatchable_units >= pending_units:
            dispatch_state = "READY"
        elif dispatchable_units:
            dispatch_state = "PARTIAL"
        result.append({
            "order_upload_id": u.id, "filename": u.filename, "uploaded_at": u.uploaded_at,
            "total_rows": u.total_rows, "unique_skus": u.unique_skus,
            "pending_count": pending, "fulfilled_count": fulfilled,
            "pending_units": pending_units, "dispatched_units": dispatched_units,
            "dispatchable_lines": sum(1 for line in pending_lines if dispatchable_by_line.get(line.id, 0) > 0),
            "dispatchable_units": dispatchable_units, "dispatch_state": dispatch_state,
            "insufficient_count": insufficient, "not_found_count": not_found,
            "status": status, "lines": lines,
        })
    return {"items": result, "total": total, "offset": offset, "limit": limit} if paged else result


# ------------------------------------------------------------------ #
# Orders (Excel / CSV upload)
# ------------------------------------------------------------------ #
@app.post("/api/orders/upload", response_model=schemas.OrderUploadResult)
async def upload_orders(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        result = excel_service.process_order_excel(db, content, file.filename or "")
        # Return line ids and freshly computed readiness so the just-uploaded
        # batch can use the same per-line selection and dispatch flow as history.
        order_lines = (
            db.query(models.OrderLine)
            .options(joinedload(models.OrderLine.variant))
            .filter(models.OrderLine.order_upload_id == result["order_upload_id"])
            .order_by(models.OrderLine.id.asc())
            .all()
        )
        dispatchable_by_line = _order_line_dispatchability(db, order_lines)
        for line_result, line in zip(result["lines"], order_lines):
            pending_qty = max(line.qty_ordered - line.qty_fulfilled, 0) if line.status in ACTIVE_ORDER_STATUSES else 0
            line_result.update({
                "id": line.id,
                "pending_qty": pending_qty,
                "dispatchable_qty": dispatchable_by_line.get(line.id, 0),
            })
        return result
    except excel_service.DuplicateUploadError as e:
        raise HTTPException(409, str(e))
    except (excel_service.BadOrderFileError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/orders/reconcile-unmapped")
def reconcile_unmapped_orders(db: Session = Depends(get_db)):
    """Retry safe catalog matching for old SKU_NOT_FOUND lines; never deduct stock."""
    matched_lines = excel_service.reconcile_unmapped_order_lines(db)
    return {
        "matched_lines": matched_lines,
        "message": f"{matched_lines} previously unmatched order lines matched to inventory. No stock was deducted.",
    }


@app.delete("/api/orders/{order_upload_id}")
def delete_order_upload(order_upload_id: int, db: Session = Depends(get_db)):
    """Remove a processed file and all its order demand, safely reversing dispatch stock."""
    upload = db.query(models.OrderUpload).filter(models.OrderUpload.id == order_upload_id).with_for_update().first()
    if upload is None:
        raise HTTPException(status_code=404, detail="Order batch not found. It may already have been deleted.")

    lines = db.query(models.OrderLine).filter(models.OrderLine.order_upload_id == order_upload_id).all()
    fulfilled_by_variant: dict[int, int] = defaultdict(int)
    for line in lines:
        if line.variant_id and line.qty_fulfilled > 0:
            fulfilled_by_variant[line.variant_id] += line.qty_fulfilled

    try:
        restored_units = 0
        if fulfilled_by_variant:
            variants = db.query(models.Variant).filter(
                models.Variant.id.in_(fulfilled_by_variant.keys())
            ).with_for_update().all()
            by_id = {variant.id: variant for variant in variants}
            for variant_id, quantity in fulfilled_by_variant.items():
                variant = by_id.get(variant_id)
                if variant is None:
                    raise HTTPException(status_code=409, detail="A dispatched size was removed; this batch cannot be safely reversed.")
                inventory_service.apply_stock_change(
                    db, variant, quantity,
                    transaction_type=models.TransactionType.MANUAL_ADJUSTMENT,
                    reference=f"order_upload:{order_upload_id}:delete-reversal",
                    created_by="order_batch_delete",
                )
                alert_service.check_variant_alert(db, variant)
                restored_units += quantity

        removed_lines = len(lines)
        filename = upload.filename
        db.query(models.OrderLine).filter(models.OrderLine.order_upload_id == order_upload_id).delete(synchronize_session=False)
        db.delete(upload)  # Also releases its unique file hash for a corrected re-upload.
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return {
        "deleted_order_upload_id": order_upload_id,
        "filename": filename,
        "removed_lines": removed_lines,
        "restored_units": restored_units,
        "message": "Order demand removed; dispatched quantities, if any, were restored to stock.",
    }


# ------------------------------------------------------------------ #
# Alerts
# ------------------------------------------------------------------ #
def _collect_alerts(db: Session, q: str | None = None):
    alerts = alert_service.get_open_alerts(db, limit=None, offset=0, q=q)
    result = [{
        "id": a.id, "variant_code": a.variant.variant_code, "sku": a.variant.product.sku, "size": a.variant.size,
        "product_name": a.variant.product.name, "alert_type": a.alert_type, "stock_at_alert": a.stock_at_alert,
        "current_stock": a.variant.current_stock,
        "created_at": a.created_at, "ordered_qty": None, "required_qty": None,
    } for a in alerts]

    # Pending marketplace orders can create a real purchase shortage even when
    # physical stock is still above the normal low-stock threshold. Show that
    # shortage in Alerts as a virtual alert; the database remains the source of
    # truth for physical-stock alerts.
    shortages = reorder_service.generate_reorder_list(db, save_batch=False, part=q)
    existing_codes = {a["variant_code"] for a in result}
    for item in shortages:
        if item.get("is_new") or item["required_qty"] <= 0:
            continue
        if item["variant_code"] in existing_codes:
            continue
        result.append({
            "id": -item["variant_code"].__hash__(),
            "variant_code": item["variant_code"],
            "sku": item["sku"],
            "size": item["size"],
            "product_name": item["name"],
            "alert_type": "ORDER_SHORTAGE",
            "stock_at_alert": item["current_stock"],
            "current_stock": item["current_stock"],
            "created_at": datetime.now(timezone.utc),
            "ordered_qty": item["ordered_qty"],
            "required_qty": item["required_qty"],
        })
    result.sort(key=lambda a: (a["alert_type"] != "OUT_OF_STOCK", a["stock_at_alert"], a["variant_code"]))
    if q and q.strip():
        tokens = _smart_search_tokens(q)
        result = [a for a in result if all(
            any(token in str(a.get(key) or "").casefold() for key in ("variant_code", "sku", "size", "product_name", "alert_type"))
            for token in tokens
        )]
    return result


@app.get("/api/alerts", response_model=list[schemas.AlertOut])
def list_alerts(limit: int = Query(1000, ge=1, le=5000), offset: int = Query(0, ge=0), q: str | None = None, db: Session = Depends(get_db)):
    alerts = _collect_alerts(db, q)
    return alerts[offset:offset + limit]


@app.get("/api/alerts/groups", response_model=schemas.AlertGroupPage)
def list_alert_groups(limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0), q: str | None = None, db: Session = Depends(get_db)):
    alerts = _collect_alerts(db, q)
    rank = {"OUT_OF_STOCK": 0, "ORDER_SHORTAGE": 1, "LOW_STOCK": 2}
    size_rank = {name: index for index, name in enumerate(("FREE SIZE", "XS", "S", "M", "L", "XL", "XXL", "3XL", "4XL"))}
    groups = {}
    for alert in alerts:
        group = groups.setdefault(alert["sku"], {
            "sku": alert["sku"], "product_name": alert["product_name"], "items": [],
            "highest_severity": alert["alert_type"], "_severity": 99, "_stock": alert["stock_at_alert"],
        })
        group["items"].append(alert)
        severity = rank.get(alert["alert_type"], 3)
        group["_severity"] = min(group["_severity"], severity)
        group["_stock"] = min(group["_stock"], alert["stock_at_alert"])
        if severity == group["_severity"]:
            group["highest_severity"] = alert["alert_type"]
    page_groups = list(groups.values())
    for group in page_groups:
        group["items"].sort(key=lambda a: (size_rank.get(a["size"].upper(), 100), a["size"].upper()))
        group["alert_count"] = len(group["items"])
        del group["_severity"], group["_stock"]
    page_groups.sort(key=lambda g: (rank.get(g["highest_severity"], 3), min(a["stock_at_alert"] for a in g["items"]), g["sku"].casefold()))
    total = len(page_groups)
    return {"total": total, "limit": limit, "offset": offset, "items": page_groups[offset:offset + limit]}


# ------------------------------------------------------------------ #
# Dispatch pending orders
# ------------------------------------------------------------------ #
@app.post("/api/orders/{order_upload_id}/dispatch")
def dispatch_order_upload(
    order_upload_id: int,
    payload: schemas.OrderDispatchRequest | None = None,
    db: Session = Depends(get_db),
):
    """Dispatch available pieces from a batch, leaving any shortage pending."""
    upload = db.query(models.OrderUpload).filter(models.OrderUpload.id == order_upload_id).with_for_update().first()
    if upload is None:
        raise HTTPException(status_code=404, detail="Order batch not found.")
    pending_lines = (
        db.query(models.OrderLine)
        .options(joinedload(models.OrderLine.variant).joinedload(models.Variant.product))
        .filter(
            models.OrderLine.order_upload_id == order_upload_id,
            models.OrderLine.status.in_(ACTIVE_ORDER_STATUSES),
            models.OrderLine.variant_id.isnot(None),
        )
        # Serialize concurrent attempts to dispatch the same batch on databases
        # that support row locks (PostgreSQL). The guarded stock update below
        # remains the final protection against a simultaneous recount/dispatch.
        .with_for_update(of=models.OrderLine)
        .order_by(models.OrderLine.id.asc())
        .all()
    )
    if not pending_lines:
        raise HTTPException(status_code=404, detail="No pending order lines found for this upload.")

    selected_ids = set(payload.line_ids) if payload is not None else None
    if selected_ids is not None:
        pending_by_id = {line.id: line for line in pending_lines}
        invalid_ids = selected_ids.difference(pending_by_id)
        if invalid_ids:
            raise HTTPException(status_code=409, detail="Some selected order lines are no longer pending. Refresh the order list and try again.")
        lines = [pending_by_id[line_id] for line_id in sorted(selected_ids)]
    else:
        lines = pending_lines

    dispatchable_by_line = _order_line_dispatchability(db, lines)
    plan = []
    for line in lines:
        remaining = max(line.qty_ordered - line.qty_fulfilled, 0)
        quantity = min(remaining, dispatchable_by_line.get(line.id, 0))
        if quantity > 0:
            plan.append((line, quantity))
    if not plan:
        raise HTTPException(
            status_code=409,
            detail="No counted stock is available for these pending order lines. Any undispatched quantity remains pending.",
        )

    try:
        for line, quantity in plan:
            variant = line.variant
            apply = inventory_service.apply_stock_change
            apply(
                db, variant, -quantity,
                transaction_type=models.TransactionType.ORDER_DEDUCTION,
                reference=f"order_upload:{order_upload_id}:dispatch",
                created_by="dispatch",
            )
            line.qty_fulfilled += quantity
            if line.qty_fulfilled >= line.qty_ordered:
                line.qty_fulfilled = line.qty_ordered
                line.status = models.OrderLineStatus.FULFILLED
            else:
                line.status = models.OrderLineStatus.PENDING
            alert_service.check_variant_alert(db, variant)
        db.commit()
    except inventory_service.InsufficientStockError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"{exc.variant_code}: stock changed during dispatch; requested {exc.requested}, only {exc.available} remain. The batch was not dispatched.",
        ) from exc
    except Exception:
        db.rollback()
        raise

    remaining_lines = [line for line in pending_lines if line.status in ACTIVE_ORDER_STATUSES]
    remaining_units = sum(max(line.qty_ordered - line.qty_fulfilled, 0) for line in remaining_lines)
    dispatched_units = sum(quantity for _, quantity in plan)
    return {
        "order_upload_id": order_upload_id,
        "dispatched_lines": len(plan),
        "dispatched_units": dispatched_units,
        "pending_lines": len(remaining_lines),
        "pending_units": remaining_units,
        "status": "PENDING" if remaining_units else "DISPATCHED",
    }


# ------------------------------------------------------------------ #
# Reorder list
# ------------------------------------------------------------------ #
@app.get("/api/reorder-list")
def reorder_list(part: str | None = None, limit: int | None = Query(None, ge=1, le=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    # Merge cross-brand aliases before pagination so one product/colour/size
    # appears once and the page count reflects the consolidated picklist.
    items = reorder_service.generate_reorder_list(db, save_batch=False)
    items = reorder_service.consolidate_reorder_items(items, part=part)
    return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset:offset + limit]} if limit is not None else items


@app.get("/api/reorder-list/export")
def reorder_list_export(part: str | None = None, db: Session = Depends(get_db)):
    if part and part.strip():
        # Keep the saved reorder batch scoped to the user's raw SKU search,
        # while building the downloadable group from all brands that match it.
        reorder_service.generate_reorder_list(db, save_batch=True, part=part)
        items = reorder_service.generate_reorder_list(db, save_batch=False)
    else:
        items = reorder_service.generate_reorder_list(db, save_batch=True)
    items = reorder_service.consolidate_reorder_items(items, part=part)
    items = [item for item in items if item["required_qty"] > 0 or item.get("is_new")]
    xlsx_bytes = reorder_service.reorder_list_to_excel(items)
    return StreamingResponse(BytesIO(xlsx_bytes), media_type=XLSX_MEDIA, headers={"Content-Disposition": "attachment; filename=reorder_list.xlsx"})


# ------------------------------------------------------------------ #
# Printable barcode labels
# ------------------------------------------------------------------ #
@app.get("/api/variants/{variant_code}/label")
def variant_label(variant_code: str, db: Session = Depends(get_db)):
    variant = _get_variant_by_code(db, variant_code)
    png_bytes = barcode_service.generate_variant_label(variant.variant_code, variant.product.name, variant.size)
    return StreamingResponse(BytesIO(png_bytes), media_type="image/png")


@app.get("/api/products/{sku}/qr")
def product_qr_label(sku: str, db: Session = Depends(get_db)):
    """One scannable design QR; scanning opens the product with every size."""
    product = _get_product_by_sku(db, sku)
    png_bytes = barcode_service.generate_product_qr_label(product.sku, product.name)
    return StreamingResponse(
        BytesIO(png_bytes), media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product.sku}_product_qr.png"'},
    )


@app.get("/api/products/{sku}/barcode")
def product_barcode_label(sku: str, db: Session = Depends(get_db)):
    """One design-level barcode; the PRODUCT: prefix routes scanners to all sizes."""
    product = _get_product_by_sku(db, sku)
    png_bytes = barcode_service.generate_product_barcode_label(product.sku, product.name)
    return StreamingResponse(
        BytesIO(png_bytes), media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product.sku}_product_barcode.png"'},
    )


@app.get("/api/products/{sku}/label")
def product_combo_label(sku: str, db: Session = Depends(get_db)):
    """One compact product label containing both QR and Code128 symbols."""
    product = _get_product_by_sku(db, sku)
    png_bytes = barcode_service.generate_product_combo_label(product.sku, product.name)
    return StreamingResponse(
        BytesIO(png_bytes), media_type="image/png",
        headers={"Content-Disposition": f'inline; filename="{product.sku}_product_label.png"'},
    )


@app.get("/api/labels/sheet")
def labels_sheet(variant_codes: str = Query(...), db: Session = Depends(get_db)):
    codes = [c.strip().upper() for c in variant_codes.split(",") if c.strip()]
    variants = (
        db.query(models.Variant).options(joinedload(models.Variant.product))
        .filter(models.Variant.variant_code.in_(codes), models.Variant.is_deleted == False).all()  # noqa: E712
    )
    if not variants:
        raise HTTPException(404, "No matching variants found for the given codes.")
    variants.sort(key=lambda v: (v.product.sku, bulk_stock_service.SIZE_ORDER.get(v.size, 99), v.size))
    png_bytes = barcode_service.generate_labels_sheet([{"variant_code": v.variant_code, "product_name": v.product.name, "size": v.size} for v in variants])
    return StreamingResponse(BytesIO(png_bytes), media_type="image/png", headers={"Content-Disposition": "attachment; filename=labels.png"})


# ------------------------------------------------------------------ #
# Analytics: movers, for the dashboard chart
# ------------------------------------------------------------------ #
@app.get("/api/analytics/movers", response_model=list[schemas.MoverOut])
def movers(days: int = Query(30, ge=1, le=365), limit: int = Query(10, ge=1, le=50), db: Session = Depends(get_db)):
    """Top ordered designs. Pending and dispatched orders both count as movement."""
    return _movers_rows(db, days, limit)


def _movers_rows(db: Session, days: int, limit: int):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        db.query(
            models.Product.sku, models.Product.name, models.Product.image_path,
            func.sum(models.OrderLine.qty_ordered).label("units"),
        )
        .join(models.Variant, models.OrderLine.variant_id == models.Variant.id)
        .join(models.Product, models.Variant.product_id == models.Product.id)
        .join(models.OrderUpload, models.OrderLine.order_upload_id == models.OrderUpload.id)
        .filter(
            models.OrderUpload.uploaded_at >= since,
            models.Variant.is_deleted == False,  # noqa: E712
            models.Product.is_deleted == False,  # noqa: E712
            models.OrderLine.status.in_([*ACTIVE_ORDER_STATUSES, models.OrderLineStatus.FULFILLED]),
        )
        .group_by(models.Product.sku, models.Product.name, models.Product.image_path)
        .order_by(func.sum(models.OrderLine.qty_ordered).desc(), models.Product.sku.asc())
        .limit(limit).all()
    )
    return [{"sku": r.sku, "name": r.name, "image_path": r.image_path, "units_moved": int(r.units)} for r in rows]


@app.get("/api/analytics/movers/export")
def export_movers(days: int = Query(30, ge=1, le=365), limit: int = Query(8, ge=1, le=50), db: Session = Depends(get_db)):
    """Download the same ranked designs and period as the dashboard chart."""
    from services.movers_export import build_movers_workbook
    content = build_movers_workbook(_movers_rows(db, days, limit), days)
    return StreamingResponse(BytesIO(content), media_type=XLSX_MEDIA, headers={
        "Content-Disposition": f"attachment; filename=MYRAA_Top_Movers_{days}d.xlsx",
        "Cache-Control": "private, no-store",
    })
