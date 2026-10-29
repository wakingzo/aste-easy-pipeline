# Google policy v3.8 — P1/P2/P3/P4/P5 dry-run

Questa release e' **solo anteprima**:

- ZERO scritture Supabase;
- ZERO nuove chiamate Google con `--google-policy-v38`;
- usa esclusivamente i `google_comparison_v3_*.csv` gia' raccolti;
- mantiene il fallback cartografico verificato fino al COMUNE.

## Regole

- P1: sempre REVIEW; Google non sceglie automaticamente tra PVP e ANNCSU.
- P2: ROOFTOP + civico exact -> CIVICO; RANGE_INTERPOLATED -> STRADA.
- P3: ROOFTOP + civico exact, distinto dal PVP e <=150 m -> CIVICO; <=1 m non e' prova indipendente; >150 m -> REVIEW.
- P4: senza PVP, un centro Google coerente puo' migliorare COMUNE -> STRADA/LOCALITA. Se l'indirizzo contiene piu' civici, un solo civico rappresentativo verificato ROOFTOP puo' essere usato per il marker CIVICO.
- P5: si preferisce mantenere la coordinata PVP. Google puo' confermare STRADA/LOCALITA se il proprio punto indicativo e' distinto e <=150 m; <=1 m non e' indipendente. Un ROOFTOP su civico rappresentativo distinto e <=150 m puo' diventare CIVICO; un conflitto civico >150 m va in REVIEW.

## Civici multipli

L'indirizzo originale resta invariato. Il parser riconosce forme come:

- `9 e 29` -> rappresentativo `9`
- `31-33-35` -> rappresentativo `31`
- `66 – 68 – 70` -> rappresentativo `66`
- `24A-26A` -> rappresentativo `24A`

Non vengono creati piu' marker. Un risultato Google `24` non viene considerato equivalente al civico sorgente `24A`.

## Comando

Da `H:\PVP_Supabase\tools`:

```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-policy-v38
```

Output attesi:

- `runtime\reports\google_policy_v3_8_*.csv`
- `runtime\reports\pvp_import_atomic_v1_*.json`

Controllare in particolare `map_coverage.unmapped`: deve rimanere 0 prima di qualunque apply.
