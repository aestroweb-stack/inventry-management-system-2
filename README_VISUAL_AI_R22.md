# MYRAA R22 — Private SigLIP 2 model service

Apply only after R21 startup hotfix. This is a deployment package, not a provisioned cloud service. Keep the current inventory volume, SQLite seller DBs, Qdrant namespace and API key unchanged. Back up seller data before deployment. No database migration or stock changes. Model service requires separately billed CPU/RAM/storage; start with 4 GB RAM and 2 vCPU if available, then measure actual peak memory and latency. Large catalogs and packaged-vs-model image accuracy remain untested.

## Railway: NEW service only
1. After pushing R22, use **New → GitHub Repo** in the existing project/environment. Select the SAME repo/branch as the inventory app. Name this second service `visual-model`. Do not replace the inventory service. Root directory: repository root (blank or `/`); keep custom build/start commands blank. If a repository Railway config overrides these settings, verify the effective service config before deployment.
2. Add the following variables on the new `visual-model` service only (use a fresh random secret, not the Qdrant API key). Generate on your own PC with PowerShell: `$bytes = New-Object byte[] 32; $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create(); $rng.GetBytes($bytes); $rng.Dispose(); [Convert]::ToBase64String($bytes)`.

```
RAILWAY_DOCKERFILE_PATH=backend/Dockerfile.visual-ai
PORT=8080
HF_HOME=/model-cache/huggingface
VISUAL_AI_API_KEY=<same private secret on both services>
VISUAL_AI_CPU_THREADS=2
RAILWAY_HEALTHCHECK_TIMEOUT_SEC=900
```

3. Attach a NEW volume to visual-model only: mount path `/model-cache` (suggest at least 5 GB available). The container downloads pretrained `google/siglip2-base-patch16-224` from Hugging Face at first startup and caches it there. No public domain, no seller DB, no Clerk/Gemini/Qdrant credentials on this service. Set healthcheck path `/health` if supported in the service network; first download may take minutes. Docker command binds `::` for private networking and uses a single worker to avoid duplicated model weights. Never set the Dockerfile path on the existing main app service.
4. Deploy and inspect visual-model logs for `SigLIP 2 ready: siglip2-base-patch16-224:v1 (768 dimensions)` and application startup complete. If healthcheck fails despite startup complete, check private networking/IPv6 and Railway healthcheck reachability before proceeding. Do not expose the model service publicly. The actual Docker image build, model weights and Railway networking have not been verified in this environment.
5. Only after the new service is ready, add to the **existing inventory app** service:

```
VISUAL_AI_URL=http://visual-model.railway.internal:8080
VISUAL_AI_API_KEY=<same private secret>
VISUAL_AI_TIMEOUT_SECONDS=8
```

Use the actual private DNS name from Railway if different. Keep Qdrant variables and worker enabled. Redeploy main service. If needed, adjust timeout after measuring inference; browser search currently times out after 25 seconds. A cold semantic exact search may be slow; wait for background indexing before testing. No image data is sent to Gemini/Claude or Hugging Face inference APIs; photos are processed on your private Railway service. Hugging Face is used to fetch pretrained files only.

## Verify
- Login and open `/api/visual-search/status`. Require `external_model_service_ready: true`, `semantic_matching_available: true`, and eventually `ann_index.state: indexed` with matching catalog count. Health shows readiness, not actual accuracy. Qdrant creates a separate encoder-specific collection; no manually-created collection needed. Old local-color collection retained; do not delete it blindly.
- Search a reference photo, inspect DevTools Network `POST /api/products/visual-match` response. `matching_mode: SigLIP 2 semantic image embeddings` and `retrieval_engine: qdrant_ann` proves that request used the Qdrant candidate path. `exact_batched` is exact search. `exact_color_fallback` is review-only local matching if private service fails. It doesn't overwrite semantic vectors; large catalogs can be slow in fallback.
- Check actual images and live SKU/stock, including different views and near-identical designs, and two sellers. Never interpret cosine similarity as confidence probability.
- If service fails, remove `VISUAL_AI_URL` on MAIN app and redeploy to return to local encoder mode. Keep seller data and Qdrant settings. Derived index rebuild may be needed. Old remote collections are not automatically removed; retention reconciliation is later work. Do not remove the existing media backup safety cap.

## Test scope
Local service lifespan/auth and HTTP response contract with mocked encoder; real SQLite seller catalog, mocked Qdrant HTTP and synthetic embeddings; wrong encoder/zero/non-finite output rejection; semantic collection switch; review-only outage fallback that leaves semantic vectors intact. This is not a full VAPT, live Docker build, real model inference benchmark or live Railway deployment. Legacy test_visual_match.py API tests currently expect unauthenticated access and return 401 under current production auth; don't disable auth to make them pass. Run tests only with test databases. Temporary barcode import stub used in local tests.
