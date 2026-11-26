"""Private CPU SigLIP model service, no seller DB or inventory credentials."""
from __future__ import annotations

import hmac
import os
import logging
from contextlib import asynccontextmanager
from io import BytesIO
from threading import BoundedSemaphore

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from PIL import Image
from services import visual_match_service

log = logging.getLogger("uvicorn.error")
_slots = BoundedSemaphore(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not os.getenv("VISUAL_AI_API_KEY", "").strip():
        raise RuntimeError("VISUAL_AI_API_KEY must be configured before starting the model service")
    log.info("Loading SigLIP 2 encoder and running startup inference")
    # Test actual inference, not just imports, before accepting traffic.
    sample = BytesIO()
    Image.new("RGB", (224, 224), (90, 120, 150)).save(sample, format="PNG")
    visual_match_service.image_embedding(sample.getvalue())
    log.info("SigLIP 2 ready: %s (%d dimensions)", visual_match_service.MODEL_VERSION, visual_match_service.VECTOR_DIMENSIONS)
    yield


app = FastAPI(title="Private Visual Model", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def _authorize(authorization: str | None) -> None:
    expected = os.getenv("VISUAL_AI_API_KEY", "").strip()
    if not expected:
        raise HTTPException(503, "Visual model service authentication is not configured.")
    supplied = authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(401, "Unauthorized")


@app.get("/health")
def health(authorization: str | None = Header(default=None)):
    # Railway health probes may omit credentials; the app always sends its key.
    if authorization is not None:
        _authorize(authorization)
    return {"status": "ready", "model": visual_match_service.MODEL_VERSION, "dimensions": visual_match_service.VECTOR_DIMENSIONS}


@app.post("/v1/embed")
def embed_image(file: UploadFile = File(...), authorization: str | None = Header(default=None)):
    _authorize(authorization)
    if not _slots.acquire(timeout=5):
        raise HTTPException(503, "Visual model busy; retry later")
    try:
        content = file.file.read(visual_match_service.MAX_SCAN_BYTES + 1)
        values = visual_match_service.image_embedding(content)
        return {"model": visual_match_service.MODEL_VERSION, "embedding": values}
    except visual_match_service.InvalidProductImage as exc:
        raise HTTPException(400, str(exc)) from exc
    except visual_match_service.ImageModelUnavailable as exc:
        raise HTTPException(503, "Visual model inference unavailable") from exc
    finally:
        _slots.release()
