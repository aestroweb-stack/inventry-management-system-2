# MYRAA inventory-management-system-2 — Missing Source / Railway Fix

## Kyon fail hua

`backend/main.py` se `import models` run ho raha hai, lekin GitHub mein `backend/models.py` nahi mila. Previous `MYRAA_Private_Seller_Changed_Files` mein only 31 files thi; full source ke 52 missing code/assets/config files is ZIP mein hain.

Railpack log ne `cd backend && uvicorn ...` ko **build** step mein chalaya. Railway par Build Command galat set hai (ya `RAILPACK_BUILD_CMD` set hai). Build step server chalane ke liye nahi hota.

## VS Code PowerShell — safe path

1. Apne **existing** Git project `MYRAA_Private_Seller_Changed_Files` ko VS Code mein open rakho.
2. Is ZIP ko *usi folder ke root* mein extract/merge karo. ZIP `backend/models.py`, etc. jaisi root-relative paths use karta hai, existing 31 changed source files ko overwrite **nahi** karta. `.git` folder ko kuch nahi hoga.
3. Verify:

```powershell
Test-Path .\backend\models.py
Test-Path .\backend\business_models.py
Test-Path .\backend\schemas.py
Test-Path .\frontend\style.css
Test-Path .\railway.toml
```

Sab par `True` hona chahiye.

```powershell
git status
git add backend frontend railway.toml Procfile .gitignore runtime.txt
git diff --cached --stat
git commit -m "Restore missing MYRAA backend modules and Railway config"
git pull --rebase origin main
git push origin main
git status
```

`git add .` mat karo jab tak `.env`, sqlite DB aur user uploads safely ignored hone ki surety nahi. Agar `git pull --rebase` mein conflict mile to push mat karo; `git status` inspect karo. `git push --force` use mat karo.

## Railway Service settings

- Root Directory: `/` (repository root)
- Builder: `Railpack`
- **Build Command:** EMPTY / remove any command; `cd backend && uvicorn ...` must NOT appear here. If Railway Variables mein `RAILPACK_BUILD_CMD` exists, remove that variable too.
- **Start Command:** `cd backend && uvicorn main:app --host 0.0.0.0 --port $PORT`
- Verify correct GitHub repository and branch: `aestroweb-stack/inventry-management-system-2` / `main`.
- `DATABASE_URL=sqlite:////data/inventory.db`, volume mounted `/data` and existing Clerk/session variables preserved (do not reset DB).

After new build, verify `backend/models.py` exists in committed GitHub tree. If build fails for a different missing import, share **new error**; missing `models` was only the first blocker reported.

## Package verification done here

Merged 31 changed-source files + 52 missing-source/config/assets files: 83 source files total. Python source compilation and all frontend JS syntax checks passed. Live Railway import/runtime/auth and deployment not tested.
