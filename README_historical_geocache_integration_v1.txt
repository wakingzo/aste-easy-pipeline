Aste Easy v3.18 - Historical Geocache integration v1

Overlay on H:\PVP_Supabase\tools

What changes:
- exact normalized historical-geocache lookup happens before Google
- only usable=1 trusted coordinates can be reused
- fresh current-run CIVICO/STRADA evidence always wins
- no fuzzy matching
- conflicts remain disabled
- successful verified geocodes incrementally update the cache
- future full cache rebuilds retain verified LOCALITA cache reuses

Persistent cache:
runtime/cache/historical_geocache_v1.sqlite3

After extraction:
  docker compose build daily

Safe static/unit check:
  docker compose run --rm --entrypoint python daily -m pytest -q tests/test_historical_geocache_runtime_v1.py

If pytest is not installed in the image, use:
  docker compose run --rm --entrypoint python daily -m py_compile src/geocoding/historical_geocache_runtime_v1.py src/geocoding/google_production_v3_18.py

The daily pipeline command remains unchanged.
