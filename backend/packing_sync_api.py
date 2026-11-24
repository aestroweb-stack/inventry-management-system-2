"""Persistent seller-scoped picklist brand groups and immutable packing snapshots."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from secrets import randbelow
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import Column, Integer, String, Text, LargeBinary, Boolean, DateTime, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, defer
from database import Base, get_db
from services import packing_sync_service as svc
from services.packing_picklist_service import MAX_UPLOAD_BYTES, PicklistError

router=APIRouter(prefix='/api/packing-sync',tags=['Packing sync'])

def utcnow(): return datetime.now(timezone.utc)

class BrandGroup(Base):
    __tablename__='myraa_packing_brand_groups'
    id=Column(Integer,primary_key=True)
    name=Column(String(100),nullable=False)
    name_key=Column(String(100),nullable=False,unique=True)
    master_prefix=Column(String(100),nullable=False,default='')
    children_json=Column(Text,nullable=False)
    updated_at=Column(DateTime(timezone=True),nullable=False,default=utcnow,onupdate=utcnow)

class SavedPicklist(Base):
    __tablename__='myraa_packing_saved_runs'
    id=Column(Integer,primary_key=True)
    group_id=Column(Integer,nullable=False,index=True)
    picklist_date=Column(String(10),nullable=False,index=True)
    fingerprint=Column(String(64),unique=True,nullable=False)
    summary_json=Column(Text,nullable=False)
    result_json=Column(Text,nullable=False)
    workbook=Column(LargeBinary,nullable=False)
    created_at=Column(DateTime(timezone=True),default=utcnow,nullable=False)
    hidden=Column(Boolean,default=False,nullable=False,index=True)

class GroupInput(BaseModel):
    name: str=Field(min_length=1,max_length=100)
    master_prefix: str=Field(default='',max_length=100)
    children: list[str]=Field(default_factory=list,max_length=100)


def group_dict(g): return {'id':g.id,'name':g.name,'master_prefix':g.master_prefix,'children':json.loads(g.children_json)}

def clean_group(body):
    name=body.name.strip(); children=[x.strip() for x in body.children if x.strip()] or [name]
    if not name or not svc.normalized(name) or any(not x or len(x)>100 for x in children) or any(ord(c)<32 for x in [name,*children] for c in x):
        raise HTTPException(422,'Valid Main Brand and source brand names are required (1–100 characters)')
    try: svc.brand_lookup(children)
    except PicklistError as exc: raise HTTPException(422,str(exc)) from exc
    return name,'',children

@router.get('/groups')
def list_groups(db:Session=Depends(get_db)):
    return [group_dict(g) for g in db.query(BrandGroup).order_by(BrandGroup.name_key).all()]

@router.post('/groups',status_code=201)
def create_group(body:GroupInput,db:Session=Depends(get_db)):
    name,prefix,children=clean_group(body)
    # SQLite can recycle the last deleted integer PK; old saved runs must never
    # become associated with a newly created group. Random safe 31-bit IDs also
    # keep simultaneous independent creates from using the same next integer.
    while True:
        group_id=1+randbelow(2**30-1)
        if db.get(BrandGroup,group_id) is None and not db.query(SavedPicklist.id).filter_by(group_id=group_id).first(): break
    g=BrandGroup(id=group_id,name=name,name_key=svc.normalized(name),master_prefix=prefix,children_json=json.dumps(children,ensure_ascii=False))
    db.add(g)
    try: db.commit()
    except IntegrityError: db.rollback(); raise HTTPException(409,'Main Brand already exists')
    db.refresh(g); return group_dict(g)

@router.put('/groups/{group_id}')
def update_group(group_id:int,body:GroupInput,db:Session=Depends(get_db)):
    g=db.get(BrandGroup,group_id)
    if g is None: raise HTTPException(404,'Brand group not found')
    name,prefix,children=clean_group(body)
    g.name=name; g.name_key=svc.normalized(name); g.master_prefix=prefix; g.children_json=json.dumps(children,ensure_ascii=False)
    try: db.commit()
    except IntegrityError: db.rollback(); raise HTTPException(409,'Main Brand already exists')
    db.refresh(g); return group_dict(g)

@router.delete('/groups/{group_id}')
def remove_group(group_id:int,db:Session=Depends(get_db)):
    g=db.get(BrandGroup,group_id)
    if g is None: raise HTTPException(404,'Brand group not found')
    db.delete(g); db.commit(); return {'deleted':True} # snapshots have no cascading FK


def summary(run):
    return {**json.loads(run.summary_json),'id':run.id,'created_at':run.created_at.isoformat()}

@router.post('/process')
def process(files:list[UploadFile]=File(...), group_id:int=Form(...), picklist_date:str=Form(...), default_brand:str=Form(''), file_marketplaces:str=Form('[]'), include_audit:bool=Form(False), db:Session=Depends(get_db)):
    g=db.get(BrandGroup,group_id)
    if g is None: raise HTTPException(404,'Choose an existing Main Brand')
    if not 1<=len(files)<=12: raise HTTPException(422,'Upload 1–12 files')
    uploads=[]; remaining=MAX_UPLOAD_BYTES
    try:
        for f in files:
            data=f.file.read(remaining+1); remaining-=len(data)
            if remaining<0: raise PicklistError('Maximum 12 MB combined')
            uploads.append(((f.filename or 'file').replace('\\','/').split('/')[-1][:255],data))
        try: markets=json.loads(file_marketplaces)
        except (TypeError,ValueError): raise PicklistError('Invalid file marketplace settings')
        if not isinstance(markets,list) or (markets and len(markets)!=len(uploads)) or any(not isinstance(x,str) or len(x)>80 for x in markets): raise PicklistError('Invalid file marketplace settings')
        result=svc.consolidate(db,uploads,group_dict(g),picklist_date,default_brand,include_audit,markets)
    except PicklistError as exc: raise HTTPException(422,str(exc)) from exc
    serialized=json.dumps(result,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    # Same file + config + result = same snapshot, even after a retry. If mappings
    # change, reprocessing creates a new version rather than rewriting history.
    fingerprint=sha256(serialized.encode()).hexdigest()
    existing=db.query(SavedPicklist).options(defer(SavedPicklist.workbook),defer(SavedPicklist.result_json)).filter_by(fingerprint=fingerprint).first()
    if existing:
        if existing.hidden: existing.hidden=False; db.commit()
        return summary(existing)
    s={k:v for k,v in result.items() if k not in ('items','audit')}
    binary=svc.workbook_bytes(result)
    run=SavedPicklist(group_id=group_id,picklist_date=result['date'],fingerprint=fingerprint,summary_json=json.dumps(s,ensure_ascii=False),result_json=serialized,workbook=binary)
    db.add(run)
    try: db.commit()
    except IntegrityError:
        db.rollback(); run=db.query(SavedPicklist).filter_by(fingerprint=fingerprint).one()
        if run.hidden: run.hidden=False; db.commit()
    return summary(run)

@router.get('/history')
def history(day:str='',group_id:int|None=None,page:int=Query(1,ge=1),page_size:int=Query(10,ge=1,le=50),db:Session=Depends(get_db)):
    q=db.query(SavedPicklist).filter(SavedPicklist.hidden.is_(False))
    if day: q=q.filter_by(picklist_date=day)
    if group_id is not None: q=q.filter_by(group_id=group_id)
    count=q.count(); pages=max(1,(count+page_size-1)//page_size); page=min(page,pages)
    runs=q.options(defer(SavedPicklist.result_json),defer(SavedPicklist.workbook)).order_by(SavedPicklist.picklist_date.desc(),SavedPicklist.id.desc()).offset((page-1)*page_size).limit(page_size).all()
    return {'items':[summary(r) for r in runs],'page':page,'pages':pages,'count':count}


def visible_run(db,id,*,binary=False):
    q=db.query(SavedPicklist).filter_by(id=id,hidden=False)
    if binary: q=q.options(defer(SavedPicklist.result_json))
    else: q=q.options(defer(SavedPicklist.workbook))
    r=q.first()
    if not r: raise HTTPException(404,'Saved picklist not found')
    return r

@router.get('/history/{id}')
def details(id:int,view:str='audit',status:str='all',brand:str='',marketplace:str='',q:str='',page:int=Query(1,ge=1),page_size:int=Query(25,ge=1,le=100),db:Session=Depends(get_db)):
    r=visible_run(db,id); result=json.loads(r.result_json)
    if view not in ('audit','items'): raise HTTPException(422,'Invalid view')
    rows=result[view]
    if view=='audit':
        if status=='review': rows=[x for x in rows if x['issue']]
        elif status=='matched': rows=[x for x in rows if not x['issue']]
        if brand: rows=[x for x in rows if x['brand']==brand]
    if marketplace: rows=[x for x in rows if x.get('marketplace','')==marketplace]
    if q.strip():
        key=q.strip().casefold(); rows=[x for x in rows if key in ' '.join(str(x.get(k,'')) for k in ('source_sku','master_sku','pick_sku','product','party','marketplace')).casefold()]
    pages=max(1,(len(rows)+page_size-1)//page_size); page=min(page,pages)
    return {'summary':summary(r),'rows':rows[(page-1)*page_size:page*page_size],'count':len(rows),'page':page,'pages':pages}

@router.get('/history/{id}/excel')
def download(id:int,allow_partial:bool=False,db:Session=Depends(get_db)):
    r=visible_run(db,id,binary=True); s=json.loads(r.summary_json)
    if s['review_count'] and not allow_partial: raise HTTPException(409,'Unmatched rows remain. Confirm partial export; they are included in Review sheet, not packing rows.')
    name=re.sub(r'[^A-Za-z0-9_-]','-',s['group']['name'])[:60] or 'Brand'
    return Response(r.workbook,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':f'attachment; filename="MYRAA_Packing_{name}_{r.picklist_date}.xlsx"','X-Picklist-Total-Qty':str(s['total_qty']),'X-Picklist-Matched-Qty':str(s['matched_qty'])})

@router.delete('/history/{id}')
def delete_history(id:int,db:Session=Depends(get_db)):
    r=visible_run(db,id); r.hidden=True; db.commit(); return {'deleted':True}
