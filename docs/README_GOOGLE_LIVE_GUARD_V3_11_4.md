# Aste Easy — Google live guard v3.11.4

Correzione successiva allo smoke v3.11.3.

## Stato confermato
- Core Google e ledger riconciliati a 2510 richieste per 2026-10.
- external_offset = 0.
- Nessuna scrittura Supabase.
- Il run v3.11.3 ha prodotto 10 cache hit e 0 nuove richieste HTTP.

## Novità v3.11.4
Lo smoke seleziona esclusivamente query `google_forward_constrained` la cui chiave `FC|...` NON è presente nella cache esistente.
Non disabilita, cancella o aggira la cache: salta semplicemente i candidati già memorizzati e continua a cercare offline, a blocchi, fino a trovare al massimo il numero richiesto di vere cache miss.

Rimangono attivi:
- hard cap mensile;
- hard cap per esecuzione;
- prenotazione ledger prima dell'HTTP;
- contatore core prima dell'HTTP;
- zero scritture Supabase.

## Smoke consigliato
```
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-live-guard-smoke --google-live-max-requests 10 --google-monthly-cap 9000
```

Se vengono trovate 10 cache miss, l'esito atteso partendo da 2510 è:
- run_network_attempts = 10
- run_cache_hits = 0
- core_effective_after = 2520
- ledger used = 2520
- counter_delta_ledger_minus_core = 0

Il report include inoltre:
- network_miss_selection = true
- candidates_scanned_for_network_miss
- cached_candidates_skipped_before_run
