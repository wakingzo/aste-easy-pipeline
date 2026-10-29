ASTE EASY — recovery v2 SOLO per i 29 SKIP_CONDITIONAL_FILTER dell'apply 2026-09-19.

Estrarre in H:\PVP_Supabase\tools, mantenendo le sottocartelle.
Non sovrascrive repair_territorial_safe_v1.py e NON aggiorna la pipeline quotidiana.
Richiede i tre report originali, NON rinominati, in runtime\reports:
 territorial_safe_v1_apply_20260919-151837.csv
 territorial_safe_v1_before_apply_20260919-151346.json
 territorial_skips_diagnostic_v1_get_only_20260919-152853.csv
Richiede anche il piano originale geocode_territorial_offline_v1_20260919-065501.csv e
le componenti ISTAT/geocode_territorial_offline_v1.py già presenti nel progetto.
Tutti i file di input sono bloccati da SHA256: input inatteso -> stop, senza richieste DB.

PASSO 1, SOLO GET:
 cd H:\PVP_Supabase\tools
 .\python\python.exe .\tests\test_territorial_skips_v2.py
 .\python\python.exe .\src\geocoding\repair_territorial_skips_v2.py

Inviare i report territorial_skips_v2_get_only_*.json e *.csv per il controllo PRIMA di applicare.
La verifica usa POSTGREST gte/lte soltanto su latitudine/longitudine originali,
con ampiezza +/- 0.000005 gradi. Tutti gli altri 10 campi mantengono il confronto
ESATTO o is.null; la riga viene ricontrollata e verificata dopo il PATCH.
Punti COMUNE sempre indicativi. Nessuna chiamata Google.

NON ESEGUIRE ancora --apply; usarlo solo dopo verifica dei report GET-only.
NON ripetere il vecchio repair_territorial_safe_v1.py --apply: 287 PATCH già riusciti.
