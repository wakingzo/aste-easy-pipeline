ASTE EASY - TERRITORIAL FALLBACK OFFLINE v1
=========================================

Obiettivo: decidere un punto coerente con il Comune per le aste con pin PVP fuori
Comune, senza inseguire singoli annunci o aprire perizie e senza chiamare Google.
Questo pacchetto AGGIUNGE un modulo di preview: non sovrascrive il geocoder
esistente e NON ha una modalita' --apply. Non e' ancora nella daily pipeline.

INPUT
- Supabase listings corrente: SOLO GET; non usa l'ultimo CSV come verita' DB.
- ISTAT: data/reference/Elenco-comuni-italiani.xlsx e Limiti01012026.zip gia'
  presenti (in mancanza dei file si ferma senza scaricarli).
- ANNCSU: data/anncsu/STRAD_ITA_*.csv e INDIR_ITA_*.csv gia' presenti;
  letti in un solo passaggio per il solo sottoinsieme problematico.
- Nessuna richiesta Google / ricerche web; niente API Google.

ORDINE DI DECISIONE
1. Se il PVP e' gia' interno o sul confine del Comune ISTAT: non toccarlo.
2. Se il PVP e' nel Comune ALTERNATIVO menzionato esplicitamente con CAP e
   sigla provinciale nell'indirizzo, conservare il pin per revisione multi-lotto.
   Non forzare Nuraminis su Muravera o Gorla Maggiore su Gorla Minore.
3. Se fuori Comune: usare un civico ANNCSU solo se passa i guard severi gia'
   presenti nel geocoder e il punto e' strettamente dentro il poligono ISTAT.
   Poi strada (solo senza civico e con corrispondenza forte/univoca) o localita'.
4. Se nessuna posizione ANNCSU affidabile: usare un punto interno al Comune
   calcolato dal poligono ISTAT. Precisione COMUNE, non posizione dell'immobile.
5. Comune non risolto, poligono assente o rappresentativo non interno: REVIEW.

NOTA SS 127: senza km e senza geometria stradale univoca, non e' corretto
promettere un punto sulla SS 127. Il ripiego e' dentro Tempio Pausania,
esplicitamente indicativo. Il codice NON contiene ID o coordinate hardcoded.

ESECUZIONE DA H:\PVP_Supabase\tools (PowerShell)
- Test senza accesso al DB:
    .\python\python.exe .\tests\test_geocode_territorial_offline_v1.py
- Prima verifica mirata SENZA scansione ANNCSU pesante:
    .\python\python.exe .\src\geocoding\geocode_territorial_offline_v1.py --pvp-id 4608215 --no-anncsu
- Audit nazionale (GET DB + ANNCSU solo sulle aste fuori Comune):
    .\python\python.exe .\src\geocoding\geocode_territorial_offline_v1.py --scope all
- Opzionale: includere anche aste senza coordinate:
    .\python\python.exe .\src\geocoding\geocode_territorial_offline_v1.py --scope all --include-missing
- Opzionale: controllo su un manifest, indicare un file reale:
    .\python\python.exe .\src\geocoding\geocode_territorial_offline_v1.py --scope touched --manifest .\runtime\handoff\NOME.jsonl

OUTPUT: runtime/reports/geocode_territorial_offline_v1_*.csv e
geocode_territorial_offline_v1_summary_*.json.
Le righe proposte sono PREVIEW NON APPLICATE: non eseguire vecchi --apply.

SICUREZZA / LIMITI
- L'unica rete consentita dall'esecuzione e' Supabase GET.
- Nessun PIN e' corretto finche' non si valida l'audit e non si integra una
  scrittura batch condizionata nella pipeline.
- Dati PVP multi-lotto non sempre dichiarano nell'indirizzo tutti i comuni:
  una proposta su un caso ambiguo deve rimanere in revisione.
- ISTAT rappresentativo e' sempre posizione indicativa nel Comune, non civico,
  terreno, strada o particella catastale.
- Non avviare geocode_unified_v11_6_4_live.py: anche DRY-RUN puo' usare Google.
- Nel contesto del modello qui non era montato lo shapefile ISTAT reale; i test
  verificano logica deterministica, non la posizione reale di Tempio nel dataset.
