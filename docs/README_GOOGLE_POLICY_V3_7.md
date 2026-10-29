# Aste Easy — Google policy v3.7 dry-run

Questa patch NON scrive su Supabase e NON esegue nuove chiamate Google.
Usa esclusivamente i report `google_comparison_v3_*.csv` gia presenti in
`tools/runtime/reports` e ricalcola il piano offline PVP + ANNCSU + ISTAT.

## Regole P1/P2/P3

- P1 (discordanza PVP/ANNCSU): sempre REVIEW; Google e' terza evidenza e non
  decide automaticamente quale pin dettagliato pubblicare.
- P2 (nessun PVP + civico):
  - Google ROOFTOP + civico structured `exact` + INSIDE + score >=82 => CIVICO.
  - Google RANGE_INTERPOLATED con gli stessi controlli => coordinata utilizzabile
    ma precisione STRADA, mai CIVICO pieno.
  - variant/mismatch/missing/debole => resta la baseline offline.
- P3 (PVP non confermato + civico):
  - Google ROOFTOP exact <=1 m dal PVP => mantiene PVP (non prova indipendente).
  - Google ROOFTOP exact >1 e <=150 m => promozione a CIVICO Google.
  - Google ROOFTOP exact >150 m => REVIEW; nessuno dei due pin dettagliati viene
    scelto automaticamente.
  - gli altri esiti restano sulla baseline offline.

## Regola mappa

La precisione geografica e la visibilita sulla mappa sono campi separati.
Se la decisione finale ha coordinate valide, la mappa usa quelle.
Se la decisione finale non ha un pin pubblicabile ma il Comune ISTAT e' risolto,
la mappa usa un punto rappresentativo verificato INSIDE nel Comune con:

- `map_source=istat_comune_fallback_v37`
- `map_precision=COMUNE`
- `map_is_fallback=true`

Questo marker e' esplicitamente indicativo e NON viene trasformato in una falsa
precisione CIVICO/STRADA. Solo se il Comune stesso non e' risolvibile in modo
sicuro il caso resta `unmappable_review_v37`.

## Comando

Da `H:\PVP_Supabase\tools`:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-policy-v37
```

Non aggiungere `--google-max-requests` o `--google-max-cases`: in questa modalita
le chiamate Google devono essere zero.

## Output da controllare

Il JSON `pvp_import_atomic_v1_*.json` conterra `policy_preview` con:

- `policy_action_counts`
- `final_precision_counts`
- `map_precision_counts`
- `map_source_counts`
- `map_coverage.total`
- `map_coverage.mapped`
- `map_coverage.fallback_mapped`
- `map_coverage.unmapped`
- `report_csv`

Il CSV `google_policy_v3_7_*.csv` contiene una riga per ogni nuova asta e mostra
baseline offline, eventuale evidenza Google, decisione proposta e marker mappa.

## Sicurezza

`--apply` resta bloccato. Questa versione serve solo a validare la policy prima
di qualsiasi scrittura persistente.
