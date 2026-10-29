# Aste Easy v3.18 — GitHub Actions v1

## Why this layout

The validated v3.18 production logic remains unchanged. GitHub Actions only supplies a Linux runner, restores persistent assets/state, runs the same Docker container, and saves state again.

The Git repository contains code and small static references only. It intentionally excludes:

- secrets;
- `runtime/`;
- the ~2.29 GB ANNCSU SQLite index;
- raw PVP JSONL;
- ISTAT extracted municipality shapefiles.

## One-time repository setup

Use a **private repository**.

Create these repository Actions secrets:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE`
- `GOOGLE_GEOCODING_API_KEY`

Never commit `.env.container`, `start_pipeline.py`, `google_geocoding_api_key.bat`, or any file containing those values.

## One-time cloud seed

Run on the Windows production machine:

```powershell
powershell -ExecutionPolicy Bypass -File .\cloud\make_cloud_seed.ps1
```

It creates `H:\PVP_Supabase\cloud_seed` containing:

- `anncsu_runtime.sqlite3.part001`, `part002`, ... (each < 2 GiB);
- `aste-easy-state-seed.zip` with the Google cache/counters, historical geocache and reports;
- `SHA256SUMS.txt`.

Create a private GitHub Release whose tag is exactly `cloud-seed` and attach all ANNCSU parts plus `aste-easy-state-seed.zip`.

The release is only a bootstrap source. After the first successful workflow, daily executions restore these assets from GitHub Actions cache.

## ISTAT data

Small static references are committed in the repository:

- `data/reference/Elenco-comuni-italiani.xlsx`
- `data/reference/comuni_cap.csv`
- `data/istat/istat_localita_2021.sqlite3`

The 2026 municipality-boundary ZIP is downloaded from the official ISTAT URL on the first run, extracted, validated and then cached. The v3.18 geocoder requires this boundary archive; the already-extracted local Windows folder does not need to be committed.

## First run

Open **Actions → Aste Easy daily update → Run workflow**.

The first run is intentionally heavier because it has to bootstrap and cache ANNCSU, ISTAT boundaries and the mutable state. Subsequent runs should restore those caches.

The workflow runs the unchanged production command through Docker:

```text
docker compose run --rm daily
```

The Docker entrypoint executes `container_preflight_v3_18.py` before production code.

## Schedule

Initial observation phase: every day at 03:17 Europe/Rome.

After 7–10 days, use the manifests to decide whether Saturday can be removed and whether Sunday evening is sufficient for weekend catch-up.

## Persistent state

A rolling Actions cache persists:

- `runtime/cache` (Google response cache, API counter, historical geocache);
- `runtime/state` (independent monthly budget ledger);
- `runtime/reports` (including unfinished manifests/staging needed by v3.18 resume logic).

`runtime/raw` is intentionally not persisted: it is reproducible from PVP and can approach 1 GB.

## ANNCSU monthly refresh

For now the cloud job uses the exact validated ANNCSU SQLite snapshot built from the 2026-09-15 national dataset. Do not switch to the public point API until a real `JSON_OK` response has been validated.

When a new monthly national dataset is intentionally adopted:

1. rebuild `anncsu_runtime.sqlite3` locally with the validated index builder;
2. validate it locally;
3. create new release seed parts;
4. change `ANNCSU_CACHE_KEY` in `.github/workflows/daily.yml` (for example `anncsu-sqlite-v1-20261015`);
5. run the workflow manually once before trusting the schedule.
