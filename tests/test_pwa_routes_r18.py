"""Run with an isolated test DATABASE_URL and installed project dependencies."""
from io import BytesIO
from PIL import Image
from fastapi.testclient import TestClient
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'backend'))

def test_pwa_routes():
    from main import app
    c=TestClient(app)
    for path in ['/app/','/app/sign-in.html','/app/manifest.webmanifest','/app/sw.js','/app/offline.html','/app/pwa-install.js','/app/pwa-mobile.css']:
        r=c.get(path);assert r.status_code==200,path
        assert 'no-store' in r.headers.get('cache-control',''),path
    m=c.get('/app/manifest.webmanifest');assert 'application/manifest+json' in m.headers['content-type']
    assert 'javascript' in c.get('/app/sw.js').headers['content-type']
    for name,size in [('icon-192',192),('icon-512',512),('icon-maskable',512),('apple-touch-icon',180)]:
        r=c.get('/app/icons/'+name+'.png');assert r.status_code==200
        with Image.open(BytesIO(r.content)) as im:assert im.size==(size,size)
    assert c.get('/api/products').status_code==401
    assert c.get('/api/health').headers['x-myraa-release']=='nov25-r18'
