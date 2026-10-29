# Aste Easy — filtro Google v3.4 (solo piano offline)

Aggiornamento degli **stessi moduli della pipeline v3**. Non aggiunge un nuovo programma alla routine e non modifica RAW, credenziali, cache Google o dati Supabase. L'entrypoint resta `src\pipeline\daily_pipeline_consolidated_v3.py`; i nomi dei moduli v3.2 restano invariati per compatibilità.

## Correzioni rispetto alla v3.3

- Recupera toponimi validi prima esclusi: `Piazzetta`, `Str.`, `Lungomare`, `Sestiere`, `Regione`, oltre a Via/Viale/Piazza/Contrada/Località/Frazione ecc.
- Separa meglio strada/località e civico, compresi `128/23`, `32/B`, `52-54` e civici espliciti seguiti dal Comune (`Via ... n.6 Arezzo`). CAP e numeri catastali non diventano civici.
- Il criterio di cercabilità dipende dalla **quantità di informazione concreta**, non da un elenco troppo ristretto di prefissi.
- Solo Comune, dati catastali, `Via nd`, `snc` senza toponimo, `agro di ...`, zona/periferia generica e multi-bene non disambiguati restano fuori da Google automatico.
- Le strade extraurbane restano conservative: una forma numerica semplice (`SS 172`, `SP 28`) è abbastanza specifica per un eventuale tentativo; forme come `SS Alberobello n.45` o `SS 47 Alberobello n.45` restano da revisione, senza inventare la SS 172 comunicata dall'utente come verifica esterna.
- Indicazioni miste (`SP 28 ... Via Diaz`, `Contrada ... SS 113`) non vengono assemblate automaticamente.
- Nessun risultato del piano modifica coordinate o Supabase; `--apply` resta fuori da questa verifica.

## Prova offline

```powershell
cd H:\PVP_Supabase\tools
.\python\python.exe .\tests\test_google_eligibility_v3_2.py
.\python\python.exe .\tests\test_google_preview_v3.py
.\python\python.exe .\src\pipeline\daily_pipeline_consolidated_v3.py --max-geocode 2000 --google-plan
```

L'ultimo comando deve produrre `google_calls: 0` e un CSV `google_policy_plan_v3_4_*.csv`. Non usare ancora `--google-preview` o `--apply`: prima si controlla il piano sui dati PVP aggiornati.
