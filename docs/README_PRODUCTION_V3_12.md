Aste Easy - Production v3.12

SCOPO
- Primo rilascio che abilita scritture reali del nuovo import PVP.
- Mantiene importer new-only atomico per pvp_id.
- Mantiene PVP -> ISTAT -> ANNCSU -> Google P1..P5.
- Bypass Google per localita/frazioni tronche senza civico.
- Hard cap Google doppio: core persistente + ledger Aste Easy, prenotazione prima dell'HTTP.
- Google cache condivisa: cache hit = zero HTTP, zero consumo del cap.
- Tutte le decisioni di geocoding vengono calcolate prima del primo PATCH DB.
- Ogni PATCH e' guardata e verificata con read-back.
- Marker garantito fino al COMUNE: i REVIEW senza pin preciso restano REVIEW, ma lat/lon pubblicate sono il punto ISTAT indicativo del Comune e geocode_precision=COMUNE.

PRECONDIZIONI VALIDATE IL 27/10/2026
- RAW SHA: c7122284223ef6e97b1d7342a51cc3466ec7f96bf917e47181de86cb95ade697
- eligible_new: 9834
- map coverage dry-run: 9834/9834, unmapped=0
- Google usage canonico dopo riconciliazione: 2520
- monthly app cap: 9000
- remaining: 6480
- ledger/core delta: 0

PASSO 1 - FINAL PREFLIGHT (ZERO GOOGLE HTTP, ZERO DB WRITES)
  .\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --production-preflight-v312 --google-monthly-cap 9000

Controllare nel JSON:
- incoming_unique / existing / eligible_new / raw_sha256
- google_calls = 0
- inserted = 0
- existing_writes = 0
- policy_preview.map_coverage.unmapped = 0
- production_v312_readiness.effective_used = 2520 (salvo altri consumi reali nel frattempo)
- production_v312_readiness.ledger.used uguale al core

PASSO 2 - APPLY REALE
Usare ESATTAMENTE expected-new e SHA appena prodotti dal passo 1.
Con lo stato del 27/10/2026 il massimo autorizzabile del run e' 6480:
  .\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --production-apply-v312 --expected-new 9834 --expected-raw-sha256 c7122284223ef6e97b1d7342a51cc3466ec7f96bf917e47181de86cb95ade697 --google-monthly-cap 9000 --google-production-run-cap 6480

IMPORTANTE: se il preflight mostra un consumo Google superiore a 2520, usare come --google-production-run-cap un valore <= remaining del preflight, non 6480 alla cieca.

MANIFEST / RIPRESA
Prima del primo INSERT viene creato:
  runtime/reports/production_manifest_v3_12_*.json

Stati principali:
- AUTHORIZED_NOT_STARTED
- IMPORT_RUNNING
- IMPORTED_PENDING_GEOCODE
- GEOCODE_INTERRUPTED
- COMPLETED

Se l'import e' completato ma il geocoding si interrompe, NON rifare l'import. Riprendere con:
  .\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --production-resume-v312 "H:\PVP_Supabase\tools\runtime\reports\production_manifest_v3_12_....json" --google-monthly-cap 9000 --google-production-run-cap <remaining_autorizzato>

Se invece l'import atomico si interrompe durante gli INSERT, i precedenti RPC riusciti restano committati e il singolo RPC fallito e' rollbackato. In quel caso eseguire un nuovo final preflight: il nuovo eligible_new sara' solo il residuo non ancora importato.

FAIL-SAFE
- SHA/count diverso: zero write.
- asset ISTAT/ANNCSU mancanti: zero write.
- chiave/core/cache/usage Google non pronti: zero write prima dell'import.
- cap mensile o run cap raggiunto durante il geocoding: nessuna ulteriore rete; i record residui usano baseline offline/map fallback e vengono comunque mappati.
- errore Google inatteso: nessun PATCH geocoding; gli INSERT gia' riusciti restano PENDING e sono riprendibili dal manifest.
- modifica concorrente di una riga PENDING: guarded PATCH non la sovrascrive; viene contata come skipped_changed.

REPORT PRODUCTION
- google_production_v3_12_*.csv: decisione per riga
- google_production_v3_12_*.json: contatori, cap, map coverage, patch verification
- pvp_import_atomic_v1_*.json: import atomico
- production_manifest_v3_12_*.json: autorizzazione e stato end-to-end
