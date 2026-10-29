# Aste Easy v3.18 — Linux / Docker readiness

v3.18 keeps the v3.17 production logic and adds a portable container layer.
No Cloud provider is selected by the code: the same image can run on a VPS/Docker
or later be adapted to Cloud Run Jobs.

## Persistent vs immutable

The image contains source code and static reference assets. Two host directories are mounted:

- `./data -> /app/data` (ANNCSU + ISTAT SQLite indexes)
- `./runtime -> /app/runtime` (raw files, reports, manifests, Google ledger/cache)

Mutable Google state is redirected to `/app/runtime/cache` in container mode.

## First migration on Windows (ZERO network, ZERO DB writes)

From `H:\PVP_Supabase\tools` after extracting the v3.18 overlay:

```powershell
.\python\python.exe .\src\pipeline\container_prepare_state_v3_18.py
```

It copies the existing Google cache and usage counter into `runtime\cache` only if
those destination files do not already exist. It never overwrites them.

Then create the secret env file:

```powershell
Copy-Item .env.container.example .env.container
notepad .env.container
```

Fill only:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE`
- `GOOGLE_GEOCODING_API_KEY`

Do not share `.env.container`.

## Build

Docker Desktop must be running:

```powershell
docker compose build daily
```

The large `data/` and `runtime/` trees are not sent into the image build context.

## Read-only/local preflight

This checks local assets/env/filesystem only: ZERO Google calls and ZERO Supabase calls.

```powershell
docker compose run --rm daily python src/pipeline/container_preflight_v3_18.py
```

Expected: `"status": "PASS"`.

## Production-equivalent manual run

Only after a PASS preflight:

```powershell
docker compose run --rm daily python src/pipeline/daily_pipeline_v3_18.py --run
```

The normal production safety gates, atomic importer, Google ledger and audit remain active.
A v3.18 run can recover unfinished v3.17/v3.16 manifests before discovering new work.

## Cloud portability

VPS: keep the two bind mounts as persistent disk directories.

Cloud Run Jobs: use the same image, but replace bind mounts with startup/finish sync of
`data/` and `runtime/` against durable object/file storage. The application code no longer
requires Windows paths or a bundled Windows Python interpreter.
