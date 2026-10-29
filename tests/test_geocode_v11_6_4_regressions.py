#!/usr/bin/env python3
from __future__ import annotations
import csv, importlib.util, tempfile
from pathlib import Path

BASE=Path(__file__).resolve().parents[1] / "src" / "geocoding"

def load(name, path):
    spec=importlib.util.spec_from_file_location(name, path)
    mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

g=load('g936', BASE/'google_geocode_core_v9_3_6.py')
ann=load('annv10', BASE/'anncsu_local_engine_v10.py')
loc=load('loc112', BASE/'anncsu_runtime_v11_2.py')

# 1) repeated civic markers: grammatical "e" must not become a suffix.
r={'indirizzo':'Zona Industriale- Via Corea n. 51 e n. 53, Olbia SS','comune':'Olbia','sigla_provincia':'SS','cap':'07026'}
assert g.expected_house_numbers(r)=={'51','53'}, g.expected_house_numbers(r)

# 2) exact locality-only remains eligible; specific address is only fallback.
ev={'runtime_locality_match_mode':'EXACT','runtime_locality_lat':41.0,'runtime_locality_lon':9.0,
    'runtime_locality_accesses':20,'runtime_locality_p90_m':80,
    'runtime_locality_source_specificity':'LOCALITY_ONLY'}
assert loc.locality_for_missing({'indirizzo':'Località Porto Rotondo'},ev,allow_fallback=False)[0]
ev['runtime_locality_source_specificity']='HAS_SPECIFIC'
assert not loc.locality_for_missing({'indirizzo':'loc. Porto Rotondo via X 16'},ev,allow_fallback=False)[0]
assert loc.locality_for_missing({'indirizzo':'loc. Porto Rotondo via X 16'},ev,allow_fallback=True)[0]

# 3) source explicit locality has precedence over secondary comma context.
#    Mocaiana must NOT silently resolve to Casamorcia.
rows=[
 {'pvp_id':'A','indirizzo':"Frazione Mocaiana, Casamorcia, lungo la Via dell'Assino, 06024 Gubbio PG",'comune':'Gubbio','sigla_provincia':'PG','cap':'06024'},
 {'pvp_id':'B','indirizzo':'Frazione Ronta, località Podere Paterno di Sotto, strada Ronta Campagna 67, Borgo San Lorenzo FI','comune':'Borgo San Lorenzo','sigla_provincia':'FI','cap':'50032'},
 {'pvp_id':'C','indirizzo':'Località Porto Rotondo, 07026 Olbia SS','comune':'Olbia','sigla_provincia':'SS','cap':'07026'},
]
resolved={'A':'054024','B':'048004','C':'090047'}
with tempfile.TemporaryDirectory() as td:
    p=Path(td)/'STRAD_TEST.csv'
    with p.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=['CODICE_ISTAT','PROGRESSIVO_NAZIONALE','ODONIMO',"LOCALITA'"],delimiter=';')
        w.writeheader()
        w.writerow({'CODICE_ISTAT':'054024','PROGRESSIVO_NAZIONALE':'1','ODONIMO':"VIA DELL'ASSINO',", "LOCALITA'":'CASAMORCIA'})
        w.writerow({'CODICE_ISTAT':'048004','PROGRESSIVO_NAZIONALE':'2','ODONIMO':"LOCALITA' RONTA CAMPAGNA", "LOCALITA'":'RONTA'})
        w.writerow({'CODICE_ISTAT':'090047','PROGRESSIVO_NAZIONALE':'3','ODONIMO':'VIA TEST', "LOCALITA'":'PORTO ROTONDO'})
    out=loc._scan_strad_localities(p,rows,resolved,ann)
assert out['A']['match_mode']=='EXPLICIT_UNMATCHED', out['A']
assert out['B']['match_mode']=='AMBIGUOUS_SOURCE_HINTS', out['B']
assert out['C']['match_mode']=='EXACT' and out['C']['anncsu_locality']=='PORTO ROTONDO', out['C']

# 4) planner must not direct-resolve a specific address to locality before Google.
text=(BASE/'geocode_unified_v11_6_4_plan.py').read_text(encoding='utf-8')
assert 'locality_for_missing(row, ev, allow_fallback=True)' not in text
assert text.count('locality_for_missing(row, ev, allow_fallback=False)') >= 3

print('OK - v11.6.4 regression suite: multicivico, specific-first locality, explicit locality precedence/fail-closed')
