#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

HERE=Path(__file__).resolve().parent
SRC=HERE.parent
for p in (SRC,HERE,SRC/"importer",SRC/"pipeline"):
    if str(p) not in sys.path:
        sys.path.insert(0,str(p))

from aste_paths import REPORTS_DIR, ANNCSU_DIR, ensure_runtime_dirs
from project_env import load_project_env

VERSION="primary_pin_publisher_v2_preview"
PAGE=500

def _json_get(base,key,path,params):
    url=base.rstrip("/")+"/rest/v1/"+path+"?"+urlencode(params,safe="(),.*")
    req=Request(url,method="GET",headers={
        "apikey":key,
        "Authorization":"Bearer "+key,
        "Accept":"application/json",
    })
    with urlopen(req,timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "[]")

def _all(base,key,path,params):
    out=[]
    offset=0
    while True:
        p=dict(params)
        p["limit"]=str(PAGE)
        p["offset"]=str(offset)
        rows=_json_get(base,key,path,p)
        out.extend(rows)
        if len(rows)<PAGE:
            break
        offset += len(rows)
    return out

def _f(v):
    try:
        x=float(v)
    except (TypeError,ValueError):
        return None
    return x if math.isfinite(x) else None

def valid_pair(lat,lon):
    la,lo=_f(lat),_f(lon)
    return la is not None and lo is not None and -90<=la<=90 and -180<=lo<=180 and not (la==0 and lo==0)

def maps_link(lat,lon):
    return "https://www.google.com/maps?q="+quote(f"{float(lat):.8f},{float(lon):.8f}")

def open_anncsu_bbox():
    path=Path(os.environ.get("ANNCSU_SQLITE_INDEX","") or (ANNCSU_DIR/"anncsu_runtime.sqlite3")).resolve()
    if not path.is_file():
        raise RuntimeError("ANNCSU SQLite index missing: "+str(path))
    uri="file:"+str(path).replace("\\","/")+"?mode=ro"
    con=sqlite3.connect(uri,uri=True)
    con.row_factory=sqlite3.Row
    return con

def municipality_fallback(loc,con,cache):
    # Only physical-bene geography from listing_locations is allowed here.
    comune=str(loc.get("comune_pvp") or "").strip()
    code="".join(ch for ch in str(loc.get("codice_istat") or "") if ch.isdigit())
    code=code.zfill(6)[-6:] if code else ""
    if not comune:
        return None, "MISSING_COMUNE_PVP"
    if not code:
        return None, "MISSING_CODICE_ISTAT"
    if code not in cache:
        row=con.execute(
            "select min_lat,max_lat,min_lon,max_lon,count from municipality_bbox where codice_istat=?",
            (code,)
        ).fetchone()
        if row:
            cache[code]={
                "lat":(float(row["min_lat"])+float(row["max_lat"]))/2.0,
                "lon":(float(row["min_lon"])+float(row["max_lon"]))/2.0,
                "count":int(row["count"]),
            }
        else:
            cache[code]=None
    rep=cache.get(code)
    if not rep:
        return None, "ANNCSU_BBOX_NOT_FOUND"
    if not valid_pair(rep.get("lat"),rep.get("lon")):
        return None, "ANNCSU_BBOX_INVALID_COORDS"
    return {
        "lat":float(rep["lat"]),
        "lon":float(rep["lon"]),
        "source":"anncsu_comune_indicativo",
        "precision":"COMUNE",
        "status":"DONE",
        "reason":"Punto indicativo del Comune del bene fisico PVP, derivato dall'estensione degli accessi ANNCSU; non rappresenta la posizione esatta dell'immobile.",
        "formatted_address":"Comune di "+comune,
        "fallback_from_status":str(loc.get("status") or "UNKNOWN"),
    }, None

def choose_pin(loc,con,cache):
    status=str(loc.get("status") or "").upper()
    if status=="RESOLVED" and valid_pair(loc.get("latitudine"),loc.get("longitudine")):
        return {
            "lat":float(loc["latitudine"]),
            "lon":float(loc["longitudine"]),
            "source":str(loc.get("source") or "listing_locations"),
            "precision":str(loc.get("precision") or "UNKNOWN"),
            "status":"DONE",
            "reason":str(loc.get("resolution_reason") or "Primary listing_location resolved"),
            "formatted_address":str(loc.get("formatted_address") or loc.get("address_raw") or ""),
            "fallback_from_status":None,
        }, None
    # REVIEW / UNRESOLVED / NEEDS_COORD / PENDING are deliberately not trusted
    # as exact points. Preserve map coverage with an explicit municipality marker.
    return municipality_fallback(loc,con,cache)

def main(argv=None):
    ap=argparse.ArgumentParser(description="Preview publication of primary map pins for newly imported PENDING listings")
    ap.add_argument("--sample",type=int,default=30)
    args=ap.parse_args(argv)

    ensure_runtime_dirs()
    load_project_env(required=("SUPABASE_URL","SUPABASE_SERVICE_ROLE"))
    base=os.environ["SUPABASE_URL"].rstrip("/")
    key=os.environ["SUPABASE_SERVICE_ROLE"]

    listings=_all(base,key,"listings",{
        "select":"listing_id,pvp_id,geocode_status,latitudine,longitudine,maps_link",
        "geocode_status":"eq.PENDING",
        "order":"pvp_id.asc",
    })
    pending_ids={str(x.get("pvp_id") or "") for x in listings}
    if not pending_ids:
        summary={"version":VERSION,"pending_listings":0,"publishable":0,"google_calls":0}
        out=REPORTS_DIR/"primary_pin_publisher_v2_preview.json"
        out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(summary,ensure_ascii=False,indent=2))
        print("[OUT]",out)
        return 0

    primaries=[]
    ids=sorted(pending_ids,key=lambda x:int(x) if x.isdigit() else x)
    for start in range(0,len(ids),50):
        batch=ids[start:start+50]
        rows=_json_get(base,key,"listing_locations",{
            "select":"listing_id,pvp_id,is_primary,address_raw,comune_pvp,provincia_pvp,sigla_provincia_pvp,cap_pvp,codice_istat,latitudine,longitudine,source,precision,confidence,status,formatted_address,resolution_reason,resolver_version,warnings",
            "pvp_id":"in.("+",".join(batch)+")",
            "is_primary":"eq.true",
        })
        primaries.extend(rows)

    by_pid={str(x.get("pvp_id") or ""):x for x in primaries}
    if len(by_pid)!=len(pending_ids):
        missing=sorted(pending_ids-set(by_pid))
        raise RuntimeError("PENDING listing(s) without exactly one primary location; first="+str(missing[:10]))

    ann_con=open_anncsu_bbox()
    rep_cache={}
    plans=[]
    counts=Counter()
    source_counts=Counter()
    precision_counts=Counter()
    no_safe_pin_cases=[]

    for listing in listings:
        pid=str(listing["pvp_id"])
        loc=by_pid[pid]
        pin,no_pin_reason=choose_pin(loc,ann_con,rep_cache)
        if pin is None:
            counts["NO_SAFE_PIN"]+=1
            case={
                "pvp_id":pid,
                "action":"NO_SAFE_PIN",
                "location_status":loc.get("status"),
                "comune_pvp":loc.get("comune_pvp"),
                "provincia_pvp":loc.get("provincia_pvp"),
                "sigla_provincia_pvp":loc.get("sigla_provincia_pvp"),
                "cap_pvp":loc.get("cap_pvp"),
                "codice_istat":loc.get("codice_istat"),
                "address_raw":loc.get("address_raw"),
                "warnings":loc.get("warnings"),
                "resolution_reason":loc.get("resolution_reason"),
                "no_safe_pin_reason":no_pin_reason,
            }
            no_safe_pin_cases.append(case)
            plans.append(case)
            continue
        action="PUBLISH_RESOLVED" if pin["fallback_from_status"] is None else "PUBLISH_COMUNE_FALLBACK"
        counts[action]+=1
        source_counts[pin["source"]]+=1
        precision_counts[pin["precision"]]+=1
        plans.append({
            "pvp_id":pid,
            "action":action,
            "location_status":loc.get("status"),
            "comune_pvp":loc.get("comune_pvp"),
            "latitudine":pin["lat"],
            "longitudine":pin["lon"],
            "maps_link":maps_link(pin["lat"],pin["lon"]),
            "geocode_status":pin["status"],
            "geocode_source":pin["source"],
            "geocode_precision":pin["precision"],
            "geocode_decision_reason":pin["reason"],
            "geocode_formatted_address":pin["formatted_address"],
            "fallback_from_status":pin["fallback_from_status"],
        })

    ann_con.close()

    summary={
        "version":VERSION,
        "mode":"PREVIEW_ZERO_DB_WRITES",
        "pending_listings":len(listings),
        "primary_locations":len(primaries),
        "publishable":sum(v for k,v in counts.items() if k.startswith("PUBLISH_")),
        "actions":dict(counts),
        "sources":dict(source_counts),
        "precisions":dict(precision_counts),
        "google_calls":0,
        "db_writes":0,
        "no_safe_pin_reasons":dict(Counter(str(x.get("no_safe_pin_reason") or "UNKNOWN") for x in no_safe_pin_cases)),
        "no_safe_pin_cases":no_safe_pin_cases,
        "sample":plans[:max(0,args.sample)],
    }
    out=REPORTS_DIR/"primary_pin_publisher_v2_preview.json"
    out.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    print("[OUT]",out)
    return 0

if __name__=="__main__":
    raise SystemExit(main())
