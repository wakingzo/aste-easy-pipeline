#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent
for p in (SRC, HERE):
    if str(p) not in __import__("sys").path:
        __import__("sys").path.insert(0, str(p))

from aste_paths import REPORTS_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION = "historical_missing_pin_diagnosis_v1"
PAGE = 1000

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

def _f(v):
    try: x=float(v)
    except (TypeError,ValueError): return None
    return x if math.isfinite(x) else None

def valid_pair(lat,lon):
    la,lo=_f(lat),_f(lon)
    return la is not None and lo is not None and -90<=la<=90 and -180<=lo<=180 and not (la==0 and lo==0)

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument("--sample",type=int,default=60)
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,data_asta_date,regione,provincia,comune,indirizzo,latitudine,longitudine,maps_link,geocode_status",
        "latitudine":"is.null",
        "longitudine":"is.null",
        "geocode_status":"is.null",
        "order":"pvp_id.asc",
    })

    ids=[str(x.get("pvp_id") or "") for x in listings if x.get("pvp_id") is not None]
    locs=[]
    for i in range(0,len(ids),50):
        batch=ids[i:i+50]
        if not batch: continue
        locs.extend(_get(base,key,"listing_locations",{
            "select":"listing_id,pvp_id,is_primary,bene_index,address_raw,comune_pvp,provincia_pvp,sigla_provincia_pvp,codice_istat,latitudine,longitudine,source,precision,confidence,status,resolution_reason,resolver_version",
            "pvp_id":"in.("+",".join(batch)+")",
            "is_primary":"eq.true",
        }))

    by_pid={str(x.get("pvp_id") or ""):x for x in locs}
    counts=Counter()
    rows=[]
    for r in listings:
        pid=str(r.get("pvp_id") or "")
        loc=by_pid.get(pid)
        if loc is None:
            bucket="NO_PRIMARY_LOCATION"
        elif str(loc.get("status") or "").upper()=="RESOLVED" and valid_pair(loc.get("latitudine"),loc.get("longitudine")):
            bucket="PRIMARY_RESOLVED_WITH_COORDS"
        elif str(loc.get("comune_pvp") or "").strip() and str(loc.get("codice_istat") or "").strip():
            bucket="PRIMARY_HAS_COMUNE_FOR_FALLBACK"
        elif str(loc.get("comune_pvp") or "").strip():
            bucket="PRIMARY_HAS_COMUNE_NO_ISTAT"
        else:
            bucket="PRIMARY_NO_SAFE_GEOGRAPHY"
        counts[bucket]+=1
        rows.append({
            "pvp_id":pid,
            "listing_id":r.get("listing_id"),
            "data_asta_date":r.get("data_asta_date"),
            "regione":r.get("regione"),
            "provincia":r.get("provincia"),
            "comune":r.get("comune"),
            "indirizzo":r.get("indirizzo"),
            "bucket":bucket,
            "primary_location":loc,
        })

    summary={
        "version":VERSION,
        "mode":"READ_ONLY_ZERO_DB_WRITES",
        "historical_missing_listings":len(listings),
        "primary_locations_found":len(locs),
        "buckets":dict(counts),
        "samples":rows[:max(0,args.sample)],
    }
    out=REPORTS_DIR/"historical_missing_pin_diagnosis_v1.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
