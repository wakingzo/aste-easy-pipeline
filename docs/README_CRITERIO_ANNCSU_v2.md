# Aste Easy — criterio ANNCSU v2 (anteprima senza scritture)

**Installazione incrementale a fianco della v1.** Questo ZIP contiene **solo** il nuovo criterio geografico e il relativo entrypoint: non contiene RAW, chiavi, dati privati, SQL o altri importer. Richiede che `aste_easy_pipeline_consolidata_offline_v1.zip` sia già stato estratto e che sul PC siano presenti i dataset locali già usati dalla v1.

## Cosa cambia

- Quando il pin PVP è nel Comune ISTAT e **non** c'è un civico univoco conclusivo, la v2 tenta di confermare l'odonimo selezionato tramite ANNCSU. Richiede Comune/provincia coerenti, via univoca, punteggi elevati, guardia località coerente e almeno due accessi con metodo ANNCSU 1 o 3. La distanza è calcolata dall'**accesso ufficiale più vicino sulla stessa strada**, non dal punto rappresentativo della strada né da una strada vicina. Entro 150 m: `PVP_STRADA_COMPATIBILE` (compatibilità con la strada, NON conferma del civico o dell'immobile). Se la strada è lunga o l'evidenza è incompleta, il pin resta `PVP_IN_COMUNE`, con il motivo registrato.
- Se l'indirizzo identifica **soltanto una località**, la corrispondenza ufficiale deve essere esatta, non ambigua e la distribuzione degli accessi compatta. Entro 200 m dall'accesso con metodo 1/3 più vicino: `PVP_LOCALITA_COMPATIBILE`, che **non** prova la posizione dell'immobile. Se è presente anche via/civico, la sola località non può confermare il pin.
- Solo per cluster ben coperti e molto compatti, scostamenti superiori a 1.500 m possono produrre `REVIEW`, senza pubblicare coordinate finali e senza inventare un nuovo pin. La v2 non genera questo allarme automaticamente per più beni, strade rurali/chilometriche o geometrie diffuse. Le soglie sono **cautele operative da verificare sulle aste reali**, non certificazioni di precisione ANNCSU.
- Il civico univoco e le aste senza coordinate seguono le regole prudenti della v1. Distanze al civico PVP fra 100 e 250 m sono segnalate come non conclusive, non equiparate a conferma. Per i punti comunali `COMUNE` resta esplicito che sono **indicativi**.
- I dati primari provengono dal bene scelto dall'importer consolidato. Si conservano le coordinate originali in `pvp_latitudine/pvp_longitudine`, separatamente da quelle finali. Il report distingue `decision_counts` e `source_counts` per rendere leggibile anche la qualità delle verifiche.

`DONE` significa che la **decisione offline** è terminata, non che l'immobile sia stato localizzato con precisione. Google rimane **sempre disabilitato**; l'esito è `NOT_REQUESTED`. Le eventuali promozioni future di `PVP_IN_COMUNE`/`COMUNE` richiederanno un flusso dedicato, non una chiamata a Google automatica ogni giorno.

## Installazione e prova

Estrarre lo ZIP in `H:\PVP_Supabase\tools` mantenendo le sottocartelle. Aggiunge i file:

- `src/geocoding/anncsu_runtime_consolidated_v2.py`
- `src/geocoding/geocode_pending_v2.py`
- `src/pipeline/daily_pipeline_consolidated_v2.py`
- `tests/test_anncsu_criterion_v2.py`

**Non sovrascrive** `daily_pipeline.py`, la v1, lo storico o le colonne del DB. Non eseguire SQL. Esegui prima i test e poi una sola anteprima su tutte le nuove aste:

```powershell
cd H:\PVP_Supabase\tools
.\python\python.exe .\tests\test_anncsu_criterion_v2.py
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v2.py --max-geocode 616
```

La preview interroga Supabase **solo in GET**, legge i file ISTAT/ANNCSU già sul PC e genera il consueto report `pvp_import_atomic_v1_*.json` in `runtime\reports`. Non avvia il builder, non importa aste, non scrive coordinate e non chiama Google. Invia **solo il JSON del riepilogo**; il numero di nuove aste può cambiare per la cancellazione automatica delle scadute.

**`--apply` è bloccato nel nuovo entrypoint e nel geocoder v2:** la v2 non è stata collaudata con i dati ANNCSU completi né su Supabase reale in questo ambiente. Dopo aver esaminato la distribuzione sui 616 ID valuteremo quali soglie confermare e se i casi `REVIEW` necessitano di un controllo.
