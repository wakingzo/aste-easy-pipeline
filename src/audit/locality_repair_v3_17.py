#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Guarded repair executor v3.17.2 for locality_consistency_v3_17_1 CSV.
Default DRY RUN. --apply required for PATCH. Auto-repair is intentionally narrower
than the audit: only unique exact ISTAT locality references at least 2 km from the
current pin are eligible. Current DB state must still equal the audit snapshot.
"""
from __future__ import annotations
import argparse,csv,json,math,os,re,sys,time
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request,urlopen
from urllib.error import HTTPError
HERE=Path(__file__).resolve(); SRC=HERE.parents[1]
if str(SRC) not in sys.path: sys.path.insert(0,str(SRC))
from project_env import load_project_env
from aste_paths import REPORTS_DIR,ensure_runtime_dirs
TIMEOUT=90
VERSION='locality_repair_v3_17_2'
MIN_AUTO_DISTANCE_M=2000.0

def clean(v): return '' if v is None else str(v).strip()
def num(v):
    try:x=float(v)
    except:return None
    return x if math.isfinite(x) else None
def eqnum(a,b,tol=1e-7):
    a=num(a);b=num(b)
    if a is None or b is None:return a is None and b is None
    return abs(a-b)<=tol
def headers(k,content=False,prefer=None):
    h={'apikey':k,'Authorization':'Bearer '+k,'Accept':'application/json'}
    if content:h['Content-Type']='application/json'
    if prefer:h['Prefer']=prefer
    return h
def getrow(base,key,pid):
    sel='pvp_id,indirizzo,comune,latitudine,longitudine,geocode_status,geocode_precision,geocode_source'
    u=base.rstrip('/')+'/rest/v1/listings?'+urlencode([('select',sel),('pvp_id','eq.'+pid),('limit','1')])
    with urlopen(Request(u,headers=headers(key)),timeout=TIMEOUT) as r:
        x=json.loads(r.read().decode() or '[]');return x[0] if x else None
def patch(base,key,pid,payload):
    u=base.rstrip('/')+'/rest/v1/listings?'+urlencode([('pvp_id','eq.'+pid)])
    req=Request(u,method='PATCH',data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers(key,True,'return=representation'))
    with urlopen(req,timeout=TIMEOUT) as r:
        x=json.loads(r.read().decode() or '[]')
        if not isinstance(x,list) or len(x)!=1: raise RuntimeError('PATCH response inattesa')
        return x[0]
def main():
    ap=argparse.ArgumentParser();ap.add_argument('--audit',type=Path);ap.add_argument('--apply',action='store_true');args=ap.parse_args()
    ensure_runtime_dirs();env=load_project_env();base=clean(env.get('SUPABASE_URL'));key=clean(env.get('SUPABASE_SERVICE_ROLE'))
    audit=args.audit
    if audit is None:
        cand=sorted(REPORTS_DIR.glob('locality_consistency_v3_17_1_*.csv'),key=lambda x:x.stat().st_mtime,reverse=True)
        if not cand: raise SystemExit('ERRORE: nessun locality_consistency_v3_17_1_*.csv trovato; usa --audit FILE.csv')
        audit=cand[0]
    rows=list(csv.DictReader(audit.open('r',encoding='utf-8-sig',newline='')))
    def eligible(r):
        return (clean(r.get('severity'))=='RED'
                and clean(r.get('proposed_action'))=='REVIEW_MOVE_TO_LOCALITA_SAFE'
                and clean(r.get('repair_safe')).lower() in {'true','1','yes'}
                and clean(r.get('ref_source'))=='ISTAT_LOCALITA_2021'
                and clean(r.get('ref_strength'))=='EXACT_UNIQUE'
                and (num(r.get('distance_to_ref_m')) or -1) >= MIN_AUTO_DISTANCE_M
                and not clean(r.get('reference_warning'))
                and num(r.get('ref_lat')) is not None and num(r.get('ref_lon')) is not None)
    targets=[r for r in rows if eligible(r)]
    excluded=[r for r in rows if clean(r.get('severity'))=='RED' and not eligible(r)]
    print(f"[LOCALITY REPAIR] audit={audit}",flush=True)
    print(f"[LOCALITY REPAIR] strict policy: ISTAT EXACT_UNIQUE + distance >= {MIN_AUTO_DISTANCE_M:.0f} m + no reference warning",flush=True)
    print(f"[LOCALITY REPAIR] mode={'APPLY' if args.apply else 'DRY_RUN'} targets={len(targets)} excluded_red={len(excluded)}",flush=True)
    result=[];cnt=Counter()
    for i,r in enumerate(targets,1):
        pid=clean(r.get('pvp_id'));status='';reason=''
        try:
            now=getrow(base,key,pid)
            if not now: status='SKIP';reason='missing'
            elif clean(now.get('indirizzo')).casefold()!=clean(r.get('indirizzo')).casefold():status='SKIP';reason='address_changed'
            elif clean(now.get('geocode_source'))!=clean(r.get('current_source')) or clean(now.get('geocode_precision'))!=clean(r.get('current_precision')):status='SKIP';reason='geocode_state_changed'
            elif not eqnum(now.get('latitudine'),r.get('current_lat')) or not eqnum(now.get('longitudine'),r.get('current_lon')):status='SKIP';reason='coords_changed'
            else:
                la,lo=num(r.get('ref_lat')),num(r.get('ref_lon'))
                payload={'latitudine':la,'longitudine':lo,'maps_link':f'https://www.google.com/maps?q={la:.8f}%2C{lo:.8f}','geocode_status':'REVIEW','geocode_precision':'LOCALITA','geocode_location_type':'LOCALITA','geocode_source':'locality_guard_'+clean(r.get('ref_source')).lower(),'geocode_version':'locality_guard_v3_17_2','geocode_checked_at':datetime.now(timezone.utc).isoformat(),'geocode_decision_reason':f"Località esplicita {clean(r.get('hint'))!r}; pin precedente incompatibile ({clean(r.get('classification'))}); spostato a riferimento indicativo {clean(r.get('ref_source'))} {clean(r.get('ref_name'))!r}. Non prova l'immobile."}
                if args.apply: patch(base,key,pid,payload);status='PATCHED'
                else:status='READY_DRYRUN'
        except Exception as e:status='ERROR';reason=f'{type(e).__name__}: {e}'
        cnt[status]+=1;x=dict(r);x['run_status']=status;x['run_reason']=reason;result.append(x);print(f'[{i}/{len(targets)}] {pid} -> {status} {reason}',flush=True)
    stamp=datetime.now().astimezone().strftime('%Y%m%d-%H%M%S');p=REPORTS_DIR/f"locality_repair_v3_17_2_{'apply' if args.apply else 'dryrun'}_{stamp}.json"
    rep={'version':VERSION,'mode':'APPLY' if args.apply else 'DRY_RUN','audit':str(audit),'policy':{'ref_source':'ISTAT_LOCALITA_2021','ref_strength':'EXACT_UNIQUE','min_distance_m':MIN_AUTO_DISTANCE_M,'reference_warning_required_empty':True},'targets':len(targets),'target_ids':[clean(r.get('pvp_id')) for r in targets],'excluded_red':len(excluded),'excluded_red_ids':[clean(r.get('pvp_id')) for r in excluded],'counts':dict(cnt),'results':result}
    p.write_text(json.dumps(rep,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print('[LOCALITY REPAIR] report:',p,flush=True);return 0 if not cnt.get('ERROR') else 2
if __name__=='__main__':raise SystemExit(main())
