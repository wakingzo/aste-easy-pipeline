# Aste Easy Google preview v3.6.1

Patch di packaging sulla v3.6. Nessuna modifica alla logica di valutazione e nessuna scrittura Supabase; `--apply` resta bloccato.

Correzione rispetto alla v3.6:
- ripristinato `src/geocoding/google_policy_plan_v3_2.py`, necessario per avere il pacchetto diagnostico completo e per eseguire l'intera suite test anche su installazione pulita.

La verifica civico introdotta in v3.6 resta invariata:
- `google_civic_relation` usa il parser v3.4 e i `street_number` strutturati di Google;
- un record senza coordinate PVP viene promosso a `GOOGLE_CIVICO_ESATTO_DA_VALIDARE` solo con civico Google `exact`;
- `variant` / `mismatch` / civico mancante non vengono promossi automaticamente.

## Prossimo campione diagnostico consigliato

Dopo il campione P3 già eseguito, misurare un nuovo campione P2 (aste senza coordinate PVP ma con civico), evitando automaticamente ID/query già presenti nei report precedenti:

```
python src/pipeline/daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-preview --google-max-requests 500 --google-max-cases 500 --google-priority-only P2_SENZA_PVP_CON_CIVICO
```

Il comando può effettuare fino a 500 richieste Geocoding fatturabili. Non usa Autocomplete/Address Validation e non scrive sul database.
