#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""General, conservative territorial repair for the 2026-09-19 national offline plan.

GET-only by default. Optional explicit APPLY has a count gate, exact confirmation,
original-state backup, per-row DB recheck, conditional PATCH and post-write GET.
No Google calls, no download, no changes to administrative fields.
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
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from aste_paths import REPORTS_DIR, REFERENCE_DIR
from project_env import load_project_env

PLAN_NAME = 'geocode_territorial_offline_v1_20260919-065501.csv'
PLAN_SHA256 = '23c9e356b1507c66538c78935f88c54a6b55475690869faec5572cdcacd1f971'
EXPECTED_PLAN_ROWS = 373
FIELDS = ('pvp_id','indirizzo','comune','sigla_provincia','cap','latitudine','longitudine',
          'maps_link','geocode_source','geocode_location_type','geocode_query_used',
          'geocode_formatted_address')
HIGH_SOURCE = frozenset({'pvp_verified', 'google_corrected_pvp'})
HIGH_TYPE = frozenset({'ROOFTOP','PVP_OK_CIVICO','PVP_OK_STRADA'})
ALLOWED_DECISIONS = frozenset({'PROPOSE_ISTAT_COMUNE_INDICATIVE','PROPOSE_ANNCSU_STRADA'})
TIMEOUT=50


def txt(x):
    return re.sub(r'\s+', ' ', str(x if x is not None else '').replace('\u00a0',' ')).strip()


def val(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError, TypeError):
        return None


def same_num(a,b):
    x,y=val(a),val(b)
    return (x is None and y is None) if x is None or y is None else abs(x-y)<=1e-7


def module(path,name):
    if not path.is_file(): raise RuntimeError(f'Modulo locale mancante: {path}')
    spec=importlib.util.spec_from_file_location(name,path)
    obj=importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(obj)
    return obj


def plan_rows():
    path=REPORTS_DIR/PLAN_NAME
    if not path.is_file(): raise RuntimeError(f'Manca il piano verificato: {path}')
    if hashlib.sha256(path.read_bytes()).hexdigest()!=PLAN_SHA256:
        raise RuntimeError('SHA256 del piano diverso: stop senza richieste al DB')
    with path.open(encoding='utf-8-sig',newline='') as f:
        rows=list(csv.DictReader(f))
    ids=[txt(r.get('pvp_id')) for r in rows]
    if (len(rows)!=EXPECTED_PLAN_ROWS or len(set(ids))!=EXPECTED_PLAN_ROWS or
        any(not i.isdigit() for i in ids)):
        raise RuntimeError('Righe o ID del piano inattesi: stop')
    return {r['pvp_id']:r for r in rows}


def preapproved_from_plan(p):
    """Never upgrade uncertain civic or a high-precision existing pin to a centroid."""
    dec=txt(p.get('decision'))
    if dec=='KEEP_ALTERNATIVE_COMUNE_REVIEW': return 'HOLD_ALTERNATIVE_COMUNE'
    if dec=='PROPOSE_ANNCSU_CIVICO': return 'HOLD_CIVICO_SEMANTICS'
    if dec not in ALLOWED_DECISIONS: return 'HOLD_UNEXPECTED_DECISION'
    if txt(p.get('old_geocode_source')).lower() in HIGH_SOURCE or txt(p.get('old_geocode_location_type')).upper() in HIGH_TYPE:
        return 'HOLD_PRECISE_SOURCE'
    if dec=='PROPOSE_ANNCSU_STRADA':
        strict=val(p.get('anncsu_strict_street_similarity'))
        score=val(p.get('anncsu_street_score'))
        if (strict is None or strict<.90 or score is None or score<.96 or
            not txt(p.get('anncsu_odonimo')) or txt(p.get('reason')) not in ('simple_no_civic','explicit_snc')):
            return 'HOLD_STREET_EVIDENCE'
    return 'CANDIDATE'


def fetch_rows(base,key,ids):
    """Bounded GETs; fail closed on missing/duplicate/unexpected rows."""
    out={}
    ids=list(ids)
    if any(not str(x).isdigit() for x in ids): raise RuntimeError('ID non numerico')
    for start in range(0,len(ids),35):
        group=ids[start:start+35]
        url=base.rstrip('/')+'/rest/v1/listings?'+urlencode({
            'select':','.join(FIELDS),'pvp_id':'in.('+','.join(group)+')','limit':'100'})
        req=Request(url,headers={'apikey':key,'Authorization':'Bearer '+key,'Accept':'application/json'},method='GET')
        with urlopen(req,timeout=TIMEOUT) as resp:
            values=json.loads(resp.read().decode('utf-8'))
        if not isinstance(values,list) or len(values)!=len(group):
            raise RuntimeError('GET lotto: numero righe inatteso, stop')
        for record in values:
            pid=txt(record.get('pvp_id'))
            if pid not in group or pid in out: raise RuntimeError('GET: ID inatteso/duplicato')
            out[pid]=record
    if set(out)!=set(ids): raise RuntimeError('GET: ID mancanti')
    return out


def boundary_context():
    if not (REFERENCE_DIR/'Elenco-comuni-italiani.xlsx').is_file():
        raise RuntimeError('ISTAT Comuni mancante: nessun download')
    b=module(HERE/'istat_boundary_runtime_v11_6.py','territorial_safe_istat_v1')
    if not b.ISTAT_ZIP.is_file(): raise RuntimeError('ISTAT confini mancante: nessun download')
    return b.get_context()


def proposal_check(p,now,ctx,alternative_fn,rep_cache):
    """Pure, exhaustive fail-closed check based on current DB and stored source snapshot."""
    dec=preapproved_from_plan(p)
    if dec!='CANDIDATE': return dec
    if now is None: return 'HOLD_NO_DB_ROW'
    if (txt(now.get('pvp_id'))!=txt(p.get('pvp_id')) or
        txt(now.get('indirizzo'))!=txt(p.get('indirizzo')) or
        txt(now.get('comune')).casefold()!=txt(p.get('comune')).casefold() or
        txt(now.get('sigla_provincia')).upper()!=txt(p.get('sigla_provincia')).upper()):
        return 'HOLD_SOURCE_CHANGED'
    if not (same_num(now.get('latitudine'),p.get('original_lat')) and
            same_num(now.get('longitudine'),p.get('original_lon'))):
        return 'HOLD_COORDS_CHANGED'
    if (txt(now.get('geocode_source'))!=txt(p.get('old_geocode_source')) or
        txt(now.get('geocode_location_type'))!=txt(p.get('old_geocode_location_type'))):
        return 'HOLD_GEOCODE_CHANGED'
    if (txt(now.get('geocode_source')).lower() in HIGH_SOURCE or
        txt(now.get('geocode_location_type')).upper() in HIGH_TYPE):
        return 'HOLD_PRECISE_SOURCE'
    meta=ctx.resolve_admin(now)
    if meta.get('status')!='RESOLVED' or txt(meta.get('canonical_code'))!=txt(p.get('istat_code')):
        return 'HOLD_ISTAT_ADMIN_CHANGED'
    old=ctx.test(now,admin_meta=meta)
    if old.get('position')!='OUTSIDE': return 'HOLD_OLD_NO_LONGER_OUTSIDE'
    if alternative_fn(now,ctx,val(now.get('latitudine')),val(now.get('longitudine'))):
        return 'HOLD_ALTERNATIVE_COMUNE'
    la,lo=val(p.get('proposed_lat')),val(p.get('proposed_lon'))
    if la is None or lo is None or not (-90<=la<=90 and -180<=lo<=180) or (la==0 and lo==0):
        return 'HOLD_BAD_PROPOSED_COORDS'
    if ctx.test(now,lat=la,lon=lo,admin_meta=meta).get('position')!='INSIDE':
        return 'HOLD_PROPOSED_OUTSIDE'
    if txt(p.get('decision'))=='PROPOSE_ISTAT_COMUNE_INDICATIVE':
        code=txt(meta.get('canonical_code'))
        if code not in rep_cache:
            rep_cache[code]=ctx.representative_point(now,admin_meta=meta)
        rep=rep_cache[code]
        if not rep.get('ok') or not (same_num(rep.get('lat'),la) and same_num(rep.get('lon'),lo)):
            return 'HOLD_REPRESENTATIVE_CHANGED'
    return 'READY_COMUNE_INDICATIVE' if txt(p.get('decision'))=='PROPOSE_ISTAT_COMUNE_INDICATIVE' else 'READY_STRADA_INDICATIVE'


def patch_payload(p):
    la,lo=val(p['proposed_lat']),val(p['proposed_lon'])
    if la is None or lo is None: raise RuntimeError('Coordinate proposte non numeriche')
    comune,sigla=txt(p.get('comune')),txt(p.get('sigla_provincia'))
    if p['decision']=='PROPOSE_ISTAT_COMUNE_INDICATIVE':
        source='istat_corrected_boundary_comune'; kind='ISTAT_COMUNE_FALLBACK'
        addr=f'Comune di {comune} ({sigla}), Italia - posizione indicativa'
        query=f'OFFLINE_TERRITORIAL:ISTAT_COMUNE:{p["istat_code"]}'
    elif p['decision']=='PROPOSE_ANNCSU_STRADA':
        source='anncsu_corrected_boundary_strada'; kind='ANNCSU_STREET_MEDOID'
        addr=f'{txt(p["anncsu_odonimo"])}, {comune} ({sigla}), Italia - posizione indicativa'
        query=f'OFFLINE_TERRITORIAL:ANNCSU_STRADA:{p["istat_code"]}'
    else: raise RuntimeError('Decisione non autorizzata al PATCH')
    return {'latitudine':la,'longitudine':lo,
            'maps_link':f'https://www.google.com/maps?q={la:.7f},{lo:.7f}',
            'geocode_source':source,'geocode_location_type':kind,
            'geocode_formatted_address':addr,'geocode_query_used':query}


def conditional_patch(base,key,pid,before,payload):
    """PostgREST atomic AND filter on original fields; no quoting around eq values."""
    if txt(before.get('pvp_id'))!=pid: raise RuntimeError('PATCH ID/GET discordanti')
    filters={f:('is.null' if before.get(f) is None else 'eq.'+str(before[f])) for f in FIELDS}
    filters['pvp_id']='eq.'+pid
    url=base.rstrip('/')+'/rest/v1/listings?'+urlencode(filters)
    req=Request(url,headers={'apikey':key,'Authorization':'Bearer '+key,
        'Accept':'application/json','Content-Type':'application/json',
        'Prefer':'return=representation'},method='PATCH',
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8'))
    with urlopen(req,timeout=TIMEOUT) as resp:
        value=json.loads(resp.read().decode('utf-8'))
    if value==[]: return False
    if not isinstance(value,list) or len(value)!=1 or txt(value[0].get('pvp_id'))!=pid:
        raise RuntimeError('PATCH: risposta non univoca; controllare DB')
    return True


def verified(now,expected):
    return all((same_num(now.get(k),v) if k in ('latitudine','longitudine')
                else txt(now.get(k))==txt(v)) for k,v in expected.items())


def reports(rows,summary,mode):
    REPORTS_DIR.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    prefix='territorial_safe_v1_'+mode+'_'+stamp
    csvfile=REPORTS_DIR/(prefix+'.csv')
    jsonfile=REPORTS_DIR/(prefix+'.json')
    columns=['pvp_id','decision','comune','indirizzo','precision','old_lat','old_lon',
             'new_lat','new_lon','status','patch_status']
    with csvfile.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns);w.writeheader();w.writerows(rows)
    summary['csv']=csvfile.name; summary['json']=jsonfile.name
    jsonfile.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print('[OUT]',csvfile.name);print('[OUT]',jsonfile.name)


def main():
    ap=argparse.ArgumentParser(description='General repair, conservative; default GET-only, never Google')
    ap.add_argument('--apply',action='store_true',help='Requires --expected-ready and explicit typed confirmation')
    ap.add_argument('--expected-ready',type=int,help='Exact READY count from a reviewed GET-only report')
    args=ap.parse_args()
    if args.apply and (args.expected_ready is None or args.expected_ready<1):
        ap.error('--apply requires --expected-ready N from the reviewed GET-only report')
    plan=plan_rows()
    source=module(HERE/'geocode_territorial_offline_v1.py','territorial_source_v1')
    ctx=boundary_context()
    load_project_env(required=('SUPABASE_URL','SUPABASE_SERVICE_ROLE'))
    base,key=os.environ['SUPABASE_URL'],os.environ['SUPABASE_SERVICE_ROLE']
    ids=list(plan)
    db=fetch_rows(base,key,ids)
    cache={}
    rows=[]
    for pid,p in plan.items():
        state=proposal_check(p,db[pid],ctx,source.alternative_comune_for_pin,cache)
        rows.append({'pvp_id':pid,'decision':p['decision'],'comune':p['comune'],
            'indirizzo':p['indirizzo'],'precision':p['precision'],
            'old_lat':p['original_lat'],'old_lon':p['original_lon'],
            'new_lat':p['proposed_lat'],'new_lon':p['proposed_lon'],
            'status':state,'patch_status':'NOT_APPLIED'})
    summary={'version':'territorial_safe_v1','plan_sha256':PLAN_SHA256,
             'mode':'APPLY' if args.apply else 'SUPABASE_GET_ONLY_ZERO_GOOGLE_ZERO_PATCH',
             'examined':len(rows),'status_counts':dict(Counter(x['status'] for x in rows)),
             'ready':sum(x['status'].startswith('READY_') for x in rows),
             'google_calls':0,'db_patches':0,
             'note':'Conserva fonti precise e Comuni alternativi; CIVICO e geometrie non confermate escluse. COMUNE = indicativo.'}
    print('[GET ONLY] esaminati:',len(rows),'pronti:',summary['ready'])
    print('[CLASSI]',summary['status_counts'])
    if not args.apply:
        reports(rows,summary,'get_only');return
    if summary['ready']!=args.expected_ready:
        reports(rows,summary,'blocked')
        raise RuntimeError('Numero READY diverso dal report approvato: zero PATCH')
    phrase=f'TERRITORIAL_APPLY_{summary["ready"]}'
    print('Il PATCH puo aggiornare SOLO coordinate e metadati geocode; MAI Comune/indirizzo.')
    print('I punti COMUNE sono indicativi. Google API: ZERO.')
    if input(f'Per autorizzare digita esattamente {phrase}: ').strip()!=phrase:
        reports(rows,summary,'cancelled');return
    # Save COMPLETE current originals BEFORE a single mutation.
    REPORTS_DIR.mkdir(parents=True,exist_ok=True)
    b=REPORTS_DIR/('territorial_safe_v1_before_apply_'+datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')+'.json')
    originals={x['pvp_id']:db[x['pvp_id']] for x in rows if x['status'].startswith('READY_')}
    b.write_text(json.dumps(originals,ensure_ascii=False,indent=2),encoding='utf-8')
    summary['before_backup']=b.name
    try:
        for x in rows:
            if not x['status'].startswith('READY_'): continue
            pid=x['pvp_id'];p=plan[pid]
            fresh=fetch_rows(base,key,[pid])[pid]
            state=proposal_check(p,fresh,ctx,source.alternative_comune_for_pin,cache)
            if state!=x['status']:
                x['patch_status']='SKIP_RECHECK_'+state;continue
            pay=patch_payload(p)
            if not conditional_patch(base,key,pid,fresh,pay):
                x['patch_status']='SKIP_CONDITIONAL_FILTER';continue
            summary['db_patches']+=1
            x['patch_status']='PATCHED_RESPONSE'
            try:
                post=fetch_rows(base,key,[pid])[pid]
                x['patch_status']='PATCHED_GET_VERIFIED' if verified(post,pay) else 'PATCHED_GET_DIFFERENT_REVIEW_DB'
            except Exception:
                x['patch_status']='PATCHED_GET_FAILED_REVIEW_DB'
            if x['patch_status']!='PATCHED_GET_VERIFIED':
                raise RuntimeError(f'{pid}: PATCH non verificabile; stop, non ritentare automaticamente')
    finally:
        summary['apply_counts']=dict(Counter(x['patch_status'] for x in rows))
        reports(rows,summary,'apply')

if __name__=='__main__':
    main()
