# Aste Easy daily v3.17 — Locality Guard integrato

## Obiettivo
v3.17 mantiene il daily v3.16 (PVP publication-sort, delta plan, import RPC atomico,
Google budget guard, audit finale) e integra il controllo delle **località esplicite**
nel geocoding di ogni nuova asta.

## Asset obbligatori
- `data/anncsu/anncsu_runtime.sqlite3`
- `data/istat/istat_localita_2021.sqlite3`

Se l'indice ISTAT località manca/non è valido, il daily si ferma prima delle scritture.
Per crearlo:

```powershell
.\python\python.exe .\src\geocoding\istat_locality_index_v1.py --download
```

## Regola Locality Guard
Il guard interviene solo se:
1. l'indirizzo contiene una località esplicita (`località`, `loc.`, `frazione`, `contrada`, `borgata`, ...);
2. la località ha un match ISTAT 2021 `EXACT_UNIQUE` nello stesso Comune;
3. il pin finale è **coarse** (`COMUNE`, `PVP_IN_COMUNE` o equivalenti), mai un CIVICO/STRADA verificato;
4. il pin è in conflitto forte (> 6 km dal punto rappresentativo ISTAT);
5. la distanza è comunque >= 2 km e non esiste warning di divergenza ANNCSU/ISTAT.

Quando scatta, il record viene pubblicato come:
- `geocode_status = REVIEW`
- `geocode_precision = LOCALITA`
- `geocode_source = locality_guard_istat_localita_2021`
- coordinate = punto rappresentativo ISTAT della località

Il punto è esplicitamente **indicativo** e non prova l'immobile.

## Daily
```powershell
.\python\python.exe .\src\pipeline\daily_pipeline_v3_17.py --run
```

Nel report geocode sono presenti:
- `locality_guard_counts`
- `locality_guard_overrides`
- `locality_guard_action`
- `locality_guard_hint`
- `locality_guard_ref`
- `locality_guard_distance_m`

## Compatibilità / recovery
v3.17 può riprendere anche un manifest v3.16 interrotto prima dell'upgrade.
