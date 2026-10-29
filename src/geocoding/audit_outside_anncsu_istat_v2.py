#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline second-level audit for Aste Easy's 31 current out-of-municipality pins.

Reads immutable 16 Sep plan plus 18 Sep GET-only report; tests ANNCSU candidate
coordinates against locally installed ISTAT 2026 municipal polygons.
No Google imports, no DB client, no sockets, no writes except local CSV/JSON reports.
The report is *never* an authorization to patch any coordinate.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from aste_paths import REPORTS_DIR, REFERENCE_DIR

PLAN_NAME = 'geocode_unified_plan_v11_6_4_20260916-224958.csv'
PLAN_SHA = '1fd8b57115ff2f638aa72c4369d1c2402b062e8c73750ea05e93206ff0b93fc9'
AUDIT_NAME = 'aste_review16_and_mispins_v1_get_only_20260918-210224.csv'
AUDIT_SHA = '659f0119b4040281b16b43118f1679c8d90311509af2ed31e2aaf7df1ea5eef7'
CONFLICTS = {
    '4625814': 'Comune DB Ponsacco, nell’indirizzo compare Cascina.',
    '4625939': 'Comune DB Gorla Minore, nell’indirizzo compare Gorla Maggiore.',
    '4626070': 'Comune DB Termini Imerese, nell’indirizzo compare Piana degli Albanesi.',
    '4629385': 'Comune DB Fermo, nell’indirizzo compare Monte Urano.',
    '4629847': 'Comune DB Muravera, nell’indirizzo compare Nuraminis e CAP 09024.',
    '4630191': 'Comune DB Volpago del Montello, nell’indirizzo compare Giavera del Montello.',
}
FIELDS = ('pvp_id','comune_db_oggi','indirizzo_db_oggi','lat_db_oggi','lon_db_oggi',
          'istat_pin_attuale','anncsu_odonimo','anncsu_civic','anncsu_metodo',
          'anncsu_lat','anncsu_lon','istat_candidato_anncsu','istat_code_atteso',
          'istat_code_testato','distanza_pin_anncsu_km','street_score',
          'civic_relation','matching_street_candidates','matching_access_candidates',
          'anomalia_amministrativa','valutazione','avvertenza')


def sha_ok(path: Path, expected: str):
    if not path.is_file():
        raise RuntimeError(f'Manca file richiesto: {path}')
    observed=hashlib.sha256(path.read_bytes()).hexdigest()
    if observed!=expected:
        raise RuntimeError(f'SHA256 non corrispondente: {path.name} (report/piano non verificato). Stop.')


def read_csv(path: Path):
    with path.open(newline='',encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def read_inputs(plan_path: Path, audit_path: Path):
    sha_ok(plan_path,PLAN_SHA);sha_ok(audit_path,AUDIT_SHA)
    plans=read_csv(plan_path); audit=read_csv(audit_path)
    if len(plans)!=2402 or len({r['pvp_id'] for r in plans})!=2402:
        raise RuntimeError('Piano inatteso: attese 2.402 aste distinte.')
    if len(audit)!=47 or len({r['pvp_id'] for r in audit})!=47:
        raise RuntimeError('Audit GET inatteso: attesi 47 ID distinti.')
    outside=[r for r in audit if r['esito_db']=='PIN_FUORI_ISTAT_DA_VERIFICARE']
    if len(outside)!=31 or any(r['istat_db_oggi']!='OUTSIDE' for r in outside):
        raise RuntimeError('Il report GET non conferma 31 pin OUTSIDE: stop.')
    if not all(r['esito_db'] in {'ANCORA_SENZA_COORDINATE','CANDIDATO_LOCALE_DA_APPROVARE_MANUALMENTE','PIN_FUORI_ISTAT_DA_VERIFICARE'} for r in audit):
        raise RuntimeError('Esiti del report inattesi: stop.')
    pidx={r['pvp_id']:r for r in plans}
    for r in outside:
        p=pidx.get(r['pvp_id'])
        if r['gruppo']=='29_PIN_FUORI_ISTAT' and (not p or p['istat_boundary_position']!='OUTSIDE'):
            raise RuntimeError(f'Snapshot piano incoerente per {r["pvp_id"]}: stop.')
        if r['gruppo'] not in {'29_PIN_FUORI_ISTAT','2_STORICI_SEGNALATI'}:
            raise RuntimeError(f'Gruppo inatteso per {r["pvp_id"]}: stop.')
    return outside,pidx


def coord(lat,lon):
    try:
        a=float(lat);b=float(lon)
        return (a,b) if math.isfinite(a) and math.isfinite(b) and -90<=a<=90 and -180<=b<=180 and (a,b)!=(0,0) else None
    except (TypeError,ValueError):
        return None


def distance_km(a,b):
    from math import radians,sin,cos,asin,sqrt
    lat1,lon1=map(radians,a);lat2,lon2=map(radians,b)
    x=sin((lat2-lat1)/2)**2+cos(lat1)*cos(lat2)*sin((lon2-lon1)/2)**2
    return round(2*6371.0088*asin(min(1.0,sqrt(x))),2)


def get_boundary():
    # Validate all local inputs *before* importing a module that could download missing ISTAT datasets.
    if not (REFERENCE_DIR/'Elenco-comuni-italiani.xlsx').is_file():
        raise RuntimeError('Elenco-comuni-italiani.xlsx assente. Nessun download eseguito.')
    fp=HERE/'istat_boundary_runtime_v11_6.py'
    if not fp.is_file():
        raise RuntimeError(f'Modulo confini non trovato: {fp}')
    spec=importlib.util.spec_from_file_location('aste_outside_istat_local_only_v2',fp)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.ISTAT_ZIP.is_file():
        raise RuntimeError('Limiti01012026.zip locale assente. Nessun download eseguito.')
    return module.get_context()


def classify(outside,pidx,boundary):
    result=[]
    for row in outside:
        pid=row['pvp_id'];p=pidx.get(pid,{})
        current=coord(row['lat_db_oggi'],row['lon_db_oggi'])
        if not current:
            raise RuntimeError(f'Pin attuale non numerico nell’audit GET: {pid}')
        alt=coord(row['lat_anncsu'],row['lon_anncsu'])
        item={k:'' for k in FIELDS}
        for k in ('pvp_id','comune_db_oggi','indirizzo_db_oggi','lat_db_oggi','lon_db_oggi'):
            item[k]=row[k]
        item.update(istat_pin_attuale=row['istat_db_oggi'],
                    anncsu_odonimo=row['odonimo_anncsu'],anncsu_civic=row['civico_anncsu'],
                    anncsu_metodo=p.get('anncsu_metodo',''),
                    anncsu_lat=row['lat_anncsu'],anncsu_lon=row['lon_anncsu'],
                    istat_code_atteso=p.get('canonical_istat_code',''),
                    street_score=p.get('street_score',''),civic_relation=p.get('civic_relation',''),
                    matching_street_candidates=p.get('matching_street_candidates',''),
                    matching_access_candidates=p.get('matching_access_candidates',''),
                    anomalia_amministrativa=CONFLICTS.get(pid,''),
                    avvertenza='Solo audit. INSIDE non dimostra via/civico né autorizza PATCH.')
        if not alt:
            item['valutazione']='NESSUNA_COORDINATA_ANNCSU'
        elif not p:
            item['valutazione']='PIANO_NON_DISPONIBILE'
        elif not p.get('canonical_istat_code'):
            item['valutazione']='COMUNE_CANONICO_NON_RISOLTO'
        else:
            # Crucial: always test against *current* administrative fields AND verify canonical code
            # matches the original approved source, never silently move to a different municipality.
            bt=boundary.test({'comune':row['comune_db_oggi'],
                              'sigla_provincia':p.get('candidate_sigla_provincia',''),
                              'cap':p.get('candidate_cap','')},lat=alt[0],lon=alt[1])
            item['istat_candidato_anncsu']=bt.get('position','UNKNOWN')
            item['istat_code_testato']=bt.get('canonical_code','')
            item['distanza_pin_anncsu_km']=distance_km(current,alt)
            if bt.get('canonical_code')!=p.get('canonical_istat_code'):
                item['valutazione']='CODICE_COMUNE_DIVERSO_NON_USARE'
            elif bt.get('position') not in {'INSIDE','BOUNDARY'}:
                item['valutazione']='ANNCSU_NON_DENTRO_COMUNE_NON_USARE'
            else:
                item['valutazione']='ANNCSU_DENTRO_COMUNE_VERIFICARE_INDIRIZZO'
        result.append(item)
    if len(result)!=31 or len({r['pvp_id'] for r in result})!=31:
        raise RuntimeError('Numero di righe in uscita inatteso: nessun report scritto.')
    return result


def main():
    ap=argparse.ArgumentParser(description='ANNCSU vs ISTAT per 31 pin esterni: 100% offline, NO Google, NO PATCH.')
    ap.add_argument('--plan',type=Path,default=REPORTS_DIR/PLAN_NAME)
    ap.add_argument('--audit',type=Path,default=REPORTS_DIR/AUDIT_NAME)
    args=ap.parse_args()
    outside,pidx=read_inputs(args.plan,args.audit)
    boundary=get_boundary()
    records=classify(outside,pidx,boundary)
    REPORTS_DIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    csvout=REPORTS_DIR/f'aste_outside_anncsu_istat_v2_offline_{stamp}.csv'
    jsonout=REPORTS_DIR/f'aste_outside_anncsu_istat_v2_summary_offline_{stamp}.json'
    with csvout.open('w',newline='',encoding='utf-8-sig') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(records)
    summary={'mode':'OFFLINE_ZERO_NETWORK_ZERO_GOOGLE_ZERO_PATCH','input_plan_sha256':PLAN_SHA,
             'input_audit_sha256':AUDIT_SHA,'outside_current_checked':len(records),
             'anncsu_candidate_coords':sum(bool(coord(r['anncsu_lat'],r['anncsu_lon'])) for r in records),
             'counts':dict(Counter(r['valutazione'] for r in records)),
             'administrative_text_conflicts':sum(bool(r['anomalia_amministrativa']) for r in records),
             'google_api_calls':0,'supabase_reads':0,'supabase_patches':0,
             'warning':'Candidato dentro il poligono NON implica indirizzo/civico corretto. Nessuna modifica ammessa.'}
    jsonout.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('[MODE]',summary['mode'],'[ID]',len(records),'[ANNCSU CON COORDINATE]',summary['anncsu_candidate_coords'])
    print('[ESITI]',summary['counts'],'[CONFLITTI AMMINISTRATIVI TESTUALI]',summary['administrative_text_conflicts'])
    print('[OUT]',csvout);print('[OUT]',jsonout)

if __name__=='__main__':
    main()
