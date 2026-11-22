#!/usr/bin/env python3
"""Offline integration smoke; creates ONLY disposable temp SQLite files."""
import os,sys,tempfile,types,uuid
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
TMP=tempfile.TemporaryDirectory(prefix='myraa-http-smoke-')
os.environ['DATABASE_URL']='sqlite:///'+str(Path(TMP.name)/'legacy.sqlite3')
os.environ['MYRAA_SELLER_DB_ROOT']=str(Path(TMP.name)/'sellers')
os.environ['MYRAA_USER_SESSION_SECRET']='Z'*64
root=Path(__file__).resolve().parents[1]/'backend';sys.path.insert(0,str(root))
try:
 import barcode
except ImportError:
 barcode=types.ModuleType('barcode');barcode.__path__=[];writer=types.ModuleType('barcode.writer');writer.ImageWriter=type('ImageWriter',(object,),{})
 barcode.get=lambda *a,**kw:None;sys.modules['barcode']=barcode;sys.modules['barcode.writer']=writer
import main,models,workspace_models,clerk_auth
from database import AuthSessionLocal,tenant_scope,SessionLocal
with AuthSessionLocal() as db:
 users=[]
 for i in (1,2):
  email=f'test{i}@example.test'
  u=workspace_models.WorkspaceUser(login_id='clerk-test'+str(i),email=email,display_name='Seller '+str(i),business_name='Shop',password_hash='clerk-managed',verified=True,approved=True,active=True,role='manager',token_version=uuid.uuid4().hex)
  db.add(u);db.flush();db.add(clerk_auth.ClerkIdentity(clerk_user_id=f'user_TEST{i}',workspace_user_id=u.id));users.append(u.id)
 db.commit()
from workspace_auth import sign_session,COOKIE
clients=[]
for seller_id in users:
 with AuthSessionLocal() as db:
  user=db.get(workspace_models.WorkspaceUser,seller_id)
  signed=sign_session(user)
 client=TestClient(main.app,base_url='https://inventory.example.test')
 client.cookies.set(COOKIE,signed,domain='inventory.example.test')
 client.cookies.set(clerk_auth.BOUND_COOKIE,clerk_auth.binding_signature(signed),domain='inventory.example.test')
 clients.append(client)
print('anon /api/summary',TestClient(main.app,base_url='https://inventory.example.test').get('/api/summary').status_code)
for n,c in enumerate(clients,1):
 r=c.get('/api/summary'); print('seller',n,'summary',r.status_code, 'response',str(r.text)[:170]);
 if r.status_code !=200: raise Exception('seller summary error')
with tenant_scope(users[0]):
 with SessionLocal() as db:
  db.add(models.Product(sku='TENANT-A-SKU',name='Only seller A'))
  db.commit()
for n,c in enumerate(clients,1):
 r=c.get('/api/products?limit=10'); print('seller',n,'GET products:',r.status_code,r.text[:200]);
 if r.status_code !=200: raise Exception('products error')
 assert ('TENANT-A-SKU' in r.text)==(n==1), 'DATA LEAK'
for c in clients: c.close()
print('HTTP ISOLATION SMOKE PASS')
