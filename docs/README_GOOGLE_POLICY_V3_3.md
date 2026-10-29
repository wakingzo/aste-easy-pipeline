# Aste Easy — filtro Google v3.3 (solo piano offline)

Aggiornamento **degli stessi moduli della pipeline v3**; non introduce un nuovo programma da lanciare ogni giorno. Estrai in `H:\PVP_Supabase\tools` mantenendo le sottocartelle. Non sostituisce il builder, il RAW, le credenziali, i report precedenti né i dati Supabase. L'entrypoint rimane `src\pipeline\daily_pipeline_consolidated_v3.py`; i moduli conservano i nomi v3.2 per compatibilità, ma il nuovo report porta `GOOGLE_AFTER_ANNCSU_V3_3`.

## Che cosa cambia

- `V. Giacomo Leopardi, 26` e altri prefissi abbreviati sono riconosciuti; **nome della via** e **civico** vengono distinti nel piano. CAP, numeri di statali/provinciali e riferimenti catastali non sono considerati civici. Sono gestiti anche `6/1` e `52-54`.
- **Non viene inventata una correzione a una statale.** `SS Alberobello n. 45` (Putignano), `SS 47 Alberobello` e indicazioni solo `SS/SP` non riscontrate localmente diventano `STRADA_EXTRAURBANA_NON_VERIFICATA`: *nessuna richiesta Google automatica*. Questo non prova che una determinata strada sia inesistente; la verifica su Google Maps/MappeCatasto comunicata dall'utente non è un dato PVP né è codificata come `SS 172`.
- Indirizzi misti (ad esempio `SP 28 / Via Diaz` o `Contrada Turrisi, SS 113`) non vengono assemblati a piacimento per produrre una query: sono segnalati come da disambiguare.
- Dopo PVP–ISTAT–ANNCSU, una query Google futura sarà ammessa soltanto se **conserva l'intero toponimo identificato e il civico, se presente**. Il campionatore riusa i CSV precedenti e la cache esistente. `--apply` resta bloccato; Google non può sostituire automaticamente coordinate PVP.
- Il CSV `google_policy_plan_v3_3_*.csv` aggiunge `address_civic` e il motivo dell'esclusione. **Un'esclusione da Google non cancella né corregge il pin PVP**, che rimane classificato secondo l'evidenza offline. Per una strada extraurbana errata servirebbe una fonte stradale esterna o una verifica manuale esplicita; non basta il testo PVP.

## Prova **senza** Google e senza scritture

```powershell
cd H:\PVP_Supabase\tools
.\python\python.exe .\tests\test_google_eligibility_v3_2.py
.\python\python.exe .\tests\test_google_preview_v3.py
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 2000 --google-plan
```

L'ultimo comando non avvia il builder, esegue solo GET su Supabase, non chiama Google e non importa aste. Restituisce un JSON `pvp_import_atomic_v1_*.json` e un CSV `google_policy_plan_v3_3_*.csv` in `runtime\reports`. Non lanciare `--google-preview`/`--apply` per verificare questo aggiornamento.

**Ambito:** filtro e confronto in anteprima; non è ancora il geocoder Google di produzione con persistenza delle decisioni nel DB. La verifica di un numero di statale richiederebbe una fonte autorevole sulle strade; non va inferita da una risposta Geocoding che potrebbe geocodificare un'altra località.
