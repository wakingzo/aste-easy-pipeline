#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aste Easy - read-only explicit-locality consistency audit v3.17.

ZERO DB writes. ZERO Google. Uses current ANNCSU SQLite + optional ISTAT 2021
point-locality index to identify obviously misplaced pins.
"""
from __future__ import annotations
import argparse, csv, importlib.util, json, math, os, re, sqlite3, sys, time, unicodedata
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

HERE=Path(__file__).resolve(); SRC=HERE.parents[1]
if str(SRC) not in sys.path: sys.path.insert(0,str(SRC))
from project_env import load_project_env
from aste_paths import ANNCSU_DIR, REPORTS_DIR, ensure_runtime_dirs

VERSION='locality_consistency_audit_v3_17_1'
TIMEOUT=90
LOCALITY_RE=re.compile(r"(?i)\b(?:localit[aà]|loc\.?|frazione(?:\s+di)?|fraz\.?|borgata|contrada|c\s*/?\s*da|c\.\s*da)\s+")
ROAD_RE=re.compile(r"(?i)\b(?:via|viale|corso|piazza|piazzale|strada|vicolo|vico|largo|traversa|sentiero|rotonda|salita|lungomare|s\.?p\.?|s\.?s\.?|s\.?r\.?)\b")
ISTAT_DB=SRC.parent/'data'/'istat'/'istat_localita_2021.sqlite3'

def clean(v): return '' if v is None else re.sub(r'\s+',' ',str(v).replace('\u00a0',' ')).strip()
def norm(v):
    s=unicodedata.normalize('NFKD',clean(v).replace('’',"'").replace('`',"'"))
    s=''.join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    s=re.sub(r'[^a-z0-9]+',' ',s); return re.sub(r'\s+',' ',s).strip()
def f(v):
    try: x=float(v)
    except (TypeError,ValueError): return None
    return x if math.isfinite(x) else None
def hav(a,b,c,d):
    if None in (a,b,c,d): return None
    r=6371008.8; p1=math.radians(a); p2=math.radians(c); dp=math.radians(c-a); dl=math.radians(d-b)
    z=math.sin(dp/2)**2+math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(min(1,math.sqrt(z)))
def load_py(path,name):
    sp=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(sp); sp.loader.exec_module(m); return m

def headers(key): return {'apikey':key,'Authorization':'Bearer '+key,'Accept':'application/json'}
def req_json(url,key):
    last=None
    for attempt in range(3):
        try:
            with urlopen(Request(url,headers=headers(key)),timeout=TIMEOUT) as r: return json.loads(r.read().decode('utf-8','replace') or '[]')
        except HTTPError as e:
            raise RuntimeError(f'Supabase HTTP {e.code}: '+e.read().decode('utf-8','replace')[:300]) from e
        except Exception as e:
            last=e
            if attempt<2: time.sleep(1.5*(attempt+1)); continue
    raise RuntimeError(f'Supabase network error: {last}')
def fetch_all(base,key):
    fields=('pvp_id,indirizzo,comune,sigla_provincia,provincia,regione,cap,'
            'pvp_latitudine,pvp_longitudine,latitudine,longitudine,geocode_status,'
            'geocode_precision,geocode_source,geocode_location_type,geocode_decision_reason')
    out=[]; off=0; size=1000
    while True:
        url=base.rstrip('/')+'/rest/v1/listings?'+urlencode([('select',fields),('order','pvp_id.asc'),('limit',size),('offset',off)])
        rows=req_json(url,key)
        if not isinstance(rows,list): raise RuntimeError('Risposta Supabase inattesa')
        out.extend(rows)
        print(f'[LOCALITY AUDIT] DB rows {len(out):,}',flush=True)
        if len(rows)<size: break
        off += size
    return out

def explicit_hint(address):
    raw=clean(address)
    m=LOCALITY_RE.search(raw.replace('’',"'"))
    if not m: return ''
    tail=raw[m.end():]; stop=len(tail)
    for sep in (',',';'):
        x=tail.find(sep)
        if x>=0: stop=min(stop,x)
    rm=ROAD_RE.search(tail)
    if rm: stop=min(stop,rm.start())
    s=clean(tail[:stop]).strip(' -/,.;')
    # Common PVP form: "Loc. Capo Comino - Podere 18".  A spaced dash
    # followed by an address/detail token is a sub-address separator, not part
    # of the locality name.  Fail closed for ordinary hyphenated place names.
    sm=re.search(r"\s+[–—-]\s+(?=(?:podere|poderi|via|viale|piazza|strada|s\.?p\.?|s\.?s\.?|civ(?:ico)?\.?|n\.?|numero|km|snc)\b)",s,re.I)
    if sm: s=clean(s[:sm.start()]).strip(' -/,.;')
    return s

def open_istat(path):
    if not path.is_file(): return None
    con=sqlite3.connect('file:'+str(path.resolve()).replace('\\','/')+'?mode=ro',uri=True); con.row_factory=sqlite3.Row
    try:
        v=con.execute("select value from meta where key='version'").fetchone()
        if not v or v[0] != 'istat_locality_index_v1': con.close(); return None
    except Exception: con.close(); return None
    return con

def istat_match(con, pro_com, hint):
    if con is None or not pro_com or not hint: return None
    nh=norm(hint)
    exact=list(con.execute('select loc21_id,tipo_loc,nome,lat,lon from localities where pro_com=? and nome_norm=?',(pro_com,nh)))
    if len(exact)==1:
        r=exact[0]; return {'mode':'EXACT_UNIQUE','score':1.0,'margin':1.0,**dict(r)}
    if len(exact)>1:
        # Same locality name can legitimately occur multiple times in one Comune.
        # Do not arbitrarily pick the first point: that would manufacture a false
        # geographic reference.
        return {'mode':'EXACT_AMBIGUOUS','score':1.0,'margin':0.0,'matches':len(exact),'best_name':exact[0]['nome']}
    rows=list(con.execute('select loc21_id,tipo_loc,nome,nome_norm,lat,lon from localities where pro_com=?',(pro_com,)))
    if not rows: return None
    scores=[]
    for r in rows:
        sc=SequenceMatcher(None,nh,r['nome_norm']).ratio(); scores.append((sc,r))
    scores.sort(key=lambda x:x[0],reverse=True); best=scores[0]; second=scores[1][0] if len(scores)>1 else 0
    margin=best[0]-second
    if best[0]>=0.94 and margin>=0.06:
        return {'mode':'FUZZY_STRONG','score':round(best[0],4),'margin':round(margin,4),**dict(best[1])}
    return {'mode':'NO_SAFE_MATCH','score':round(best[0],4),'margin':round(margin,4),'best_name':best[1]['nome']}

def classify(row,ev,im):
    clat,clon=f(row.get('latitudine')),f(row.get('longitudine'))
    hint=clean(ev.get('runtime_locality_source_hint')) or explicit_hint(row.get('indirizzo'))
    ann_mode=clean(ev.get('runtime_locality_match_mode'))
    ann_lat,ann_lon=f(ev.get('runtime_locality_lat')),f(ev.get('runtime_locality_lon'))
    ann_near=f(ev.get('matched_locality_nearest_high_accuracy_access_m'))
    ann_n=int(float(ev.get('matched_locality_high_accuracy_accesses') or 0))
    ann_p90=f(ev.get('runtime_locality_p90_m'))
    istat_safe=bool(im and im.get('mode') in {'EXACT_UNIQUE','FUZZY_STRONG'} and f(im.get('lat')) is not None and f(im.get('lon')) is not None)
    istat_exact=bool(im and im.get('mode')=='EXACT_UNIQUE')
    ilat,ilon=(f(im.get('lat')),f(im.get('lon'))) if istat_safe else (None,None)
    ann_has_point=ann_mode=='EXACT' and ann_lat is not None and ann_lon is not None
    ann_geometry_sane=ann_has_point and (ann_p90 is None or ann_p90<=5000)
    cross_dist=hav(ann_lat,ann_lon,ilat,ilon) if ann_has_point and istat_safe else None
    ann_cross_ok=(cross_dist is None or cross_dist<=5000)

    # Prefer a unique official ISTAT locality point when available. ANNCSU is
    # excellent for access distributions, but a malformed/foreign locality
    # geometry must never override an unambiguous ISTAT point.
    ref_source=''; rlat=rlon=None; ref_name=''; ref_strength=''; ref_valid=False
    if istat_safe:
        ref_source='ISTAT_LOCALITA_2021'; rlat,rlon=ilat,ilon; ref_name=clean(im.get('nome')); ref_strength=im.get('mode'); ref_valid=True
    elif ann_geometry_sane:
        ref_source='ANNCSU_LOCALITA'; rlat,rlon=ann_lat,ann_lon; ref_name=clean(ev.get('runtime_anncsu_locality')); ref_strength='EXACT'; ref_valid=True

    dist=hav(clat,clon,rlat,rlon) if rlat is not None else None
    precision=clean(row.get('geocode_precision')).upper(); source=clean(row.get('geocode_source')).lower()
    coarse=precision in {'COMUNE','PVP_IN_COMUNE','PVP_INDICATIVO_ALTRO_COMUNE','NESSUNA'} or source in {'pvp_comune_compatibile','istat_comune_indicativo','istat_comune_fallback_v312','pvp_localita_non_conclusiva'}

    # If ANNCSU and ISTAT disagree by kilometres, do not call either an automatic
    # repair reference unless ISTAT is unique. Record the inconsistency explicitly.
    reference_warning=''
    if cross_dist is not None and cross_dist>5000:
        reference_warning='ANNCSU_ISTAT_REFERENCE_DIVERGENCE'

    # Use ANNCSU access geometry only when it is internally sane and, where an
    # ISTAT reference exists, not grossly contradictory.
    ann_access_safe=ann_mode=='EXACT' and ann_near is not None and ann_n>=5 and ann_geometry_sane and ann_cross_ok
    if ann_access_safe:
        if ann_near>3000:
            severity='RED'; cls='STRONG_LOCALITY_CONFLICT_ANNCSU'
        elif ann_near>1500 and ann_p90 is not None and ann_p90<=1000:
            severity='RED'; cls='LOCALITY_CONFLICT_ANNCSU_COMPACT'
        elif ann_near>750:
            severity='AMBER'; cls='LOCALITY_DISTANCE_WARN_ANNCSU'
        else:
            severity='OK'; cls='LOCALITY_COMPATIBLE_ANNCSU'
    elif dist is not None:
        # Point locality is a representative point, not a boundary: only flag large gaps.
        if dist>6000 and coarse:
            severity='RED'; cls='STRONG_LOCALITY_CONFLICT_ISTAT_POINT' if ref_source.startswith('ISTAT') else 'STRONG_LOCALITY_CONFLICT_LOCALITY_POINT'
        elif dist>3500:
            severity='AMBER'; cls='LOCALITY_DISTANCE_WARN_ISTAT_POINT' if ref_source.startswith('ISTAT') else 'LOCALITY_DISTANCE_WARN_LOCALITY_POINT'
        else:
            severity='OK'; cls='LOCALITY_COMPATIBLE_ISTAT_POINT' if ref_source.startswith('ISTAT') else 'LOCALITY_COMPATIBLE_LOCALITY_POINT'
    elif ann_mode=='EXACT' and ann_has_point and not ann_geometry_sane:
        severity='NEEDS_REF'; cls='ANNCSU_LOCALITY_GEOMETRY_SUSPECT'
    elif im and im.get('mode')=='EXACT_AMBIGUOUS':
        severity='NEEDS_REF'; cls='ISTAT_EXACT_LOCALITY_AMBIGUOUS'
    elif ann_mode=='EXACT':
        severity='NEEDS_REF'; cls='EXACT_LOCALITY_NO_GEOMETRY'
    else:
        severity='NEEDS_REF'; cls='EXPLICIT_LOCALITY_UNRESOLVED'

    # Automatic repair is deliberately narrower than RED. A unique ISTAT match is
    # safe for an indicative LOCALITA pin; ANNCSU-only repairs require sane geometry.
    repair_safe=bool(severity=='RED' and ref_valid and ((ref_source=='ISTAT_LOCALITA_2021' and istat_exact) or (ref_source=='ANNCSU_LOCALITA' and ann_geometry_sane)))
    proposed='KEEP'
    if severity=='RED' and repair_safe: proposed='REVIEW_MOVE_TO_LOCALITA_SAFE'
    elif severity=='RED': proposed='MANUAL_REVIEW_RED'
    elif severity=='AMBER': proposed='MANUAL_REVIEW'
    elif severity=='NEEDS_REF': proposed='NEEDS_LOCALITY_REFERENCE'
    return {'hint':hint,'ref_source':ref_source,'ref_name':ref_name,'ref_strength':ref_strength,'ref_lat':rlat,'ref_lon':rlon,'distance_to_ref_m':round(dist,1) if dist is not None else '', 'anncsu_nearest_high_accuracy_m':ann_near if ann_near is not None else '', 'anncsu_high_accuracy_accesses':ann_n,'anncsu_p90_m':ann_p90 if ann_p90 is not None else '', 'anncsu_istat_ref_distance_m':round(cross_dist,1) if cross_dist is not None else '', 'reference_warning':reference_warning,'repair_safe':repair_safe,'classification':cls,'severity':severity,'proposed_action':proposed}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--istat-db',type=Path,default=ISTAT_DB); args=ap.parse_args()
    ensure_runtime_dirs(); env=load_project_env(); base=clean(env.get('SUPABASE_URL')); key=clean(env.get('SUPABASE_SERVICE_ROLE'))
    if not base or not key: raise SystemExit('ERRORE: credenziali Supabase mancanti')
    rows=fetch_all(base,key); targets=[r for r in rows if LOCALITY_RE.search(clean(r.get('indirizzo')).replace('’',"'"))]
    print(f'[LOCALITY AUDIT] explicit locality rows={len(targets):,}',flush=True)
    local=load_py(SRC/'geocoding'/'anncsu_runtime_consolidated_v2.py','locality_audit_anncsu_runtime')
    evs,meta=local.build_local_evidence(targets); byev={clean(x.get('pvp_id')):x for x in evs}
    icon=open_istat(args.istat_db); print('[LOCALITY AUDIT] ISTAT point index:', 'YES' if icon else 'NO',flush=True)
    out=[]
    for r in targets:
        pid=clean(r.get('pvp_id')); ev=byev.get(pid,{})
        pro=clean(ev.get('anncsu_code_used')); hint=clean(ev.get('runtime_locality_source_hint')) or explicit_hint(r.get('indirizzo'))
        im=istat_match(icon,pro,hint)
        c=classify(r,ev,im)
        x={
          'pvp_id':pid,'indirizzo':clean(r.get('indirizzo')),'comune':clean(r.get('comune')),'provincia':clean(r.get('sigla_provincia')),
          'current_status':clean(r.get('geocode_status')),'current_precision':clean(r.get('geocode_precision')),'current_source':clean(r.get('geocode_source')),
          'current_lat':r.get('latitudine'),'current_lon':r.get('longitudine'),'pvp_lat':r.get('pvp_latitudine'),'pvp_lon':r.get('pvp_longitudine'),
          'anncsu_code':pro,'anncsu_match_mode':clean(ev.get('runtime_locality_match_mode')),'anncsu_locality':clean(ev.get('runtime_anncsu_locality')),
          'istat_match_mode':clean((im or {}).get('mode')),'istat_match_name':clean((im or {}).get('nome') or (im or {}).get('best_name')),'istat_match_score':(im or {}).get('score',''),'istat_match_margin':(im or {}).get('margin',''),
          **c
        }
        out.append(x)
    if icon: icon.close()
    rank={'RED':0,'AMBER':1,'NEEDS_REF':2,'OK':3}; out.sort(key=lambda x:(rank.get(x['severity'],9), -(float(x['distance_to_ref_m']) if x['distance_to_ref_m']!='' else -1),x['pvp_id']))
    stamp=datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')
    csvp=REPORTS_DIR/f'locality_consistency_v3_17_1_{stamp}.csv'; jsonp=REPORTS_DIR/f'locality_consistency_v3_17_1_{stamp}.json'
    fields=list(out[0].keys()) if out else ['pvp_id']
    with csvp.open('w',encoding='utf-8-sig',newline='') as fh:
        w=csv.DictWriter(fh,fieldnames=fields); w.writeheader(); w.writerows(out)
    counts=Counter(x['severity'] for x in out); classes=Counter(x['classification'] for x in out)
    report={'version':VERSION,'mode':'READ_ONLY_ZERO_DB_WRITES_ZERO_GOOGLE','created_at':datetime.now().astimezone().isoformat(),'total_db_rows':len(rows),'explicit_locality_rows':len(targets),'provider':meta,'istat_index':str(args.istat_db) if args.istat_db.exists() else None,'severity_counts':dict(counts),'safe_repair_count':sum(1 for x in out if x.get('repair_safe')),'classification_counts':dict(classes),'red_ids':[x['pvp_id'] for x in out if x['severity']=='RED'],'amber_ids':[x['pvp_id'] for x in out if x['severity']=='AMBER'],'needs_reference_ids':[x['pvp_id'] for x in out if x['severity']=='NEEDS_REF'],'top_suspects':out[:50],'csv':str(csvp)}
    jsonp.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('[LOCALITY AUDIT] severity:',dict(counts),flush=True)
    for x in out[:20]:
        if x['severity']=='OK': break
        print(f"[{x['severity']}] {x['pvp_id']} {x['comune']} | {x['hint']} | {x['classification']} | dist={x['distance_to_ref_m']}m | {x['indirizzo']}",flush=True)
    print('[LOCALITY AUDIT] CSV:',csvp,flush=True); print('[LOCALITY AUDIT] JSON:',jsonp,flush=True); return 0
if __name__=='__main__': raise SystemExit(main())
