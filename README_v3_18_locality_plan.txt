Aste Easy v3.18 - historical locality repair planner

Overlay this ZIP on H:\PVP_Supabase\tools

READ ONLY. Zero DB writes. Zero Google calls.

Run in Docker:
docker compose run --rm --entrypoint python daily src/audit/historical_locality_repair_plan_v3_18.py

It automatically reads the latest:
runtime/reports/locality_consistency_v3_17_1_*.csv

Outputs JSON + CSV under runtime/reports.
