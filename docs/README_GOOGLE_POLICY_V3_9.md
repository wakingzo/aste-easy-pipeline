# Aste Easy — Google policy v3.9 locality bypass (dry-run)

## Scopo

Questa patch resta **solo anteprima**:

- zero scritture Supabase;
- zero nuove chiamate Google con `--google-policy-v39`;
- usa PVP + ISTAT + ANNCSU prima di qualunque futura eleggibilità Google;
- gli indirizzi composti soltanto da località/frazione/contrada senza civico
  non vengono più inviati a Google;
- se ANNCSU identifica esattamente la località nel Comune e dispone di una
  geometria utile, usa un punto rappresentativo con precisione `LOCALITA`;
- se ANNCSU non può localizzarla, mantiene il PVP già nel Comune oppure il
  fallback ISTAT del Comune: l'asta resta comunque visibile in mappa.

## Località ANNCSU indicativa

La regola nuova è distinta dalla verifica stretta già esistente.

Per una sorgente `LOCALITY_ONLY` senza strada/civico:

1. match ANNCSU `EXACT` nella municipalità risolta;
2. guard `explicit_exact` o `context_exact`;
3. almeno una coordinata di accesso ANNCSU valida;
4. p90 del cloud <= 5 km;
5. il punto rappresentativo viene ricontrollato dentro il poligono ISTAT.

Il risultato è `anncsu_localita_indicativa / LOCALITA`: non è mai CIVICO e non
viene usato per contraddire automaticamente un PVP.

## Google gate

`google_eligibility_v3_2.py` ora restituisce
`LOCALITA_TRONCATA_USA_ANNCSU_O_COMUNE` per località/frazioni senza civico.
Le località con un civico reale restano eleggibili se ANNCSU non le risolve.

Il report v3.9 aggiunge `future_google_gate`, con:

- `eligible_rows`
- `skipped_rows`
- `eligible_group_counts`
- `skip_reason_counts`

così si misura il consumo Google potenziale prima dell'apply.

## Esecuzione

Da `H:\PVP_Supabase\tools`:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-policy-v39
```

Output:

- `pvp_import_atomic_v1_*.json`
- `google_policy_v3_9_*.csv`

Atteso: `google_calls = 0`, `inserted = 0`, `existing_writes = 0`.

## Test

Suite inclusa: 114 test, tutti OK nella build del pacchetto.
