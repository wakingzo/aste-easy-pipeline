Aste Easy - Historical Geocache Builder v1

Overlay on H:\PVP_Supabase\tools

After extracting:
  docker compose build daily

Then:
  docker compose run --rm --entrypoint python daily src/geocoding/build_historical_geocache_v1.py

Safety:
- Supabase READ ONLY
- Google ZERO calls
- writes only runtime/cache/historical_geocache_v1.sqlite3
- report in runtime/reports

This first phase only measures/builds the cache. It is NOT yet used by the daily pipeline.
