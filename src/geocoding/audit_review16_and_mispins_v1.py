#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only audit of 16 held new listings + 29 out-of-municipality PVP pins + 2 historical reports.
Offline by default; --db-check issues ONLY SELECT/GET to Supabase. No geocoding services, no PATCH.
"""
from __future__ import annotations
import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from aste_paths import REFERENCE_DIR, REPORTS_DIR

PLAN_NAME = 'geocode_unified_plan_v11_6_4_20260916-224958.csv'
PLAN_SHA = '1fd8b57115ff2f638aa72c4369d1c2402b062e8c73750ea05e93206ff0b93fc9'
REVIEW_IDS = ('4575297','4622509','4625134','4625162','4625830','4625843',
              '4625845','4626435','4627298','4627299','4627417','4627665',
              '4629548','4630736','4630773','4631382')
HISTORIC_IDS = ('4611074', '4608215')  # Previously user-reported Villasor and Tempio Pausania pins
CHECK_IDS = ('4625162','4627299','4627417')  # No automatic updates: extra review only
NOTES = {
 '4575297': ('INDIRIZZO_GENERICO','Il PVP indica solo Collepasso: Via Lecce non dimostrata.'),
 '4622509': ('INDIRIZZO_GENERICO','Via snc non dimostra Contrada Via di Chieti.'),
 '4625134': ('CIVICO_MANCANTE','Via Ghino Valenti SNC: mediana della strada troppo imprecisa.'),
 '4625162': ('CIVICO_DA_VERIFICARE','Contrada Caselle 5 coincide con il civico ANNCSU; verificare sul DB e nel poligono.'),
 '4625830': ('ODONIMO_DISCORDANTE','Strada Comunale della Roveda 10/A non equivale a Via Brianza 10/A.'),
 '4625843': ('ODONIMO_DISCORDANTE','Stesso indirizzo Strada Comunale della Roveda 10/A; non usare Via Brianza.'),
 '4625845': ('ODONIMO_DISCORDANTE','Strada Comunale 10/A non prova Via Brianza 10/A.'),
 '4626435': ('INDIRIZZO_GENERICO','Il PVP indica solo Collepasso: Via Lecce non dimostrata.'),
 '4627298': ('LOCALITA_APPROSSIMATA','Carnaiola ha medoid ANNCSU con dispersione p90 ~465 m: non è il fabbricato.'),
 '4627299': ('CIVICO_DA_VERIFICARE','Località Pietrara 3 coincide con Zona Pietrara 3 ANNCSU; verificare sul DB e nel poligono.'),
 '4627417': ('ALIAS_DA_VERIFICARE','Via Alberto Mario è riportata come vecchia denominazione di Via Mario Alberto; confermare alias/civico.'),
 '4627665': ('ODONIMO_DISCORDANTE','Via Manzoni 2 non equivale a Corso Vercelli 2.'),
 '4629548': ('ODONIMO_DISCORDANTE','Strada Terza di Zona Industriale 4 non equivale a Via del Piano 4.'),
 '4630736': ('CIVICO_MANCANTE','Via Torino 181: punto medio della via non è il civico; menzione anche del 177.'),
 '4630773': ('CIVICO_MANCANTE','Via Torino 181: punto medio della via non è il civico.'),
 '4631382': ('ODONIMO_DISCORDANTE','Via Cesare Battisti 23 non equivale a Via Belluno 23.'),
}
COLS = ('pvp_id','gruppo','priorita','comune_pvp','indirizzo_pvp','lat_pvp_snapshot',
        'lon_pvp_snapshot','istat_snapshot','odonimo_anncsu','civico_anncsu',
        'lat_anncsu','lon_anncsu','nota','lat_db_oggi','lon_db_oggi',
        'comune_db_oggi','indirizzo_db_oggi','istat_db_oggi','istat_anncsu_oggi',
        'esito_db','geocode_source_oggi')


def read_plan(p: Path):
    if not p.is_file():
        raise RuntimeError(f'Piano assente: {p}')
    if hashlib.sha256(p.read_bytes()).hexdigest() != PLAN_SHA:
        raise RuntimeError('SHA256 piano diverso dal piano verificato; stop senza GET')
    with p.open(encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    ids = [r['pvp_id'] for r in rows]
    if len(rows) != 2402 or len(set(ids)) != 2402:
        raise RuntimeError('Piano diverso: righe mancanti o duplicate')
    if not set(REVIEW_IDS).issubset(ids):
        raise RuntimeError('Mancano ID da verificare; stop')
    outside = [r for r in rows if r['istat_boundary_position'] == 'OUTSIDE']
    if len(outside) != 29 or set(REVIEW_IDS) & {r['pvp_id'] for r in outside}:
        raise RuntimeError('Conteggi REVIEW/OUTSIDE diversi dal batch verificato; stop')
    return {r['pvp_id']: r for r in rows}, outside


def point(lat, lon):
    try:
        a,b = float(lat),float(lon)
        return (a,b) if math.isfinite(a) and math.isfinite(b) and -90<=a<=90 and -180<=b<=180 and (a,b)!=(0,0) else None
    except (TypeError, ValueError):
        return None


def db_read_only(ids):
    from project_env import load_project_env
    load_project_env(required=('SUPABASE_URL','SUPABASE_SERVICE_ROLE'))
    base = os.environ['SUPABASE_URL'].rstrip('/')
    key = os.environ['SUPABASE_SERVICE_ROLE']
    columns = 'pvp_id,indirizzo,comune,sigla_provincia,cap,latitudine,longitudine,geocode_source,geocode_location_type,geocode_query_used,geocode_formatted_address'
    result = {}
    for i in range(0, len(ids), 15):
        batch = ids[i:i+15]
        query = urlencode({'select':columns,'pvp_id':'in.('+','.join(batch)+')','limit':'30'})
        req = Request(base+'/rest/v1/listings?'+query, headers={'apikey':key,'Authorization':'Bearer '+key,'Accept':'application/json'})
        with urlopen(req, timeout=45) as resp:
            rows = json.loads(resp.read().decode('utf-8'))
        if not isinstance(rows,list) or len(rows)>len(batch):
            raise RuntimeError('Risposta GET inattesa: stop, nessuna scrittura eseguita')
        for r in rows:
            pid = str(r.get('pvp_id') or '')
            if pid not in batch or pid in result:
                raise RuntimeError('GET con ID inatteso/duplicato: stop')
            result[pid] = r
    return result


def boundary_context():
    # Prevent optional ISTAT download path in the installed module.
    if not (REFERENCE_DIR/'Elenco-comuni-italiani.xlsx').is_file():
        raise RuntimeError('Manca Elenco-comuni-italiani.xlsx: stop, nessun download')
    fp = HERE/'istat_boundary_runtime_v11_6.py'
    if not fp.is_file():
        raise RuntimeError('Manca istat_boundary_runtime_v11_6.py: stop, nessun download')
    spec = importlib.util.spec_from_file_location('audit_review16_boundary', fp)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.ISTAT_ZIP.is_file():
        raise RuntimeError('ZIP ISTAT configurato assente: stop, nessun download')
    return module.get_context()


def assess(plan, outside, db, boundary):
    results=[]
    byid={r['pvp_id']:r for r in outside}
    for pid in (*REVIEW_IDS,*[r['pvp_id'] for r in outside],*HISTORIC_IDS):
        p=plan.get(pid,{})
        group = '16_IN_REVISIONE' if pid in REVIEW_IDS else ('29_PIN_FUORI_ISTAT' if pid in byid else '2_STORICI_SEGNALATI')
        prio,nota = NOTES[pid] if pid in NOTES else ('VERIFICA_TERRITORIO','Pin fuori Comune nello snapshot 16/09: controllare PVP, indirizzo e DB attuale.') if pid in byid else ('VERIFICA_STORICA','Pin segnalato dall’utente; non presente nel manifest nuovi ID.')
        row = {'pvp_id':pid,'gruppo':group,'priorita':prio,'comune_pvp':p.get('source_comune',''),
               'indirizzo_pvp':p.get('source_indirizzo',''), 'lat_pvp_snapshot':p.get('candidate_latitudine',''),
               'lon_pvp_snapshot':p.get('candidate_longitudine',''), 'istat_snapshot':p.get('istat_boundary_position',''),
               'odonimo_anncsu':p.get('anncsu_odonimo',''), 'civico_anncsu':p.get('anncsu_civic',''),
               'lat_anncsu':p.get('anncsu_lat',''), 'lon_anncsu':p.get('anncsu_lon',''),'nota':nota,
               'lat_db_oggi':'','lon_db_oggi':'','comune_db_oggi':'','indirizzo_db_oggi':'',
               'istat_db_oggi':'','istat_anncsu_oggi':'','esito_db':'NON_VERIFICATO_OFFLINE','geocode_source_oggi':''}
        if db is not None:
            now=db.get(pid)
            if now is None:
                row['esito_db']='NON_TROVATO_DB'
            else:
                row.update(lat_db_oggi=now.get('latitudine'),lon_db_oggi=now.get('longitudine'),
                           comune_db_oggi=now.get('comune'),indirizzo_db_oggi=now.get('indirizzo'),
                           geocode_source_oggi=now.get('geocode_source'))
                current=point(now.get('latitudine'),now.get('longitudine'))
                if current:
                    bt=boundary.test(now,lat=current[0],lon=current[1])
                    row['istat_db_oggi']=bt.get('position','UNKNOWN')
                    row['esito_db']='PIN_FUORI_ISTAT_DA_VERIFICARE' if bt.get('position')=='OUTSIDE' else 'PIN_PRESENTE_DA_VERIFICARE'
                else:
                    row['esito_db']='ANCORA_SENZA_COORDINATE' if pid in REVIEW_IDS else 'PIN_NON_PIU_PRESENTE'
                if pid in CHECK_IDS:
                    candidate=point(p.get('anncsu_lat'),p.get('anncsu_lon'))
                    if candidate:
                        check={'comune':p.get('candidate_comune'),'sigla_provincia':p.get('candidate_sigla_provincia'),'cap':p.get('candidate_cap')}
                        bt=boundary.test(check,lat=candidate[0],lon=candidate[1])
                        row['istat_anncsu_oggi']=bt.get('position','UNKNOWN')
                        expected=(('indirizzo','source_indirizzo'),('comune','source_comune'),('sigla_provincia','source_sigla_provincia'),('cap','source_cap'))
                        unchanged=all(str(now.get(a) or '').strip().casefold()==str(p.get(b) or '').strip().casefold() for a,b in expected)
                        empty_geo=not any(now.get(k) not in (None,'') for k in ('geocode_source','geocode_location_type','geocode_query_used','geocode_formatted_address'))
                        if not current and unchanged and empty_geo and bt.get('position')=='INSIDE' and bt.get('canonical_code')==p.get('canonical_istat_code'):
                            row['esito_db']='CANDIDATO_LOCALE_DA_APPROVARE_MANUALMENTE'
        results.append(row)
    return results


def main():
    parser=argparse.ArgumentParser(description='Aste Easy: offline audit / optional Supabase GET-only, ZERO GOOGLE ZERO PATCH')
    parser.add_argument('--db-check',action='store_true',help='GET solo Supabase su 47 ID; controllo ISTAT locale')
    parser.add_argument('--plan',type=Path,default=REPORTS_DIR/PLAN_NAME)
    args=parser.parse_args()
    plan,outside=read_plan(args.plan)
    db=boundary=None
    if args.db_check:
        boundary=boundary_context()  # Validate files before network
        ids=[*REVIEW_IDS,*[r['pvp_id'] for r in outside],*HISTORIC_IDS]
        db=db_read_only(ids)
    records=assess(plan,outside,db,boundary)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp=datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    csvpath=REPORTS_DIR/f'aste_review16_and_mispins_v1_{"get_only" if args.db_check else "offline"}_{stamp}.csv'
    jsonpath=REPORTS_DIR/f'aste_review16_and_mispins_v1_summary_{"get_only" if args.db_check else "offline"}_{stamp}.json'
    with csvpath.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=COLS);w.writeheader();w.writerows(records)
    summary={'mode':'SUPABASE_GET_ONLY_ZERO_GOOGLE_ZERO_PATCH' if args.db_check else 'OFFLINE_ZERO_NETWORK',
             'plan_sha256':PLAN_SHA,'review_16':16,'outside_snapshot_29':29,'historic_2':2,
             'ids_total':len(records),'db_found':len(db) if db is not None else None,
             'outcome_counts':dict(Counter(r['esito_db'] for r in records)),
             'supabase_patches':0,'google_api_calls':0,
             'notes':'Questo audit non corregge nulla. OUTSIDE deriva dal 16/09; stato attuale verificato solo con --db-check.'}
    jsonpath.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('[MODE]',summary['mode'],'[ROWS]',len(records),'[DB FOUND]',summary['db_found'])
    print('[COUNTS]',summary['outcome_counts'])
    print('[OUT]',csvpath,'\n[OUT]',jsonpath)

if __name__=='__main__':
    main()
