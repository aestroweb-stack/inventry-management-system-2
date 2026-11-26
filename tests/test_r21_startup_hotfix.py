"""Boot the real ASGI application without legacy event registration APIs.
Uses a temporary database and no live Qdrant or production credentials.
"""
import os
from pathlib import Path
import subprocess
import sys


def test_real_app_lifespan(tmp_path):
    backend = Path(__file__).resolve().parents[1] / 'backend'
    env = {k: v for k, v in os.environ.items() if not k.startswith(('RAILWAY_', 'VISUAL_', 'CLERK_', 'MYRAA_', 'APP_'))}
    env.update(DATABASE_URL=f'sqlite:///{(tmp_path / "inventory.db").as_posix()}',
               MYRAA_SELLER_DB_ROOT=str(tmp_path / 'sellers'),
               PRODUCT_IMAGE_DIR=str(tmp_path / 'images'),
               PYTHONPATH=os.pathsep.join([str(backend), env.get('PYTHONPATH', '')]))
    script = r'''
import os
import threading
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch
# Reproduce the production API removal, even under older FastAPI versions.
for cls in FastAPI.__mro__:
    if 'add_event_handler' in cls.__dict__:
        delattr(cls, 'add_event_handler')
assert not hasattr(FastAPI, 'add_event_handler')
import main
from services import visual_ann_worker as worker

def check(client):
    assert client.get('/api/health').json() == {'status': 'ok'}
    assert client.get('/api/products').status_code == 401
    assert client.get('/app/').status_code == 200

# Actual worker disabled.
with TestClient(main.app) as client:
    check(client)
    assert worker._thread is None

# Actual worker startup, with no external requests.
started = threading.Event()
with patch.dict(os.environ, {'VISUAL_QDRANT_URL': 'https://example.invalid', 'VISUAL_QDRANT_WORKER': '1'}), patch.object(worker, 'run_pass', lambda stop: started.set()):
    # On non-POSIX hosts, production worker intentionally stays disabled.
    with TestClient(main.app) as client:
        check(client)
        if os.name == 'posix':
            assert started.wait(3)
            assert worker._thread.is_alive()
    assert worker._stop.is_set()
    if worker._thread:
        worker._thread.join(3)
        assert not worker._thread.is_alive()

# Optional worker errors cannot take down inventory.
with patch.object(worker, 'start', side_effect=RuntimeError('test startup error')) as start, patch.object(worker, 'shutdown', side_effect=RuntimeError('test shutdown error')) as shutdown:
    with TestClient(main.app) as client:
        check(client)
    start.assert_called_once()
    shutdown.assert_called_once()
'''
    result = subprocess.run([sys.executable, '-c', script], cwd=backend, env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
