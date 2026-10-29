ASTE EASY - diagnosi in sola lettura dei 29 SKIP_CONDITIONAL_FILTER

Estrarre lo ZIP in H:\PVP_Supabase\tools mantenendo le sottocartelle.
Richiede il repair originale src\geocoding\repair_territorial_safe_v1.py.
Richiede i due report ORIGINALI, non modificati, nella cartella runtime\reports:
  territorial_safe_v1_apply_20260919-151837.csv
  territorial_safe_v1_before_apply_20260919-151346.json
Lo script ne verifica gli SHA256 prima di connettersi a Supabase.

Da H:\PVP_Supabase\tools:
  .\python\python.exe .\tests\test_diagnose_territorial_skips_v1.py
  .\python\python.exe .\src\geocoding\diagnose_territorial_skips_v1.py

Nessun parametro --apply. Nessun PATCH. Nessuna chiamata Google.
Si limita ai 29 ID saltati; confronta lo snapshot originale, poi prova
le condizioni PostgREST mediante soli GET, senza riportare dati sensibili.
Produzione: runtime\reports\territorial_skips_diagnostic_v1_get_only_*.csv e .json

NON rieseguire repair_territorial_safe_v1.py --apply: 287 record sono gia stati aggiornati.
