# Aste Easy v3.15 — ANNCSU persistent index

## Goal
Remove the full STRAD/INDIR scan from each daily run without changing the validated geocoding policy.

The runtime index stores only the ANNCSU fields used by Aste Easy:
- STRAD: municipality/cadastral code, ANNCSU municipality code, national street id, odonym, locality, bilingual odonyms;
- INDIR: ANNCSU municipality code, national street id, normalized civic, latitude, longitude, method;
- precomputed municipality access bounding boxes;
- cadastral -> ANNCSU code counts for the existing territorial crosswalk logic.

The geocoder still calls the same `evaluate_record_v5` and the same policy functions. Only the data-access layer changes.

## 1. Build the index once from the newest official national ZIPs

The builder can auto-detect the newest STRAD and INDIR ZIP/CSV in the project ANNCSU directory or the current user's Downloads folder:

```powershell
.\python\python.exe .\src\geocoding\anncsu_sqlite_index_v1.py --auto
```

Default output:

```text
<ANNCSU_DIR>\anncsu_runtime.sqlite3
<ANNCSU_DIR>\anncsu_runtime.build.json
```

The builder prints the exact STRAD/INDIR files it selected before reading them. Stop with Ctrl+C immediately if those are not the two newly downloaded national datasets.

To specify them explicitly:

```powershell
.\python\python.exe .\src\geocoding\anncsu_sqlite_index_v1.py `
  --strad "C:\Users\<USER>\Downloads\<STRAD>.zip" `
  --indir "C:\Users\<USER>\Downloads\<INDIR>.zip"
```

To intentionally rebuild an existing index:

```powershell
.\python\python.exe .\src\geocoding\anncsu_sqlite_index_v1.py --auto --overwrite
```

The build is offline. It performs zero Supabase writes, zero Google calls and zero ANNCSU API calls.

## 2. Daily production v3.15

v3.15 requires a valid `anncsu_runtime.sqlite3`; it refuses to fall back silently to a multi-million-row CSV scan.

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_v3_15.py --run
```

All v3.14 protections remain: publication-date sorted PVP discovery, monotonic-order guard, daily safety gate, atomic import, resumable geocoding, Google monthly/run caps and final read-only audit.

## 3. ANNCSU Open Data API probe

The API parser is intentionally NOT used for production decisions until one real live JSON response has been validated. Availability can be checked safely with:

```powershell
.\python\python.exe .\src\geocoding\anncsu_api_probe_v1.py
```

Expected while the service is under maintenance: `MAINTENANCE`.
When it returns `JSON_OK`, save/send the output so the API provider can be finalized against the real schema. The intended final architecture is API first -> SQLite fallback -> existing Google policy.
