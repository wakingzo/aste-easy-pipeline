# Aste Easy — Daily Production v3.13

## Scopo
Un unico comando per l'aggiornamento quotidiano sicuro:

1. carica le credenziali del progetto e verifica la RPC atomica (zero write);
2. esegue la discovery PVP sulle aste con data di vendita futura;
3. usa un overlap di 2 giorni sulla data di pubblicazione per i dettagli;
4. costruisce il piano new-only confrontando il RAW con Supabase;
5. se non ci sono nuove aste, fa solo un audit globale read-only e termina;
6. se le nuove aste superano 1.500, si ferma prima delle scritture;
7. salva manifest + staging autorizzato prima del primo INSERT;
8. importa con RPC atomica, progress e retry di rete idempotente;
9. geocodifica con ANNCSU/ISTAT/Google, cap mensile 9.000 e cap daily 1.000;
10. esegue audit read-only finale e chiude il manifest come COMPLETED.

## Comando quotidiano

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_v3_13.py --run
```

Non richiede SHA/count manuali. L'autorizzazione è interna al singolo run e viene fissata nel manifest/staging prima delle scritture.

## Resume automatico
Se un run v3.13 si interrompe:

- durante l'import: il comando successivo ricarica il payload di staging e ripete le RPC in modo idempotente; gli ID già presenti vengono saltati;
- durante il geocoding: il comando successivo lavora solo le righe ancora PENDING;
- dopo il geocoding ma prima dell'audit: il comando successivo esegue soltanto l'audit.

Non serve un comando resume separato.

## Safety gate
Default:

- `--daily-max-new 1500`
- `--max-geocode 2000`
- `--google-monthly-cap 9000`
- `--google-daily-run-cap 1000`
- `--safety-days 2`
- `--page-size 200`

Il cap Google daily limita solo nuove chiamate HTTP: risultati in cache non consumano il cap; i candidati oltre cap mantengono comunque il fallback offline/mappa.

## Perché non si usa lo stop precoce per dataPubblicazione
La ricerca PVP del builder è paginata per `dataOraVendita desc`, quindi la data di pubblicazione non è monotona lungo le pagine. v3.13 scorre le aste future fino allo stop per data vendita, ma evita di scaricare i dettagli di annunci vecchi usando la soglia `max(data_pubblicazione_date DB) - 2 giorni`.

## Report
I file principali finiscono in `runtime/reports`:

- `daily_manifest_v3_13_*.json`
- `daily_import_payload_v3_13_*.jsonl`
- `pvp_import_atomic_v1_*.json`
- `google_production_v3_12_*.json/.csv`
- `production_audit_v3_13_*.json`

A fine run la console stampa anche i tempi delle fasi e il tempo totale.
