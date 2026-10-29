Aste Easy - Google live guard v3.11.5

Fix definitivo contabilità live:
- NON usa più il numero di righe dei CSV google_live_guard come chiamate HTTP;
- legge run_network_attempts dai pvp_import_atomic_v1_*.json completati;
- conserva google_comparison_v3 come storico delle chiamate diagnostiche pre-live;
- core usage + ledger persistenti restano fail-safe e pre-HTTP;
- ripara una sola volta la firma esatta del bug v3.11.4 (cache hit contati come billabili);
- nessuna scrittura Supabase nel live smoke.

Riconciliazione rapida, senza ANNCSU e senza Google:
  .\\python\\python.exe .\\src\\geocoding\\google_usage_reconcile_v3_11_5.py --monthly-cap 9000

Atteso sullo stato del 27/10/2026 dopo v3.11.4:
- comparison_attempts = 2500
- prior_live_guard_network_attempts_exact = 20
- observed_billable_attempts_exact = 2520
- legacy_live_guard_csv_rows = 30
- phantom_live_rows_detected = 10
- repair_signature = V3_11_4_LIVE_CSV_CACHE_HIT_OVERCOUNT
- core_after.effective = 2520
- ledger_after.used = 2520
- counter_delta_ledger_minus_core = 0
- google_http_attempts = 0
