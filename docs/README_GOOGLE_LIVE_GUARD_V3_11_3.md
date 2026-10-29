# Google live guard v3.11.3

Fix del path del contatore Google.

La v3.11.2 poteva costruire il percorso del file usage come `src/geocoding/google_api_usage_v9_2.json`, mentre il core Google reale dell’installazione usa `runtime/cache/google_api_usage_v9_2.json`. Il controllo falliva correttamente prima di qualsiasi chiamata HTTP.

La v3.11.3 non tenta più di dedurre il percorso. Carica prima il core Google in modalità network-free, legge direttamente `g.API_USAGE_FILE`, riconcilia core e ledger su quel file esatto e ricrea solo il tracker in memoria. Le protezioni restano: cap mensile, cap per esecuzione, ledger pre-HTTP e zero scritture Supabase.

Comando smoke test:

    .\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-live-guard-smoke --google-live-max-requests 10 --google-monthly-cap 9000
