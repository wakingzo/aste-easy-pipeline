# Aste Easy daily v3.16 — fast delta planner

v3.16 keeps the v3.15 PVP publication-sorted discovery, ANNCSU SQLite runtime,
atomic import/resume, Google caps and final audit. It optimizes only the planning stage.

## Planner changes

1. Reads the current Supabase `listings.pvp_id` column with keyset pagination (about
   one request per 1000 DB rows) instead of querying every RAW ID in batches of 150.
2. For RAW IDs absent from Supabase, checks the auction lifecycle date before doing
   expensive listing/address/admin normalization.
3. Only rows inside the retention/future window are fully derived and checked against ISTAT.
4. Subjects and attachments are scanned/retained only for relevant IDs.
5. The RAW SHA-256 and atomic import contract are unchanged.

## Read-only equivalence benchmark

    .\python\python.exe .\src\importer\plan_benchmark_v3_16.py

Expected: `[PLAN BENCH] RESULT=PASS`. This performs zero DB writes and zero Google calls.

## Daily

    .\python\python.exe .\src\pipeline\daily_pipeline_v3_16.py --run

The daily manifest includes `plan_meta.planner_stats` with DB page count, date pre-screen
survivors/rejections and number of fully derived rows.


## v3.16.1 pagination fix
Existing listing IDs are fetched with stable offset/limit pagination, independent of whether Supabase stores pvp_id as text or numeric.
