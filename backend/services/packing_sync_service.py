"""Strict catalog-based packing consolidation. Does not write to inventory.

Only registered SKU mappings are used; unmatched quantities are retained in review.
Party is read from the registered MASTER SKU, not from marketplace aliases.
"""
from collections import defaultdict, Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from hashlib import sha256
import re
from sqlalchemy.orm import joinedload
from models import Product, Variant
from business_models import MarketplaceSkuAlias, ProductMarketplaceAlias
from services.table_reader import read_table
from services.packing_picklist_service import (PicklistError, MAX_UPLOAD_BYTES, MAX_SOURCE_ROWS,
    SOURCE_COLUMNS, QTY_COLUMNS, SIZE_COLUMNS, _pick_column, _norm_size, _size_order, party_rank)

TOTAL = re.compile(r'^(?:grand[\s_-]*total|sub[\s_-]*total|total)(?:\s*[:.]?\s*)$', re.I)
BRAND_COLUMNS = ('source brand','seller brand','seller_brand','brand','supplier brand','seller account','marketplace')

def clean(value):
    return str(value or '').strip().upper()

def normalized(value):
    return re.sub(r'[_\s-]+', '_', clean(value)).strip('_')

def valid_qty(value):
    try:
        n = Decimal(str(value).strip())
        if not n.is_finite() or n != n.to_integral_value() or not 0 < n <= 100000000:
            raise ValueError
        return int(n)
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        raise PicklistError('Qty must be a positive whole number (maximum 100000000).')

class CatalogIndex:
    def __init__(self, db):
        self.variants = {v.id: v for v in db.query(Variant).join(Product).options(joinedload(Variant.product)).filter(
            Variant.is_deleted.is_(False), Product.is_deleted.is_(False)).all()}
        self.exact = defaultdict(set)
        self.normal = defaultdict(set)
        self.products = defaultdict(set)
        self.products_normal = defaultdict(set)
        self.by_product = defaultdict(list)
        def add(code, vid):
            if clean(code):
                self.exact[clean(code)].add(vid); self.normal[normalized(code)].add(vid)
        def product_code(code, pid):
            if clean(code):
                self.products[clean(code)].add(pid); self.products_normal[normalized(code)].add(pid)
                for v in self.by_product[pid]:
                    for size in {v.size, _norm_size(v.size)} | ({"2XL"} if _norm_size(v.size)=="XXL" else set()):
                        add(f'{code}_{size}', v.id); add(f'{code}-{size}', v.id)
        for v in self.variants.values():
            self.by_product[v.product_id].append(v)
            for code in (v.variant_code,v.seller_sku_code,v.marketplace_sku_code): add(code,v.id)
        for pid, vs in self.by_product.items(): product_code(vs[0].product.sku, pid)
        for a in db.query(MarketplaceSkuAlias).all():
            if a.variant_id in self.variants: add(a.normalized_code,a.variant_id); add(a.alias_code,a.variant_id)
        for a in db.query(ProductMarketplaceAlias).all():
            if a.product_id in self.by_product: product_code(a.normalized_code,a.product_id); product_code(a.alias_code,a.product_id)

    def resolve(self, sku, size):
        raw, norm, size = clean(sku), normalized(sku), _norm_size(size)
        # Exact identifiers take priority. Separator-only fallback never drops design/color tokens.
        for variants, products, key in ((self.exact,self.products,raw),(self.normal,self.products_normal,norm)):
            hits = set(variants.get(key, ()))
            pids = products.get(key, ())
            if pids:
                if size:
                    hits.update(v.id for pid in pids for v in self.by_product[pid] if _norm_size(v.size)==size)
                elif not hits:
                    return None, 'Master/product alias requires a Size column or SKU size suffix'
                else:
                    return None, 'SKU also identifies a whole product; provide Size to disambiguate'
            if len(hits)>1: return None,'Ambiguous registered SKU; check product aliases'
            if hits:
                v=self.variants[next(iter(hits))]
                if size and _norm_size(v.size)!=size: return None,'Uploaded Size conflicts with registered variant'
                return v,''
            if pids: return None,'Size not registered for this product'
        return None,'SKU not registered in active product catalog'


def source_brand(sku, given, children, default):
    lookup = {normalized(c):c for c in children}
    if given:
        n=normalized(given)
        if n in lookup: return lookup[n],''
        # Marketplace exports often wrap seller names in MY_(name).
        wrapped=re.search(r'\(\s*MY[_ -]([^)]*)\)', str(given), re.I)
        if wrapped:
            n=normalized(wrapped.group(1))
            if n in lookup: return lookup[n],''
        # Explicit brand is authoritative; do not guess/override a conflicting value.
        return clean(given),'Source brand is not synced in selected Main Brand'
    key=normalized(sku)
    matches=[(len(n),c) for n,c in lookup.items() if key.startswith(n+'_')]
    if matches: return max(matches)[1],''
    if default: return lookup[normalized(default)],''
    if len(children)==1: return children[0],''
    return 'UNSPECIFIED','Source brand missing; select a fallback brand or supply Brand column'


def master_party(master, prefix):
    # The brand group display name need not equal the physical master prefix.
    # Explicit optional prefix is validated; otherwise first underscore token is the brand.
    parts=clean(master).split('_')
    if prefix:
        p=clean(prefix)+'_'
        if not clean(master).startswith(p): return '', 'Master SKU prefix is outside this group'
        rest=clean(master)[len(p):].split('_')
    else: rest=parts[1:]
    if len(rest)<2 or not rest[0] or not any(rest[1:]):
        return '', 'Master SKU must use BRAND_PARTY_DESIGN format'
    return rest[0],''


def consolidate(db, files, group, selected_date, default_brand='', include_audit=False):
    try: day=date.fromisoformat(selected_date).isoformat()
    except (TypeError,ValueError): raise PicklistError('Picklist date must be YYYY-MM-DD')
    children=group['children']; prefix=group.get('master_prefix','')
    if default_brand and normalized(default_brand) not in {normalized(x) for x in children}:
        raise PicklistError('Fallback brand must belong to selected Main Brand')
    if not files or len(files)>12 or sum(len(p) for _,p in files)>MAX_UPLOAD_BYTES:
        raise PicklistError('Upload 1–12 files, maximum 12 MB combined')
    if len({sha256(payload).digest() for _,payload in files}) != len(files):
        raise PicklistError("Duplicate input file detected; upload each file only once")
    catalog=CatalogIndex(db); audit=[]; skipped=0; aggregate={}; total=0
    for filename,payload in files:
        if not payload or not filename.lower().endswith(('.xlsx','.xls','.csv')): raise PicklistError('Upload Excel or CSV files')
        try: frame=read_table(payload,filename)
        except Exception as exc: raise PicklistError(f'Could not read {filename}: {str(exc)[:150]}') from exc
        keys=list(frame.columns); sku_col=_pick_column(keys,SOURCE_COLUMNS); qty_col=_pick_column(keys,QTY_COLUMNS)
        size_col=_pick_column(keys,SIZE_COLUMNS); brand_col=_pick_column(keys,BRAND_COLUMNS)
        if sku_col is None or qty_col is None: raise PicklistError(f'{filename}: SKU and Qty columns required')
        for num,row in enumerate(frame.where(frame.notna(),None).to_dict(orient='records'),2):
            sku=clean(row.get(sku_col)); qty_raw=row.get(qty_col)
            if TOTAL.fullmatch(sku) or (not sku and any(TOTAL.fullmatch(clean(x)) for x in row.values())):
                skipped+=1; continue
            if not sku and qty_raw is None: skipped+=1; continue
            if not sku: raise PicklistError(f'{filename} row {num}: quantity has no SKU')
            try: qty=valid_qty(qty_raw)
            except PicklistError as exc: raise PicklistError(f'{filename} row {num}: {exc}') from exc
            if len(audit)>=MAX_SOURCE_ROWS: raise PicklistError('Maximum 20,000 item rows per picklist')
            total+=qty; brand,issue=source_brand(sku,row.get(brand_col) if brand_col is not None else '',children,default_brand)
            v,match_issue=catalog.resolve(sku,row.get(size_col) if size_col is not None else '')
            master=clean(v.product.sku) if v else ''; party=''; printable=''; name=v.product.name if v else ''; size=_norm_size(v.size) if v else ''
            if v: party,party_issue=master_party(master,prefix)
            else: party_issue=''
            issue='; '.join(x for x in (issue,match_issue,party_issue) if x)
            if v: printable=f'{master}_{size}'
            item={'file':filename,'row':num,'brand':brand,'source_sku':sku,'master_sku':master,'size':size,'pick_sku':printable,'party':party,'product':name,'qty':qty,'issue':issue}
            audit.append(item)
            if not issue:
                key=(party,v.id)
                entry=aggregate.setdefault(key,{'party':party,'master_sku':master,'size':size,'pick_sku':printable,'product':name,'qty':0,'brands':set()})
                entry['qty']+=qty; entry['brands'].add(brand)
    if not audit: raise PicklistError('No item rows found after ignoring total and blank rows')
    items=sorted(aggregate.values(),key=lambda x:(party_rank(x['party']),x['master_sku'],_size_order(x['size'])))
    for item in items: item['brands']=sorted(item['brands'])
    review=[x for x in audit if x['issue']]; matched=sum(x['qty'] for x in items); review_qty=sum(x['qty'] for x in review)
    if matched+review_qty!=total: raise PicklistError('Quantity reconciliation failed')
    parties=Counter()
    for item in items: parties[item['party']]+=item['qty']
    return {'date':day,'group':group,'files':[x[0] for x in files], 'include_audit':bool(include_audit),'source_rows':len(audit),'total_qty':total,'matched_qty':matched,'review_qty':review_qty,'review_count':len(review),'merged_rows':len(items),'skipped_rows':skipped,'items':items,'audit':audit,'party_totals':dict(parties)}


def pack_lanes(items, capacity=40):
    """Ordered group packing. Never split a party that fits in an empty lane.
    Long parties continue in consecutive lanes. One blank row between parties
    sharing a lane. No padding at end; caller inserts exactly one separator column.
    """
    groups=defaultdict(list)
    for item in sorted(items,key=lambda x:(party_rank(x['party']),x['master_sku'],_size_order(x['size']))): groups[item['party']].append(item)
    lanes=[]; lane=[]
    for group in groups.values():
        if lane:
            if len(lane)+1+len(group)<=capacity: lane.append(None)
            else: lanes.append(lane); lane=[]
        for item in group:
            if len(lane)==capacity: lanes.append(lane); lane=[]
            lane.append(item)
    if lane: lanes.append(lane)
    return lanes


def workbook_bytes(result):
    """Production XLSX exporter, one main group sheet, audit sheets only as requested/needed."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Border, Side, Alignment
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.pagebreak import Break
    wb=Workbook(); ws=wb.active
    name=re.sub(r'[\\/*?:\[\]]','-',result['group']['name']).strip(" '")[:31] or 'Packing'
    if name.lower() in {'review','source audit'}: name='Packing '+name
    ws.title=name; ws.sheet_view.showGridLines=False
    black=Side(style='thin',color='FF000000'); border=Border(left=black,right=black,top=black,bottom=black)
    font=Font(name='Calibri',size=22,bold=True,color='FF000000'); align=Alignment(horizontal='center',vertical='center',shrink_to_fit=False)
    lanes=pack_lanes(result['items'])
    if not lanes:
        # No fake packing quantities when all rows require review.
        ws.cell(1,1,'NO MATCHED ITEMS — REVIEW REQUIRED'); ws['A1'].font=font; ws['A1'].border=border; ws.row_dimensions[1].height=38; ws.column_dimensions['A'].width=70
    for idx,lane in enumerate(lanes):
        col=1+idx*3
        max_sku=max(len(x['pick_sku']) for x in lane if x)
        ws.column_dimensions[get_column_letter(col)].width=max(24,min(235,max_sku*2.5+4))
        ws.column_dimensions[get_column_letter(col+1)].width=max(12,max(len(str(x["qty"])) for x in lane if x)*2.5+3)
        if idx<len(lanes)-1: ws.column_dimensions[get_column_letter(col+2)].width=2.5
        for row,item in enumerate(lane,1):
            ws.row_dimensions[row].height=38
            if item is None: continue
            for c,value in ((col,item['pick_sku']),(col+1,item['qty'])):
                cell=ws.cell(row,c,value)
                if isinstance(value,str): cell.data_type='s'
                cell.font=font; cell.border=border; cell.alignment=align
    ws.print_area=f'A1:{get_column_letter(ws.max_column)}{ws.max_row}'
    ws.print_options.horizontalCentered=True
    ws.page_setup.orientation='landscape'; ws.page_setup.paperSize=ws.PAPERSIZE_A4
    ws.sheet_properties.pageSetUpPr.fitToPage=False
    ws.page_setup.scale=34 # 40 rows × 38pt fit vertically on A4 landscape with these margins
    ws.page_margins.left=.15; ws.page_margins.right=.15; ws.page_margins.top=.2; ws.page_margins.bottom=.2
    for idx in range(2,len(lanes),2): ws.col_breaks.append(Break(id=idx*3))
    # Always include explicit review sheet if quantities were excluded from packing.
    for title,rows in [('Review',[x for x in result['audit'] if x['issue']]),('Source Audit',result['audit'] if result['include_audit'] else [])]:
        if not rows: continue
        sheet=wb.create_sheet(title); keys=['file','row','brand','source_sku','master_sku','party','size','qty','issue']; sheet.append(keys)
        for row in rows:
            sheet.append([row[k] for k in keys])
            for cell in sheet[sheet.max_row]:
                if isinstance(cell.value,str): cell.data_type='s'
        for cell in sheet[1]: cell.font=Font(bold=True); cell.border=border
        for index,width in enumerate([30,10,22,45,45,18,12,12,70],1): sheet.column_dimensions[get_column_letter(index)].width=width
        sheet.freeze_panes='A2'; sheet.auto_filter.ref=sheet.dimensions
    out=BytesIO(); wb.save(out); return out.getvalue()
