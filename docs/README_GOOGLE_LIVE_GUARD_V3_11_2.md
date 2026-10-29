# Google live guard v3.11.2

Patch di sicurezza del live smoke test.

Correzioni:
- usa lo stesso `GOOGLE_API_USAGE_FILE` configurato e usato dal core Google;
- riconosce e ripara soltanto le firme esatte del doppio conteggio v3.11 e dello stato parziale lasciato dal crash v3.11.1;
- verifica che il core caricato punti allo stesso file usage riconciliato prima di qualsiasi chiamata HTTP;
- mantiene doppio hard cap (ledger Aste Easy + contatore core), entrambi prenotati prima dell'HTTP;
- nessuna scrittura Supabase.

Smoke test consigliato:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-live-guard-smoke --google-live-max-requests 10 --google-monthly-cap 9000
```

La patch può migrare il noto stato 5010 -> 2510 solo quando la firma del bug è verificata esattamente. Negli altri casi non abbassa contatori.
