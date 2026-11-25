"""Excel template and transactional bulk product/size import."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from business_api import upsert_product_cost, upsert_variant_cost, add_product_alias, add_alias
from services.catalog_identity import design_key, lock_catalog, duplicate_message, canonical_conflict
from sqlalchemy.orm import Session

from models import Alert, InventoryTransaction, Product, Variant, TransactionType
from services.table_reader import read_table


MAX_IMPORT_ROWS = 20000
MAX_FILE_BYTES = 12 * 1024 * 1024

COLUMN_ALIASES = {
    "sku": {"sku", "master sku", "style code", "sku / style code", "product sku"},
    "name": {"product name", "name", "design name", "design"},
    "category": {"category", "type"},
    "size": {"size", "size name", "variant size"},
    "stock": ("stock", "opening stock", "initial stock", "current inventory", "total inventory (=)", "inventory", "quantity", "qty"),
    "threshold": {"alert below", "alert threshold", "reorder threshold"},
    "target": {"target", "target stock", "target stock level"},
    "cost": ("default unit cost", "purchase cost", "unit cost"),
    "size_cost": ("size unit cost", "size cost"),
    "product_aliases": ("product sku aliases", "whole product sku aliases"),
    "size_aliases": ("size sku aliases", "size-specific sku aliases"),
    "photos": ("photo filenames", "photo files"),
}


def _find_column(columns, aliases):
    normalized = {" ".join(str(column).strip().lower().split()): column for column in columns}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def _cell_text(value):
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def _nonnegative_int(value, *, default=None):
    text = _cell_text(value)
    if not text:
        if default is not None:
            return default
        raise ValueError("Stock is required.")
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        raise ValueError(f"'{text}' must be a whole number 0 or greater.")
    if not number.is_finite() or number != number.to_integral_value() or number < 0:
        raise ValueError(f"'{text}' must be a whole number 0 or greater.")
    return int(number)


def _strip_size_suffix(value: str, size: str) -> str:
    """Remove a terminal size token from a variant SKU/name, if present.

    Catalog exports commonly use codes such as ``STYLE-BLACK-XL`` or
    ``STYLE_BLACK_26``. Keep the color/design portion and group the sizes on
    the shared Product record.
    """
    import re

    value = value.strip()
    if not value or not size:
        return value
    return re.sub(rf"[-_\s]+{re.escape(size.strip())}$", "", value, flags=re.IGNORECASE).strip()


def _design_sku(source_sku: str, size: str) -> str:
    return _strip_size_suffix(source_sku, size).upper() or source_sku.upper()


HEADERS = ["Master SKU", "Product Name", "Category", "Size", "Stock", "Alert Below", "Target Stock",
           "Default Unit Cost", "Size Unit Cost", "Product SKU Aliases", "Size SKU Aliases", "Photo Filenames"]


def build_bulk_product_template() -> bytes:
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(HEADERS)
    widths = [30, 32, 22, 14, 14, 16, 16, 20, 18, 40, 40, 42]
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill("solid", fgColor="0D6454")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[1].height = 34
    for col, width in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(col)].width = width
    for row in range(2, 502):
        for col in range(1, len(HEADERS)+1):
            ws.cell(row, col).number_format = "@" if col in (1,2,3,4,10,11,12) else "0.00" if col in (8,9) else "0"
    ws.freeze_panes = "E2"
    ws.auto_filter.ref = f"A1:L{MAX_IMPORT_ROWS+1}"
    for cols, kind, maximum in [("E:G", "whole", 100000000), ("H:I", "decimal", 99999999)]:
        start, end = cols.split(":")
        dv = DataValidation(type=kind, operator="between", formula1=0, formula2=maximum, allow_blank=True)
        dv.showErrorMessage=True; dv.errorTitle="Invalid number"; dv.error="Enter a valid non-negative value."
        ws.add_data_validation(dv); dv.add(f"{start}2:{end}{MAX_IMPORT_ROWS+1}")
    help_ws = wb.create_sheet("Instructions")
    instructions = [
        ["MYRAA | Complete bulk product import"],
        ["Fill the Products sheet only. One row per size; repeat the exact Master SKU for the same design. The Master SKU does not include the size suffix."],
        ["Master SKU, Size and Stock are required columns. Blank size means FREE SIZE for legacy compatibility. Stock is opening physical stock, not ordered quantity."],
        ["Product Name is the design identity including color (e.g. DESIGN01_BLACK). Same name with a different SKU is blocked; use distinct design/color names for genuinely different products."],
        ["Category is optional. Alert Below defaults to 5; Target Stock defaults to 20. Default Unit Cost and Size Unit Cost are optional non-negative INR values. Size cost overrides default cost."],
        ["Product SKU Aliases: one MARKETPLACE | EXTERNAL-SKU per line; applies to whole product and requires size in orders. Size SKU Aliases: same format, applies only to this row size. Use new lines for multiple aliases. Marketplace may be any platform name."],
        ["Photo Filenames: optional, one filename per line (maximum 8). Repeat only if identical. Select the actual local image files in the bulk import panel along with the workbook. URLs and embedded Excel pictures are not imported."],
        ["Existing master SKU: missing sizes may be added; existing size stock, costs, aliases and photos are not overwritten. Edit existing product details through the product editor. Duplicate rows are reported; conflicting rows skip the whole design."],
        ["Legacy SKU column is accepted and a terminal matching size suffix is grouped. Do not put a size suffix in the new Master SKU column."],
        ["Maximum 20,000 rows / 12 MB for workbook. CSV, XLS and XLSX supported. Example sheet is illustrative only; it is not imported."],
    ]
    for row in instructions: help_ws.append(row)
    help_ws["A1"].font=Font(bold=True,color="FFFFFF",size=15)
    help_ws["A1"].fill=PatternFill("solid",fgColor="0D6454")
    help_ws.column_dimensions["A"].width=116
    for row in help_ws.iter_rows(min_row=2):
        row[0].alignment=Alignment(wrap_text=True,vertical="center")
        row[0].font=Font(color="334155",size=11)
        help_ws.row_dimensions[row[0].row].height=48
    example=wb.create_sheet("Example - do not import")
    example.append(HEADERS)
    example.append(["SAMPLE_V_D01_BLACK","D01_BLACK","Kurta set","S",5,2,20,300,320,"Platform A | EXT-D01-BLACK","Platform A | EXT-D01-BLACK-S","D01-front.jpg\nD01-back.jpg"])
    example.append(["SAMPLE_V_D01_BLACK","D01_BLACK","Kurta set","M",0,2,20,300,None,None,"Platform B | EXT-D01-BLACK-M",None])
    for cell in example[1]:
        cell.font=Font(bold=True,color="FFFFFF"); cell.fill=PatternFill("solid",fgColor="0D6454"); cell.alignment=Alignment(wrap_text=True)
    for col,width in enumerate(widths,1): example.column_dimensions[get_column_letter(col)].width=width
    example.freeze_panes="E2"
    buf=BytesIO(); wb.save(buf); return buf.getvalue()


def _cost(value):
    raw=_cell_text(value)
    if not raw: return None
    try:
        number=Decimal(raw)
        if not number.is_finite() or not 0 <= number <= 99999999: raise ValueError
        return number.quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise ValueError('Cost must be a non-negative amount up to 99,999,999.')


def _aliases(value):
    result=[]
    for line in _cell_text(value).splitlines():
        if not line.strip(): continue
        parts=line.split('|')
        if len(parts)!=2 or not all(p.strip() for p in parts):
            raise ValueError('Alias must use MARKETPLACE | EXTERNAL-SKU, one per line.')
        market,code=(p.strip() for p in parts)
        if len(market)>80 or len(code)>180 or any(ord(c)<32 for c in code+market):
            raise ValueError('Invalid marketplace or external SKU length/characters.')
        result.append((market.upper(),code.upper()))
    return list(dict.fromkeys(result))


def _photos(value):
    result=list(dict.fromkeys(p.strip() for p in _cell_text(value).splitlines() if p.strip()))
    if len(result)>8 or any(len(p)>200 or '/' in p or chr(92) in p or ':' in p or p.startswith('.') or not p.lower().endswith(('.jpg','.jpeg','.png','.webp')) for p in result):
        raise ValueError('Use up to 8 local JPG, PNG or WEBP photo filenames, one per line.')
    return result


def import_bulk_products(db: Session, file_bytes: bytes, filename: str = ""):
    if len(file_bytes)>MAX_FILE_BYTES: raise ValueError('File is larger than 12 MB.')
    df=read_table(file_bytes,filename)
    if df.empty: raise ValueError('The file has no product rows.')
    if len(df)>MAX_IMPORT_ROWS: raise ValueError('Upload at most 20,000 rows at a time.')
    columns={key:_find_column(df.columns,aliases) for key,aliases in COLUMN_ALIASES.items()}
    # Explicit Master SKU is never shortened; legacy SKU preserves suffix grouping.
    explicit=_find_column(df.columns,('master sku',))
    if explicit is not None: columns['sku']=explicit
    missing=[key for key in ('sku','size','stock') if columns[key] is None]
    if missing: raise ValueError('Missing required columns: '+', '.join(missing)+'. Download the template.')
    groups={}; errors=[]; invalid=set(); blanks=free=duplicates=0
    def error(row,sku,message): errors.append({'row':row,'sku':sku,'message':message})
    for row,record in enumerate(df.to_dict('records'),2):
        if not any(_cell_text(v) for v in record.values()): blanks+=1; continue
        def get(k): return record.get(columns[k]) if columns[k] is not None else None
        source=_cell_text(get('sku')).upper()
        if not source: error(row,None,'SKU is required.'); continue
        size=_cell_text(get('size')).upper()
        if not size: size='FREE SIZE'; free+=1
        sku=source if explicit is not None else _design_sku(source,size)
        groups.setdefault(sku,[])
        try:
            name=_cell_text(get('name')) or sku
            if explicit is None: name=_strip_size_suffix(name,size)
            category=_cell_text(get('category'))
            if not design_key(name) or len(sku)>64 or len(name)>300 or len(size)>20 or len(category)>100 or len(sku+'_'+size)>80:
                raise ValueError('Invalid product name, SKU, size or category length.')
            stock=_nonnegative_int(get('stock')); threshold=_nonnegative_int(get('threshold'),default=5); target=_nonnegative_int(get('target'),default=20)
            if max(stock,threshold,target)>100000000: raise ValueError('Stock and thresholds must not exceed 100,000,000.')
            groups[sku].append(dict(row=row,sku=sku,source=source,name=name,category=category,size=size,stock=stock,threshold=threshold,target=target,cost=_cost(get('cost')),size_cost=_cost(get('size_cost')),pa=_aliases(get('product_aliases')),va=_aliases(get('size_aliases')),photos=_photos(get('photos'))))
        except ValueError as exc:
            error(row,sku,str(exc)); invalid.add(sku)
    created=variants=0; photo_assignments=[]; duplicate_warnings=0; missing_sizes=0
    # One writer lock, savepoint per design; a bad alias must never leave half
    # a design or stock ledger entries in the catalog.
    lock_catalog(db)
    catalog={p.sku:p for p in db.query(Product).all()}
    names={}
    for p in catalog.values(): names.setdefault(design_key(p.name),[]).append(p)
    try:
        for sku,entries in groups.items():
            if not entries: continue
            def fail(msg):
                for e in entries: error(e['row'],sku,msg)
            if sku in invalid: fail('Other rows for this design are invalid; all rows skipped.'); continue
            first=entries[0]
            if len({design_key(e['name']) for e in entries})!=1 or len({e['category'].casefold() for e in entries if e['category']})>1 or len({e['cost'] for e in entries if e['cost'] is not None})>1 or len({tuple(e['photos']) for e in entries if e['photos']})>1:
                fail('Conflicting design name, category, default cost or photo list. Keep product details consistent.'); continue
            existing=catalog.get(sku)
            conflict=next((p for p in names.get(design_key(first['name']),[]) if p.sku!=sku),None)
            if conflict:
                fail(duplicate_message(conflict)); duplicate_warnings+=1; continue
            if existing and existing.is_deleted: fail('Master SKU is in Trash; restore it first.'); duplicate_warnings+=1; continue
            if existing and design_key(existing.name)!=design_key(first['name']) and first['name']!=sku:
                fail('Existing Master SKU has a different product name; check the mapping.'); duplicate_warnings+=1; continue
            by_size={}; bad=False
            for e in entries:
                previous=by_size.get(e['size'])
                if previous:
                    if any(previous[k]!=e[k] for k in ('stock','threshold','target','size_cost')):
                        bad=True; break
                    previous['va']=list(dict.fromkeys(previous['va']+e['va']))
                    if e['source']!=sku and e['source']!=previous['source']: previous['va'].append(('OTHER',e['source']))
                else: by_size[e['size']]=dict(e,va=list(e['va']))
            if bad: fail('Repeated size has conflicting stock, thresholds or size cost.'); continue
            known={v.size.upper():v for v in existing.variants} if existing else {}
            new=[e for size,e in by_size.items() if size not in known]
            repeated=[e for size,e in by_size.items() if size in known]
            if not new:
                fail('Duplicate product / sizes already exist. Existing stock and details were not overwritten.'); duplicate_warnings+=1; continue
            # Refuse alias collisions before writing; existing products may add
            # missing sizes only, not silently modify existing metadata.
            try:
                with db.begin_nested():
                    product=existing
                    if product is None:
                        if canonical_conflict(db,sku): raise ValueError('Master SKU already belongs to another product or linked SKU.')
                        product=Product(sku=sku,name=first['name'],category=next((e['category'] for e in entries if e['category']),None))
                        db.add(product); db.flush()
                        cost=next((e['cost'] for e in entries if e['cost'] is not None),None)
                        if cost is not None: upsert_product_cost(db,product.id,cost)
                    now=datetime.now(timezone.utc)
                    for e in new:
                        code=e['source'] if explicit is None and e['source']!=sku else f"{sku}_{e['size']}"
                        if len(code)>80 or canonical_conflict(db,code): raise ValueError(f'Size SKU {code} already belongs to another product or linked SKU.')
                        v=Variant(product_id=product.id,size=e['size'],variant_code=code,current_stock=e['stock'],reorder_threshold=e['threshold'],target_stock_level=e['target'],last_counted_at=now)
                        db.add(v); db.flush()
                        if e['size_cost'] is not None: upsert_variant_cost(db,v.id,e['size_cost'])
                        if e['stock']: db.add(InventoryTransaction(variant_id=v.id,change_qty=e['stock'],transaction_type=TransactionType.INITIAL_STOCK,reference='bulk product import',balance_after=e['stock'],created_by='bulk_product_import'))
                        if e['stock']<=e['threshold']: db.add(Alert(variant_id=v.id,alert_type='OUT_OF_STOCK' if e['stock']<=0 else 'LOW_STOCK',stock_at_alert=e['stock']))
                        # All new aliases must see canonical codes for every new size.
                        e['saved_variant']=v
                    db.flush()
                    if not existing:
                        for market,code in dict.fromkeys(a for e in entries for a in e['pa']):
                            add_product_alias(db,product,market,code); db.flush()
                    for e in new:
                        for market,code in dict.fromkeys(e['va']):
                            add_alias(db,e['saved_variant'],market,code); db.flush()
                if not existing:
                    created+=1; catalog[sku]=product; names.setdefault(design_key(product.name),[]).append(product)
                    photos=next((e['photos'] for e in entries if e['photos']),[])
                    if photos: photo_assignments.append({'sku':sku,'files':photos})
                else:
                    missing_sizes+=len(new)
                variants+=len(new)
                duplicates+=len(entries)-len(by_size)
                if repeated:
                    duplicate_warnings+=1
                    for e in repeated: error(e['row'],sku,'Duplicate existing size; stock and details unchanged.')
                if len(entries)>len(by_size):
                    duplicate_warnings+=1
            except (HTTPException, ValueError, IntegrityError) as exc:
                fail(str(exc.detail) if isinstance(exc,HTTPException) else str(exc) if isinstance(exc,ValueError) else 'SKU or alias already exists; entire design skipped.')
                duplicate_warnings+=1
        db.commit()
    except Exception:
        db.rollback(); raise
    errors.sort(key=lambda e:e['row'])
    return dict(rows_processed=len(df),products_created=created,products_merged=0,variants_created=variants,variants_merged=0,duplicate_rows_merged=duplicates,free_size_rows=free,rows_skipped=max(0,len(df)-blanks-variants-duplicates),blank_rows=blanks,errors=errors[:100],errors_truncated=len(errors)>100,duplicate_warnings=duplicate_warnings,photo_assignments=photo_assignments)
