# Aste Easy — criterio Google dopo PVP + ANNCSU (v3.2)

**Aggiornamento della pipeline v3 esistente. Non aggiunge un comando quotidiano.** Installare nella cartella `H:\PVP_Supabase\tools` mantenendo le sottocartelle. Sovrascrive il modulo Google v3, il geocoder locale v2, l'entrypoint v3 e due file di test; aggiunge due piccoli moduli interni e un test. Non sovrascrive RAW, cache, API usage, report, chiavi, vecchia pipeline o tabelle Supabase.

## Regole implementate

1. Importer new-only sceglie un unico bene coerente e conserva il pin PVP separatamente. Se l'indirizzo è ambiguo tra più beni, il confronto Google automatico viene escluso.
2. ISTAT/ANNCSU vengono valutati **sempre prima**: un civico, una strada o una località già risolti con evidenza locale adeguata non generano richieste Google; i fallback stradali o comunali restano indicativi, non coordinate dell'immobile certificate.
3. **Senza pin PVP:** se la ricerca locale non risolve sufficientemente l'indirizzo, Google è *ammissibile* soltanto con una via/contrada/località riconoscibile; Comune, CAP, dati catastali, `Via nd`, `Via snc`, un toponimo non identificabile o due strade diverse non bastano. Con solo Comune resta il punto comunale indicativo.
4. **Con pin PVP:** il solo contenimento nel Comune non verifica l'indirizzo. Google è *ammissibile* se il pin non trova conferma di via/località/civico in ANNCSU, oppure un civico/cluster ANNCSU è in forte disaccordo, **e** l'indirizzo è sufficientemente specifico. Se il pin è confermato dall'evidenza locale, nessuna chiamata.
5. **Civico ANNCSU oltre 250 m dal PVP:** la v2 non sostituisce più automaticamente il pin; ora produce `REVIEW` con coordinate finali vuote e coordinate PVP originali conservate separatamente. Soglia cautelativa, non prova che il PVP sia errato.
6. Google continua a usare il motore esistente, cache e tetto di richieste esplicito, senza Autocomplete/Address Validation. Una risposta Geocoding non sposta automaticamente il pin: deve passare controlli di Comune, corrispondenza d'indirizzo e precisione, e resta un **confronto da verificare**.

**Limite della release:** questa è una verifica dei criteri, non ancora il geocoder di produzione con Google e scrittura persistente degli esiti nel DB. `--apply` rimane disabilitato. Il report `DONE:COMUNE` continua a indicare *decisione offline conclusa*, non localizzazione precisa.

## Prima prova: zero Google, zero scritture DB

```powershell
cd H:\PVP_Supabase\tools
.\python\python.exe .\tests\test_google_eligibility_v3_2.py
.\python\python.exe .\tests\test_anncsu_criterion_v2.py
.\python\python.exe .\tests\test_google_preview_v3.py
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 2000 --google-plan
```

L'ultimo comando **legge Supabase in GET** per trovare solo gli annunci nuovi, legge RAW e dataset locali ISTAT/ANNCSU, ma **non chiama Google e non scrive nel DB**. Non avvia il builder PVP. Il riepilogo in `runtime\reports\pvp_import_atomic_v1_*.json` contiene `google_policy_plan.eligible_count`, i gruppi e i motivi di esclusione. Produce anche **un solo CSV** `runtime\reports\google_policy_plan_v3_2_*.csv` con la motivazione per ogni asta. Le aste ammissibili nel piano *non* equivalgono a chiamate nuove: il successivo confronto deve escludere ID/query già presenti nei CSV v3 e riusare la cache.

**Non lanciare ancora `--google-preview` né `--apply`.** Esaminare prima il nuovo JSON e, se serve per i falsi positivi, il CSV del piano. La vecchia prova da 40 richieste v3.1 non va avviata prima di questa verifica.
