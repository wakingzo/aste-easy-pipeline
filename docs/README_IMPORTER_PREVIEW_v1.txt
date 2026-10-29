ASTE EASY — CONSOLIDAMENTO, FASE 1: IMPORTER UNICO IN ANTEPRIMA

Scopo: riportare nel futuro importer unico, conservando intatto l'originale operativo
le regole di selezione dei soli ID nuovi, data d'asta (14 giorni di
retention / >730 giorni in revisione), Comune ISTAT ufficiale e sorgente
geografica coerente. Non importa pvp_import_incremental_v12_3.py.
Non modifica il builder; non completa ancora il geocoder o lo stato persistente.

ATTENZIONE: QUESTA VERSIONE E' SOLO ANTEPRIMA, --apply E' BLOCCATO.
NON sostituisce ancora daily_pipeline.py o la pipeline operativa attuale.
Non eseguire il vecchio importer in modalità scrittura.

1) Il pacchetto NON sovrascrive alcun file esistente.
2) Estrarre il pacchetto nella cartella tools, mantenendo le sottocartelle.
3) Test:  .\python\python.exe .\tests\test_single_importer_preview_v1.py
4) Anteprima: .\python\python.exe .\src\importer\pvp_import_from_api_raw_hybrid_FINAL.py

La preview interroga Supabase con soli GET per gli ID esistenti, legge il RAW
PVP e produce UN JSON in runtime/reports: pvp_single_importer_preview_v1_*.json.
Nessun POST, PATCH, DELETE, chiamata Google o nuovo manifest.

La preview non va inserita nella pipeline fino al completamento del geocoder
unico e dello stato nel DB. Se i filtri e i dati nuovi risultano coerenti,
si implementa la scrittura INSERT-only con recovery per inserimento parziale;
solo a quel punto si sostituisce la pipeline.

NOTA: pvp_import_consolidated_preview.py e solo un prototipo transitorio;
il suo contenuto sostituira quello originale solo quando anche geocoder e recovery
sono verificati. NON cambiare daily_pipeline.py in questa fase.
