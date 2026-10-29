ASTE EASY - Unified Geocoder v11.6.4 RELEASE

Stato QA al 2026-09-15:
- plan scope=all eseguito su 20.619 listing
- ZERO Google / ZERO PATCH nel plan nazionale
- fabbisogno Google invariato rispetto a v11.6.3: 5.599 record; 4.167 job minimi stimati
- specific-first validato: indirizzi localita+strada/civico non vengono degradati subito a LOCALITA
- guard localita esplicita validato: hint secondari non sostituiscono una localita esplicita non confermata
- parser multicivico 51 e 53 validato
- repair storici gia applicati separatamente non fanno parte del normale workflow

Dipendenze dati/progetto non incluse:
- start_pipeline.py
- STRAD_ITA_20260803.csv
- INDIR_ITA_20260803.csv
- Elenco-comuni-italiani.xlsx
- comuni_cap.csv
- Limiti01012026.zip
- google_geocode_cache_v8.json / usage file gia in uso nel progetto

WORKFLOW NORMALE
1) importer v11 -> genera manifest/hybrid
2) assicurarsi che scope sia touched:
   Remove-Item Env:UNIFIED_SCOPE -ErrorAction SilentlyContinue
3) PLAN zero Google / zero patch:
   .\python\python.exe .\geocode_unified_v11_6_4_plan.py
4) Solo se quote/cap Google lo consentono: LIVE dry-run (Google reale, zero patch):
   .\python\python.exe .\geocode_unified_v11_6_4_live.py
5) Dopo verifica dell'output:
   .\python\python.exe .\geocode_unified_v11_6_4_live.py --apply

NON usare UNIFIED_SCOPE=all con il live operativo: scope=all e' solo per audit/plan diagnostici.
