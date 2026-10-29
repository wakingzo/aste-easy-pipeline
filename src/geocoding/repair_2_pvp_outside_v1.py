#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy: two specifically verified PVP out-of-municipality pins.

Default mode: offline audit, no network.  --db-check: Supabase SELECT (4 IDs),
local ISTAT polygons, zero PATCH and zero Google.  --apply: same all-or-nothing
preflight, an explicit typed confirmation, per-row GET, atomic conditional
PATCH on all read fields.  Only 4625019 and 4630701 can ever be PATCHed.

No Google client/library is imported.  No municipality/admin-field updates.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import REFERENCE_DIR, REPORTS_DIR

# Exact files from the 16/09 plan and 18/09 read-only verification.
PLAN_FILE = 'geocode_unified_plan_v11_6_4_20260916-224958.csv'
PLAN_SHA = '1fd8b57115ff2f638aa72c4369d1c2402b062e8c73750ea05e93206ff0b93fc9'
AUDIT_FILE = 'aste_review16_and_mispins_v1_get_only_20260918-210224.csv'
AUDIT_SHA = '659f0119b4040281b16b43118f1679c8d90311509af2ed31e2aaf7df1ea5eef7'
ISTAT_AUDIT_FILE = 'aste_outside_anncsu_istat_v2_offline_20260918-211720.csv'
ISTAT_AUDIT_SHA = '4c46c3290cda639280a872026c505b1e443bc3019901467e110af838480c654c'
TARGETS = {
    '4625019': {'comune':'Rodi Garganico', 'sigla':'FG', 'istat':'071043',
                'odonimo':'VIA GIUSEPPE DI VITTORIO','civico':'26',
                'method':'1', 'lat':41.924866, 'lon':15.886721,
                'evidence':'https://www.asteannunci.it/aste/4625019/10001/abitazione-di-tipo-economico-rodi-garganico',
                'note':'Via di Vittorio 26 / Via Giuseppe Di Vittorio 26: nome proprio espanso.'},
    '4630701': {'comune':'Frosinone', 'sigla':'FR', 'istat':'060038',
                'odonimo':'PIAZZA RISORGIMENTO','civico':'5',
                'method':'3', 'lat':41.641538, 'lon':13.3579665,
                'evidence':'https://asteflorio.it/aste/negozio-a-frosinone-fr-frosinone-3',
                'note':'Fabbricato su civici 4–5–6; civico 5 è punto rappresentativo, non tutti gli ingressi.'},
}
# Only administrative diagnostics, no corrections or municipality changes.
ALT_MUNICIPALITIES = {
    '4629847': ('Nuraminis', 'CA', 'Via Nazionale 233: lotto Nuraminis; campo comune DB Muravera.'),
    '4625939': ('Gorla Maggiore', 'VA', 'Pin PVP preciso, campo comune DB Gorla Minore.'),
}
ALLOWED_IDS = frozenset(TARGETS) | frozenset(ALT_MUNICIPALITIES)
FIELDS = ('pvp_id','indirizzo','comune','sigla_provincia','cap','latitudine','longitudine',
          'maps_link','geocode_source','geocode_location_type','geocode_query_used',
          'geocode_formatted_address')
TIMEOUT = 50


def clean(x):
    return re.sub(r'\s+', ' ', str(x if x is not None else '').replace('\u00a0',' ')).strip()


def cap(x):
    raw=clean(x)
    if not raw: return ''
    m=re.fullmatch(r'(\d+)(?:\.0+)?',raw)
    digits=m.group(1) if m else re.sub(r'\D','',raw)
    return digits.zfill(5) if digits else ''


def number(x):
    try: v=float(x)
    except (TypeError,ValueError): return None
    return v if math.isfinite(v) else None


def same_number(a,b,tol=1e-7):
    x,y=number(a),number(b)
    return (x is None and y is None) if x is None or y is None else abs(x-y)<=tol


def read_pinned(name,sha,expected_rows):
    p=REPORTS_DIR/name
    if not p.is_file():
        raise RuntimeError(f'Manca input locale {p}: zero rete/PATCH. Usa i report originali.')
    actual=hashlib.sha256(p.read_bytes()).hexdigest()
    if actual!=sha:
        raise RuntimeError(f'SHA256 inatteso per {name}: stop senza GET/PATCH')
    with p.open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    ids=[clean(r.get('pvp_id')) for r in rows]
    if len(ids)!=expected_rows or any(not x for x in ids) or len(set(ids))!=len(ids):
        raise RuntimeError(f'File {name}: conteggio o ID inattesi, stop')
    return {r['pvp_id']:r for r in rows}


def source_data():
    plan=read_pinned(PLAN_FILE,PLAN_SHA,2402)
    audit=read_pinned(AUDIT_FILE,AUDIT_SHA,47)
    territorial=read_pinned(ISTAT_AUDIT_FILE,ISTAT_AUDIT_SHA,31)
    if not ALLOWED_IDS.issubset(plan) or not ALLOWED_IDS.issubset(audit) or not ALLOWED_IDS.issubset(territorial):
        raise RuntimeError('Manca uno dei quattro ID nei report: stop')
    for pid,t in TARGETS.items():
        p,a,d=plan[pid],audit[pid],territorial[pid]
        def assert_clean(name,v,expected):
            if clean(v).casefold()!=clean(expected).casefold():
                raise RuntimeError(f'{pid}: {name} inatteso, stop')
        assert_clean('comune piano',p['source_comune'],t['comune'])
        assert_clean('sigla piano',p['source_sigla_provincia'],t['sigla'])
        assert_clean('ISTAT code piano',p['canonical_istat_code'],t['istat'])
        assert_clean('odonimo ANNCSU',p['anncsu_odonimo'],t['odonimo'])
        assert_clean('civico ANNCSU',p['anncsu_civic'],t['civico'])
        assert_clean('metodo ANNCSU',p['anncsu_metodo'],t['method'])
        assert_clean('stato plan',p['plan'],'GOOGLE_ARBITRATION_NEW_PVP')
        assert_clean('boundary plan',p['istat_boundary_position'],'OUTSIDE')
        assert_clean('boundary stato DB',a['istat_db_oggi'],'OUTSIDE')
        assert_clean('boundary candidato',d['istat_candidato_anncsu'],'INSIDE')
        assert_clean('esito audit',a['esito_db'],'PIN_FUORI_ISTAT_DA_VERIFICARE')
        assert_clean('civic relation',p['civic_relation'],'exact')
        if p['matching_street_candidates'] not in ('0','1') or not same_number(p['anncsu_lat'],t['lat']) or not same_number(p['anncsu_lon'],t['lon']):
            raise RuntimeError(f'{pid}: ambiguità/candidate coords diverse, stop')
        if not same_number(p['current_latitudine'],a['lat_db_oggi']) or not same_number(p['current_longitudine'],a['lon_db_oggi']):
            raise RuntimeError(f'{pid}: report DB e piano non coincidono, stop')
        if p['source_indirizzo']!=a['indirizzo_db_oggi'] or p['source_comune']!=a['comune_db_oggi']:
            raise RuntimeError(f'{pid}: indirizzo/comune cambiano tra report, stop')
        if d['valutazione']!='ANNCSU_DENTRO_COMUNE_VERIFICARE_INDIRIZZO':
            raise RuntimeError(f'{pid}: candidato non nel gruppo atteso, stop')
    return plan,audit,territorial


def boundary_context():
    # Prevent the boundary runtime's optional network download fallback.
    if not (REFERENCE_DIR/'Limiti01012026.zip').is_file() or not (REFERENCE_DIR/'Elenco-comuni-italiani.xlsx').is_file():
        raise RuntimeError('ISTAT 2026 locale assente: stop senza download')
    fp=HERE/'istat_boundary_runtime_v11_6.py'
    if not fp.is_file(): raise RuntimeError('Modulo ISTAT non trovato: stop')
    spec=importlib.util.spec_from_file_location('targeted_two_boundaries',fp)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.ISTAT_ZIP.is_file(): raise RuntimeError('ZIP ISTAT configurato mancante: stop senza download')
    return module.get_context()


def safe_point(row,lat,lon):
    x,y=number(lat),number(lon)
    if x is None or y is None or not(-90<=x<=90 and -180<=y<=180) or (x==0 and y==0):
        raise RuntimeError('Coordinate non valide')
    return row,x,y


def istat_test(ctx,comune,sigla,lat,lon):
    safe_point({},lat,lon)
    return ctx.test({'comune':comune,'sigla_provincia':sigla},lat=float(lat),lon=float(lon))


def get_one(base,key,pid):
    if pid not in ALLOWED_IDS: raise RuntimeError('GET fuori allowlist')
    url=base.rstrip('/')+'/rest/v1/listings?'+urlencode({'select':','.join(FIELDS),'pvp_id':'eq.'+pid,'limit':'2'})
    req=Request(url,headers={'apikey':key,'Authorization':'Bearer '+key,'Accept':'application/json'})
    with urlopen(req,timeout=TIMEOUT) as resp:
        data=json.loads(resp.read().decode('utf-8'))
    if not isinstance(data,list) or len(data)!=1 or clean(data[0].get('pvp_id'))!=pid:
        raise RuntimeError(f'GET {pid}: attesa esattamente una riga')
    return data[0]


def same_source(p,a,now):
    # Use snapshot equality, rather than blindly trusting an outdated plan.
    for db,field in [('indirizzo','source_indirizzo'),('comune','source_comune'),('sigla_provincia','source_sigla_provincia')]:
        if clean(now.get(db)).casefold()!=clean(p.get(field)).casefold(): return False,db+' diverso dal piano'
    if cap(now.get('cap'))!=cap(p.get('source_cap')): return False,'CAP diverso dal piano'
    if not same_number(now.get('latitudine'),a.get('lat_db_oggi')): return False,'lat diversa da ultimo GET'
    if not same_number(now.get('longitudine'),a.get('lon_db_oggi')): return False,'lon diversa da ultimo GET'
    if clean(now.get('geocode_source'))!=clean(a.get('geocode_source_oggi')): return False,'source cambiata'
    # The last audit did not include these three geocode fields. Never overwrite if already populated.
    for field in ('geocode_location_type','geocode_query_used','geocode_formatted_address'):
        if clean(now.get(field)): return False,field+' già valorizzato'
    return True,'ok'


def safe_filter(v):
    # Direct PostgREST eq must NOT wrap address in double quotes (see prior v1.2 bug).
    return 'is.null' if v is None else 'eq.'+str(v)


def patch_one(base,key,pid,before,payload):
    if pid not in TARGETS: raise RuntimeError('PATCH fuori allowlist')
    conditions={f:safe_filter(before.get(f)) for f in FIELDS}
    conditions['pvp_id']=safe_filter(pid)
    url=base.rstrip('/')+'/rest/v1/listings?'+urlencode(conditions)
    req=Request(url,method='PATCH',headers={'apikey':key,'Authorization':'Bearer '+key,
                 'Accept':'application/json','Content-Type':'application/json',
                 'Prefer':'return=representation'},data=json.dumps(payload,ensure_ascii=False).encode('utf-8'))
    with urlopen(req,timeout=TIMEOUT) as resp: answer=json.loads(resp.read().decode('utf-8'))
    if answer==[]: return None
    if not isinstance(answer,list) or len(answer)!=1 or clean(answer[0].get('pvp_id'))!=pid:
        raise RuntimeError(f'PATCH {pid}: risposta inattesa; verificare DB prima di riprovare')
    for field,val in payload.items():
        got=answer[0].get(field)
        if field in ('latitudine','longitudine'):
            if not same_number(got,val): raise RuntimeError(f'PATCH {pid}: {field} non corrisponde')
        elif clean(got)!=clean(val):
            raise RuntimeError(f'PATCH {pid}: {field} non corrisponde')
    return answer[0]


def payload(p,t,a):
    lat,lon=t['lat'],t['lon']
    fingerprint=hashlib.sha256(f"{p['pvp_id']}|{p['source_indirizzo']}|{a['lat_db_oggi']}|{a['lon_db_oggi']}|{lat}|{lon}".encode()).hexdigest()[:16]
    addr=f"{t['odonimo']} {t['civico']}, {cap(p['source_cap'])} {t['comune']} {t['sigla']}, Italia"
    return {'latitudine':lat,'longitudine':lon,
            'maps_link':f'https://www.google.com/maps?q={lat:.7f},{lon:.7f}',
            'geocode_source':'anncsu_corrected_boundary_civico',
            'geocode_location_type':f"ANNCSU_METHOD_{t['method']}",
            'geocode_formatted_address':addr,
            'geocode_query_used':'outside_anncsu_verified_v1:'+fingerprint}


def make_record(pid,plan,audit,territorial,ctx,now):
    p,a,d=plan[pid],audit[pid],territorial[pid]
    out={'pvp_id':pid,'tipo':'PIN_DA_CORREGGERE' if pid in TARGETS else 'CONFLITTO_COMUNE_DA_VERIFICARE',
         'comune_db_snapshot':a['comune_db_oggi'],'indirizzo_db_snapshot':a['indirizzo_db_oggi'],
         'lat_db_snapshot':a['lat_db_oggi'],'lon_db_snapshot':a['lon_db_oggi'],
         'lat_anncsu':d['anncsu_lat'],'lon_anncsu':d['anncsu_lon'],
         'check_db':'NON_ESEGUITO','istat_pin_comune_db':'NON_ESEGUITO',
         'istat_pin_comune_alternativo':'NON_ESEGUITO',
         'istat_anncsu_comune_db':'NON_ESEGUITO','proposta':'NESSUNA',
         'esito':'OFFLINE_ONLY','motivo':'','fonte_esterna':TARGETS.get(pid,{}).get('evidence',''),
         'patch_status':'NOT_APPLIED'}
    if pid in TARGETS:
        t=TARGETS[pid]
        out.update(lat_anncsu=t['lat'],lon_anncsu=t['lon'],proposta='COORDINATE_ANNCSU')
        out['motivo']=t['note']
    else:
        out.update(proposta='NESSUN_PATCH_COMUNE',motivo=ALT_MUNICIPALITIES[pid][2])
    if now is None: return out
    out['check_db']='TROVATO'
    out['comune_db_oggi']=clean(now.get('comune'))
    out['indirizzo_db_oggi']=clean(now.get('indirizzo'))
    out['lat_db_oggi']=now.get('latitudine')
    out['lon_db_oggi']=now.get('longitudine')
    ok,why=same_source(p,a,now)
    if not ok:
        out.update(esito='BLOCCATO_STATO_DB_CAMBIATO',motivo=why)
        return out
    cur=safe_point({},now.get('latitudine'),now.get('longitudine'))
    bt=istat_test(ctx,clean(now['comune']),clean(now.get('sigla_provincia')),cur[1],cur[2])
    out['istat_pin_comune_db']=bt.get('position','UNKNOWN')
    if bt.get('position')!='OUTSIDE':
        out.update(esito='BLOCCATO_PIN_NON_PIU_FUORI_COMUNE',motivo='Posizione nel comune dichiarato non è OUTSIDE')
        return out
    if pid in ALT_MUNICIPALITIES:
        alt,prov,note=ALT_MUNICIPALITIES[pid]
        out['istat_pin_comune_alternativo']=istat_test(ctx,alt,prov,cur[1],cur[2]).get('position','UNKNOWN')
        out.update(esito=('PIN_NEL_COMUNE_ALTERNATIVO_VERIFICARE_LOTTO' if out['istat_pin_comune_alternativo']=='INSIDE'
                          else 'CONFLITTO_NON_RISOLTO_VERIFICARE_ATTI'),motivo=note)
        return out
    t=TARGETS[pid]
    found=istat_test(ctx,t['comune'],t['sigla'],t['lat'],t['lon'])
    out['istat_anncsu_comune_db']=found.get('position','UNKNOWN')
    if found.get('position')!='INSIDE' or clean(found.get('canonical_code'))!=t['istat']:
        out.update(esito='BLOCCATO_CANDIDATO_ISTAT',motivo='ANNCSU non strettamente INSIDE nel Comune/codice ISTAT atteso')
        return out
    out['esito']='PRONTO_SOLO_DOPO_CONFERMA_MANUALE'
    return out


def write_reports(rows,summary,mode):
    REPORTS_DIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    prefix=f'aste_due_pin_istatrecheck_v1_{mode}_{stamp}'
    csvpath=REPORTS_DIR/(prefix+'.csv')
    jsonpath=REPORTS_DIR/(prefix+'.json')
    cols=['pvp_id','tipo','comune_db_snapshot','indirizzo_db_snapshot','lat_db_snapshot','lon_db_snapshot',
          'comune_db_oggi','indirizzo_db_oggi','lat_db_oggi','lon_db_oggi',
          'lat_anncsu','lon_anncsu','check_db','istat_pin_comune_db',
          'istat_pin_comune_alternativo','istat_anncsu_comune_db',
          'proposta','esito','motivo','fonte_esterna','patch_status']
    with csvpath.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols,extrasaction='ignore');w.writeheader();w.writerows(rows)
    jsonpath.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('[OUT]',csvpath)
    print('[OUT]',jsonpath)


def main():
    ap=argparse.ArgumentParser(description='Due pin PVP fuori Comune: zero Google, conditional PATCH solo su 2 ID')
    group=ap.add_mutually_exclusive_group()
    group.add_argument('--db-check',action='store_true',help='Supabase GET only + ISTAT, ZERO PATCH')
    group.add_argument('--apply',action='store_true',help='PATCH due ID, SOLO dopo GET e conferma manuale')
    args=ap.parse_args()
    plan,audit,territorial=source_data()
    ctx=boundary_context() if (args.db_check or args.apply) else None
    base=key=None
    if ctx is not None:
        from project_env import load_project_env
        load_project_env(required=('SUPABASE_URL','SUPABASE_SERVICE_ROLE'))
        base,key=os.environ['SUPABASE_URL'],os.environ['SUPABASE_SERVICE_ROLE']
    ids=list(TARGETS)+list(ALT_MUNICIPALITIES)
    now_by_id={pid:get_one(base,key,pid) for pid in ids} if ctx else {}
    rows=[make_record(pid,plan,audit,territorial,ctx,now_by_id.get(pid)) for pid in ids]
    states=Counter(r['esito'] for r in rows)
    summary={'version':'due_pin_istatrecheck_v1',
             'mode':'APPLY' if args.apply else ('SUPABASE_GET_ONLY_ZERO_GOOGLE_ZERO_PATCH' if ctx else 'OFFLINE_ZERO_NETWORK'),
             'input_sha256':{'plan':PLAN_SHA,'last_db_get':AUDIT_SHA,'istat_offline':ISTAT_AUDIT_SHA},
             'ids_examined':len(rows),'writable_ids':list(TARGETS),'diagnostic_only_ids':list(ALT_MUNICIPALITIES),
             'outcome_counts':dict(states),'google_api_calls':0,'supabase_patches':0,
             'note':'Civico 5 Frosinone rappresenta i civici 4-5-6. Comune Nuraminis/Gorla Maggiore: solo diagnosi, nessun PATCH.'}
    print('[MODE]',summary['mode'])
    for r in rows: print('[ID]',r['pvp_id'],r['esito'],'ISTAT_DB',r['istat_pin_comune_db'],'ISTAT_ALT',r['istat_pin_comune_alternativo'])
    if not args.apply:
        write_reports(rows,summary,'get_only' if args.db_check else 'offline')
        print('[DONE] Zero PATCH; nessuna chiamata Google.')
        return
    eligible=[r for r in rows if r['pvp_id'] in TARGETS and r['esito']=='PRONTO_SOLO_DOPO_CONFERMA_MANUALE']
    if len(eligible)!=2:
        write_reports(rows,summary,'blocked')
        raise RuntimeError('Non entrambi i due ID sono idonei: stop con ZERO PATCH.')
    phrase='CORREGGI_2_PIN_ANNCSU'
    print('\nATTENZIONE: SOLO due ID, per ciascuno pin PVP fuori Comune -> civico ANNCSU verificato dentro ISTAT.')
    print('ID:',', '.join(TARGETS))
    print('Nessun aggiornamento del campo Comune. Nessuna chiamata Google.')
    if input(f'Per autorizzare i 2 PATCH digita esattamente {phrase}: ').strip()!=phrase:
        write_reports(rows,summary,'cancelled')
        print('[DONE] Annullato; zero PATCH.')
        return
    # Snapshot BEFORE any write; preserve the original PVP links, coordinates and metadata.
    REPORTS_DIR.mkdir(parents=True,exist_ok=True)
    backup_path=REPORTS_DIR/('aste_due_pin_istatrecheck_v1_before_apply_'+datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')+'.json')
    backup_path.write_text(json.dumps({pid:now_by_id[pid] for pid in TARGETS},ensure_ascii=False,indent=2),encoding='utf-8')
    summary['before_backup_file']=backup_path.name
    print('[BACKUP]',backup_path)
    try:
        for r in rows:
            pid=r['pvp_id']
            if pid not in TARGETS: continue
            now=get_one(base,key,pid)
            fresh=make_record(pid,plan,audit,territorial,ctx,now)
            if fresh['esito']!='PRONTO_SOLO_DOPO_CONFERMA_MANUALE':
                r['patch_status']='SKIP_RECHECK_'+fresh['esito'];continue
            patched=patch_one(base,key,pid,now,payload(plan[pid],TARGETS[pid],audit[pid]))
            if patched is None:
                r['patch_status']='SKIP_CONDITIONAL_FILTER_NO_MATCH';continue
            r['patch_status']='PATCHED_RESPONSE_CONFIRMED'
            summary['supabase_patches']+=1
            print('[PATCHED]',pid)
            # Independent post-write GET: report failures honestly; never silently retry PATCH.
            try:
                verified=get_one(base,key,pid)
                expected=payload(plan[pid],TARGETS[pid],audit[pid])
                if not (same_number(verified.get('latitudine'),expected['latitudine'])
                        and same_number(verified.get('longitudine'),expected['longitudine'])
                        and all(clean(verified.get(f))==clean(expected[f]) for f in expected if f not in ('latitudine','longitudine'))):
                    r['patch_status']='PATCHED_GET_DIFFERENT_REVIEW_DB'
                else:
                    r['patch_status']='PATCHED_GET_VERIFIED'
            except Exception as ex:
                r['patch_status']='PATCHED_GET_FAILED_REVIEW_DB'
                r['motivo']='PATCH confermato, GET successivo fallito: '+type(ex).__name__
    finally:
        summary['apply_counts']=dict(Counter(r['patch_status'] for r in rows))
        write_reports(rows,summary,'apply')
    print('[DONE]',summary['apply_counts'])


if __name__=='__main__':
    main()
