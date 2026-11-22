# MYRAA Private Seller Workspace — Hacker Room

## Delivered from your uploaded project

- **Clerk only**: Google OAuth / email OTP / email+password through Clerk's own authentication UI. Clerk-verified primary email auto-creates and approves a manager account; no MYRAA admin/seller approval workflow. Existing admin remains admin.
- **Private data by seller**: Root `DATABASE_URL` is global Clerk identity/legacy registry; each new seller's inventory gets `/data/seller_workspaces/seller_<user_id>/inventory.db`. Products, sizes, stock transactions, marketplace aliases, uploaded orders, purchase challans and invoice files are NOT shared between accounts. A different Clerk account = a different seller workspace, even if same company.
- **Never silently reassign legacy data**: The existing root `/data/inventory.db` and images are left unchanged. New sellers start empty. To give an existing catalog to someone, first verify the owner's exact Clerk account and restore/back up first; do not drag the legacy SQLite file onto the tenant database. No migration has been performed.
- **Purchase Challans**: Screenshot-inspired white panel, pastel Ordered/Received/Pending/Value counters, editable items, supplier/date, invoice photos/PDF and download Excel. Saves/edit drafts to private seller DB; stock changes only after a fully received/priced challan is explicitly confirmed using the original **one-time** push route. Previous Excel import, search and history are retained.
- **Seller Profile** is in sidebar with name/business/phone/address/photo editing. Seller Accounts and admin approval UI removed.
- **MYRAA Gemini/Claude chat, its MCP bridge and obsolete tests are removed**. Scan & Recount, image similarity search (SigLIP/optional service), orders, SKU matching, picklist, products, stock costing, history, printing continue to use previous business logic.
- **Sounds**: off by default; click Sound On. Buttons beep, successful scans speak short status (where Web Speech is supported), errors get a short low beep. Browser audio policy and user settings apply.
- **Copyright notice**: © 2026 Hacker Room on UI/static previews and new modifications in LICENSE, preserving the pre-existing MIT license and aestroweb-stack attribution. This is attribution, **not government copyright registration**.
- Contacts: `aestroweb@gmail.com`, `+91 8630404031`.

## Install files

1. Backup live `/data/inventory.db`, volume uploads and Git repo **before** changes. Do not reset, rename, or overwrite `/data`.
2. Use **Changed Files ZIP** to copy files onto the exact folders of your open VS Code project; create missing folders. The **Full Source ZIP** is complete updated source but does **not** include your Railway database, secrets or `.git`. Do not delete your repo and re-clone blindly.
3. Remove only the obsolete AI/MCP files named in `RETIRED_AI_FILES.txt` after verifying nothing custom imports them. If preserving personal AI customizations, don't remove those manually until reconciled.
4. Inspect `git status`, run tests if Python dependencies installed, and push after resolving Git conflicts:

```powershell
git status
git diff --check
git add -A
git diff --cached --stat
git commit -m "MYRAA private seller workspaces, Clerk login and challan UI"
git pull --rebase origin main
git push origin main
git status
```

**Don't paste the `PS C:\...>` or `>>` prompt into PowerShell.** If `git pull --rebase` shows conflicts, stop and resolve; **never `git push --force`**. The model cannot control your Windows VS Code or push to GitHub without a connected GitHub app, so delivery of ZIPs does not mean a remote commit occurred.

## Railway runtime

Existing environment:

```env
DATABASE_URL=sqlite:////data/inventory.db
CLERK_PUBLISHABLE_KEY=pk_test_...or_pk_live_...
CLERK_SECRET_KEY=sk_test_...or_sk_live_...
CLERK_AUTHORIZED_PARTIES=https://YOUR-HOST.up.railway.app
MYRAA_USER_SESSION_SECRET=keep-your-existing-long-secret
```

**Persistent volume mount `/data` is required**. Railway provides `RAILWAY_VOLUME_MOUNT_PATH=/data`. Per-seller DB path under volume is automatic. Optional `MYRAA_SELLER_DB_ROOT=/data/seller_workspaces` can override. Never set it under `/app` on Railway. A single Railway instance with SQLite suits small teams/testing; for multi-instance/higher scale, implement tenant-aware Postgres before scaling horizontally.

Clerk dashboard settings (same dev/prod Clerk instance as key pair):

- Enable **Sign-up with email** and **Email verification code**.
- Enable **Sign-up with password** (and sign-in with password) under Password.
- Enable **Sign-in with email using verification code** for OTP.
- Enable **Google Social Connection** and allow sign-ups.
- Use proper Clerk production instance / configured frontend domain to go production. A development `pk_test_...` may have dev domain limitations.

No SMTP app password needed, no local password handling. No MYRAA approval gate. Anyone with a verified Clerk sign-up gets a PRIVATE manager workspace, not another seller's data.

## Checks before accepting production

In Railway Shell:

```bash
cd /app/backend
/app/.venv/bin/python ../tools/check_seller_workspaces.py
```

On a **staging copy** (not your live inventory), sign in as two different verified Clerk sellers. A creates a product `DEMO-A` and challan; B must show zero products, no A challan, and cannot access A's invoice/stock/photo. Then B creates `DEMO-A`, A's stock must remain unchanged. Reload and redeploy; check both databases persist under `/data/seller_workspaces`.

1. Clerk Google / password / OTP sign-in and auto-approved seller dashboard.
2. Edit Seller Profile photo/name/business and reload.
3. Products/size stock + photo, import orders, packing picklist and print.
4. Purchase Challans: Save draft => stock unchanged; upload PDF/photo, reload, edit receipt, download Excel; one-time stock push after all fully received.
5. Scan barcode: toggle Sound On once, success beep + spoken product (depends on browser permission).
6. UI mobile 390px, tablet 834px, desktop 1440px; test Chrome/Edge/Safari and day/night.
7. Backup legacy DB; **no automatic claim / transfer of old products**.

## Test results (local, offline)

57 tests passed, 1 legacy local-password/admin-approval test skipped intentionally. Additional offline FastAPI `TestClient` smoke passed: anonymous inventory denied (401), two independently signed Clerk-linked sellers see different product lists and cannot read each other's catalog. The smoke script uses disposable data and stubs `python-barcode` only when missing in this execution environment. Python compileall & Node JS syntax passed. **Actual Railway Clerk OAuth/email delivery, live server boot, and real mobile rendering were not verified from this container.** Dependencies `python-barcode` and `clerk-backend-api` were not installed in the test runtime; they are specified in requirements for Railway installation. Browser screenshot tool was blocked by the execution environment; HTML static previews are supplied for user-side inspection.

## Static offline previews

- Open `frontend/preview.html` for Dashboard, Purchase Challans, Seller Profile tabs and Day/Night preview.
- Open `frontend/preview-login.html` to view email/password/OTP/Google login design.
- They are **DEMO DATA only**; not a substitute for real Clerk authentication. The live `/app/sign-in.html` is the real Clerk page.

## Legacy ownership

Before any migration of old root `/data/inventory.db` records, the owner must say **which verified Clerk email** receives them. The contact email in the footer is not automatically the owner. Preserve source backup and record counts; migrate selected product/order/finance files without copying other users' Clerk identity records.
