ASTE EASY - INTEGRAZIONE FALLBACK TERRITORIALE NELLA PIPELINE
Versione 1 - 2026-09-19

CHE COSA CAMBIA
- La pipeline continua a creare il RAW PVP e a importare SOLO le nuove aste.
- Dopo l'INSERT legge SOLO gli ID dell'handoff realmente creato dall'importer.
- Se un pin e' fuori dal Comune ufficiale ISTAT, propone un punto INTERNO al Comune.
- Il punto comunale e' INDICATIVO, non la posizione catastale o il km della strada.
- Se il pin e' gia' nel Comune, NON lo sposta.
- Se il pin e' in un altro Comune esplicitamente identificato nell'indirizzo,
  lo mantiene in revisione (possibili lotti/Comuni diversi).
- Se la fonte e' gia' classificata ROOFTOP, PVP_OK_CIVICO/PVP_OK_STRADA,
  pvp_verified/google_corrected_pvp, NON sostituisce con un punto generico.
- Per le nuove aste SENZA COORDINATE non genera automaticamente punti comunali:
  l'opzione --territorial-include-missing va richiesta esplicitamente.
- Per velocita' NON scansiona automaticamente il grande archivio ANNCSU:
  --with-anncsu attiva su richiesta la ricerca locale di strade affidabili.
  Le proposte CIVICO/LOCALITA' dubbie NON vengono applicate automaticamente.
- La correzione aggiorna SOLO coordinate, maps_link e metadati geocode.
  Comune, indirizzo, lotti, atti e altri campi non sono modificati.
- Nessuna eccezione hardcoded per ID d'asta, nessuna perizia scaricata.

GOOGLE: CAMBIO IMPORTANTE E INTENZIONALE
Prima --apply avviava automaticamente il geocoder Google. ORA NON PIU'.
Solo il flag ESPLICITO --with-google lo avvia. NON usarlo con la quota esaurita.
Lo script territoriale usa la rete solo per GET/PATCH Supabase; geodati
ISTAT/ANNCSU sono file locali. Se i confini locali mancano, si ferma:
non esegue download.

INSTALLAZIONE (dalla cartella H:\PVP_Supabase\tools)
1) Conservare copia del vecchio src\pipeline\daily_pipeline.py fuori dallo ZIP.
2) Estrarre lo ZIP mantenendo i percorsi; sovrascrivere SOLO daily_pipeline.py.
   Lo ZIP aggiunge territorial_touched_daily_v1.py, un test e questa guida.
   NON sovrascrive importer, unified geocoder, vecchi repair, cache o dati.
3) Verificare i test (senza DB/rete):
   .\python\python.exe .\tests\test_territorial_daily_integration_v1.py

TEST DI INTEGRAZIONE IN SOLA LETTURA SUL MANIFEST DEL 16 SETTEMBRE
   .\python\python.exe .\src\geocoding\territorial_touched_daily_v1.py --manifest .\runtime\handoff\geocode_handoff_daily_20260916-223416.jsonl
Il test legge SOLO quei pvp_id, produce CSV/JSON in runtime\reports;
ZERO PATCH / ZERO Google. Non usare --apply durante questa prova.
Il manifest storico contiene ID gia' corretti: molte righe dovrebbero
risultare INSIDE e non richiedere nuovi aggiornamenti.

UTILIZZO FUTURO
Dry run PVP senza scritture e senza geocoding:
   .\python\python.exe .\src\pipeline\daily_pipeline.py

IMPORT REALE + FALLBACK TERRITORIALE OFFLINE AUTOMATICO, SENZA GOOGLE:
   .\python\python.exe .\src\pipeline\daily_pipeline.py --apply

Solo importazione, mantenendo il manifest e SALTANDO qualunque geocoding:
   .\python\python.exe .\src\pipeline\daily_pipeline.py --apply --skip-geocode

Ricerca ANNCSU locale (piu' lenta; soltanto se desiderata):
   .\python\python.exe .\src\pipeline\daily_pipeline.py --apply --with-anncsu

Includere anche i nuovi record senza coordinate (punti solo indicativi):
   .\python\python.exe .\src\pipeline\daily_pipeline.py --apply --territorial-include-missing

Google a consumo SOLO con autorizzazione esplicita, NON consigliato ora:
   .\python\python.exe .\src\pipeline\daily_pipeline.py --apply --with-google

NOTE DI SICUREZZA
- L'--apply dell'importer richiede ancora la propria conferma testuale.
  Soltanto dopo conferma/import riuscito la pipeline autorizza lo stage locale.
- Lo stage precontrolla il DB con GET; salva backup JSON PRIMA di qualsiasi PATCH;
  applica filtri PostgREST condizionati (coordinate originali in intervallo
  +-0,000005 gradi) e rilegge i record dopo ogni scrittura.
- Su errori o PATCH parziali controllare report e manifest; NON ripetere
  automaticamente l'intera pipeline, dato che gli INSERT sono gia' riusciti.
- La pipeline opera solo sui NUOVI ID in handoff: NON riesamina automaticamente
  il database storico (i 316 repair gia' applicati rimangono intatti).
- Eventuali record con fonte precisa fuori Comune restano in revisione.
- I report mostrano anche i motivi delle esclusioni.
- Non inserire API key/service role in ZIP o repository; usare env/secrets.
- I metadati ISTAT_COMUNE_FALLBACK / ANNCSU_STREET_MEDOID restano da rendere
  visibili nell'app come 'Posizione indicativa'.
