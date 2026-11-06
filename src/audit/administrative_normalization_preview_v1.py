#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from openpyxl import load_workbook

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0,str(p))

from aste_paths import REPORTS_DIR, REFERENCE_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION="administrative_normalization_preview_v1"
PAGE=1000

def clean(v):
    return "" if v is None else re.sub(r"\s+"," ",str(v).replace("\u00a0"," ")).strip()

def norm(v):
    s=clean(v).replace("’","'").replace("`","'")
    s=unicodedata.normalize("NFKD",s)
    s="".join(ch for ch in s if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s+"," ",re.sub(r"[^a-z0-9]+"," ",s)).strip()

def _get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={"apikey":key,"Authorization":"Bearer "+key,"Accept":"application/json"})
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def _all(base,key,path,params):
    out=[]; offset=0
    while True:
        p=dict(params); p["limit"]=str(PAGE); p["offset"]=str(offset)
        rows=_get(base,key,path,p); out.extend(rows)
        if len(rows)<PAGE: break
        offset += len(rows)
    return out

def find_header_map(ws):
    for row_no,row in enumerate(ws.iter_rows(min_row=1,max_row=20,values_only=True),1):
        vals=[norm(x) for x in row]
        def idx_contains(*tokens):
            for i,v in enumerate(vals):
                if all(t in v for t in tokens):
                    return i
            return None
        name_i=idx_contains("denominazione","italiano")
        if name_i is None: name_i=idx_contains("denominazione","comune")
        code_i=idx_contains("codice","comune","alfanumerico")
        if code_i is None: code_i=idx_contains("codice","comune")
        prov_i=idx_contains("denominazione","unita","territoriale","sovracomunale")
        if prov_i is None:
            prov_i=idx_contains("denominazione","provincia")
        sigla_i=idx_contains("sigla","automobilistica")
        reg_i=idx_contains("denominazione","regione")
        tipo_i=idx_contains("tipologia","unita","territoriale","sovracomunale")
        if tipo_i is None:
            tipo_i=idx_contains("tipologia","unita","sovracomunale")
        if name_i is not None and code_i is not None and reg_i is not None:
            return row_no,{"name":name_i,"code":code_i,"province":prov_i,"sigla":sigla_i,"region":reg_i,"type":tipo_i}
    raise RuntimeError("Header ISTAT comuni non riconosciuto")

def load_index(path):
    wb=load_workbook(path,read_only=True,data_only=True)
    ws=wb[wb.sheetnames[0]]
    header,hm=find_header_map(ws)
    by_name=defaultdict(list)
    for row in ws.iter_rows(min_row=header+1,values_only=True):
        def get(k):
            i=hm[k]
            return clean(row[i] if i is not None and i < len(row) else "")
        name=get("name"); code=get("code")
        if not name or not code: continue
        digits="".join(ch for ch in code if ch.isdigit())
        by_name[norm(name)].append({
            "comune":name,
            "codice_istat":digits.zfill(6)[-6:] if digits else code,
            "provincia":get("province"),
            "sigla_provincia":get("sigla"),
            "regione":get("region"),
            "tipo_ente":get("type"),
        })
    wb.close()
    return by_name

def resolve(row,index):
    cands=list(index.get(norm(row.get("comune")),[]))
    if not cands:
        return None,"NO_EXACT_COMUNE",[]
    if len(cands)==1:
        return cands[0],"EXACT_COMUNE_UNIQUE",cands
    nr=norm(row.get("regione"))
    by_reg=[x for x in cands if nr and norm(x.get("regione"))==nr]
    if len(by_reg)==1:
        return by_reg[0],"EXACT_COMUNE_REGIONE",cands
    np=norm(row.get("provincia"))
    by_prov=[x for x in cands if np and norm(x.get("provincia"))==np]
    if len(by_prov)==1:
        return by_prov[0],"EXACT_COMUNE_PROVINCIA",cands
    return None,"AMBIGUOUS_COMUNE",cands

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--sample",type=int,default=100)
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    rows=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,comune,provincia,sigla_provincia,regione,data_asta_date,status",
        "order":"pvp_id.asc"
    })
    index=load_index(REFERENCE_DIR/"Elenco-comuni-italiani.xlsx")

    resolution=Counter()
    changes=Counter()
    current_sardinia=Counter()
    canonical_sardinia=Counter()
    samples=[]
    unresolved=[]

    for r in rows:
        if norm(r.get("regione"))=="sardegna":
            current_sardinia[clean(r.get("provincia")) or "NULL"]+=1

        match,reason,cands=resolve(r,index)
        resolution[reason]+=1
        if not match:
            unresolved.append({
                "pvp_id":str(r.get("pvp_id") or ""),
                "comune":r.get("comune"),"provincia":r.get("provincia"),
                "sigla_provincia":r.get("sigla_provincia"),"regione":r.get("regione"),
                "reason":reason,"candidate_count":len(cands),
            })
            continue

        if norm(match.get("regione"))=="sardegna":
            canonical_sardinia[clean(match.get("provincia")) or "NULL"]+=1

        diff={}
        for field in ("regione","provincia","sigla_provincia"):
            old=clean(r.get(field)); new=clean(match.get(field))
            if old!=new:
                diff[field]={"old":old or None,"new":new or None}
                changes[field]+=1
        if diff and len(samples)<args.sample:
            samples.append({
                "pvp_id":str(r.get("pvp_id") or ""),
                "comune":r.get("comune"),
                "codice_istat":match.get("codice_istat"),
                "changes":diff,
                "resolution":reason,
            })

    summary={
        "version":VERSION,
        "mode":"PREVIEW_ZERO_DB_WRITES",
        "db_listings":len(rows),
        "resolution":dict(resolution),
        "would_change_by_field":dict(changes),
        "would_change_any":sum(1 for r in rows if False),
        "current_sardinia_province_values":dict(sorted(current_sardinia.items())),
        "canonical_sardinia_province_values":dict(sorted(canonical_sardinia.items())),
        "canonical_sardinia_distinct_count":len(canonical_sardinia),
        "unresolved_count":len(unresolved),
        "unresolved":unresolved[:args.sample],
        "sample_changes":samples,
        "google_calls":0,
        "db_writes":0,
    }
    # correct would_change_any from collected sample-independent pass
    any_change=0
    for r in rows:
        match,_,_=resolve(r,index)
        if not match: continue
        if any(clean(r.get(f))!=clean(match.get(f)) for f in ("regione","provincia","sigla_provincia")):
            any_change+=1
    summary["would_change_any"]=any_change

    out=REPORTS_DIR/"administrative_normalization_preview_v1.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
