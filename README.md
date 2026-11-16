# PR → PO assistant (Corvo procurement)

Chat-driven purchase requests, decided by a policy that is stored as data.

## Run locally
```bash
pip install -r requirements.txt
cd backend && DATA_DIR=./data uvicorn app.main:app --port 8000   # http://localhost:8000
# optional: export ANTHROPIC_API_KEY=...   (better policy-amendment reading; app works without it)
```
Open **Setup** (role: Procurement head) and upload the kit zip (or its CSVs + contract PDFs) once.
Docker: `docker build -t pr2po . && docker run -p 8000:8000 -v pr2po-data:/data pr2po`.

## Typical flows
- **Requester**: *Request (chat)* → “need 200 nitrile gloves for Chakan by next Friday”.
- **Buyer**: *Buyer workspace* → upload PR batch CSV (or use the live chat queue) → click a requirement for the full reasoning → Approve / Override. “Download decisions CSV” gives the export in the required column format.
- **Procurement**: *Policy* → upload amendment or describe it → review the plain-language diff → Approve → *Reprocess* the batch.
- **Preferred suppliers** tab: any item, any as-of date, score breakdown, CSV export (`item_code,rank,supplier_id,reference_price`).

## API (same engine as the UI)
`POST /api/process-batch?format=csv` (multipart `file`, header `X-Role: buyer`), `POST /api/batches/{id}/reprocess`, `GET /api/runs/{id}/export.csv`, `GET /api/preferred-export.csv?as_of=YYYY-MM-DD`, `POST /api/policies/upload`, `POST /api/policies/{id}/approve` (role procurement).

## Deploy
Any Docker host. SQLite needs a persistent volume at `/data` (`render.yaml` provided; Railway/Fly volumes also work). Set `SEED_DATA_URL` to a private zip URL if you want data re-loaded automatically after a restart. Tests: `cd backend && pytest` (`KIT_DIR=<kit path>` adds the worked-example checks).

See `DECISIONS.md` (assumptions) and `APPROACH.md` (design, AI usage).
