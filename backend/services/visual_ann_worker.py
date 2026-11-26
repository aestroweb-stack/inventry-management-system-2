"""Optional background indexer on the SAME persistent seller volume.
One process at a time (Linux flock). No web requests or user-supplied tenant IDs.
"""
import logging
import os
import threading
from pathlib import Path
from database import SELLER_DB_ROOT, SessionLocal, tenant_scope
from services import visual_ann_service as ann

log = logging.getLogger(__name__)
_stop = threading.Event()
_thread = None


def run_pass(stop):
    import fcntl
    if not SELLER_DB_ROOT.is_dir(): return
    with (SELLER_DB_ROOT / '.visual-ann-r21.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        for directory in sorted(SELLER_DB_ROOT.glob('seller_*')):
            if stop.is_set(): break
            account = directory.name.removeprefix('seller_')
            if not account.isascii() or not account.isdecimal() or not account or account.startswith('0') or len(account)>17: continue
            if directory.is_symlink() or not (directory / 'inventory.db').is_file() or (directory / 'inventory.db').is_symlink(): continue
            try:
                with tenant_scope(account), SessionLocal() as db:
                    ann.build(db, directory / 'product_images', stop)
            except Exception as exc:
                log.warning('Visual background indexing will retry (%s)', type(exc).__name__)


def loop(stop):
    try: interval = max(30, int(os.getenv('VISUAL_QDRANT_WORKER_INTERVAL', '120')))
    except ValueError: interval = 120
    while not stop.is_set():
        try: run_pass(stop)
        except Exception as exc: log.warning('Visual index worker pass failed (%s)', type(exc).__name__)
        stop.wait(interval)


def start():
    global _thread
    if not ann.configured() or os.getenv('VISUAL_QDRANT_WORKER') != '1': return
    if os.name != 'posix':
        log.warning('Visual ANN worker requires a Linux production host')
        return
    if _thread and _thread.is_alive(): return
    _stop.clear()
    _thread = threading.Thread(target=loop,args=(_stop,),name='visual-ann-r21',daemon=True)
    _thread.start()


def shutdown():
    _stop.set()
