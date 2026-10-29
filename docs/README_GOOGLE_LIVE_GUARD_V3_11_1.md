Aste Easy Google Live Guard v3.11.1
===================================

Fix mirato al doppio conteggio emerso nello smoke v3.11:
- le chiamate storiche del mese sono memorizzate una volta sola;
- migrazione automatica 5010 -> 2510 SOLO se la firma del bug v3.11 coincide esattamente;
- il core usa network_attempts come totale canonico e external_offset=0;
- il ledger applicativo riserva una chiamata PRIMA di ogni HTTP Google;
- il core Google mantiene in parallelo il proprio hard cap e contatore;
- nessuna scrittura Supabase.

Smoke consigliato:
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-live-guard-smoke --google-live-max-requests 10 --google-monthly-cap 9000

Atteso dopo il primo smoke v3.11 gia eseguito:
- raw state puo mostrare 5010 (bug noto)
- bug_signature_repaired = true
- canonical_used_before = 2510
- dopo 10 nuove chiamate: core_effective_after = 2520, ledger used = 2520
- external_offset_after = 0
- counter_delta_ledger_minus_core = 0
- ZERO scritture DB.
