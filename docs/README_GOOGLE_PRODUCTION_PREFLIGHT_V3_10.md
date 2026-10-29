# Aste Easy - Google production preflight v3.10

Scopo: simulare la coda Google che verrebbe usata in produzione dopo PVP + ISTAT + ANNCSU + locality bypass, applicando un hard cap mensile, senza effettuare chiamate Google e senza scrivere su Supabase.

## Regole budget

- cap applicativo predefinito: 9.000 tentativi/mese;
- ordine di servizio: P1 -> P2 -> P3 -> P4 -> P5;
- le righe oltre il budget non spariscono: mantengono il marker PVP/ANNCSU/LOCALITA/COMUNE già calcolato e vengono rinviate;
- il mese è quello locale del processo (`YYYY-MM`);
- il consumo già avvenuto viene stimato conservativamente prendendo il massimo tra ledger persistente, report locali `google_comparison_v3_YYYYMM*.csv` e un eventuale override esplicito;
- il ledger live conta la prenotazione PRIMA del tentativo HTTP: in caso di crash si preferisce sovrastimare il consumo anziché superare il cap.

Nota: il conteggio locale non sostituisce la quota/provider-side impostata in Google Cloud Console. Le due protezioni sono complementari.

## Dry-run

Da `H:\PVP_Supabase\tools`:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-production-preflight --google-monthly-cap 9000
```

Opzionale, se Cloud Console mostra un consumo corrente superiore al conteggio locale:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-production-preflight --google-monthly-cap 9000 --google-monthly-used 3000
```

Il valore effettivo usato dal preflight è comunque il massimo fra override, ledger e report locali.

## Output

Il JSON importer conterrà `policy_preview.production_budget_preflight`, inclusi:

- `effective_used_before`
- `remaining_before`
- `eligible_rows`
- `scheduled_rows`
- `deferred_rows`
- `scheduled_priority_counts`
- `deferred_priority_counts`
- `deferred_rows_still_mapped`
- `remaining_after_scheduled`

Viene inoltre prodotto:

`runtime/reports/google_budget_preflight_v3_10_*.csv`

con `budget_priority`, `budget_selected` e `budget_reason` per ogni asta.

## Stato attuale atteso sul dataset 2026-10-27

Dall'ultimo v3.9: 6.284 righe Google eleggibili. I cinque report diagnostici di ottobre disponibili localmente contengono 2.500 tentativi. Con cap 9.000 rimangono 6.500 tentativi: tutte le 6.284 righe entrano nel budget e restano 216 tentativi di margine applicativo.

Questi valori sono solo un controllo atteso: il comando ricalcola tutto dai file presenti nel runtime.

## Sicurezza

Questa release resta preview-only: `--apply` è ancora bloccato. Il modulo `google_budget_v3_10.py` contiene le primitive di ledger (`seed_ledger_floor`, `reserve_before_http`) da collegare al caller Google nella successiva release live, dopo il collaudo del preflight.
