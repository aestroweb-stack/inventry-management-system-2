"""MYRAA catalog costing, marketplace alias and inbound challan APIs.
All writes occur behind existing /api authentication and in DB transactions.
"""
from __future__ import annotations

import hashlib
import json
import re
import os
import io
import uuid
from pathlib import Path
from urllib.parse import quote
from zipfile import ZipFile, ZIP_DEFLATED
from xml.sax.saxutils import escape as xml_escape
from PIL import Image, UnidentifiedImageError
from collections import defaultdict
from datetime import datetime, timezone, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional, Annotated

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Form, Query
from fastapi.responses import FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, or_, case, update
from sqlalchemy.orm import Session, joinedload, selectinload

from database import get_db, tenant_root
import models
from business_models import ProductCost, VariantCost, MarketplaceSkuAlias, ProductMarketplaceAlias, PurchaseChallan, PurchaseChallanLine, PurchaseChallanAttachment, PurchaseReturn, PurchaseReturnLine, PurchaseReturnDraft, PurchaseReturnDocument
from services import excel_service, inventory_service, alert_service
from services.table_reader import read_table, clean_code, to_int
from services.catalog_identity import duplicate_design, duplicate_message, canonical_conflict, lock_catalog

router = APIRouter(prefix="/api", tags=["Finance and purchase invoices"])
MAX_EXCEL_SIZE = 12 * 1024 * 1024
MAX_INVOICE_BYTES = 10 * 1024 * 1024
MAX_CHALLAN_ATTACHMENTS = 12
# PRIVATE directory: never mount it with FastAPI StaticFiles.
# All invoice documents are tenant-private, not globally mounted.

def challan_image_dir():
    return tenant_root() / 'challan_scans'




def cost_decimal(value, name="Unit cost"):
    if value is None or str(value).strip() == "":
        return None
    try:
        v = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(422, f"{name} must be a valid number")
    if not v.is_finite() or v < 0 or v > 99999999:
        raise HTTPException(422, f"{name} must be between 0 and 99999999")
    return v


def upsert_product_cost(db, product_id: int, cost):
    value = cost_decimal(cost)
    if value is None:
        return
    current = db.query(ProductCost).filter_by(product_id=product_id).first()
    if current:
        current.unit_cost = value
    else:
        db.add(ProductCost(product_id=product_id, unit_cost=value))


def upsert_variant_cost(db, variant_id: int, cost):
    value = cost_decimal(cost)
    if value is None:
        return
    current = db.query(VariantCost).filter_by(variant_id=variant_id).first()
    if current:
        current.unit_cost = value
    else:
        db.add(VariantCost(variant_id=variant_id, unit_cost=value))


def normalize_alias(s: str) -> str:
    value = str(s or "").strip().upper()
    if not value or len(value) > 180 or any(ord(ch) < 32 for ch in value):
        raise HTTPException(422, "Marketplace SKU must be 1–180 printable characters")
    return value


def add_alias(db, variant: models.Variant, marketplace: str, alias: str):
    normalized = normalize_alias(alias)
    if normalized in {variant.variant_code.upper(), variant.product.sku.upper()}:
        return None  # the master code is already matched by the catalog
    product_mapping = db.query(ProductMarketplaceAlias).filter_by(normalized_code=normalized).first()
    if product_mapping:
        raise HTTPException(409, "SKU already used as a whole-product mapping; choose another SKU")
    existing = db.query(MarketplaceSkuAlias).filter_by(normalized_code=normalized).first()
    if existing:
        if existing.variant_id != variant.id:
            raise HTTPException(409, f"Marketplace SKU '{normalized}' already belongs to another size/product")
        existing.marketplace = (marketplace or "OTHER").strip().upper()[:80]
        return existing
    # Cannot create a silent ambiguous alias against any existing canonical SKU.
    direct = db.query(models.Variant).filter(
        or_(models.Variant.variant_code == normalized,
            models.Variant.seller_sku_code == normalized,
            models.Variant.marketplace_sku_code == normalized),
        models.Variant.id != variant.id,
        models.Variant.is_deleted.is_(False),
    ).first()
    if direct:
        raise HTTPException(409, f"SKU '{normalized}' already identifies another variant")
    direct_product = db.query(models.Product).filter(models.Product.sku == normalized, models.Product.id != variant.product_id).first()
    if direct_product:
        raise HTTPException(409, f"SKU '{normalized}' is already a different master SKU")
    item = MarketplaceSkuAlias(variant_id=variant.id, marketplace=(marketplace or "OTHER").strip().upper()[:80], alias_code=alias.strip(), normalized_code=normalized)
    db.add(item)
    return item


def add_product_alias(db, product: models.Product, marketplace: str, alias: str):
    """Whole-product SKU. Orders MUST supply an explicit or inferred size."""
    normalized = normalize_alias(alias)
    existing = db.query(ProductMarketplaceAlias).filter_by(normalized_code=normalized).first()
    if existing:
        if existing.product_id != product.id:
            raise HTTPException(409, "Marketplace SKU already belongs to another product")
        existing.marketplace = (marketplace or "OTHER").strip().upper()[:80]
        return existing
    if db.query(MarketplaceSkuAlias).filter_by(normalized_code=normalized).first():
        raise HTTPException(409, "SKU already mapped to a specific size")
    if db.query(models.Variant).filter(
        or_(models.Variant.variant_code == normalized,
            models.Variant.seller_sku_code == normalized,
            models.Variant.marketplace_sku_code == normalized),
    ).first():
        raise HTTPException(409, "SKU already assigned to a stockable size")
    direct = db.query(models.Product).filter(models.Product.sku == normalized).first()
    if direct:
        if direct.id == product.id:
            return None  # canonical Master SKU already matched by product + Size
        raise HTTPException(409, "SKU is another product's Master SKU")
    item = ProductMarketplaceAlias(product_id=product.id,
        marketplace=(marketplace or "OTHER").strip().upper()[:80],
        alias_code=alias.strip(), normalized_code=normalized)
    db.add(item)
    return item


class CostsAndAliases(BaseModel):
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, le=99999999)
    size_costs: dict[str, Optional[Decimal]] = Field(default_factory=dict)


class AliasRequest(BaseModel):
    size: Optional[str] = None
    marketplace: str = "OTHER"
    sku: str


@router.put("/products/{sku}/costing")
def save_product_costing(sku: str, payload: CostsAndAliases, db: Session = Depends(get_db)):
    product = db.query(models.Product).filter(models.Product.sku == sku.strip().upper(), models.Product.is_deleted.is_(False)).first()
    if not product:
        raise HTTPException(404, "Product not found")
    sizes = {v.size.upper(): v for v in product.variants if not v.is_deleted}
    for size in payload.size_costs:
        if size.strip().upper() not in sizes:
            raise HTTPException(422, f"Size {size} does not belong to {product.sku}")
    upsert_product_cost(db, product.id, payload.unit_cost)
    for size, value in payload.size_costs.items():
        if value is not None:
            upsert_variant_cost(db, sizes[size.strip().upper()].id, value)
    db.commit()
    return {"status": "saved", "sku": product.sku}


@router.post("/products/{sku}/marketplace-aliases")
def save_product_alias(sku: str, payload: AliasRequest, db: Session = Depends(get_db)):
    product = db.query(models.Product).filter(models.Product.sku == sku.strip().upper(), models.Product.is_deleted.is_(False)).first()
    if not product:
        raise HTTPException(404, "Product not found")
    if payload.size:
        variant = next((v for v in product.variants if not v.is_deleted and v.size.upper() == payload.size.strip().upper()), None)
        if not variant:
            raise HTTPException(422, "Choose a size belonging to this product")
        add_alias(db, variant, payload.marketplace, payload.sku)
        resolved_size = variant.size
    else:
        add_product_alias(db, product, payload.marketplace, payload.sku)
        resolved_size = None
    db.commit()
    excel_service.reconcile_unmapped_order_lines(db)
    return {"status": "saved", "sku": product.sku, "size": resolved_size, "scope": "size" if resolved_size else "product"}


@router.delete("/products/{sku}/marketplace-aliases/{alias_id}")
def remove_product_alias(sku: str, alias_id: int, db: Session = Depends(get_db)):
    alias = db.query(MarketplaceSkuAlias).join(models.Variant).join(models.Product).filter(
        MarketplaceSkuAlias.id == alias_id, models.Product.sku == sku.strip().upper()
    ).first()
    if not alias:
        raise HTTPException(404, "SKU mapping not found")
    db.delete(alias)
    db.commit()
    return {"status": "deleted"}


@router.delete("/products/{sku}/product-marketplace-aliases/{alias_id}")
def remove_whole_product_alias(sku: str, alias_id: int, db: Session = Depends(get_db)):
    alias = db.query(ProductMarketplaceAlias).join(models.Product).filter(
        ProductMarketplaceAlias.id == alias_id,
        models.Product.sku == sku.strip().upper(),
    ).first()
    if not alias:
        raise HTTPException(404, "Product-wide SKU mapping not found")
    db.delete(alias)
    db.commit()
    return {"status": "deleted"}


@router.get("/costing/summary")
def costing_summary(db: Session = Depends(get_db)):
    products = db.query(models.Product).options(
        selectinload(models.Product.variants).selectinload(models.Variant.cost_record),
        joinedload(models.Product.cost_record),
    ).filter(models.Product.is_deleted.is_(False)).all()
    rows = []
    total_known = Decimal("0")
    unpriced = 0
    pieces_unpriced = 0
    for p in products:
        stock = 0
        amount = Decimal("0")
        for v in p.variants:
            if v.is_deleted or v.last_counted_at is None or v.current_stock <= 0:
                continue
            stock += v.current_stock
            cost = v.cost_record.unit_cost if v.cost_record else (p.cost_record.unit_cost if p.cost_record else None)
            if cost is None:
                unpriced += 1
                pieces_unpriced += v.current_stock
            else:
                amount += cost * v.current_stock
        total_known += amount
        if stock:
            rows.append({"sku": p.sku, "name": p.name, "stock": stock, "cost_value": float(amount)})
    rows.sort(key=lambda x: -x["cost_value"])
    return {"total_known_cost": float(total_known), "currency": "INR", "unpriced_sizes": unpriced, "unpriced_pieces": pieces_unpriced, "products": rows[:30]}


@router.get("/costing/designs")
def costing_designs(q: str = "", limit: int = Query(20, ge=1, le=100),
                    offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    """Every design, searchable and paginated; never truncate the catalog to a top-N chart."""
    query = db.query(models.Product).options(
        joinedload(models.Product.cost_record),
        selectinload(models.Product.variants).selectinload(models.Variant.cost_record),
    ).filter(models.Product.is_deleted.is_(False))
    if q.strip():
        query = query.filter((models.Product.sku.ilike(f"%{q.strip()}%")) |
                             (models.Product.name.ilike(f"%{q.strip()}%")))
    total = query.count()
    products = query.order_by(models.Product.sku.asc()).offset(offset).limit(limit).all()
    items = []
    for p in products:
        stock = 0
        amount = Decimal("0")
        missing = 0
        for v in p.variants:
            if v.is_deleted or v.last_counted_at is None or v.current_stock <= 0:
                continue
            stock += v.current_stock
            cost = v.cost_record.unit_cost if v.cost_record else (p.cost_record.unit_cost if p.cost_record else None)
            if cost is None:
                missing += v.current_stock
            else:
                amount += cost * v.current_stock
        items.append({"sku": p.sku, "name": p.name, "stock": stock,
                      "cost_value": float(amount), "unpriced_pieces": missing})
    return {"total": total, "offset": offset, "limit": limit, "items": items}


SKU_FIELDS = ("sku", "master sku", "master_sku", "style", "style code", "style id", "design", "design code", "product sku")
NAME_FIELDS = ("product name", "name", "design name", "product")
SIZE_FIELDS = ("size", "variant size", "size name")
QTY_FIELDS = ("qty", "quantity", "order qty", "ordered qty", "pieces", "order quantity", "total qty")
COST_FIELDS = ("unit cost", "cost", "rate", "cost price", "purchase rate", "purchase price", "price")
CATEGORY_FIELDS = ("category", "product category")


def pick(row: dict, keys: tuple):
    # Normalize column header punctuation and whitespace, but NOT cell SKU values.
    fields = {re.sub(r"[^a-z0-9]", "", str(k).lower()): v for k, v in row.items()}
    for k in keys:
        v = fields.get(re.sub(r"[^a-z0-9]", "", k.lower()))
        if v is not None and str(v).strip().lower() not in ("", "nan", "none"):
            return str(v).strip()
    return None


def parse_challan_file(blob: bytes, filename: str, db: Session | None = None):
    if not blob or len(blob) > MAX_EXCEL_SIZE:
        raise HTTPException(422, "Excel file must be between 1 byte and 12 MB")
    try:
        df = read_table(blob, filename)
    except Exception as exc:
        raise HTTPException(422, str(exc)) from exc
    if df.empty or len(df) > 10000:
        raise HTTPException(422, "Invoice must contain between 1 and 10,000 rows")
    merged = defaultdict(lambda: {"quantity": 0})
    for idx, raw in enumerate(df.to_dict("records"), 2):
        sku = (pick(raw, SKU_FIELDS) or "").strip().upper()
        name = (pick(raw, NAME_FIELDS) or "").strip()
        size = (pick(raw, SIZE_FIELDS) or "").strip().upper()
        # Simple 3-column supplier sheet: Product Name, Size, Qty.
        if not sku and name:
            if db is not None:
                matches = db.query(models.Product).filter(
                    func.lower(models.Product.name) == name.lower(),
                    models.Product.is_deleted.is_(False),
                ).limit(3).all()
                if len(matches) > 1:
                    raise HTTPException(422, f"Row {idx}: product name '{name}' matches multiple Master SKUs; use SKU column to disambiguate")
                if matches:
                    sku = matches[0].sku
            if not sku:
                sku = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")[:64]
                if not sku:
                    raise HTTPException(422, f"Row {idx}: Product Name must contain letters or digits")
                if db is not None:
                    conflict = db.query(models.Product).filter_by(sku=sku).first()
                    if conflict and conflict.name.lower() != name.lower():
                        raise HTTPException(422, f"Row {idx}: generated SKU collision; supply Master SKU column")
        if not size:
            match = re.search(r"(?:[_\-\s]+)(5XL|4XL|3XL|XXL|XL|XS|S|M|L|\d{2,3})$", sku)
            if match:
                size = match.group(1)
                sku = sku[:match.start()].rstrip("_- ")
        rawqty = pick(raw, QTY_FIELDS)
        if not sku and not rawqty:
            continue
        qty = to_int(rawqty)
        if not sku or not size or qty is None or qty <= 0:
            raise HTTPException(422, f"Row {idx}: SKU, size and positive qty are required. Keep any unknown size in the Size column.")
        if len(sku) > 64 or len(size) > 20:
            raise HTTPException(422, f"Row {idx}: SKU/size too long")
        cost = cost_decimal(pick(raw, COST_FIELDS), f"Row {idx} cost")
        key = (sku, size)
        name = (name or sku).strip()[:300]
        category = (pick(raw, CATEGORY_FIELDS) or "").strip()[:100]
        existing = merged[key]
        if existing["quantity"] and (existing["unit_cost"] != cost or existing["name"] != name):
            raise HTTPException(422, f"Row {idx}: duplicate {sku}/{size} has different name or cost. Correct before importing.")
        existing.update({"sku": sku, "size": size, "name": name, "category": category, "unit_cost": cost})
        existing["quantity"] += qty
    if not merged:
        raise HTTPException(422, "No valid invoice rows found")
    return list(merged.values())


def serialize_challan(batch: PurchaseChallan):
    lines = [{"id": x.id, "sku": x.sku, "name": x.name, "size": x.size,
              "qty_ordered": x.qty_ordered, "qty_received": x.qty_received,
              "pending": x.qty_ordered - x.qty_received,
              "unit_cost": float(x.unit_cost) if x.unit_cost is not None else None} for x in batch.lines]
    return {"id": batch.id, "party_name": batch.party_name, "challan_no": batch.challan_no,
            "attachments": [{"id": a.id, "filename": a.original_filename,
                             "content_type": a.content_type, "size_bytes": a.size_bytes,
                             "url": f"/api/purchase-challans/{batch.id}/attachments/{a.id}"}
                            for a in batch.attachments],
            "received_unpriced_pieces": sum(x["qty_received"] for x in lines if x["unit_cost"] is None),
            "received_value_known": float(sum((Decimal(str(x["unit_cost"])) * x["qty_received"] for x in lines if x["unit_cost"] is not None), Decimal("0"))),
            "pending_value_known": float(sum((Decimal(str(x["unit_cost"])) * x["pending"] for x in lines if x["unit_cost"] is not None), Decimal("0"))),
            "invoice_no": batch.invoice_no, "order_date": batch.order_date,
            "status": batch.status, "created_at": batch.created_at,
            "posted_at": batch.posted_at, "filename": batch.source_filename,
            "ordered": sum(x["qty_ordered"] for x in lines),
            "received": sum(x["qty_received"] for x in lines),
            "pending": sum(x["pending"] for x in lines),
            "cost_total_known": float(sum((Decimal(str(x["unit_cost"])) * x["qty_ordered"] for x in lines if x["unit_cost"] is not None), Decimal("0"))),
            "lines": lines}


@router.get("/purchase-challans")
def list_purchase_challans(
    q: str = "", date_filter: str = "",
    limit: Annotated[int, Query(ge=1, le=100)] = 6,
    offset: Annotated[int, Query(ge=0)] = 0,
    db: Session = Depends(get_db)
):
    """Search every challan; calculate inventory-wide totals, not just current page."""
    query = db.query(PurchaseChallan)
    term = q.strip()[:100]
    if term:
        term_sql = f"%{term}%"
        query = query.filter(or_(
            PurchaseChallan.party_name.ilike(term_sql),
            PurchaseChallan.challan_no.ilike(term_sql),
            PurchaseChallan.invoice_no.ilike(term_sql),
            PurchaseChallan.source_filename.ilike(term_sql),
            PurchaseChallan.order_date.ilike(term_sql),
            PurchaseChallan.lines.any(or_(
                PurchaseChallanLine.name.ilike(term_sql),
                PurchaseChallanLine.sku.ilike(term_sql),
            )),
        ))
    if date_filter:
        try:
            datetime.strptime(date_filter, "%Y-%m-%d")
        except ValueError as exc:
            raise HTTPException(422, "Date must be YYYY-MM-DD") from exc
        query = query.filter(PurchaseChallan.order_date == date_filter)
    count = query.count()
    batches = (query.options(selectinload(PurchaseChallan.lines))
               .order_by(PurchaseChallan.id.desc()).offset(offset).limit(limit).all())
    page = [serialize_challan(b) for b in batches]

    # All-time totals independently of current search and pagination.
    pending = PurchaseChallanLine.qty_ordered - PurchaseChallanLine.qty_received
    not_posted = PurchaseChallan.status != "POSTED"
    cost_known = PurchaseChallanLine.unit_cost.isnot(None)
    sums = db.query(
        func.coalesce(func.sum(PurchaseChallanLine.qty_ordered), 0),
        func.coalesce(func.sum(PurchaseChallanLine.qty_received), 0),
        func.coalesce(func.sum(case((not_posted, pending), else_=0)), 0),
        func.coalesce(func.sum(case((cost_known, PurchaseChallanLine.qty_received * PurchaseChallanLine.unit_cost), else_=0)), 0),
        func.coalesce(func.sum(case((PurchaseChallanLine.unit_cost.is_(None), PurchaseChallanLine.qty_received), else_=0)), 0),
        func.coalesce(func.sum(case((not_posted & cost_known, pending * PurchaseChallanLine.unit_cost), else_=0)), 0),
        func.coalesce(func.sum(case((not_posted & PurchaseChallanLine.unit_cost.is_(None), pending), else_=0)), 0),
    ).join(PurchaseChallan, PurchaseChallanLine.challan_id == PurchaseChallan.id).one()
    ordered, received, remaining, received_value, received_unpriced, pending_value, pending_unpriced = sums
    return {"items": page, "total": count, "limit": limit, "offset": offset,
            "received_pieces": int(received or 0), "received_value_known": float(received_value or 0),
            "received_unpriced_pieces": int(received_unpriced or 0),
            "pending_pieces": int(remaining or 0), "ordered_pieces": int(ordered or 0),
            "pending_cost_known": float(pending_value or 0),
            "pending_unpriced_pieces": int(pending_unpriced or 0)}


@router.get('/purchase-challans/summary')
def purchase_challan_summary(date_filter: str = '', db: Session = Depends(get_db)):
    """Invoice-date totals include POSTED records; only pending is outstanding."""
    day = date_filter or datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    try:
        datetime.strptime(day, '%Y-%m-%d')
    except ValueError as exc:
        raise HTTPException(422, 'Date must be YYYY-MM-DD') from exc
    def totals(start, end):
        q = db.query(PurchaseChallanLine).join(PurchaseChallan)
        q = q.filter(PurchaseChallan.order_date >= start, PurchaseChallan.order_date < end)
        lines = q.with_entities(PurchaseChallanLine.qty_ordered, PurchaseChallanLine.qty_received,
            PurchaseChallanLine.unit_cost, PurchaseChallan.status).all()
        return {'ordered_pieces':sum(x[0] for x in lines),
                'received_pieces':sum(x[1] for x in lines),
                'pending_pieces':sum(max(0,x[0]-x[1]) for x in lines if x[3] != 'POSTED'),
                'ordered_value':float(sum(Decimal(x[0])*x[2] for x in lines if x[2] is not None)),
                'received_value':float(sum(Decimal(x[1])*x[2] for x in lines if x[2] is not None)),
                'unpriced_pieces':sum(x[0] for x in lines if x[2] is None),
                'invoice_count':db.query(PurchaseChallan).filter(PurchaseChallan.order_date >= start, PurchaseChallan.order_date < end).count()}
    d=date.fromisoformat(day)
    next_day=(d+timedelta(days=1)).isoformat()
    month_start=d.replace(day=1)
    month_end=(month_start.replace(year=month_start.year+1,month=1) if month_start.month==12 else month_start.replace(month=month_start.month+1)).isoformat()
    return {'date':day,'today':totals(day,next_day),'month':totals(month_start.isoformat(),month_end),
            'all_time':totals('0001-01-01','9999-12-31')}


@router.get("/purchase-challans/template")
def download_challan_template():
    rows = [["Product Name", "Size", "Qty"], ["URVISHA RED", "S", 20], ["URVISHA RED", "M", 25]]
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="MYRAA_Invoice_3_Column_Template.xlsx"'})





@router.get("/purchase-challans/export-records")
def export_all_invoice_records(db: Session = Depends(get_db)):
    """Export invoice records scoped to the authenticated seller's tenant database."""
    rows = [["Invoice No", "Supplier", "Invoice Date", "Status",
             "Product", "Master SKU", "Size", "Ordered", "Received",
             "Pending", "Unit Cost INR", "Received Value INR"]]
    batches = (db.query(PurchaseChallan)
               .options(selectinload(PurchaseChallan.lines))
               .order_by(PurchaseChallan.id.desc()).all())
    for batch in batches:
        for line in batch.lines:
            cost = float(line.unit_cost) if line.unit_cost is not None else None
            rows.append([batch.challan_no, batch.party_name, batch.order_date,
                         batch.status, line.name, line.sku, line.size,
                         line.qty_ordered, line.qty_received,
                         line.qty_ordered - line.qty_received,
                         cost if cost is not None else "",
                         round(cost * line.qty_received, 2) if cost is not None else ""])
    filename = "MYRAA_Purchase_Invoices.xlsx"
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@router.delete('/purchase-challans/{batch_id}/lines/{line_id}')
def delete_saved_invoice_item(batch_id: int, line_id: int, db: Session = Depends(get_db)):
    """Remove a saved draft item within the authenticated seller's database.

    Posted invoices cannot be modified because their quantities are in stock.
    Empty drafts remain OPEN and cannot be pushed to inventory.
    """
    # SQLite ignores FOR UPDATE. A no-op write acquires the writer lock before reading.
    db.execute(update(PurchaseChallan).where(PurchaseChallan.id == batch_id).values(status=PurchaseChallan.status))
    batch = db.query(PurchaseChallan).filter_by(id=batch_id).populate_existing().with_for_update().first()
    if batch is None:
        raise HTTPException(404, 'Invoice not found')
    if batch.status == 'POSTED':
        raise HTTPException(409, 'Posted invoices cannot be changed after stock is added')
    line = db.query(PurchaseChallanLine).filter_by(id=line_id, challan_id=batch_id).first()
    if line is None:
        raise HTTPException(404, 'Invoice item not found')
    db.delete(line)
    db.flush()
    remaining = db.query(PurchaseChallanLine).filter_by(challan_id=batch_id).all()
    batch.status = _draft_status([
        {'qty_ordered': x.qty_ordered, 'qty_received': x.qty_received} for x in remaining
    ])
    db.commit()
    db.expire_all()
    stored = (db.query(PurchaseChallan)
              .options(selectinload(PurchaseChallan.lines), selectinload(PurchaseChallan.attachments))
              .filter_by(id=batch_id).one())
    return serialize_challan(stored)


@router.delete('/purchase-challans/{batch_id}')
def delete_saved_purchase_invoice(batch_id: int, db: Session = Depends(get_db)):
    """Delete an unposted invoice, its draft items and its private scans.

    Seller isolation is provided by get_db's tenant-scoped Session.  Posted
    stock is audit evidence and must never be removed by a delete request.
    """
    db.execute(update(PurchaseChallan).where(PurchaseChallan.id == batch_id).values(status=PurchaseChallan.status))
    batch = (db.query(PurchaseChallan)
             .options(selectinload(PurchaseChallan.lines),
                      selectinload(PurchaseChallan.attachments))
             .filter_by(id=batch_id).with_for_update().first())
    if batch is None:
        raise HTTPException(404, 'Invoice not found')
    if batch.status == 'POSTED' or batch.posted_at is not None:
        raise HTTPException(409, 'Cannot delete an invoice already posted to inventory')
    # Compute tenant-private paths before the rows are deleted.  Do not touch
    # scans until the transaction succeeds, so rollback preserves all files.
    scans = [_invoice_file_path(a.storage_filename) for a in batch.attachments]
    db.delete(batch)  # delete-orphan cascade for draft rows and attachments
    db.commit()
    for scan in scans:
        try:
            scan.unlink(missing_ok=True)
        except OSError:
            # The DB deletion succeeded; keep running even if filesystem
            # cleanup needs later maintenance. Never expose another tenant.
            import logging
            logging.getLogger(__name__).warning('Could not remove an old private invoice scan')
    return {'status': 'deleted', 'id': batch_id}


@router.get("/purchase-challans/{batch_id}")
def get_purchase_challan(batch_id: int, db: Session = Depends(get_db)):
    b = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch_id).first()
    if not b:
        raise HTTPException(404, "Invoice not found")
    return serialize_challan(b)


@router.post("/purchase-challans/import")
async def import_challan(file: UploadFile = File(...), party_name: str = Form(...), challan_no: str = Form(...), invoice_no: str = Form(""), order_date: str = Form(""), db: Session = Depends(get_db)):
    party, number = party_name.strip()[:180], challan_no.strip()[:120]
    if not party or not number:
        raise HTTPException(422, "Supplier and invoice reference number are required")
    d = order_date.strip() or date.today().isoformat()
    try:
        date.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, "Date must be YYYY-MM-DD")
    data = await file.read(MAX_EXCEL_SIZE + 1)
    rows = parse_challan_file(data, file.filename or "", db=db)
    filehash = hashlib.sha256(data + party.upper().encode() + number.upper().encode()).hexdigest()
    if db.query(PurchaseChallan).filter_by(file_hash=filehash).first():
        raise HTTPException(409, "This invoice file was imported earlier")
    if db.query(PurchaseChallan).filter(func.upper(PurchaseChallan.party_name) == party.upper(), func.upper(PurchaseChallan.challan_no) == number.upper()).first():
        raise HTTPException(409, "This supplier and invoice reference number already exist")
    batch = PurchaseChallan(party_name=party, challan_no=number, invoice_no=invoice_no.strip()[:120], order_date=d, source_filename=(file.filename or "upload.xlsx")[:255], file_hash=filehash)
    db.add(batch)
    db.flush()
    for x in rows:
        db.add(PurchaseChallanLine(challan_id=batch.id, sku=x["sku"], name=x["name"], size=x["size"], category=x["category"], qty_ordered=x["quantity"], qty_received=0, unit_cost=x["unit_cost"]))
    db.commit()
    return serialize_challan(db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch.id).one())



def _invoice_file_path(storage_filename: str) -> Path:
    # Names are server-generated UUIDs. Reject unexpected path components anyway.
    if Path(storage_filename).name != storage_filename:
        raise HTTPException(500, "Unsafe stored invoice filename")
    return challan_image_dir() / storage_filename


@router.post("/purchase-challans/{batch_id}/attachments")
async def upload_challan_attachments(batch_id: int, files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    batch = db.query(PurchaseChallan).filter_by(id=batch_id).first()
    if not batch:
        raise HTTPException(404, "Invoice not found")
    if not files or len(files) + db.query(PurchaseChallanAttachment).filter_by(challan_id=batch_id).count() > MAX_CHALLAN_ATTACHMENTS:
        raise HTTPException(422, f"Maximum {MAX_CHALLAN_ATTACHMENTS} attachments per invoice")
    challan_image_dir().mkdir(parents=True, exist_ok=True)
    stored = []
    try:
        for upload in files:
            content = await upload.read(MAX_INVOICE_BYTES + 1)
            if not content or len(content) > MAX_INVOICE_BYTES:
                raise HTTPException(422, "Each invoice attachment must be 1 byte–10 MB")
            kind = None
            if content.startswith(b"%PDF-"):
                kind = ("pdf", "application/pdf")
            else:
                try:
                    with Image.open(io.BytesIO(content)) as im:
                        im.verify()
                        typ = im.format
                    kind = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                            "WEBP": ("webp", "image/webp")}.get(typ)
                except (UnidentifiedImageError, ValueError, OSError, Image.DecompressionBombError):
                    pass
            if not kind:
                raise HTTPException(422, "Only valid JPG, PNG, WEBP or PDF invoice attachments are allowed")
            storage = f"{uuid.uuid4().hex}.{kind[0]}"
            target = _invoice_file_path(storage)
            with open(target, "xb") as fh:
                fh.write(content)
            stored.append(target)
            db.add(PurchaseChallanAttachment(
                challan_id=batch_id, original_filename=Path((upload.filename or "invoice").replace("\\", "/")).name[:255],
                storage_filename=storage, content_type=kind[1], size_bytes=len(content)))
        db.commit()
    except Exception:
        db.rollback()
        for target in stored:
            target.unlink(missing_ok=True)
        raise
    return serialize_challan(db.query(PurchaseChallan).options(
        selectinload(PurchaseChallan.lines), selectinload(PurchaseChallan.attachments)).filter_by(id=batch_id).one())


@router.get("/purchase-challans/{batch_id}/attachments/{attachment_id}")
def download_challan_attachment(batch_id: int, attachment_id: int, db: Session = Depends(get_db)):
    record = db.query(PurchaseChallanAttachment).filter_by(id=attachment_id, challan_id=batch_id).first()
    if not record:
        raise HTTPException(404, "Invoice attachment not found")
    path = _invoice_file_path(record.storage_filename)
    if not path.is_file():
        raise HTTPException(404, "Saved scan unavailable: check persistent Railway file volume")
    return FileResponse(path, media_type=record.content_type, filename=record.original_filename,
                        content_disposition_type="inline" if record.content_type.startswith("image/") else "attachment")


@router.delete("/purchase-challans/{batch_id}/attachments/{attachment_id}")
def delete_challan_attachment(batch_id: int, attachment_id: int, db: Session = Depends(get_db)):
    """Remove a mistaken pre-posting document; retain posted bill evidence."""
    record = db.query(PurchaseChallanAttachment).filter_by(id=attachment_id, challan_id=batch_id).first()
    if not record:
        raise HTTPException(404, "Invoice attachment not found")
    if record.challan.status == "POSTED":
        raise HTTPException(409, "Posted invoice documents are retained for audit")
    path = _invoice_file_path(record.storage_filename)
    db.delete(record)
    db.commit()
    path.unlink(missing_ok=True)
    return {"status": "deleted", "id": attachment_id}


def _simple_xlsx(rows: list[list]):
    """Small standards-compliant XLSX without requiring new Railway dependencies."""
    def col(i):
        result = ""
        while i:
            i, r = divmod(i - 1, 26)
            result = chr(65 + r) + result
        return result
    cells = []
    for n, values in enumerate(rows, 1):
        contents = []
        for j, val in enumerate(values, 1):
            location = f"{col(j)}{n}"
            if isinstance(val, (float, int)) and not isinstance(val, bool):
                contents.append(f'<c r="{location}"><v>{val}</v></c>')
            else:
                txt = xml_escape(str(val if val is not None else ""), {'"':'&quot;'})
                contents.append(f'<c r="{location}" t="inlineStr"><is><t>{txt}</t></is></c>')
        cells.append(f'<row r="{n}">{"".join(contents)}</row>')
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             '<sheetData>' + ''.join(cells) + '</sheetData></worksheet>')
    output = io.BytesIO()
    with ZipFile(output, 'w', ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml', '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Invoice Items" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels', '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return output.getvalue()


@router.get("/purchase-challans/{batch_id}/export")
def export_challan(batch_id: int, db: Session = Depends(get_db)):
    batch = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch_id).first()
    if not batch:
        raise HTTPException(404, "Invoice not found")
    rows = [["Product Name", "Size", "Qty Ordered", "Qty Received", "Qty Pending", "Unit Cost INR", "Received Value INR", "Pending Value INR", "Master SKU"]]
    for line in batch.lines:
        cost = float(line.unit_cost) if line.unit_cost is not None else None
        rows.append([line.name, line.size, line.qty_ordered, line.qty_received,
                     line.qty_ordered - line.qty_received, cost if cost is not None else "",
                     round(cost * line.qty_received, 2) if cost is not None else "",
                     round(cost * (line.qty_ordered - line.qty_received), 2) if cost is not None else "", line.sku])
    filename = f"MYRAA_Invoice_{batch.id}.xlsx"
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class ReceiptLine(BaseModel):
    line_id: int
    received: int = Field(ge=0)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, le=99999999)


class ReceiptUpdate(BaseModel):
    lines: list[ReceiptLine] = Field(min_length=1)


@router.patch("/purchase-challans/{batch_id}/receipt")
def update_receipt(batch_id: int, payload: ReceiptUpdate, db: Session = Depends(get_db)):
    db.execute(update(PurchaseChallan).where(PurchaseChallan.id == batch_id).values(status=PurchaseChallan.status))
    batch = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch_id).first()
    if not batch:
        raise HTTPException(404, "Invoice not found")
    if batch.status == "POSTED":
        raise HTTPException(409, "Already pushed to inventory; receipt is locked")
    line_index = {x.id: x for x in batch.lines}
    if len({x.line_id for x in payload.lines}) != len(payload.lines):
        raise HTTPException(422, "Duplicate receipt line")
    for item in payload.lines:
        line = line_index.get(item.line_id)
        if not line or item.received > line.qty_ordered:
            raise HTTPException(422, "Invalid line or received quantity exceeds ordered quantity")
    for item in payload.lines:
        line = line_index[item.line_id]
        line.qty_received = item.received
        if item.unit_cost is not None:
            line.unit_cost = cost_decimal(item.unit_cost)
    batch.status = "READY" if all(x.qty_received == x.qty_ordered for x in batch.lines) else "PARTIAL" if any(x.qty_received for x in batch.lines) else "OPEN"
    db.commit()
    return serialize_challan(batch)


@router.post("/purchase-challans/{batch_id}/push")
def push_challan_to_inventory(batch_id: int, db: Session = Depends(get_db)):
    lock_catalog(db)
    db.execute(update(PurchaseChallan).where(PurchaseChallan.id == batch_id).values(status=PurchaseChallan.status))
    batch = db.query(PurchaseChallan).filter_by(id=batch_id).populate_existing().with_for_update().first()
    if not batch:
        raise HTTPException(404, "Invoice not found")
    if batch.status == "POSTED":
        raise HTTPException(409, "This invoice has already been posted; inventory was not updated twice")
    lines = db.query(PurchaseChallanLine).filter_by(challan_id=batch_id).order_by(PurchaseChallanLine.id).all()
    if not lines or any(x.qty_received != x.qty_ordered for x in lines):
        raise HTTPException(409, "Receive all ordered sizes before posting the invoice. Pending quantities will remain visible.")
    if any(x.unit_cost is None for x in lines):
        raise HTTPException(409, "Enter unit cost for every invoice item before posting, to keep inventory costing accurate.")
    try:
        for item in lines:
            product = db.query(models.Product).filter_by(sku=item.sku).first()
            if product and product.is_deleted:
                raise HTTPException(409, f"{item.sku} is in Trash: restore manually first")
            if not product:
                duplicate=duplicate_design(db,item.name)
                if duplicate: raise HTTPException(409,duplicate_message(duplicate))
                if canonical_conflict(db,item.sku): raise HTTPException(409, "Master SKU already belongs to another product or linked SKU.")
                product = models.Product(sku=item.sku, name=item.name, category=item.category or None)
                db.add(product)
                db.flush()
            variant = db.query(models.Variant).filter_by(product_id=product.id, size=item.size).first()
            if variant and variant.is_deleted:
                raise HTTPException(409, f"{item.sku} size {item.size} is in Trash")
            if not variant:
                code = f"{item.sku}_{item.size}"
                if db.query(models.Variant).filter_by(variant_code=code).first():
                    raise HTTPException(409, f"Variant code {code} already exists")
                variant = models.Variant(product_id=product.id, size=item.size, variant_code=code, current_stock=0, last_counted_at=datetime.now(timezone.utc))
                db.add(variant)
                db.flush()
            # Moving-average landed unit cost: preserve valuation of existing
            # pieces instead of replacing every old piece with latest price.
            old_qty = max(int(variant.current_stock), 0) if variant.last_counted_at is not None else 0
            old_cost = (variant.cost_record.unit_cost if variant.cost_record else
                        product.cost_record.unit_cost if product.cost_record else None)
            if old_qty and old_cost is None:
                raise HTTPException(409, f"Set costing for existing {item.sku}/{item.size} ({old_qty} pieces) before receiving more stock")
            old_value = Decimal(old_qty) * old_cost if old_cost is not None else Decimal("0")
            new_avg_cost = ((old_value + Decimal(item.qty_received) * item.unit_cost) /
                            Decimal(old_qty + item.qty_received)).quantize(Decimal("0.01"))
            inventory_service.apply_stock_change(db, variant, item.qty_received, transaction_type=models.TransactionType.RESTOCK, reference=f"purchase_challan:{batch.id}:{batch.challan_no}", created_by="challan_receipt")
            variant.last_counted_at = datetime.now(timezone.utc)
            upsert_variant_cost(db, variant.id, new_avg_cost)
            alert_service.check_variant_alert(db, variant)
        batch.status = "POSTED"
        batch.posted_at = datetime.now(timezone.utc)
        db.commit()
    except Exception:
        db.rollback()
        raise
    excel_service.reconcile_unmapped_order_lines(db)
    return {"status": "POSTED", "id": batch.id, "received_pieces": sum(x.qty_received for x in lines)}

# --- Manual purchase challan editor, alongside existing Excel import ---
class ChallanDraftLine(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    sku: str = Field(default='', max_length=64)
    size: str = Field(min_length=1, max_length=20)
    qty_ordered: int = Field(ge=1, le=1000000)
    qty_received: int = Field(default=0, ge=0, le=1000000)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0)

class ChallanDraft(BaseModel):
    party_name: str = Field(min_length=1, max_length=180)
    challan_no: str = Field(min_length=1, max_length=120)
    order_date: str = Field(min_length=10, max_length=10)
    invoice_no: str = Field(default='',max_length=120)
    lines: list[ChallanDraftLine] = Field(min_length=1,max_length=300)


def _draft_lines(db: Session, draft: ChallanDraft):
    try: date.fromisoformat(draft.order_date)
    except ValueError as e: raise HTTPException(422,'Use ISO YYYY-MM-DD date') from e
    rows=[]; codes=set()
    for n,item in enumerate(draft.lines,1):
        name=item.name.strip();size=item.size.strip().upper();sku=item.sku.strip().upper()
        if not name or not size or any(ord(c)<32 for c in name+size+sku):
            raise HTTPException(422,f'Row {n}: invalid product/size/SKU')
        if item.qty_received > item.qty_ordered:
            raise HTTPException(422,f'Row {n}: received exceeds ordered')
        if not sku:
            products=db.query(models.Product).filter(func.lower(models.Product.name)==name.lower(),models.Product.is_deleted.is_(False)).limit(2).all()
            if len(products)>1: raise HTTPException(422,f'Row {n}: ambiguous product name; enter Master SKU')
            if products: sku=products[0].sku
            else:
                sku=re.sub(r'[^A-Z0-9]+','_',name.upper()).strip('_')[:64]
                exists=db.query(models.Product).filter_by(sku=sku).first()
                if exists and exists.name.casefold()!=name.casefold():
                    raise HTTPException(422,f'Row {n}: Master SKU collision; enter SKU manually')
        key=(sku,size)
        if not sku or key in codes:raise HTTPException(422,f'Row {n}: missing or repeated SKU/size')
        codes.add(key)
        rows.append(dict(sku=sku,name=name,size=size,category=None,
                         qty_ordered=item.qty_ordered,qty_received=item.qty_received,
                         unit_cost=cost_decimal(item.unit_cost)))
    return rows


def _draft_status(lines):
    if not lines: return 'OPEN'
    return 'READY' if all(x['qty_ordered']==x['qty_received'] for x in lines) else ('PARTIAL' if any(x['qty_received'] for x in lines) else 'OPEN')

@router.post('/purchase-challans/manual',status_code=201)
def create_manual_challan(payload: ChallanDraft, db: Session=Depends(get_db)):
    party=payload.party_name.strip();number=payload.challan_no.strip()
    if not party or not number:raise HTTPException(422,'Supplier and invoice reference number are required')
    lines=_draft_lines(db,payload)
    if db.query(PurchaseChallan).filter(func.upper(PurchaseChallan.party_name)==party.upper(),func.upper(PurchaseChallan.challan_no)==number.upper()).first():
        raise HTTPException(409,'This supplier and invoice reference number already exist')
    batch=PurchaseChallan(party_name=party,challan_no=number,invoice_no=payload.invoice_no.strip(),order_date=payload.order_date,
        source_filename='manual',file_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),status=_draft_status(lines))
    db.add(batch);db.flush()
    for line in lines:db.add(PurchaseChallanLine(challan_id=batch.id,**line))
    db.commit()
    return serialize_challan(db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch.id).one())

@router.put('/purchase-challans/{batch_id}/manual')
def edit_manual_challan(batch_id:int,payload:ChallanDraft,db:Session=Depends(get_db)):
    lock_catalog(db)
    db.execute(update(PurchaseChallan).where(PurchaseChallan.id == batch_id).values(status=PurchaseChallan.status))
    batch=db.query(PurchaseChallan).filter_by(id=batch_id).populate_existing().with_for_update().first()
    if batch is None:raise HTTPException(404,'Invoice not found')
    if batch.status == 'POSTED':
        if db.query(PurchaseReturn.id).filter_by(purchase_id=batch_id).first():
            raise HTTPException(409, 'A supplier return exists for this invoice. Original purchase quantities are locked for audit.')
        # Reconcile quantities, not the original stock push. Other receipts and
        # marketplace deductions must never be reversed as part of this edit.
        lines = _draft_lines(db, payload)
        current = db.query(PurchaseChallanLine).filter_by(challan_id=batch_id).order_by(PurchaseChallanLine.id).all()
        old = {(line.sku, line.size): line for line in current}
        new = {(line['sku'], line['size']): line for line in lines}
        if old.keys() != new.keys():
            raise HTTPException(409, 'Posted invoice products/sizes cannot be removed or renamed. Edit quantities on the existing lines.')
        if any(line['qty_received'] != line['qty_ordered'] or line['unit_cost'] is None for line in lines):
            raise HTTPException(409, 'Posted invoice corrections must remain fully received and costed.')
        try:
            for key, line in new.items():
                original = old[key]
                if line['unit_cost'] != original.unit_cost:
                    raise HTTPException(409, 'Changing a posted unit cost requires a separate valuation adjustment; quantity corrections only.')
                delta = line['qty_received'] - original.qty_received
                if not delta:
                    continue
                variant = db.query(models.Variant).join(models.Product).filter(
                    models.Product.sku == key[0], models.Variant.size == key[1],
                    models.Product.is_deleted.is_(False), models.Variant.is_deleted.is_(False)).first()
                if variant is None:
                    raise HTTPException(409, f'{key}: active inventory size not found')
                # Find the original receipt in the ledger, not the coarse timestamp.
                receipt = db.query(models.InventoryTransaction).filter_by(
                    variant_id=variant.id, reference=f'purchase_challan:{batch.id}:{batch.challan_no}',
                    transaction_type=models.TransactionType.RESTOCK).order_by(models.InventoryTransaction.id).first()
                if receipt is None:
                    raise HTTPException(409, 'Original purchase receipt not found in stock ledger; manual review required.')
                later = db.query(models.InventoryTransaction).filter(
                    models.InventoryTransaction.variant_id == variant.id,
                    models.InventoryTransaction.id > receipt.id,
                    models.InventoryTransaction.transaction_type.in_([
                        models.TransactionType.ORDER_DEDUCTION, models.TransactionType.MANUAL_ADJUSTMENT,
                        models.TransactionType.AI_PHOTO_UPDATE, models.TransactionType.INITIAL_STOCK]),
                    or_(models.InventoryTransaction.reference.is_(None),
                        ~models.InventoryTransaction.reference.like('purchase_challan_correction:%'))).first()
                if later:
                    raise HTTPException(409, f'{key}: stock was dispatched or recounted after posting. Correction blocked to protect physical stock; review the ledger.')
                # Reconcile both units and moving-average inventory valuation.
                old_qty = int(variant.current_stock)
                next_qty = old_qty + delta
                old_cost = variant.cost_record.unit_cost if variant.cost_record else (variant.product.cost_record.unit_cost if variant.product.cost_record else None)
                if next_qty < 0 or (old_qty and old_cost is None):
                    raise HTTPException(409, f'{key}: insufficient or unpriced available stock; review the ledger.')
                new_value = Decimal(old_qty) * (old_cost or Decimal('0')) + Decimal(delta) * original.unit_cost
                if new_value < Decimal('-0.01'):
                    raise HTTPException(409, f'{key}: valuation would be negative; review the ledger.')
                try:
                    inventory_service.apply_stock_change(db, variant, delta,
                        transaction_type=models.TransactionType.MANUAL_ADJUSTMENT,
                        reference=f'purchase_challan_correction:{batch.id}:{batch.challan_no}',
                        created_by='posted_challan_correction')
                except inventory_service.InsufficientStockError as exc:
                    raise HTTPException(409, str(exc)) from exc
                if next_qty:
                    upsert_variant_cost(db, variant.id, (max(new_value, Decimal('0')) / Decimal(next_qty)).quantize(Decimal('0.01')))
                    db.flush()
                alert_service.check_variant_alert(db, variant)
            for key, line in new.items():
                original = old[key]
                original.qty_ordered = line['qty_ordered']
                original.qty_received = line['qty_received']
                original.name = line['name']
            batch.party_name=payload.party_name.strip()
            batch.invoice_no=payload.invoice_no.strip()
            # Keep the original bill identity and date for auditability.
            if batch.challan_no != payload.challan_no.strip() or batch.order_date != payload.order_date:
                raise HTTPException(409, 'Posted invoice number/date cannot change; correct quantities only.')
            db.commit()
        except Exception:
            db.rollback()
            raise
        return serialize_challan(db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch.id).one())
    party=payload.party_name.strip();number=payload.challan_no.strip()
    if not party or not number:raise HTTPException(422,'Supplier and invoice reference number are required')
    duplicate=db.query(PurchaseChallan).filter(func.upper(PurchaseChallan.party_name)==party.upper(),func.upper(PurchaseChallan.challan_no)==number.upper(),PurchaseChallan.id!=batch_id).first()
    if duplicate:raise HTTPException(409,'Invoice reference number already exists for this supplier')
    lines=_draft_lines(db,payload)
    batch.party_name=party;batch.challan_no=number;batch.invoice_no=payload.invoice_no.strip();batch.order_date=payload.order_date;batch.status=_draft_status(lines)
    db.query(PurchaseChallanLine).filter_by(challan_id=batch_id).delete(synchronize_session=False)
    db.flush()
    for line in lines:db.add(PurchaseChallanLine(challan_id=batch.id,**line))
    db.commit()
    return serialize_challan(db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=batch.id).one())


# Supplier returns: financially separate from receipts, atomic outbound stock.
class SupplierReturnItem(BaseModel):
    line_id: Optional[int] = Field(default=None, gt=0, strict=True)
    variant_id: Optional[int] = Field(default=None, gt=0, strict=True)
    sku: str = Field(default='', max_length=64)
    size: str = Field(default='', max_length=20)
    name: str = Field(default='', max_length=300)
    unit_cost: Optional[Decimal] = Field(default=None, ge=0, le=99999999, decimal_places=2)
    record_only: bool = False
    quantity: int = Field(gt=0, le=1000000, strict=True)


class SupplierReturnDraft(BaseModel):
    purchase_id: Optional[int] = Field(default=None, gt=0, strict=True)
    party_name: str = Field(default='', max_length=180)
    return_no: str = Field(min_length=1, max_length=120)
    return_date: date
    reason: str = Field(default='', max_length=500)
    lines: list[SupplierReturnItem] = Field(min_length=1, max_length=10000)

    @model_validator(mode='after')
    def check_mode(self):
        if self.purchase_id is not None:
            if not self.reason.strip() or any(x.line_id is None for x in self.lines):
                raise ValueError('Original purchase lines and reason required')
        elif not self.party_name.strip() or any(x.line_id is not None or not x.sku.strip() or not x.size.strip() or x.unit_cost is None for x in self.lines):
            raise ValueError('Supplier, SKU, size, quantity and return rate required')
        return self


def _manual_variant(db, sku, size):
    products = db.query(models.Product).filter(func.upper(models.Product.sku)==sku.upper()).all()
    if len(products)>1:
        raise HTTPException(409, 'Ambiguous Master SKU; check catalog')
    if not products:
        return None, None
    product=products[0]
    if product.is_deleted:
        raise HTTPException(409, 'Product is in Trash; restore it before returning stock')
    variants=db.query(models.Variant).filter(models.Variant.product_id==product.id,func.upper(models.Variant.size)==size.upper()).populate_existing().all()
    if len(variants)!=1 or variants[0].is_deleted:
        raise HTTPException(409, 'Master SKU exists, but size is missing or deleted; check inventory')
    return product, variants[0]


def _manual_row(db, item):
    sku=_return_text(item.sku); size=_return_text(item.size)
    product,v=_manual_variant(db,sku,size)
    if v is None:
        if not item.record_only or item.variant_id is not None:
            raise HTTPException(409, 'SKU not found. Explicitly choose record only, or correct SKU.')
        return (item, None, sku, _return_text(item.name) if item.name.strip() else sku, size, None)
    if item.record_only or item.variant_id!=v.id:
        raise HTTPException(409, 'Catalog match changed. Check SKU/size again before posting.')
    if v.last_counted_at is None or item.quantity>v.current_stock:
        raise HTTPException(409, f'{sku} / {size}: insufficient or uncounted stock')
    avg=v.cost_record.unit_cost if v.cost_record else product.cost_record.unit_cost if product.cost_record else None
    return (item,v,product.sku,product.name,v.size,avg)


def _manual_rows(db,payload):
    rows=[]; seen=set()
    for item in payload.lines:
        row=_manual_row(db,item)
        key=(row[2].upper(),row[4].upper())
        if key in seen:raise HTTPException(422,'Duplicate SKU and size; combine quantities into one item')
        seen.add(key);rows.append(row)
    return rows


@router.get('/purchase-returns/catalog')
def return_catalog(sku: str=Query(...,min_length=1,max_length=64),db:Session=Depends(get_db)):
    products=db.query(models.Product).filter(func.upper(models.Product.sku)==sku.strip().upper()).all()
    if not products:return {'found':False,'items':[]}
    if len(products)!=1 or products[0].is_deleted:raise HTTPException(409,'SKU ambiguous or in Trash; check catalog')
    p=products[0];items=[]
    for v in p.variants:
        if v.is_deleted:continue
        cost=v.cost_record.unit_cost if v.cost_record else p.cost_record.unit_cost if p.cost_record else None
        items.append(dict(variant_id=v.id,sku=p.sku,name=p.name,size=v.size,stock=v.current_stock if v.last_counted_at is not None else None,unit_cost=float(cost) if cost is not None else None))
    return {'found':True,'items':items}


def _return_text(value):
    value = value.strip()
    if not value or any(ord(c) < 32 for c in value):
        raise HTTPException(422, 'Enter non-empty printable return number and reason.')
    return value


def _returned_quantities(db, purchase_id):
    return dict(db.query(PurchaseReturnLine.purchase_line_id, func.sum(PurchaseReturnLine.quantity))
        .join(PurchaseReturn, PurchaseReturn.id == PurchaseReturnLine.return_id)
        .filter(PurchaseReturn.purchase_id == purchase_id)
        .group_by(PurchaseReturnLine.purchase_line_id).all())


def _return_variant(db, invoice, line):
    # Never deduct a new product that happens to reuse an old SKU. Require the
    # original receipt ledger entry as well as the current active SKU and size.
    return db.query(models.Variant).join(models.Product).filter(
        models.Product.sku == line.sku, models.Product.is_deleted.is_(False),
        models.Variant.size == line.size, models.Variant.is_deleted.is_(False),
        models.Variant.transactions.any((models.InventoryTransaction.reference ==
            f'purchase_challan:{invoice.id}:{invoice.challan_no}') &
            (models.InventoryTransaction.transaction_type == models.TransactionType.RESTOCK))
    ).populate_existing().first()


def _return_availability(db, invoice):
    returned = _returned_quantities(db, invoice.id)
    rows = []
    for line in invoice.lines:
        v = _return_variant(db, invoice, line)
        prior = int(returned.get(line.id, 0))
        stock = max(0, int(v.current_stock)) if v and v.last_counted_at is not None else 0
        cost = (v.cost_record.unit_cost if v.cost_record else v.product.cost_record.unit_cost if v.product.cost_record else None) if v else None
        available = min(max(0, line.qty_received - prior), stock) if line.unit_cost is not None and cost is not None else 0
        rows.append(dict(line_id=line.id, sku=line.sku, name=line.name, size=line.size,
            received=line.qty_received, returned=prior, stock=stock, available=available,
            unit_cost=float(line.unit_cost) if line.unit_cost is not None else None,
            note='' if v and v.last_counted_at is not None and cost is not None and line.unit_cost is not None else 'Original active receipt or costing unavailable; review inventory.'))
    return rows


@router.get('/purchase-returns/sources')
def return_sources(q: str = Query('', max_length=200), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = db.query(PurchaseChallan).filter_by(status='POSTED')
    if q.strip():
        term = '%' + q.strip() + '%'
        query = query.filter(or_(PurchaseChallan.party_name.ilike(term), PurchaseChallan.challan_no.ilike(term), PurchaseChallan.invoice_no.ilike(term), PurchaseChallan.lines.any(or_(PurchaseChallanLine.sku.ilike(term), PurchaseChallanLine.name.ilike(term)))))
    total = query.count()
    items = query.order_by(PurchaseChallan.id.desc()).offset(offset).limit(20).all()
    return {'total':total, 'items':[dict(id=b.id, party_name=b.party_name, purchase_no=b.challan_no, purchase_date=b.order_date) for b in items]}


@router.get('/purchase-returns/sources/{purchase_id}')
def return_source(purchase_id: int, db: Session = Depends(get_db)):
    invoice = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=purchase_id, status='POSTED').first()
    if not invoice:
        raise HTTPException(404, 'Posted purchase invoice not found')
    return dict(id=invoice.id, party_name=invoice.party_name, purchase_no=invoice.challan_no,
        purchase_date=invoice.order_date, lines=_return_availability(db, invoice))


def _serialize_return(b):
    lines = [dict(sku=x.sku, name=x.name, size=x.size, purchase_line_id=x.purchase_line_id,
        quantity=x.quantity, unit_cost=float(x.unit_cost), amount=float(x.unit_cost*x.quantity),
        inventory_unit_cost=float(x.inventory_unit_cost) if x.inventory_unit_cost is not None else None, balance_after=x.balance_after, record_only=x.variant_id is None) for x in b.lines]
    return dict(id=b.id, purchase_id=b.purchase_id, return_no=b.return_no, party_name=b.party_name,
        purchase_no=b.purchase_no, return_date=b.return_date, reason=b.reason, status='POSTED',
        created_at=b.created_at, quantity=sum(x.quantity for x in b.lines),
        amount=float(sum((x.unit_cost*x.quantity for x in b.lines), Decimal('0'))), lines=lines)


@router.post('/purchase-returns', status_code=201)
def post_supplier_return(payload: SupplierReturnDraft, db: Session = Depends(get_db)):
    return _post_supplier_return(payload, db)


def _post_supplier_return(payload, db, draft_id=None, revision=None):
    # Draft lookup and consumption share the stock transaction.
    lock_catalog(db)
    draft = None
    if draft_id is not None:
        draft = _get_return_draft(db, draft_id, revision)
        payload = SupplierReturnDraft.model_validate_json(draft.payload)
    number = _return_text(payload.return_no).upper()
    reason = _return_text(payload.reason) if payload.reason.strip() else ""
    if len(number) > 120:
        raise HTTPException(422, 'Return number is too long')
    if payload.purchase_id is not None and len({x.line_id for x in payload.lines}) != len(payload.lines):
        raise HTTPException(422, 'Duplicate purchase line')
    try:
        if payload.purchase_id is None:
            if db.query(PurchaseReturn.id).filter_by(return_no=number).first():raise HTTPException(409,'Return invoice already posted')
            reserved=db.query(PurchaseReturnDraft).filter_by(return_no=number).first()
            if reserved and (draft is None or reserved.id!=draft.id):raise HTTPException(409,'Return number belongs to a saved draft')
            rows=_manual_rows(db,payload)
            batch=PurchaseReturn(purchase_id=None,return_no=number,party_name=_return_text(payload.party_name),purchase_no='',return_date=payload.return_date.isoformat(),reason=reason)
            db.add(batch);db.flush()
            for item,v,sku,name,size,avg in rows:
                if v is not None:
                    inventory_service.apply_stock_change(db,v,-item.quantity,models.TransactionType.MANUAL_ADJUSTMENT,reference=f'purchase_return:{batch.id}:{number}',created_by='supplier_return',expected_current_stock=v.current_stock)
                    alert_service.check_variant_alert(db,v)
                db.add(PurchaseReturnLine(return_id=batch.id,purchase_line_id=None,variant_id=v.id if v is not None else None,sku=sku,name=name,size=size,quantity=item.quantity,unit_cost=item.unit_cost,inventory_unit_cost=avg,balance_after=v.current_stock if v is not None else None))
            if draft is not None:
                db.query(PurchaseReturnDocument).filter_by(draft_id=draft.id).update({PurchaseReturnDocument.return_id:batch.id,PurchaseReturnDocument.draft_id:None},synchronize_session=False)
                db.delete(draft)
            db.commit()
            return _serialize_return(db.query(PurchaseReturn).options(selectinload(PurchaseReturn.lines)).filter_by(id=batch.id).one())
        # Same lock order as receipt posting/correction. SQLite serializes writers;
        # the invoice row lock also protects cumulative quantity validation.
        db.execute(update(PurchaseChallan).where(PurchaseChallan.id == payload.purchase_id).values(status=PurchaseChallan.status))
        invoice = db.query(PurchaseChallan).filter_by(id=payload.purchase_id, status='POSTED').populate_existing().with_for_update().first()
        if not invoice:
            raise HTTPException(404, 'Posted purchase invoice not found')
        if payload.return_date.isoformat() < invoice.order_date:
            raise HTTPException(422, 'Return date cannot precede purchase date')
        if db.query(PurchaseReturn.id).filter_by(return_no=number).first():
            raise HTTPException(409, 'Return invoice number already posted; check saved records before retrying.')
        reserved = db.query(PurchaseReturnDraft).filter_by(return_no=number).first()
        if reserved and (draft is None or reserved.id != draft.id):
            raise HTTPException(409, 'Return number belongs to a saved draft; open the draft to post.')
        availability = {x['line_id']:x for x in _return_availability(db, invoice)}
        original = {x.id:x for x in invoice.lines}
        for item in payload.lines:
            row = availability.get(item.line_id)
            if row is None or item.quantity > row['available']:
                raise HTTPException(409, f'Return exceeds available stock or original purchase balance (line {item.line_id}). Refresh invoice.')
        batch = PurchaseReturn(purchase_id=invoice.id, return_no=number, party_name=invoice.party_name,
            purchase_no=invoice.challan_no, return_date=payload.return_date.isoformat(), reason=reason)
        db.add(batch); db.flush()
        for item in sorted(payload.lines, key=lambda x:x.line_id):
            line = original[item.line_id]
            v = _return_variant(db, invoice, line)
            if v is None:
                raise HTTPException(409, 'Original active stock size not found')
            avg = v.cost_record.unit_cost if v.cost_record else v.product.cost_record.unit_cost if v.product.cost_record else None
            if avg is None or v.last_counted_at is None:
                raise HTTPException(409, 'Current stock costing unavailable')
            # Outbound inventory uses current moving average. Supplier credit is
            # the original receipt price, recorded separately; no refund claim.
            inventory_service.apply_stock_change(db, v, -item.quantity,
                transaction_type=models.TransactionType.MANUAL_ADJUSTMENT,
                reference=f'purchase_return:{batch.id}:{number}', created_by='supplier_return',
                expected_current_stock=v.current_stock)
            alert_service.check_variant_alert(db, v)
            db.add(PurchaseReturnLine(return_id=batch.id, purchase_line_id=line.id, variant_id=v.id,
                sku=line.sku, name=line.name, size=line.size, quantity=item.quantity, unit_cost=line.unit_cost,
                inventory_unit_cost=avg, balance_after=v.current_stock))
        if draft is not None:
            db.query(PurchaseReturnDocument).filter_by(draft_id=draft.id).update({PurchaseReturnDocument.return_id:batch.id,PurchaseReturnDocument.draft_id:None},synchronize_session=False)
            db.delete(draft)
        db.commit()
    except (inventory_service.InsufficientStockError, inventory_service.StaleStockError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    return _serialize_return(db.query(PurchaseReturn).options(selectinload(PurchaseReturn.lines)).filter_by(id=batch.id).one())


def _return_query(db, q, date_filter):
    query = db.query(PurchaseReturn)
    if q.strip():
        term = '%' + q.strip() + '%'
        query = query.filter(or_(PurchaseReturn.return_no.ilike(term), PurchaseReturn.purchase_no.ilike(term), PurchaseReturn.party_name.ilike(term), PurchaseReturn.lines.any(or_(PurchaseReturnLine.sku.ilike(term), PurchaseReturnLine.name.ilike(term)))))
    if date_filter:
        query = query.filter(PurchaseReturn.return_date == date_filter.isoformat())
    return query


@router.get('/purchase-returns')
def list_supplier_returns(q: str = Query('', max_length=200), date_filter: Optional[date] = None,
    offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    query = _return_query(db,q,date_filter)
    total = query.count()
    ids = query.with_entities(PurchaseReturn.id).subquery()
    qty, amount = db.query(func.coalesce(func.sum(PurchaseReturnLine.quantity),0), func.coalesce(func.sum(PurchaseReturnLine.quantity*PurchaseReturnLine.unit_cost),0)).filter(PurchaseReturnLine.return_id.in_(db.query(ids.c.id))).one()
    records = query.options(selectinload(PurchaseReturn.lines)).order_by(PurchaseReturn.id.desc()).offset(offset).limit(6).all()
    return dict(total=total, quantity=int(qty), amount=float(amount), items=[_serialize_return(b) for b in records])


def _return_export(records, filename):
    rows = [['Return No', 'Return Date', 'Supplier', 'Purchase Invoice', 'Master SKU', 'Product', 'Size', 'Returned Qty', 'Return Rate INR', 'Return Value INR', 'Inventory Unit Cost INR', 'Stock After Return', 'Reason']]
    for b in records:
        for x in b.lines:
            rows.append([b.return_no,b.return_date,b.party_name,b.purchase_no,x.sku,x.name,x.size,x.quantity,float(x.unit_cost),float(x.unit_cost*x.quantity),float(x.inventory_unit_cost) if x.inventory_unit_cost is not None else None,x.balance_after,b.reason])
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition':f'attachment; filename="{filename}"'})


@router.get('/purchase-returns/export-records')
def export_supplier_returns(q: str = Query('', max_length=200), date_filter: Optional[date] = None, db: Session = Depends(get_db)):
    return _return_export(_return_query(db,q,date_filter).options(selectinload(PurchaseReturn.lines)).order_by(PurchaseReturn.id.desc()).all(), 'MYRAA_Purchase_Returns.xlsx')


# R24: saved return drafts and report-period totals. Drafts never reserve or deduct stock.
class ReturnDraftUpdate(SupplierReturnDraft):
    revision: str = Field(min_length=32, max_length=32)


class ReturnDraftRevision(BaseModel):
    revision: str = Field(min_length=32, max_length=32)


def _get_return_draft(db, draft_id, revision=None):
    d = db.query(PurchaseReturnDraft).filter_by(id=draft_id).populate_existing().first()
    if not d:
        raise HTTPException(404, 'Return draft not found; it may already have been posted.')
    if revision is not None and d.revision != revision:
        raise HTTPException(409, 'Draft changed in another session. Reopen before saving or posting.')
    return d


def _draft_dict(d):
    return dict(id=d.id, purchase_id=d.purchase_id, return_no=d.return_no, party_name=d.party_name,
        purchase_no=d.purchase_no, return_date=d.return_date, quantity=d.quantity, amount=float(d.amount),
        revision=d.revision, status='DRAFT', payload=json.loads(d.payload))


def _save_return_draft(db, payload, draft_id=None):
    try:
        lock_catalog(db)
        d = _get_return_draft(db, draft_id, payload.revision) if draft_id is not None else None
        if d is not None and d.purchase_id != payload.purchase_id:
            raise HTTPException(409, "Saved draft is linked to its original purchase. Create a new return for another invoice.")
        number = _return_text(payload.return_no).upper()
        reason = _return_text(payload.reason) if payload.reason.strip() else ""
        if len(number)>120 or (payload.purchase_id is not None and len({x.line_id for x in payload.lines})!=len(payload.lines)):
            raise HTTPException(422, 'Invalid return number or duplicate purchase line')
        if payload.purchase_id is None:
            if db.query(PurchaseReturn.id).filter_by(return_no=number).first():raise HTTPException(409,'Return invoice already posted')
            other=db.query(PurchaseReturnDraft).filter_by(return_no=number).first()
            if other and (d is None or other.id!=d.id):raise HTTPException(409,'Return invoice number already saved as draft')
            rows=_manual_rows(db,payload)
            if d is None:d=PurchaseReturnDraft();db.add(d)
            d.purchase_id=None;d.return_no=number;d.party_name=_return_text(payload.party_name);d.purchase_no='';d.return_date=payload.return_date.isoformat()
            d.quantity=sum(x.quantity for x in payload.lines);d.amount=sum((x.quantity*x.unit_cost for x in payload.lines),Decimal('0'))
            clean=SupplierReturnDraft(party_name=d.party_name,return_no=number,return_date=payload.return_date,reason=reason,lines=[item.model_copy(update=dict(sku=sku,name=name,size=size)) for item,v,sku,name,size,avg in rows])
            d.payload=clean.model_dump_json();d.revision=uuid.uuid4().hex
            db.commit();return _draft_dict(d)
        invoice = db.query(PurchaseChallan).options(selectinload(PurchaseChallan.lines)).filter_by(id=payload.purchase_id,status='POSTED').first()
        if not invoice:
            raise HTTPException(404, 'Posted purchase invoice not found')
        if payload.return_date.isoformat()<invoice.order_date:
            raise HTTPException(422, 'Return date cannot precede purchase date')
        if db.query(PurchaseReturn.id).filter_by(return_no=number).first():
            raise HTTPException(409, 'Return invoice already posted')
        other=db.query(PurchaseReturnDraft).filter_by(return_no=number).first()
        if other and (d is None or other.id!=d.id):
            raise HTTPException(409, 'Return invoice number already saved as draft')
        available={x['line_id']:x for x in _return_availability(db,invoice)}
        original={x.id:x for x in invoice.lines}
        for item in payload.lines:
            if item.line_id not in available or item.quantity>available[item.line_id]['available']:
                raise HTTPException(409, 'Return exceeds current available stock or purchase balance. Refresh invoice.')
        if d is None:
            d=PurchaseReturnDraft();db.add(d)
        d.purchase_id=invoice.id;d.return_no=number;d.party_name=invoice.party_name;d.purchase_no=invoice.challan_no
        d.return_date=payload.return_date.isoformat();d.quantity=sum(x.quantity for x in payload.lines)
        d.amount=sum((original[x.line_id].unit_cost*x.quantity for x in payload.lines),Decimal('0'))
        clean=SupplierReturnDraft(purchase_id=invoice.id,return_no=number,return_date=payload.return_date,reason=reason,lines=payload.lines)
        d.payload=clean.model_dump_json(exclude_defaults=True);d.revision=uuid.uuid4().hex
        db.commit();return _draft_dict(d)
    except Exception:
        db.rollback();raise


@router.post('/purchase-returns/drafts',status_code=201)
def create_return_draft(payload: SupplierReturnDraft, db: Session=Depends(get_db)):
    return _save_return_draft(db,payload)


@router.get('/purchase-returns/drafts')
def list_return_drafts(q: str=Query('',max_length=200),date_filter: Optional[date]=None,offset: int=Query(0,ge=0),db: Session=Depends(get_db)):
    query=db.query(PurchaseReturnDraft)
    if q.strip():
        term='%'+q.strip()+'%'
        query=query.filter(or_(PurchaseReturnDraft.return_no.ilike(term),PurchaseReturnDraft.party_name.ilike(term),PurchaseReturnDraft.purchase_no.ilike(term)))
    if date_filter:query=query.filter_by(return_date=date_filter.isoformat())
    return dict(total=query.count(),items=[_draft_dict(x) for x in query.order_by(PurchaseReturnDraft.id.desc()).offset(offset).limit(6).all()])


@router.get('/purchase-returns/drafts/export')
def export_return_drafts(q: str=Query('',max_length=200),date_filter: Optional[date]=None,db: Session=Depends(get_db)):
    query=db.query(PurchaseReturnDraft)
    if q.strip():
        term='%'+q.strip()+'%'
        query=query.filter(or_(PurchaseReturnDraft.return_no.ilike(term),PurchaseReturnDraft.party_name.ilike(term),PurchaseReturnDraft.purchase_no.ilike(term)))
    if date_filter:query=query.filter_by(return_date=date_filter.isoformat())
    rows=[['Return No','Date','Supplier','Purchase Invoice','Pending Pieces','Draft Goods Value INR','Status']]
    for d in query.order_by(PurchaseReturnDraft.id.desc()).all():
        rows.append([d.return_no,d.return_date,d.party_name,d.purchase_no,d.quantity,float(d.amount),'DRAFT'])
    return StreamingResponse(io.BytesIO(_simple_xlsx(rows)),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="MYRAA_Return_Drafts.xlsx"'})


@router.get('/purchase-returns/drafts/{draft_id}')
def get_return_draft(draft_id: int,db: Session=Depends(get_db)):
    return _draft_dict(_get_return_draft(db,draft_id))


@router.put('/purchase-returns/drafts/{draft_id}')
def update_return_draft(draft_id: int,payload: ReturnDraftUpdate,db: Session=Depends(get_db)):
    return _save_return_draft(db,payload,draft_id)


@router.delete('/purchase-returns/drafts/{draft_id}')
def delete_return_draft(draft_id: int,revision: str=Query(...,min_length=32,max_length=32),db: Session=Depends(get_db)):
    try:
        lock_catalog(db);d=_get_return_draft(db,draft_id,revision);db.query(PurchaseReturnDocument).filter_by(draft_id=d.id).delete(synchronize_session=False);db.delete(d);db.commit()
        return {'deleted':True}
    except Exception:db.rollback();raise


@router.post('/purchase-returns/drafts/{draft_id}/post',status_code=201)
def post_return_draft(draft_id: int,payload: ReturnDraftRevision,db: Session=Depends(get_db)):
    try:
        return _post_supplier_return(None,db,draft_id,payload.revision)
    except Exception:db.rollback();raise


@router.get('/purchase-returns/periods')
def return_periods(report_date: date,db: Session=Depends(get_db)):
    def totals(start=None,end=None):
        posted=db.query(func.count(func.distinct(PurchaseReturn.id)),func.coalesce(func.sum(PurchaseReturnLine.quantity),0),func.coalesce(func.sum(PurchaseReturnLine.quantity*PurchaseReturnLine.unit_cost),0)).join(PurchaseReturnLine,PurchaseReturnLine.return_id==PurchaseReturn.id)
        drafts=db.query(func.count(PurchaseReturnDraft.id),func.coalesce(func.sum(PurchaseReturnDraft.quantity),0),func.coalesce(func.sum(PurchaseReturnDraft.amount),0))
        if start:
            posted=posted.filter(PurchaseReturn.return_date>=start);drafts=drafts.filter(PurchaseReturnDraft.return_date>=start)
        if end:
            posted=posted.filter(PurchaseReturn.return_date<=end);drafts=drafts.filter(PurchaseReturnDraft.return_date<=end)
        n,q,a=posted.one();dn,dq,da=drafts.one()
        return dict(count=n+dn,posted_count=n,draft_count=dn,quantity=int(q),amount=float(a),pending_quantity=int(dq),pending_amount=float(da))
    day=report_date.isoformat();month=day[:7]
    return dict(day=totals(day,day),month=totals(month+'-01',month+'-31'),all=totals())


@router.get('/purchase-returns/import-template')
def return_import_template():
    return StreamingResponse(io.BytesIO(_simple_xlsx([['Master SKU','Size','Return Qty','Return Rate','Product Name']])),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="MYRAA_Return_Template.xlsx"'})


async def _read_return_import(file):
    raw=await file.read(MAX_EXCEL_SIZE+1)
    if not raw or len(raw)>MAX_EXCEL_SIZE:raise HTTPException(422,'Excel or CSV must be 1 byte–12 MB')
    # Existing spreadsheet reader; reject oversized expanded XLSX archives first.
    if raw.startswith(b'PK'):
        try:
            with ZipFile(io.BytesIO(raw)) as z:
                if sum(x.file_size for x in z.infolist())>64*1024*1024:raise ValueError('Expanded workbook too large')
        except Exception as e:raise HTTPException(422,'Invalid or oversized workbook') from e
    try:df=read_table(raw,file.filename or '')
    except ValueError as e:raise HTTPException(422,str(e)) from e
    return df


@router.post('/purchase-returns/import-preview')
async def preview_manual_return_import(file:UploadFile=File(...),db:Session=Depends(get_db)):
    df=await _read_return_import(file)
    headers={str(x).strip().lower():x for x in df.columns}
    if not {'master sku','size','return qty','return rate'}<=headers.keys() or len(df)>10000 or df.empty:raise HTTPException(422,'Use Master SKU, Size, Return Qty, Return Rate (1–10000 rows)')
    lines={}
    for idx,row in enumerate(df.to_dict('records'),2):
        sku=clean_code(row[headers['master sku']]);size=clean_code(row[headers['size']]);rate=cost_decimal(row[headers['return rate']])
        try:
            q=Decimal(str(row[headers['return qty']]).strip())
            if not q.is_finite() or q!=q.to_integral_value() or not 1<=q<=1000000 or rate is None:raise ValueError()
        except (InvalidOperation,ValueError):raise HTTPException(422,f'Row {idx}: invalid quantity or return rate')
        if not sku or not size or len(sku)>64 or len(size)>20:raise HTTPException(422,f'Row {idx}: invalid SKU/size')
        p,v=_manual_variant(db,sku,size)
        key=(sku,size)
        if key in lines and lines[key]['unit_cost']!=float(rate):raise HTTPException(422,f'Row {idx}: duplicate SKU/size with different rates')
        qty=lines.get(key,{}).get('quantity',0)+int(q)
        if qty>1000000 or (v is not None and (v.last_counted_at is None or qty>v.current_stock)):raise HTTPException(409,f'Row {idx}: insufficient or uncounted stock')
        name=sku
        if 'product name' in headers:
            raw_name=row[headers['product name']]
            if clean_code(raw_name):name=_return_text(str(raw_name))
            if len(name)>300:raise HTTPException(422,f'Row {idx}: product name too long')
        lines[key]=dict(variant_id=v.id if v is not None else None,sku=p.sku if p is not None else sku,name=p.name if p is not None else name,size=v.size if v is not None else size,stock=v.current_stock if v is not None else None,quantity=qty,unit_cost=float(rate),record_only=v is None)
    return {'lines':list(lines.values())}


@router.post('/purchase-returns/import-preview/{purchase_id}')
async def preview_return_import(purchase_id:int,file:UploadFile=File(...),db:Session=Depends(get_db)):
    df=await _read_return_import(file)
    headers={str(x).strip().lower():x for x in df.columns}
    if not {'master sku','size','return qty'}<=headers.keys() or len(df)>10000 or df.empty:raise HTTPException(422,'Use Master SKU, Size, Return Qty (1–10000 rows)')
    src=return_source(purchase_id,db);available={}
    for r in src['lines']:available.setdefault((r['sku'].strip().upper(),r['size'].strip().upper()),[]).append(r)
    counts={}
    for idx,row in enumerate(df.to_dict('records'),2):
        key=(clean_code(row[headers['master sku']]),clean_code(row[headers['size']]))
        matches=available.get(key,[])
        try:
            q=Decimal(str(row[headers['return qty']]).strip())
            if not q.is_finite() or q!=q.to_integral_value() or not 1<=q<=1000000:raise ValueError()
        except (InvalidOperation,ValueError):raise HTTPException(422,f'Row {idx}: enter a positive whole return quantity')
        if len(matches)!=1:raise HTTPException(422,f'Row {idx}: SKU and size must identify one line in the selected purchase invoice')
        r=matches[0];counts[r['line_id']]=counts.get(r['line_id'],0)+int(q)
        if counts[r['line_id']]>r['available']:raise HTTPException(409,f'Row {idx}: return quantity exceeds available stock or purchase balance')
    return {'lines':[{'line_id':k,'quantity':v} for k,v in counts.items()]}


def _return_documents(db, field, record_id):
    cols=(PurchaseReturnDocument.id,PurchaseReturnDocument.filename,PurchaseReturnDocument.content_type,PurchaseReturnDocument.size_bytes)
    return [dict(id=r.id,filename=r.filename,content_type=r.content_type,size_bytes=r.size_bytes) for r in db.query(*cols).filter(field==record_id).order_by(PurchaseReturnDocument.id).all()]


@router.get('/purchase-returns/drafts/{draft_id}/documents')
def list_draft_documents(draft_id:int,db:Session=Depends(get_db)):
    d=_get_return_draft(db,draft_id)
    return {'revision':d.revision,'items':_return_documents(db,PurchaseReturnDocument.draft_id,draft_id)}


@router.post('/purchase-returns/drafts/{draft_id}/documents')
async def upload_return_documents(draft_id:int,revision:str=Form(...),files:list[UploadFile]=File(...),db:Session=Depends(get_db)):
    if not files or len(files)>MAX_CHALLAN_ATTACHMENTS:raise HTTPException(422,'Maximum 12 attachments per invoice')
    prepared=[]
    for file in files:
        content=await file.read(MAX_INVOICE_BYTES+1)
        if not content or len(content)>MAX_INVOICE_BYTES:raise HTTPException(422,'Each attachment must be 1 byte–10 MB')
        kind=None
        if content.startswith(b'%PDF-'):kind='application/pdf'
        else:
            try:
                with Image.open(io.BytesIO(content)) as im:
                    im.verify();kind={'JPEG':'image/jpeg','PNG':'image/png','WEBP':'image/webp'}.get(im.format)
            except (UnidentifiedImageError,ValueError,OSError,Image.DecompressionBombError):pass
        if not kind:raise HTTPException(422,'Only JPG, PNG, WEBP or PDF documents are allowed')
        name=Path((file.filename or 'document').replace(chr(92),'/')).name[:255]
        name=''.join(c for c in name if ord(c)>=32) or 'document'
        prepared.append((content,kind,name))
    try:
        lock_catalog(db);d=_get_return_draft(db,draft_id,revision)
        if db.query(PurchaseReturnDocument.id).filter_by(draft_id=draft_id).count()+len(prepared)>MAX_CHALLAN_ATTACHMENTS:raise HTTPException(422,'Maximum 12 attachments per invoice')
        for content,kind,name in prepared:db.add(PurchaseReturnDocument(draft_id=draft_id,filename=name,content_type=kind,size_bytes=len(content),content=content))
        d.revision=uuid.uuid4().hex;db.commit()
        return list_draft_documents(draft_id,db)
    except Exception:db.rollback();raise


@router.delete('/purchase-returns/drafts/{draft_id}/documents/{document_id}')
def delete_return_document(draft_id:int,document_id:int,revision:str=Query(...),db:Session=Depends(get_db)):
    try:
        lock_catalog(db);d=_get_return_draft(db,draft_id,revision)
        doc=db.query(PurchaseReturnDocument).filter_by(id=document_id,draft_id=draft_id).first()
        if not doc:raise HTTPException(404,'Document not found')
        db.delete(doc);d.revision=uuid.uuid4().hex;db.commit();return list_draft_documents(draft_id,db)
    except Exception:db.rollback();raise


def _download_return_document(db,document_id,**owner):
    doc=db.query(PurchaseReturnDocument).filter_by(id=document_id,**owner).first()
    if not doc:raise HTTPException(404,'Document not found')
    return Response(doc.content,media_type=doc.content_type,headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(doc.filename,safe=''),'Cache-Control':'private, no-store','X-Content-Type-Options':'nosniff'})


@router.get('/purchase-returns/drafts/{draft_id}/documents/{document_id}')
def download_draft_document(draft_id:int,document_id:int,db:Session=Depends(get_db)):
    return _download_return_document(db,document_id,draft_id=draft_id)


@router.get('/purchase-returns/{return_id}/documents')
def list_posted_return_documents(return_id:int,db:Session=Depends(get_db)):
    if not db.query(PurchaseReturn.id).filter_by(id=return_id).first():raise HTTPException(404,'Return invoice not found')
    return {'items':_return_documents(db,PurchaseReturnDocument.return_id,return_id)}


@router.get('/purchase-returns/{return_id}/documents/{document_id}')
def download_posted_return_document(return_id:int,document_id:int,db:Session=Depends(get_db)):
    return _download_return_document(db,document_id,return_id=return_id)


@router.get('/purchase-returns/{return_id}')
def get_supplier_return(return_id: int, db: Session = Depends(get_db)):
    b = db.query(PurchaseReturn).options(selectinload(PurchaseReturn.lines)).filter_by(id=return_id).first()
    if not b:
        raise HTTPException(404, 'Return invoice not found')
    return _serialize_return(b)


@router.get('/purchase-returns/{return_id}/export')
def export_supplier_return(return_id: int, db: Session = Depends(get_db)):
    b = db.query(PurchaseReturn).options(selectinload(PurchaseReturn.lines)).filter_by(id=return_id).first()
    if not b:
        raise HTTPException(404, 'Return invoice not found')
    return _return_export([b], f'MYRAA_Return_{b.id}.xlsx')
