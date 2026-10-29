# Aste Easy — importer atomico e geocoder PENDING, v1 offline

**Questa release si installa A FIANCO degli script vecchi** e non modifica `daily_pipeline.py`, il builder, né la struttura delle altre tabelle. Per le nuove aste serve una *sola* nuova funzione SQL transazionale. Non contiene chiavi, RAW, cache o dati personali.

## Che cosa è implementato

- Importer new-only: il confronto col DB e il filtro su data d'asta/ISTAT sono quelli dell'anteprima già verificata; zero aggiornamenti sugli ID esistenti.
- Una chiamata RPC per `pvp_id` inserisce `listings`, `listing_soggetti`, `listing_allegati` nella stessa transazione. Un allegato orfano che collide con l'URL provoca **rollback** dell'intero ID, non perdita silenziosa di documenti. Un errore su un ID non annulla gli ID già importati: per riprendere si rifà la preview con nuovo numero/hash.
- `pvp_latitudine`/`pvp_longitudine` conservano i dati originari. `latitudine`/`longitudine` sono inizialmente NULL per i nuovi ID, `geocode_status=PENDING` e hash dei dati d'origine.
- Un geocoder, zero Google, legge **solo** `geocode_status=PENDING` in Supabase; valuta ISTAT/ANNCSU locali, conserva il PVP coerente nel Comune senza spacciarlo per civico verificato, usa un punto comunale esplicitamente indicativo in ultima istanza, o `REVIEW` se non può decidere. Salva precisione, fonte, motivo, timestamp, versione, esito `NOT_REQUESTED`. È recuperabile dopo interruzione con `--geocode-only`.
- Default **GET-only**, zero chiamate Google; il builder NON parte da solo. `--build` richiede esplicitamente limiti per pagine e dettagli. Lo storico con `geocode_status IS NULL` è escluso.

**Limiti da non confondere con funzionalità complete:** Google è deliberatamente **non collegato** in questa release e il flusso non promuove automaticamente i vecchi punti comunali quando Google sarà abilitato. Il caso con più beni e un pin esterno è mandato in `REVIEW` senza inventare una posizione. Non abbiamo accesso al RAW completo da 616 ID, al database live né ai dataset ISTAT/ANNCSU grandi nel nostro ambiente: abbiamo eseguito test simulati, non un collaudo di inserimento reale. La funzione SQL non è stata eseguita su Supabase da qui. Non invocare i vecchi geocoder sul nuovo `PENDING`.

## Installazione conservativa sul PC Windows

1. Estrai lo ZIP in `H:\PVP_Supabase\tools`, **mantenendo la struttura** delle cartelle. Questo pacchetto aggiunge solo `src/importer/pvp_import_atomic_v1.py`, `src/geocoding/geocode_pending_v1.py`, `src/pipeline/daily_pipeline_consolidated_v1.py`, SQL e test. Include anche la stessa anteprima precedente come dipendenza. NON sovrascrive l'importer o la pipeline operativa precedenti.
2. Verifica i file preesistenti: `src/geocoding/istat_boundary_runtime_v11_6.py`, `src/geocoding/geocode_territorial_offline_v1.py`, `src/geocoding/anncsu_runtime_v11_2.py`, `data/reference/Limiti01012026.zip`, `data/reference/Elenco-comuni-italiani.xlsx`, `data/anncsu/STRAD_ITA_*.csv`, `data/anncsu/INDIR_ITA_*.csv`. Se questi ultimi due non sono presenti, lo script si ferma senza chiamate di rete o fallback silenziosi. Per un test senza ANNCSU esiste `--no-anncsu`, che usa punti indicativi comunali: sceglilo solo se vuoi accettare esplicitamente questo compromesso.
3. Test locale (nessuna rete):

   ```powershell
   cd H:\PVP_Supabase\tools
   .\python\python.exe .\tests\test_consolidated_v1.py
   ```

4. Anteprima dei dati reali (**GET Supabase + scansione locale ISTAT/ANNCSU**, zero POST/PATCH e zero Google; non avvia build):

   ```powershell
   .\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v1.py
   ```

   Il riepilogo `pvp_import_atomic_v1_*.json` in `runtime/reports` contiene `eligible_new`, `new_soggetti`, `new_allegati`, `raw_sha256` e `geocode_preview.decision_counts` (decisioni **simulate**, nessuna coordinata ancora scritta). L'analisi ANNCSU scansiona i file locali grandi e può essere impegnativa; per provarla su un campione iniziale usa `--max-geocode 30`, ma prima della scrittura è opportuno confrontare l'anteprima completa. Confronta il risultato con l'ultima anteprima: 616 aste, 1408 soggetti, 2551 allegati erano validi *il 19 settembre 2026*; i conteggi possono variare in base alla data e ai duplicati intercettati. La verifica in anteprima non prova che ogni RPC sarà accettata dal database.

5. **Soltanto dopo aver validato i test e la preview**, apri Supabase SQL Editor ed esegui `sql/01_atomic_import_rpc.sql` (salvato nel pacchetto), che crea e autorizza per il solo `service_role` la funzione `aste_insert_new_listing_v1`. Non eseguire `db reset`, `db push` o altri SQL. Il precedente `ALTER TABLE` delle 11 colonne geografiche deve già essere completato.
6. Il comando apply **non va lanciato finché non hai confrontato la preview e controllato che la funzione SQL sia stata creata**. Quando confermato, usa il numero `eligible_new` e l'hash della *preview appena eseguita*, non quelli storici:

   ```powershell
   .\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v1.py --apply --expected-new N --expected-raw-sha256 HASH
   ```

   Lo script importa i nuovi ID atomici e poi completa le righe `PENDING` offline. Se il geocoder si interrompe dopo un inserimento, NON reimportare gli ID: basta `...daily_pipeline_consolidated_v1.py --geocode-only --apply`. La selezione e l'hash dei dati protetti sono nel DB; nessun manifest.

Per aggiornare il RAW in una routine futura, e **solo se lo vuoi esplicitamente**:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v1.py --build --max-pages 5 --max-details 30
```

La semplice preview `daily_pipeline_consolidated_v1.py` **non** fa build. Non lanciare `--build` senza i due limiti positivi.

**Rollback della sola funzione:** `sql/99_remove_atomic_rpc_only.sql`; non rimuove righe, coordinate o colonne. Non usare il file se la pipeline è in esecuzione.
