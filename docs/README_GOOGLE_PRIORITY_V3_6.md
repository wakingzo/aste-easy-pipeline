# Aste Easy Google preview v3.6

Patch diagnostica sulla v3.5. Nessuna scrittura Supabase; `--apply` resta bloccato.

Modifiche:
- il civico riconosciuto dal parser v3.4 viene verificato anche nei `street_number` strutturati restituiti da Google;
- un risultato senza PVP viene classificato `GOOGLE_CIVICO_ESATTO_DA_VALIDARE` solo se il civico Google coincide esattamente;
- civico variante/mancante resta `INDIZIO_NON_PRECISO_NON_SOSTITUIRE`;
- il CSV aggiunge `google_civic_relation`;
- nuova opzione diagnostica `--google-priority-only` per campionare una sola priorità senza cambiare l'ordine predefinito.

Per il prossimo test consigliato:

```
python src/pipeline/daily_pipeline_consolidated_v3.py --max-geocode 12000 --google-preview --google-max-requests 500 --google-max-cases 500 --google-priority-only P3_PVP_NON_CONFERMATO_CON_CIVICO
```

Il comando può fare fino a 500 richieste Geocoding fatturabili, non usa Autocomplete/Address Validation e non scrive sul DB.
