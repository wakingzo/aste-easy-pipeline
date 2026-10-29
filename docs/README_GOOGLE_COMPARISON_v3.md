# Aste Easy — confronto Google v3 (anteprima controllata)

**Solo prova diagnostica.** Non esegue INSERT/UPDATE/PATCH/DELETE, non attiva la funzione SQL, non modifica i pin pubblicati e non scarica nuove aste PVP. La v2 offline resta disponibile e intatta. Non avvia Google senza `--google-preview` e **non** considera un geocoding Google una conferma automatica dell'immobile.

## File installati

Estrarre nella cartella `H:\PVP_Supabase\tools` mantenendo le sottocartelle. Il pacchetto aggiunge `src/geocoding/google_preview_v3.py`, `src/pipeline/daily_pipeline_consolidated_v3.py`, `tests/test_google_preview_v3.py`; aggiorna `src/importer/pvp_import_atomic_v1.py` con **una sola riga** per riportare anche nel riepilogo principale il numero reale di chiamate Google effettuate in una preview opzionale. Non cambia l'importazione, il suo RPC, lo schema, `geocode_pending_v2.py` o la v2. Se hai modificato manualmente l'importer v1 nel frattempo, non sovrascriverlo: comunica la modifica prima di estrarre.

Richiede la v1 e la v2 già installate, i dataset locali, `src/geocoding/google_geocode_core_v9_3_6.py` e **la sua cache e il suo file di contatori uso già esistenti**. Se manca uno di questi file si interrompe *prima di fare richieste Google*; non creare un contatore vuoto per superare il blocco. Richiede `GOOGLE_GEOCODING_API_KEY` configurata nell'ambiente già usato dal progetto; non inserire la chiave nel comando e non inviarla in chat.

## Prima prova

Da PowerShell:

```powershell
cd H:\PVP_Supabase\tools
.\python\python.exe .\tests\test_google_preview_v3.py
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 2000 --google-preview --google-max-requests 25 --google-max-cases 25
```

**Il secondo comando può generare fino a 25 richieste Google Geocoding fatturabili.** `--google-max-requests` conta ogni tentativo HTTP (anche un retry, che qui viene disattivato); un cache hit non richiede una nuova chiamata. La prova non chiama Autocomplete né Address Validation. Il limite è *per questa esecuzione*, non una garanzia che la tua fatturazione mensile sia gratuita: quote Cloud, soglie gratuite e abbonamento sono concetti distinti. Se preferisci iniziare con cinque richieste, imposta entrambi i limiti a `5`. La CLI/console Cloud resta la fonte ufficiale sui costi e sulle quote.

Il campione è deterministico e diversificato: fino a 7 civici PVP non conclusivi, 10 punti comunali indicativi e 8 pin PVP non confermati oltre il Comune; se un gruppo ha meno casi, integra con gli altri. Esclude i casi multi-bene, che richiedono prima un accertamento della provenienza del pin. Per ogni caso formula **una sola query vincolata** tramite il vecchio motore Google, riusa la cache e verifica il risultato con Comune ISTAT, corrispondenza dell'indirizzo, `partial_match`, `location_type` e distanza rispetto al pin originale. Anche un risultato `ROOFTOP` non aggiorna il DB.

Il report `runtime\reports\pvp_import_atomic_v1_*.json` include `geocode_preview.google_preview` con gruppi, contatori e la posizione del CSV. Il file `runtime\reports\google_comparison_v3_*.csv` descrive per ogni caso l'indirizzo, la decisione offline, il candidato Google e il motivo per **verificarlo**, scartarlo o lasciarlo indicativo. Invia il JSON e il CSV (non cache, contatori, file `.env` o chiavi). Se il CSV contiene dettagli che non vuoi condividere, puoi oscurarli prima.

**Non usare `--apply`.** Resta bloccato in questa v3. Non richiede nuovi SQL o un altro build PVP. Se la cache/contatore non sono trovati oppure le quote fermano la prova, riporta l'errore *senza la chiave*, e non aggirare il controllo.
