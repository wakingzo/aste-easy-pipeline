# Aste Easy Daily v3.14

Daily production pipeline with PVP publication-date ordering.

## Main change from v3.13

The PVP search endpoint is requested with:

- `sort=dataPubblicazione,desc`
- `sort=citta,asc`

The builder reads the latest `data_pubblicazione_date` already present in Supabase,
subtracts the configured overlap (`--safety-days`, default 2), and can stop once an
entire page is older than that threshold.

The early stop is fail-safe: before trusting it, v3.14 verifies that every row has a
parseable `dataPubblicazione`, that dates are non-increasing inside each page, and that
page boundaries are also non-increasing. If that verification fails, early stop is
disabled and the builder continues conservatively instead of risking missed auctions.

## Run

From `H:\PVP_Supabase\tools`:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_v3_14.py --run
```

Defaults:

- publication overlap: 2 days
- PVP page size: 200
- daily new-row safety gate: 1500
- max geocode rows: 2000
- Google daily network cap: 1000
- Google monthly app cap: 9000

The rest of the production protections from v3.13 remain unchanged: RPC probe,
idempotent import/retry, staging manifest, geocode resume, guarded/read-back PATCH,
Google ledger/core reconciliation, and final read-only audit.
