Aste Easy v3.18 - historical locality repair executor

Overlay on H:\PVP_Supabase\tools

Default DRY RUN:
docker compose run --rm --entrypoint python daily src/audit/historical_locality_repair_v3_18.py

IMPORTANT:
Rebuild image after extracting:
docker compose build daily

No DB writes unless --apply is explicitly supplied.
Zero Google calls in both modes.
