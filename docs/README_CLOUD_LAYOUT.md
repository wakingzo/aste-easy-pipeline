# Aste Easy - cloud-ready layout

## Layout

- `src/importer/`: PVP build/import code
- `src/geocoding/`: unified geocoder and local ANNCSU/ISTAT runtime
- `src/pipeline/daily_pipeline.py`: non-interactive daily entrypoint
- `data/reference/`: small/medium reference datasets (ISTAT, CAP, boundaries)
- `data/anncsu/`: large ANNCSU datasets (`STRAD`, `INDIR`)
- `runtime/cache/`: persistent Google cache and API usage counters
- `runtime/raw/`: PVP raw/intermediate JSONL
- `runtime/handoff/`: touched manifests
- `runtime/reports/`: run CSV/JSON/logs
- `tests/`: regression/layout tests

## Local compatibility

During migration, missing environment variables may still be read from the legacy root `start_pipeline.py`.
This is controlled by `ASTE_ALLOW_LEGACY_CONFIG=1` (default locally).

## Cloud mode

Set `ASTE_ALLOW_LEGACY_CONFIG=0` and inject secrets using the cloud secret manager/environment:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE`
- `GOOGLE_GEOCODING_API_KEY`

Optional mount overrides:

- `ASTE_DATA_DIR`
- `ASTE_REFERENCE_DIR`
- `ASTE_ANNCSU_DIR`
- `ASTE_RUNTIME_DIR`
- `ASTE_CACHE_DIR`
- `ASTE_RAW_DIR`
- `ASTE_HANDOFF_DIR`
- `ASTE_REPORTS_DIR`

The code directory can therefore be immutable/ephemeral while datasets and cache are mounted separately.

## Daily commands

Dry run:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline.py
```

Real daily run:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline.py --apply
```

In a cloud container use the normal interpreter, e.g. `python src/pipeline/daily_pipeline.py --apply`.

## Important next optimization before cloud production

`INDIR_ITA` is currently streamed by the ANNCSU runtime. Before production cloud deployment, convert it to an indexed format (SQLite/Parquet/DuckDB or equivalent) keyed by municipality/progressivo/civic, so the daily job does not scan ~27M rows for a small touched set.
